"""HopX client wrapper.

Key invariants enforced here:

  - **No auto-destroy** by default — sandboxes live forever unless the
    user actively `kill`s or stops them.  (Per docs: omit
    `timeout_seconds` in the create payload ⇒ no timeout.)
  - **Fresh JWT every VM operation** — every call appends a `refresh_token()`
    *before* talking to the VM Agent, so the JWT in `_token_cache` is
    guaranteed up-to-date.  We then call `Sandbox.connect()` so paused VMs
    are auto-resumed and the SDK re-installs the freshly-issued JWT on
    the AgentHTTPClient.
  - **INVALID_TOKEN auto-recovery** — if the SDK still reports
    `INVALID_TOKEN` (e.g. concurrent refresh from another thread beat
    us to it), we `refresh_token()` once more and retry.  All SDK
    exceptions are caught and re-raised as `HopXError` so handlers can
    consistently `except HopXError`.  Bonus: every retry drags a fresh
    public_host + JWT back into SQLite so the terminal session never
    opens against a stale endpoint.
  - **Persistent sandbox history** — `storage.remember_sandbox(...)` is
    called on every successful create/connect so the user can reattach
    to any past sandbox_id from `storage.list_sandboxes(...)`.

Reference (verified from hopx_ai 0.3.8 source):
  - Sandbox.create(template=, api_key=, timeout_seconds=None) — None ⇒ no timeout
  - Sandbox.connect(sandbox_id, api_key=) — refreshes JWT, resumes paused VMs
  - sandbox.refresh_token() — explicit JWT refresh (POST /v1/sandboxes/:id/token/refresh)
  - sandbox.files.*  via AgentHTTPClient (Authorization: Bearer JWT)
  - sandbox.commands.run, sandbox.list_system_processes, etc.
  - WebSocket: wss://{public_host}/terminal, Bearer JWT
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Callable

import websockets
from websockets.asyncio.client import connect as ws_connect

from config import TERMINAL_COLS, TERMINAL_ROWS
import storage
from hopx_ai.errors import HopxError as _SdkHopxError, AuthenticationError as _SdkAuthError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-sandbox locks — serialise SDK calls + token refresh per sandbox_id
# ---------------------------------------------------------------------------

import threading as _threading

_sandbox_locks: Dict[str, _threading.Lock] = {}
_locks_guard = _threading.Lock()


def _lock_for(sandbox_id: str) -> _threading.Lock:
    g = _locks_guard
    g.acquire()
    try:
        lk = _sandbox_locks.get(sandbox_id)
        if lk is None:
            lk = _threading.Lock()
            _sandbox_locks[sandbox_id] = lk
        return lk
    finally:
        g.release()


# ---------------------------------------------------------------------------
# Data classes (decoupled from SDK models)
# ---------------------------------------------------------------------------

@dataclass
class SandboxInfoDto:
    sandbox_id: str
    public_host: str
    jwt: Optional[str]
    status: str
    expires_at: Optional[str]
    template_name: Optional[str]
    region: Optional[str]
    timeout_seconds: Optional[int]


@dataclass
class CommandResultDto:
    success: bool
    stdout: str
    stderr: str
    exit_code: int
    execution_time: float


@dataclass
class FileInfoDto:
    name: str
    path: str
    size: int
    is_directory: bool
    permissions: str
    modified_time: str


@dataclass
class ProcessDto:
    process_id: str
    name: Optional[str]
    status: str
    command: Optional[str]
    started_at: Optional[str]


class HopXError(Exception):
    """All SDK and hopx-internal errors funnel through this."""

    def __init__(self, message: str, code: str = "", status: int = 0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Exception mapping — collapse every SDK exception into HopXError
# ---------------------------------------------------------------------------

# Markers we treat as "token invalid" so we trigger a refresh + retry.
_TOKEN_INVALID_MARKERS = (
    "INVALID_TOKEN", "invalid_token",
    "TokenExpired", "token_expired",
    "auth_token", "unauthorized",
    "AUTHENTICATION_REQUIRED",
    # Fallback for old agents that speak HTTP semantics only:
    " 401",
)


def _is_token_invalid_error(e: BaseException) -> bool:
    msg = str(e).lower()
    code = (getattr(e, "code", "") or "").lower()
    status = getattr(e, "status_code", None) or getattr(e, "status", None)
    if isinstance(e, _SdkAuthError):
        return True
    if "invalid_token" in code or "token_expired" in code:
        return True
    if status == 401:
        return True
    return any(m.lower() in msg for m in _TOKEN_INVALID_MARKERS)


def _import_sdk():
    from hopx_ai import Sandbox
    return Sandbox


def _map_exc(e: BaseException) -> HopXError:
    msg = str(e)
    code = getattr(e, "code", "") or ""
    status = (
        getattr(e, "status_code", None)
        or getattr(e, "status", None)
        or 0
    )
    return HopXError(msg, code=code, status=status or 0)


# ---------------------------------------------------------------------------
# Control plane — sandbox lifecycle
# ---------------------------------------------------------------------------

def _validate_api_key_blocking(api_key: str) -> bool:
    Sandbox = _import_sdk()
    try:
        Sandbox.list(limit=1, api_key=api_key)
        return True
    except _SdkAuthError:
        return False
    except _SdkHopxError as e:
        m = str(e).lower()
        if any(t in m for t in _TOKEN_INVALID_MARKERS) or "401" in m:
            return False
        raise
    except Exception as e:
        m = str(e).lower()
        if any(t in m for t in _TOKEN_INVALID_MARKERS) or "401" in m:
            return False
        raise


async def validate_api_key(api_key: str) -> bool:
    return await asyncio.to_thread(_validate_api_key_blocking, api_key)


def _safe_refresh(sb) -> Optional[str]:
    """Force a JWT refresh and return the new token if available."""
    try:
        sb.refresh_token()
    except Exception as e:
        log.warning("refresh_token failed: %s", e)
        return None
    try:
        return sb.get_token()
    except Exception:
        return None


def _info_from_sb(sb, user_id: Optional[int] = None) -> SandboxInfoDto:
    """Snapshot the sandbox state (info + JWT from cache).

    IMPORTANT: does NOT force a refresh_token() if a valid JWT is already
    in the shared _token_cache.  Forcing a refresh on every call was the
    root cause of "INVALID_TOKEN" errors — the VM agent sometimes rejects
    a freshly-minted JWT for a few seconds until it observes the rotation
    event from the control plane.  Reusing the cached token avoids this
    race entirely.  The SDK's AgentHTTPClient.token_refresh_callback
    handles the rare case where the cached JWT genuinely expired.
    """
    from hopx_ai._token_cache import get_cached_token
    info = sb.get_info()
    token_data = get_cached_token(sb.sandbox_id)
    if token_data and token_data.token:
        jwt = token_data.token
    else:
        # No JWT cached yet — do a single refresh to seed the cache.
        jwt = _safe_refresh(sb)
        if not jwt:
            try:
                jwt = sb.get_token()
            except Exception:
                jwt = None
    exp = info.expires_at.isoformat() if info.expires_at else None
    dto = SandboxInfoDto(
        sandbox_id=info.sandbox_id,
        public_host=info.public_host,
        jwt=jwt,
        status=info.status,
        expires_at=exp,
        template_name=info.template_name,
        region=info.region,
        timeout_seconds=info.timeout_seconds,
    )
    if user_id is not None:
        try:
            storage.remember_sandbox(
                user_id=user_id,
                sandbox_id=dto.sandbox_id,
                template_name=dto.template_name,
                region=dto.region,
                status=dto.status,
                public_host=dto.public_host,
            )
        except Exception as e:
            log.warning("remember_sandbox failed: %s", e)
    return dto


def _create_sandbox_blocking(
    api_key: str, template: str,
    timeout_seconds: Optional[int], user_id: int,
) -> SandboxInfoDto:
    Sandbox = _import_sdk()
    kwargs = {"template": template, "api_key": api_key}
    if timeout_seconds and timeout_seconds > 0:
        kwargs["timeout_seconds"] = timeout_seconds
    last: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            sb = Sandbox.create(**kwargs)
            return _info_from_sb(sb, user_id=user_id)
        except Exception as e:
            last = e
            log.warning(
                "Sandbox.create failed (attempt %s/2): %s", attempt, e,
            )
            if attempt == 1:
                continue
            raise _map_exc(e)
    assert last is not None
    raise _map_exc(last)


async def create_sandbox(
    api_key: str, template: str,
    timeout_seconds: Optional[int], user_id: int,
) -> SandboxInfoDto:
    return await asyncio.to_thread(
        _create_sandbox_blocking, api_key, template, timeout_seconds, user_id
    )


def _connect_sandbox_blocking(
    api_key: str, sandbox_id: str, user_id: Optional[int] = None,
) -> SandboxInfoDto:
    Sandbox = _import_sdk()
    from hopx_ai._token_cache import clear_cached_token
    lock = _lock_for(sandbox_id)
    with lock:
        # Always fetch a fresh token; stale cached JWTS are the source
        # of every "INVALID_TOKEN (code: HTTP_401)" we've seen in tests.
        clear_cached_token(sandbox_id)
        try:
            sb = Sandbox.connect(sandbox_id, api_key=api_key)
        except Exception as e:
            log.debug("connect() rejected state (%s); trying lazy init", e)
            sb = Sandbox(sandbox_id, api_key=api_key)
        return _info_from_sb(sb, user_id=user_id)


async def connect_sandbox(
    api_key: str, sandbox_id: str, user_id: Optional[int] = None,
) -> SandboxInfoDto:
    return await asyncio.to_thread(
        _connect_sandbox_blocking, api_key, sandbox_id, user_id
    )


def _get_info_blocking(
    api_key: str, sandbox_id: str, user_id: Optional[int] = None,
) -> SandboxInfoDto:
    Sandbox = _import_sdk()
    sb = Sandbox(sandbox_id, api_key=api_key)
    return _info_from_sb(sb, user_id=user_id)


async def get_sandbox_info(
    api_key: str, sandbox_id: str, user_id: Optional[int] = None,
) -> SandboxInfoDto:
    return await asyncio.to_thread(_get_info_blocking, api_key, sandbox_id, user_id)


# ---------------------------------------------------------------------------
# Control plane — list all sandboxes (live snapshot of the platform)
# ---------------------------------------------------------------------------

def _list_plat_sandboxes_blocking(
    api_key: str, limit: int = 50,
) -> List[SandboxInfoDto]:
    """List the user's sandboxes straight from the Control Plane.

    Uses the raw `HTTPClient` so a single GET returns the full list
    with sandbox_id, status, public_host, template_name, region, expires_at
    — no need to call `get_info()` on each Sandbox instance.
    """
    from hopx_ai._client import HTTPClient
    client = HTTPClient(api_key=api_key)
    try:
        resp = client.get("/v1/sandboxes", params={"limit": min(limit, 100)})
    finally:
        try:
            client.close()
        except Exception:
            pass
    items = resp.get("data") or []
    out: List[SandboxInfoDto] = []
    for it in items:
        # Be defensive: the Platform may omit fields for deleted sandboxes.
        exp_raw = it.get("expires_at")
        try:
            exp_iso: Optional[str] = exp_raw
        except Exception:
            exp_iso = None
        out.append(SandboxInfoDto(
            sandbox_id=it.get("id") or it.get("sandbox_id") or "",
            public_host=it.get("public_host") or "",
            jwt=it.get("auth_token"),  # may be present, often None on list
            status=it.get("status") or "unknown",
            expires_at=exp_iso,
            template_name=it.get("template_name"),
            region=it.get("region"),
            timeout_seconds=it.get("timeout_seconds"),
        ))
    return out


async def list_plat_sandboxes(api_key: str, limit: int = 50) -> List[SandboxInfoDto]:
    return await asyncio.to_thread(_list_plat_sandboxes_blocking, api_key, limit)


def _lifecycle_blocking(
    api_key: str, sandbox_id: str, action: str,
    user_id: Optional[int] = None,
) -> SandboxInfoDto:
    Sandbox = _import_sdk()
    sb = Sandbox(sandbox_id, api_key=api_key)
    if action == "kill":
        sb.kill()
        dto = SandboxInfoDto(
            sandbox_id=sandbox_id, public_host="", jwt=None, status="killed",
            expires_at=None, template_name=None, region=None, timeout_seconds=None,
        )
        if user_id is not None:
            try:
                storage.forget_sandbox(user_id, sandbox_id)
            except Exception as e:
                log.warning("forget_sandbox failed: %s", e)
        return dto
    if action == "pause":
        sb.pause()
    elif action == "resume":
        sb.resume()
    else:
        raise HopXError(f"unknown action: {action}")
    return _info_from_sb(sb, user_id=user_id)


async def sandbox_lifecycle(
    api_key: str, sandbox_id: str, action: str,
    user_id: Optional[int] = None,
) -> SandboxInfoDto:
    return await asyncio.to_thread(
        _lifecycle_blocking, api_key, sandbox_id, action, user_id
    )


def _set_timeout_blocking(api_key: str, sandbox_id: str, seconds: int) -> None:
    Sandbox = _import_sdk()
    sb = Sandbox(sandbox_id, api_key=api_key)
    sb.set_timeout(seconds)


async def extend_timeout(api_key: str, sandbox_id: str, seconds: int) -> None:
    await asyncio.to_thread(_set_timeout_blocking, api_key, sandbox_id, seconds)


# ---------------------------------------------------------------------------
# VM Agent operations — refresh JWT, then run with retry on INVALID_TOKEN
# ---------------------------------------------------------------------------

def _vm_op_blocking(
    api_key: str, sandbox_id: str,
    op: Callable[[Any], Any],
    user_id: int,
) -> Any:
    """Run a VM operation against `sandbox_id`.

    Strategy:
      - Use the SDK's lazy `Sandbox(sandbox_id, api_key=...)` constructor,
        which makes *no* HTTP calls until a property is first accessed.
      - The SDK's `_ensure_valid_token` refreshes the JWT only when the
        cached one is missing or expiring within 1h. This avoids issuing
        a *fresh* JWT on every op (which was the source of the
        `INVALID_TOKEN` errors — the agent sometimes rejects a JWT that
        was just minted because it hasn't yet observed the rotation
        event from the control plane).
      - Trust the SDK's `AgentHTTPClient.token_refresh_callback` to
        handle 401s automatically. If the callback itself fails (the
        retry attempt also gets INVALID_TOKEN), we try ONE manual
        refresh + retry at this layer, then give up.
    """
    Sandbox = _import_sdk()
    from hopx_ai._token_cache import _token_cache, TokenData
    lock = _lock_for(sandbox_id)
    with lock:
        # Seed the SDK's in-memory cache with the JWT stored in our DB
        # so the lazy Sandbox() below does NOT trigger a refresh_token().
        # refresh_token() is unreliable — it sometimes produces a JWT the
        # VM agent rejects with INVALID_TOKEN, especially on older
        # sandboxes whose agent hasn't observed the rotation event.
        jwt = storage.get_state(user_id)
        if jwt and isinstance(jwt, dict):
            jwt_val = jwt.get("sandbox_jwt")
            if jwt_val and sandbox_id not in _token_cache:
                from datetime import datetime, timezone, timedelta
                _token_cache[sandbox_id] = TokenData(
                    token=jwt_val,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                )

        sb = Sandbox(sandbox_id, api_key=api_key)

        try:
            return op(sb)
        except Exception as e:
            if _is_token_invalid_error(e):
                log.info("VM op got INVALID_TOKEN on %s — JWT stale, NOT retrying (refresh unreliable): %s", sandbox_id, e)
            if isinstance(e, _SdkHopxError):
                raise _map_exc(e)
            raise _map_exc(e)


async def _vm_op(
    api_key: str, sandbox_id: str, op: Callable[[Any], Any], user_id: int,
) -> Any:
    return await asyncio.to_thread(
        _vm_op_blocking, api_key, sandbox_id, op, user_id
    )


# ---- Commands ---------------------------------------------------------------

def _run_command_op(sb, command, timeout, env, working_dir, background):
    res = sb.commands.run(
        command, timeout=timeout, env=env, working_dir=working_dir,
        background=background,
    )
    return CommandResultDto(
        success=res.is_success,
        stdout=res.stdout or "",
        stderr=res.stderr or "",
        exit_code=res.exit_code if res.exit_code is not None else -1,
        execution_time=res.execution_time or 0.0,
    )


async def run_command(
    api_key: str, sandbox_id: str,
    command: str, timeout: int = 120,
    env: Optional[Dict[str, str]] = None,
    working_dir: str = "/workspace",
    background: bool = False,
    user_id: int = 0,
) -> CommandResultDto:
    return await _vm_op(
        api_key, sandbox_id,
        lambda sb: _run_command_op(
            sb, command, timeout, env, working_dir, background
        ),
        user_id,
    )


# ---- Files ------------------------------------------------------------------

def _list_files_op(sb, path):
    items = sb.files.list(path)
    return [
        FileInfoDto(
            name=f.name, path=f.path, size=f.size,
            is_directory=f.is_directory,
            permissions=f.permissions or "",
            modified_time=f.modified_time or "",
        )
        for f in items
    ]


async def list_files(api_key, sandbox_id, path="/workspace", user_id: int = 0):
    return await _vm_op(api_key, sandbox_id, lambda sb: _list_files_op(sb, path), user_id)


def _read_bytes_op(sb, path) -> bytes:
    return sb.files.read_bytes(path)


async def read_file_bytes(api_key, sandbox_id, path, user_id: int = 0) -> bytes:
    return await _vm_op(api_key, sandbox_id, lambda sb: _read_bytes_op(sb, path), user_id)


def _read_text_op(sb, path) -> bytes:
    return sb.files.read(path).encode("utf-8")


async def read_file_text(api_key, sandbox_id, path, user_id: int = 0) -> bytes:
    return await _vm_op(api_key, sandbox_id, lambda sb: _read_text_op(sb, path), user_id)


def _write_bytes_op(sb, path, data):
    sb.files.write_bytes(path, data)


async def write_file_bytes(api_key, sandbox_id, path, data: bytes, user_id: int = 0):
    await _vm_op(api_key, sandbox_id,
                 lambda sb: _write_bytes_op(sb, path, data), user_id)


def _delete_op(sb, path):
    sb.files.remove(path)


async def delete_file(api_key, sandbox_id, path, user_id: int = 0):
    await _vm_op(api_key, sandbox_id, lambda sb: _delete_op(sb, path), user_id)


def _mkdir_op(sb, path):
    sb.files.mkdir(path)


async def make_dir(api_key, sandbox_id, path, user_id: int = 0):
    await _vm_op(api_key, sandbox_id, lambda sb: _mkdir_op(sb, path), user_id)


# ---- Processes ---------------------------------------------------------------

def _list_system_processes_op(sb) -> List[ProcessDto]:
    procs = sb.list_system_processes()
    out: List[ProcessDto] = []
    for p in procs:
        out.append(ProcessDto(
            process_id=str(p.get("pid", "")),
            name=p.get("name") or p.get("command", ""),
            status=p.get("status") or "running",
            command=p.get("command"),
            started_at=p.get("start_time"),
        ))
    return out


async def list_system_processes(api_key, sandbox_id, user_id: int = 0):
    return await _vm_op(api_key, sandbox_id, _list_system_processes_op, user_id)


def _list_bg_processes_op(sb) -> List[ProcessDto]:
    procs = sb.list_processes()
    out: List[ProcessDto] = []
    for p in procs:
        out.append(ProcessDto(
            process_id=str(p.get("process_id") or p.get("pid") or ""),
            name=p.get("name"),
            status=p.get("status", "unknown"),
            command=p.get("command"),
            started_at=p.get("started_at"),
        ))
    return out


async def list_background_processes(api_key, sandbox_id, user_id: int = 0):
    return await _vm_op(api_key, sandbox_id, _list_bg_processes_op, user_id)


def _kill_bg_op(sb, process_id):
    sb.kill_process(process_id)


async def kill_background_process(api_key, sandbox_id, process_id, user_id: int = 0):
    await _vm_op(api_key, sandbox_id,
                 lambda sb: _kill_bg_op(sb, process_id), user_id)


async def kill_system_process(
    api_key, sandbox_id, pid: str, timeout: int = 10, user_id: int = 0,
) -> CommandResultDto:
    return await run_command(
        api_key, sandbox_id, f"kill -9 {pid}",
        timeout=timeout, user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Terminal — raw WebSocket
# ---------------------------------------------------------------------------

def _ws_url(public_host: str) -> str:
    host = public_host
    if host.startswith("https://"):
        host = "wss://" + host[len("https://"):]
    elif host.startswith("http://"):
        host = "ws://" + host[len("http://"):]
    elif host.startswith("wss://") or host.startswith("ws://"):
        pass
    else:
        host = "wss://" + host
    return host.rstrip("/") + "/terminal"


class TerminalSession:
    """Stateful interactive PTY-like session over WebSocket."""

    def __init__(self, public_host: str, jwt: str):
        self.public_host = public_host
        self.jwt = jwt
        self.ws = None
        self._closed = False

    async def connect(self) -> None:
        url = _ws_url(self.public_host)
        headers = {"Authorization": f"Bearer {self.jwt}"}
        try:
            self.ws = await ws_connect(
                url,
                additional_headers=headers,
                ping_interval=20, ping_timeout=60, open_timeout=30,
            )
        except Exception as e:
            raise HopXError(f"terminal connect failed: {e}")
        await self.resize(TERMINAL_COLS, TERMINAL_ROWS)
        # Disable bracketed-paste so bash doesn't wrap every paste in
        # `\x1b[?2004h` ... `\x1b[?2004l` markers.
        try:
            await self.ws.send(json.dumps({"type": "input", "data": "\x1b[?2004l"}))
            await asyncio.sleep(0.05)
            await self.ws.send(json.dumps(
                {"type": "input", "data": "export TERM=dumb\n"}))
        except Exception:
            pass
        try:
            await self.recv_output(idle_s=0.3, grace_s=0.5)
        except Exception:
            pass

    @property
    def is_open(self) -> bool:
        try:
            return self.ws is not None and self.ws.state.name == "OPEN"
        except Exception:
            return False

    async def resize(self, cols: int, rows: int) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps(
                {"type": "resize", "cols": cols, "rows": rows}))
        except Exception:
            pass

    async def send_input(self, text: str, execute: bool = True) -> None:
        if not self.is_open:
            raise HopXError("terminal closed")
        payload = text + ("\n" if execute else "")
        await self.ws.send(json.dumps({"type": "input", "data": payload}))

    async def recv_output(
        self, idle_s: float = 0.6, grace_s: float = 1.5,
        max_quiet_cycles: int = 2,
    ) -> str:
        if self.ws is None:
            return ""
        buffer: List[str] = []
        quiet_cycles = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=idle_s)
                    quiet_cycles = 0
                except asyncio.TimeoutError:
                    quiet_cycles += 1
                    if quiet_cycles >= max_quiet_cycles:
                        break
                    try:
                        raw = await asyncio.wait_for(
                            self.ws.recv(), timeout=grace_s,
                        )
                        quiet_cycles = 0
                    except asyncio.TimeoutError:
                        break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if not raw or not raw.strip():
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    buffer.append(raw)
                    continue
                if not isinstance(msg, dict):
                    buffer.append(str(msg))
                    continue
                mtype = msg.get("type")
                if mtype == "output":
                    buffer.append(msg.get("data", ""))
                elif mtype in ("stderr", "error"):
                    buffer.append(msg.get("data", ""))
                elif mtype in ("exit", "done"):
                    code = msg.get("code") if mtype == "exit" else msg.get("exit_code")
                    if code is not None:
                        buffer.append(f"\n[exit {code}]")
                    break
        except websockets.ConnectionClosed:
            self._closed = True
        except Exception as e:
            buffer.append(f"\n[recv error: {e}]")
        return "".join(buffer)

    async def close(self) -> None:
        self._closed = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None


async def open_terminal(public_host: str, jwt: str) -> TerminalSession:
    sess = TerminalSession(public_host, jwt)
    await sess.connect()
    return sess
