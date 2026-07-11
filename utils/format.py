"""Formatting helpers."""
from datetime import datetime, timezone
from typing import Optional


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    units = ["KB", "MB", "GB", "TB"]
    f = float(num_bytes)
    for u in units:
        f /= 1024.0
        if f < 1024.0:
            return f"{f:.2f} {u}"
    return f"{f:.2f} PB"


def human_timedelta(seconds: int) -> str:
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def seconds_until(iso: Optional[str]) -> int:
    if not iso:
        return -1
    try:
        dt = parse_iso(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return int(delta)
    except Exception:
        return -1
