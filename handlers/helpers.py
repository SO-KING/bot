"""Reusable handler helpers."""
import html
from typing import Optional

from telegram import Update, InlineKeyboardMarkup
from telegram.error import BadRequest

from ui import append_log


async def safe_edit(q, text: str, *, reply_markup=None,
                    parse_mode: str = "HTML", **kwargs) -> bool:
    """`q.edit_message_text` that tolerates `Message is not modified` and
    falls back to `q.message.reply_text` if the message was deleted,
    and to `bot.send_message` if even the message is gone.

    Returns True iff the message was actually modified.
    """
    if q is None:
        return False
    try:
        await q.edit_message_text(
            text, reply_markup=reply_markup,
            parse_mode=parse_mode, **kwargs,
        )
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return False
        if "message to edit not found" in msg or "message can't be edited" in msg:
            try:
                inner_msg = q.message
                if inner_msg is None:
                    return False
                await inner_msg.reply_text(
                    text, reply_markup=reply_markup,
                    parse_mode=parse_mode, **kwargs,
                )
                return True
            except Exception:
                return False
        raise


async def safe_answer(q, text: str = None, *, show_alert: bool = False) -> None:
    """`q.answer(...)` that ignores stale-query errors."""
    try:
        await q.answer(text=text, show_alert=show_alert)
    except BadRequest:
        pass


def html_escape(s: str) -> str:
    return html.escape(s)[:400]
