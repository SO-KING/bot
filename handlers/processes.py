"""Processes handler — only show background / actively running commands
the user started (no system processes).

Per design: bot must NEVER implicitly stop a user-started process.
Background processes live inside the sandbox VM and continue running
across terminal close/reconnect — exactly like a VPS. The user can
explicitly kill a process via the kill button; everything else stays.
"""
import html
import logging
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hopx_client import (
    list_background_processes,
    kill_background_process,
    HopXError,
)
from keyboards import processes_keyboard, main_menu
from ui import append_log
from .sandbox import _ensure_creds
from .helpers import safe_edit, html_escape

log = logging.getLogger(__name__)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    parts = data.split(":")
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        await _list(update, ctx)
    elif sub == "refresh":
        await _list(update, ctx)
    elif sub == "kill":
        pid = parts[2] if len(parts) > 2 else ""
        await _kill(update, ctx, pid)


async def _list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List only user-background processes (those started via
    HopX `/commands/background` or persisted inside the sandbox).
    These persist across terminal sessions exactly like a VPS —
    the bot never implicitly stops them.
    """
    user_id = update.effective_user.id
    q = update.callback_query
    creds = await _ensure_creds(user_id)
    if not creds:
        await safe_edit(
            q,
            append_log("⚠️ لا يوجد سيرفر نشط.", user_id),
            reply_markup=main_menu(False),
        )
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    try:
        items = await list_background_processes(api_key, sandbox_id, user_id=user_id)
    except HopXError as e:
        await safe_edit(
            q,
            append_log(f"❌ {html_escape(str(e))}", user_id),
            reply_markup=main_menu(True),
        )
        return

    title = f"🔁 العمليات الشغّالة ({len(items)})"
    if items:
        body = (
            f"{title}\n\n"
            "<i>هذه عملياتك اللي شغّالة داخل السيرفر — تستمر شغّالة "
            "حتى لو خرجت من الترمنال أو أغلقته (مثل VPS بالضبط</i>"
        )
    else:
        body = (
            f"{title}\n\n"
            "<i>لا توجد عمليات خلفية شغّالة حالياً.\n\n"
            "شغّل أمراً من الترمنال باستخدام:\n"
            "<code>nohup YOUR_COMMAND &amp</code>\n"
            "ليُحفظ في الخلفية</i>"
        )
    await safe_edit(
        q,
        append_log(body, user_id),
        reply_markup=processes_keyboard(items),
    )


async def _kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE, pid: str) -> None:
    """User-initiated kill. Only triggered by explicit tap on the kill
    button — never implicit. Always works, never silently drops.
    """
    user_id = update.effective_user.id
    q = update.callback_query
    if not pid:
        return
    creds = await _ensure_creds(user_id)
    if not creds:
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    try:
        await kill_background_process(api_key, sandbox_id, pid, user_id=user_id)
        await q.message.reply_text(
            append_log(f"🛑 أُوقفت العملية: <code>{html.escape(pid)}</code>", user_id),
            parse_mode=ParseMode.HTML,
        )
    except HopXError as e:
        await q.message.reply_text(
            append_log(f"❌ {html_escape(str(e))}", user_id),
            parse_mode=ParseMode.HTML,
        )
    await _list(update, ctx)
