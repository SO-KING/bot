"""Central text & callback router — dispatches to other handlers.

Wraps `q.edit_message_text` so a no-op edit (clicking the same button
twice) doesn't raise `BadRequest: Message is not modified` and the user
sees a friendly feedback instead.
"""
import asyncio
import html
import logging
from typing import Optional

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import storage
from keyboards import main_menu, forced_sub_keyboard
from ui import append_log
from . import sandbox_state
from . import sandbox as sb_h
from . import terminal as term_h
from . import files as files_h
from . import processes as proc_h
from . import admin as admin_h
from .helpers import safe_edit

log = logging.getLogger(__name__)


async def edit_message_text_safe(q, text: str, *, reply_markup=None,
                                 parse_mode: str = "HTML", **kwargs) -> bool:
    """`editMessageText` that tolerates `Message is not modified` and any
    other Telegram `BadRequest`.  Returns True iff the message was
    actually edited; returns False if the edit was a no-op.
    """
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
        if "message to edit not found" in msg:
            # Original message was deleted — fall back to plain reply.
            try:
                await q.message.reply_text(
                    text, reply_markup=reply_markup,
                    parse_mode=parse_mode, **kwargs,
                )
                return True
            except Exception:
                return False
        raise


async def _check_subscription(user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if the user is subscribed to all forced channels or
    if there are no forced channels."""
    channels = storage.get_forced_channels()
    if not channels:
        return True
    try:
        for ch in channels:
            member = await ctx.bot.get_chat_member(chat_id=f"@{ch}", user_id=user_id)
            status = member.status
            if status in ("left", "kicked", "restricted"):
                return False
        return True
    except Exception as e:
        log.warning("sub check failed for %s: %s", user_id, e)
        return True


async def _send_subscription_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the forced subscription prompt to the user.

    Robust against any callback/update shape: if `effective_message`
    isn't available (e.g. callback from an inline-only message), falls
    back to bot.send_message on the user's id.
    """
    user_id = update.effective_user.id
    channels = storage.get_forced_channels()
    text = (
        "⚠️ <b>الاشتراك الإجباري</b>\n\n"
        "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:\n\n"
        + "\n".join(f"• @{ch}" for ch in channels)
        + "\n\nبعد الاشتراك اضغط '✅ تحقق من الاشتراك'."
    )
    msg = update.effective_message
    if msg is None:
        msg = update.callback_query.message if update.callback_query else None
    if msg is not None:
        try:
            await msg.reply_text(
                text,
                reply_markup=forced_sub_keyboard(channels),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    try:
        await ctx.bot.send_message(
            chat_id=user_id, text=text,
            reply_markup=forced_sub_keyboard(channels),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("sub prompt send failed for %s: %s", user_id, e)


def user_has_active_sandbox(user_id: int) -> bool:
    try:
        st = storage.get_state(user_id)
        return bool(st and st.get("sandbox_id"))
    except Exception:
        return False


async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    user_id = update.effective_user.id

    if storage.is_banned(user_id):
        return

    st = sandbox_state.get(user_id)
    text = update.effective_message.text or ""

    if st.awaiting.startswith("admin:"):
        await admin_h.on_admin_text(update, ctx)
        return

    if not storage.is_admin(user_id):
        sub_ok = await _check_subscription(user_id, ctx)
        if not sub_ok:
            await _send_subscription_prompt(update, ctx)
            return

    mode = st.awaiting
    log.info("text_router user=%s mode=%s", user_id, mode)

    if mode == "api_key" or mode == "api_key_change":
        await sb_h.receive_api_key(update, ctx)
    elif mode == "terminal":
        await term_h.on_text(update, ctx)
    elif mode == "files:upload":
        await files_h.on_upload_path_text(update, ctx)
    elif mode == "files:mkdir":
        await files_h.on_mkdir_text(update, ctx)
    else:
        await update.effective_message.reply_text(
            append_log("اختر من القائمة أدناه 👇", user_id),
            reply_markup=main_menu(user_has_active_sandbox(user_id)),
            parse_mode="HTML",
        )


async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query:
        return
    q = update.callback_query

    try:
        await q.answer()
    except BadRequest:
        pass

    user_id = q.from_user.id
    data = q.data or ""
    log.info("cb user=%s data=%s", user_id, data)

    if storage.is_banned(user_id):
        return

    if data == "check_sub":
        sub_ok = await _check_subscription(user_id, ctx)
        if sub_ok:
            text = "✅ تم التحقق! اشتراكك مفعّل.\n\nاختر من القائمة أدناه 👇"
            kb = main_menu(user_has_active_sandbox(user_id))
            try:
                await safe_edit(
                    q, text, reply_markup=kb,
                )
            except Exception:
                # Fallback: reply as new message if edit fails (deleted msg etc.)
                try:
                    await q.message.reply_text(
                        text, reply_markup=kb, parse_mode="HTML",
                    )
                except Exception:
                    try:
                        await ctx.bot.send_message(
                            chat_id=user_id, text=text,
                            reply_markup=kb, parse_mode="HTML",
                        )
                    except Exception as e:
                        log.warning("check_sub reply failed for %s: %s", user_id, e)
        else:
            await _send_subscription_prompt(update, ctx)
        return

    if data == "menu:home" and not storage.is_admin(user_id):
        sub_ok = await _check_subscription(user_id, ctx)
        if not sub_ok:
            await _send_subscription_prompt(update, ctx)
            return

    try:
        if data.startswith("menu:"):
            await _menu(update, ctx, data)
        elif data == "ui:cancel":
            from handlers.start import cmd_cancel
            await cmd_cancel(update, ctx)
        elif data.startswith("admin:"):
            await admin_h.on_callback(update, ctx, data)
        elif data.startswith("sb:"):
            if not storage.is_admin(user_id):
                sub_ok = await _check_subscription(user_id, ctx)
                if not sub_ok:
                    await _send_subscription_prompt(update, ctx)
                    return
            await sb_h.on_callback(update, ctx, data)
        elif data.startswith("term:"):
            if not storage.is_admin(user_id):
                sub_ok = await _check_subscription(user_id, ctx)
                if not sub_ok:
                    await _send_subscription_prompt(update, ctx)
                    return
            await term_h.on_callback(update, ctx, data)
        elif data.startswith("files:"):
            if not storage.is_admin(user_id):
                sub_ok = await _check_subscription(user_id, ctx)
                if not sub_ok:
                    await _send_subscription_prompt(update, ctx)
                    return
            await files_h.on_callback(update, ctx, data)
        elif data.startswith("proc:"):
            if not storage.is_admin(user_id):
                sub_ok = await _check_subscription(user_id, ctx)
                if not sub_ok:
                    await _send_subscription_prompt(update, ctx)
                    return
            await proc_h.on_callback(update, ctx, data)
        elif data == "noop":
            pass
        else:
            log.warning("unknown callback data: %s", data)
    except BadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            log.info("ignoring 'not modified' for %s", data)
            return
        log.exception("callback BadRequest: %s", e)
        try:
            await q.message.reply_text(
                append_log(f"⚠️ {html.escape(str(e))[:300]}", user_id),
                reply_markup=main_menu(user_has_active_sandbox(user_id)),
                parse_mode="HTML",
            )
        except Exception:
            pass
    except Exception as e:
        log.exception("callback error: %s", e)
        try:
            await q.message.reply_text(
                append_log(
                    f"⚠️ خطأ: <code>{html.escape(str(e))[:300]}</code>",
                    user_id,
                ),
                reply_markup=main_menu(user_has_active_sandbox(user_id)),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    user_id = update.effective_user.id
    if data == "menu:home":
        st = sandbox_state.get(user_id)
        st.awaiting = ""
        # Keep the terminal session alive across home navigation —
        # closing it would force a reconnect + cwd reset when the user
        # returns. Session is closed explicitly via term:close, never
        # implicitly here.
        await edit_message_text_safe(
            update.callback_query,
            append_log("📋 القائمة الرئيسية", user_id),
            reply_markup=main_menu(user_has_active_sandbox(user_id)),
        )
