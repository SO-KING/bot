"""SQLite storage — per-user state (encrypted API key, sandbox_id, public_host, JWT, expiry).

Schema:
    users:
        user_id        INTEGER PRIMARY KEY
        api_key_enc    TEXT   (Fernet-encrypted HopX API key)
        sandbox_id     TEXT   (current sandbox id, NULL if none)
        public_host    TEXT   (sandbox public_host URL for VM agent)
        sandbox_jwt    TEXT   (last known JWT for VM agent — refreshed on demand)
        sandbox_exp    TEXT   (ISO 8601 expiry timestamp, NULL = no timeout)
        status         TEXT   (latest sandbox status: running / paused / killed / ...)
        created_at, updated_at
    terminal_sessions:
        user_id, active (0/1), cwd_hint, started_at
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config import DB_PATH
from crypto import encrypt, decrypt

_lock = threading.Lock()


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                api_key_enc  TEXT,
                sandbox_id   TEXT,
                public_host  TEXT,
                sandbox_jwt  TEXT,
                sandbox_exp  TEXT,
                status       TEXT,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS terminal_sessions (
                user_id      INTEGER PRIMARY KEY,
                active       INTEGER DEFAULT 0,
                cwd_hint     TEXT DEFAULT '/workspace',
                started_at   TEXT
            )
            """
        )
        # History of every sandbox a user has ever created — so we can
        # reattach to any of them later even after `users.sandbox_id` is reset.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sandboxes (
                user_id        INTEGER NOT NULL,
                sandbox_id     TEXT    NOT NULL,
                template_name  TEXT,
                region         TEXT,
                status         TEXT,
                public_host    TEXT,
                created_at     TEXT,
                last_used_at   TEXT,
                PRIMARY KEY (user_id, sandbox_id)
            )
            """
        )
        # Lightweight migrations: add columns if missing (setting defaults to NULL)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "public_host" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN public_host TEXT")
        if "sandbox_jwt" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN sandbox_jwt TEXT")
        if "sandbox_exp" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN sandbox_exp TEXT")
        if "status" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN status TEXT")

        # New admin/subscription tables
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS forced_channels (
                channel_username TEXT PRIMARY KEY
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY
            )
            """
        )
        # Seed the default admin
        c.execute(
            "INSERT OR IGNORE INTO admins (user_id) VALUES (7979799419)"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# API key
# -----------------------------------------------------------------------------

def set_api_key(user_id: int, api_key: str) -> None:
    enc = encrypt(api_key)
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO users (user_id, api_key_enc, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                api_key_enc = excluded.api_key_enc,
                sandbox_id  = NULL,
                public_host = NULL,
                sandbox_jwt = NULL,
                sandbox_exp = NULL,
                status      = NULL,
                updated_at  = excluded.updated_at
            """,
            (user_id, enc, _now()),
        )


def get_api_key(user_id: int) -> Optional[str]:
    with _conn() as c:
        row = c.execute("SELECT api_key_enc FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row or not row["api_key_enc"]:
        return None
    return decrypt(row["api_key_enc"])


def has_api_key(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT api_key_enc FROM users WHERE user_id=? AND api_key_enc IS NOT NULL",
            (user_id,),
        ).fetchone()
    return row is not None


def delete_api_key(user_id: int) -> None:
    """Permanently delete a user's API key entry (used by /reset)."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM sandboxes WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM terminal_sessions WHERE user_id = ?", (user_id,))


def change_api_key(user_id: int, new_api_key: str) -> None:
    """Replace the user's API key but keep the `users` row.

    - Clears the cached sandbox credentials (`sandbox_id`, `public_host`,
      `sandbox_jwt`, `sandbox_exp`, `status`) so a request against the
      old sandbox can never reuse them against the new key.
    - Clears the sandbox history table as well: sandboxes created under
      the previous key are not visible to the new key on the HopX
      Control Plane, so showing them in "servers" would only confuse the
      user with dead entries.
    - Terminal cwd is preserved — the path inside the sandbox does not
      change just because the API key changed.

    The caller MUST have validated `new_api_key` before calling this.
    """
    enc = encrypt(new_api_key)
    with _lock, _conn() as c:
        c.execute(
            """
            UPDATE users SET
                api_key_enc = ?,
                sandbox_id   = NULL,
                public_host  = NULL,
                sandbox_jwt  = NULL,
                sandbox_exp  = NULL,
                status       = NULL,
                updated_at   = ?
            WHERE user_id = ?
            """,
            (enc, _now(), user_id),
        )
        c.execute("DELETE FROM sandboxes WHERE user_id = ?", (user_id,))


# -----------------------------------------------------------------------------
# Sandbox state
# -----------------------------------------------------------------------------

def set_sandbox(
    user_id: int,
    sandbox_id: str,
    public_host: str,
    jwt: Optional[str],
    sandbox_exp: Optional[str],
    status: str,
) -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            UPDATE users SET
                sandbox_id  = ?,
                public_host = ?,
                sandbox_jwt = ?,
                sandbox_exp = ?,
                status      = ?,
                updated_at  = ?
            WHERE user_id = ?
            """,
            (sandbox_id, public_host, jwt, sandbox_exp, status, _now(), user_id),
        )


def update_jwt(user_id: int, jwt: Optional[str]) -> None:
    with _lock, _conn() as c:
        c.execute(
            "UPDATE users SET sandbox_jwt = ?, updated_at = ? WHERE user_id = ?",
            (jwt, _now(), user_id),
        )


def clear_sandbox(user_id: int) -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            UPDATE users SET
                sandbox_id  = NULL,
                public_host = NULL,
                sandbox_jwt = NULL,
                sandbox_exp = NULL,
                status      = NULL,
                updated_at  = ?
            WHERE user_id = ?
            """,
            (_now(), user_id),
        )


# --- Sandbox history (multiple sandbox_ids per user) -------------------------

def remember_sandbox(
    user_id: int,
    sandbox_id: str,
    template_name: Optional[str] = None,
    region: Optional[str] = None,
    status: Optional[str] = None,
    public_host: Optional[str] = None,
) -> None:
    """Insert or update an entry in the user's sandbox history."""
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO sandboxes
                (user_id, sandbox_id, template_name, region, status, public_host,
                 created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, sandbox_id) DO UPDATE SET
                template_name = COALESCE(excluded.template_name, sandboxes.template_name),
                region        = COALESCE(excluded.region, sandboxes.region),
                status        = COALESCE(excluded.status, sandboxes.status),
                public_host   = COALESCE(excluded.public_host, sandboxes.public_host),
                last_used_at  = excluded.last_used_at
            """,
            (user_id, sandbox_id, template_name, region, status, public_host,
             _now(), _now()),
        )


def forget_sandbox(user_id: int, sandbox_id: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM sandboxes WHERE user_id = ? AND sandbox_id = ?",
            (user_id, sandbox_id),
        )


def list_sandboxes(user_id: int, limit: int = 20) -> list[dict]:
    """Return list of {sandbox_id, template_name, region, status, public_host,
    last_used_at} sorted by most recently used.
    """
    with _conn() as c:
        rows = c.execute(
            """
            SELECT sandbox_id, template_name, region, status, public_host,
                   created_at, last_used_at
            FROM sandboxes
            WHERE user_id = ?
            ORDER BY last_used_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [
        {
            "sandbox_id": r["sandbox_id"],
            "template_name": r["template_name"],
            "region": r["region"],
            "status": r["status"],
            "public_host": r["public_host"],
            "created_at": r["created_at"],
            "last_used_at": r["last_used_at"],
        }
        for r in rows
    ]


def get_active_sandbox_id(user_id: int) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT sandbox_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["sandbox_id"] if row else None


def set_active_sandbox(user_id: int, sandbox_id: str) -> None:
    """Mark a historical sandbox_id as the user's active one (without info
    reload — caller should populate public_host/jwt/status via set_sandbox)."""
    with _lock, _conn() as c:
        c.execute(
            "UPDATE users SET sandbox_id = ?, updated_at = ? WHERE user_id = ?",
            (sandbox_id, _now(), user_id),
        )


def get_state(user_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            """
            SELECT api_key_enc, sandbox_id, public_host, sandbox_jwt, sandbox_exp, status
            FROM users WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "has_api_key": bool(row["api_key_enc"]),
        "sandbox_id": row["sandbox_id"],
        "public_host": row["public_host"],
        "sandbox_jwt": row["sandbox_jwt"],
        "sandbox_exp": row["sandbox_exp"],
        "status": row["status"],
    }


# -----------------------------------------------------------------------------
# Terminal sessions (bookkeeping only — actual WebSocket held in memory)
# -----------------------------------------------------------------------------

def set_terminal_active(user_id: int, active: bool, cwd_hint: str = "/workspace") -> None:
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO terminal_sessions (user_id, active, cwd_hint, started_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                active     = excluded.active,
                cwd_hint   = excluded.cwd_hint,
                started_at = excluded.started_at
            """,
            (user_id, 1 if active else 0, cwd_hint, _now() if active else None),
        )


def is_terminal_active(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT active FROM terminal_sessions WHERE user_id=?", (user_id,)
        ).fetchone()
    return bool(row and row["active"])


def get_terminal_cwd(user_id: int) -> str:
    """Return the user's last-known terminal cwd, falling back to /workspace."""
    with _conn() as c:
        row = c.execute(
            "SELECT cwd_hint FROM terminal_sessions WHERE user_id=?", (user_id,)
        ).fetchone()
    if row and row["cwd_hint"]:
        return row["cwd_hint"]
    return "/workspace"


def set_terminal_cwd(user_id: int, cwd: str) -> None:
    """Persist the user's current terminal cwd (called after `pwd` runs)."""
    if not cwd or not cwd.startswith("/"):
        return
    with _lock, _conn() as c:
        c.execute(
            """
            INSERT INTO terminal_sessions (user_id, active, cwd_hint, started_at)
            VALUES (?, 0, ?, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                cwd_hint = excluded.cwd_hint
            """,
            (user_id, cwd),
        )


# -----------------------------------------------------------------------------
# Forced channels (mandatory subscription)
# -----------------------------------------------------------------------------

def add_forced_channel(channel_username: str) -> None:
    """Add a channel username (without @) that users must join."""
    uname = channel_username.strip().lstrip("@")
    with _lock, _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO forced_channels (channel_username) VALUES (?)",
            (uname,),
        )


