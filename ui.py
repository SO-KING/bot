"""UI helpers: log bar under each message + sandbox-state-driven text."""
from typing import Optional

from utils.format import seconds_until, human_timedelta
import storage


def log_bar(user_id: int) -> str:
    state = storage.get_state(user_id) or {}
    sb_id = state.get("sandbox_id")
    status = state.get("status") or "—"
    if status == "—":
        return "\n\n⚙️ <i>لا يوجد سيرفر — اضغط 🚀 للإنشاء</i>"
    exp = state.get("sandbox_exp")
    short = sb_id[:18] + "…" if sb_id and len(sb_id) > 19 else (sb_id or "no-sandbox")
    if sb_id and exp:
        secs = seconds_until(exp)
        if secs > 0:
            time_left = human_timedelta(secs)
        else:
            time_left = "منتهي"
    else:
        time_left = "—"
    return f"\n\n⚙️ <code>{short}</code> • {status} • ⏳ {time_left}"


def append_log(text: str, user_id: int) -> str:
    return text + log_bar(user_id)
