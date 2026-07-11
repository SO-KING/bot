"""In-memory state for active users — JWT caching + active terminal sessions."""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Dict

from hopx_client import TerminalSession

# user_id → cached runtime state
@dataclass
class RuntimeState:
    api_key: str = ""
    sandbox_id: str = ""
    public_host: str = ""
    jwt: str = ""
    # mode flags — used by menu.text_router
    awaiting: str = ""        # 'api_key' | 'terminal' | 'files:upload' | 'files:mkdir' | ''
    terminal_path: str = ""  # nothing yet
    files_path: str = "/workspace"
    upload_filename: str = ""  # path hint for next document upload
    files_pending: str = ""
    terminal_session: Optional[TerminalSession] = None
    terminal_stdin: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Anti-duplicate-create guard. When user taps "create" we set this to
    # `time.monotonic()`. Any subsequent _create call within
    # _CREATE_LOCK_TTL seconds is ignored. Stops double-taps from
    # spawning multiple sandboxes on HopX.
    create_started_at: float = 0.0


_CREATE_LOCK_TTL: float = 6.0


def is_create_locked(user_id: int) -> bool:
    s = get(user_id)
    if not s.create_started_at:
        return False
    return (time.monotonic() - s.create_started_at) < _CREATE_LOCK_TTL


def clear_create_lock(user_id: int) -> None:
    s = get(user_id)
    s.create_started_at = 0.0


def acquire_create_lock(user_id: int) -> bool:
    """Mark that a create-sandbox flow has begun. Returns True if this
    caller should proceed, False if another create-sandbox flow is
    already in flight (and we should ignore this click)."""
    if is_create_locked(user_id):
        return False
    get(user_id).create_started_at = time.monotonic()
    return True


_state: Dict[int, RuntimeState] = {}


def get(user_id: int) -> RuntimeState:
    s = _state.get(user_id)
    if not s:
        s = RuntimeState()
        _state[user_id] = s
    return s


async def drop(user_id: int) -> None:
    s = _state.pop(user_id, None)
    if s and s.terminal_session:
        await s.terminal_session.close()