def remove_forced_channel(channel_username: str) -> None:
    """Remove a channel from the forced subscription list."""
    uname = channel_username.strip().lstrip("@")
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM forced_channels WHERE channel_username = ?",
            (uname,),
        )


def get_forced_channels() -> list[str]:
    """Return list of channel usernames for forced subscription."""
    with _conn() as c:
        rows = c.execute("SELECT channel_username FROM forced_channels").fetchall()
    return [r["channel_username"] for r in rows]


def has_forced_channels() -> bool:
    with _conn() as c:
        row = c.execute("SELECT COUNT(*) as cnt FROM forced_channels").fetchone()
    return row["cnt"] > 0 if row else False


# -----------------------------------------------------------------------------
# Admins
# -----------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id FROM admins WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def add_admin(user_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))


def remove_admin(user_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))


def get_all_admins() -> list[int]:
    with _conn() as c:
        rows = c.execute("SELECT user_id FROM admins").fetchall()
    return [r["user_id"] for r in rows]


# -----------------------------------------------------------------------------
# Banned users
# -----------------------------------------------------------------------------

def is_banned(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


def ban_user(user_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))


def unban_user(user_id: int) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))


def get_banned_users() -> list[int]:
    with _conn() as c:
        rows = c.execute("SELECT user_id FROM banned_users").fetchall()
    return [r["user_id"] for r in rows]


# -----------------------------------------------------------------------------
# Admin statistics
# -----------------------------------------------------------------------------

def get_stats() -> dict:
    with _conn() as c:
        total_users = c.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        active_sandboxes = c.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE sandbox_id IS NOT NULL"
        ).fetchone()
        total_sandboxes = c.execute(
            "SELECT COUNT(*) as cnt FROM sandboxes"
        ).fetchone()
        total_banned = c.execute(
            "SELECT COUNT(*) as cnt FROM banned_users"
        ).fetchone()
        total_admins = c.execute(
            "SELECT COUNT(*) as cnt FROM admins"
        ).fetchone()
        forced_channels = c.execute(
            "SELECT COUNT(*) as cnt FROM forced_channels"
        ).fetchone()
    return {
        "total_users": total_users["cnt"] if total_users else 0,
        "active_sandboxes": active_sandboxes["cnt"] if active_sandboxes else 0,
        "total_sandboxes": total_sandboxes["cnt"] if total_sandboxes else 0,
        "total_banned": total_banned["cnt"] if total_banned else 0,
        "total_admins": total_admins["cnt"] if total_admins else 0,
        "forced_channels": forced_channels["cnt"] if forced_channels else 0,
    }


def get_all_users_with_sandboxes() -> list[dict]:
    """Return list of users who have a sandbox_id in users table."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT user_id, sandbox_id, status, public_host, sandbox_exp
            FROM users WHERE sandbox_id IS NOT NULL
            ORDER BY user_id
            """
        ).fetchall()
    return [
        {
            "user_id": r["user_id"],
            "sandbox_id": r["sandbox_id"],
            "status": r["status"],
            "public_host": r["public_host"],
            "sandbox_exp": r["sandbox_exp"],
        }
        for r in rows
    ]
