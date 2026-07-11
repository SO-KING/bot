"""Admin panel handler — forced sub, bans, admins, server browser."""
import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import storage
from config import TELEGRAM_BOT_TOKEN
from keyboards import (
    admin_main,
    admin_forced_sub_keyboard,
    admin_cancel_keyboard,
    admin_servers_keyboard,
    admin_user_server_actions,
    main_menu,
)
from ui import append_log
from . import sandbox_state
from .helpers import safe_edit, safe_answer

log = logging.getLogger(__name__)

ADMIN_ID = 7979799419


# -----------------------------------------------------------------------------
# /admin command
# -----------------------------------------------------------------------------

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not storage.is_admin(user_id):
        await update.effective_message.reply_text("🚫 هذا الأمر للأدمن فقط.")
        return
    stats = storage.get_stats()
    text = (
        "🛡️ <b>لوحة تحكم الأدمن</b>\n\n"
        f"👥 المستخدمين: <b>{stats['total_users']}</b>\n"
        f"🖥️ السيرفرات النشطة: <b>{stats['active_sandboxes']}</b>\n"
        f"📦 إجمالي السيرفرات: <b>{stats['total_sandboxes']}</b>\n"
        f"🚫 المحظورين: <b>{stats['total_banned']}</b>\n"
        f"👤 الأدمن: <b>{stats['total_admins']}</b>\n"
        f"📢 قنوات الاشتراك: <b>{stats['forced_channels']}</b>\n"
    )
    await update.effective_message.reply_text(
        append_log(text, user_id),
        reply_markup=admin_main(),
        parse_mode=ParseMode.HTML,
    )


# -----------------------------------------------------------------------------
# Callback router for admin:* callbacks
# -----------------------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query

    if not storage.is_admin(user_id):
        await safe_answer(q, "🚫 غير مسموح", show_alert=True)
        return

    if data == "admin:panel":
        await _show_panel(update, ctx)
    elif data == "admin:stats":
        await _show_panel(update, ctx)
    elif data == "admin:forced_sub":
        await _forced_sub_menu(update, ctx)
    elif data == "admin:add_ch":
        await _ask_channel(update, ctx)
    elif data.startswith("admin:remove_ch:"):
        ch = data.split(":", 2)[2]
        await _remove_channel(update, ctx, ch)
    elif data.startswith("admin:bans"):
        await _bans_menu(update, ctx)
    elif data == "admin:ban_user":
        await _ask_user_id(update, ctx, "ban")
    elif data == "admin:unban_user":
        await _ask_user_id(update, ctx, "unban")
    elif data.startswith("admin:do_ban:"):
        target = int(data.split(":")[2])
        await _do_ban(update, ctx, target)
    elif data.startswith("admin:do_unban:"):
        target = int(data.split(":")[2])
        await _do_unban(update, ctx, target)
    elif data == "admin:admins":
        await _admins_menu(update, ctx)
    elif data == "admin:add_admin":
        await _ask_user_id(update, ctx, "add_admin")
    elif data == "admin:remove_admin":
        await _ask_user_id(update, ctx, "remove_admin")
    elif data.startswith("admin:do_add_admin:"):
        target = int(data.split(":")[2])
        await _do_add_admin(update, ctx, target)
    elif data.startswith("admin:do_remove_admin:"):
        target = int(data.split(":")[2])
        await _do_remove_admin(update, ctx, target)
    elif data.startswith("admin:servers:"):
        page = int(data.split(":")[2])
        await _servers_list(update, ctx, page)
    elif data.startswith("admin:enter_server:"):
        parts = data.split(":")
        uid = int(parts[2])
        sid = parts[3]
        await _enter_server(update, ctx, uid, sid)
    elif data.startswith("admin:status:"):
        parts = data.split(":")
        uid = int(parts[2])
        sid = parts[3]
        await _show_server_status(update, ctx, uid, sid)
    elif data.startswith("admin:term:"):
        parts = data.split(":")
        uid = int(parts[2])
        sid = parts[3]
        await _admin_terminal(update, ctx, uid, sid)
    elif data.startswith("admin:files:"):
        parts = data.split(":")
        uid = int(parts[2])
        sid = parts[3]
        await _admin_files(update, ctx, uid, sid)


# -----------------------------------------------------------------------------
# Text input handler for admin (awaiting mode)
# -----------------------------------------------------------------------------

async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not storage.is_admin(user_id):
        return
    text = (update.effective_message.text or "").strip()
    st = sandbox_state.get(user_id)
    mode = st.awaiting

    if mode == "admin:channel_add":
        st.awaiting = ""
        if not text:
            await _forced_sub_menu(update, ctx)
            return
        username = text.strip().lstrip("@").replace("https://t.me/", "").split("/")[0]
        storage.add_forced_channel(username)
        await update.effective_message.reply_text(
            f"✅ تمت إضافة القناة @{username} للاشتراك الإجباري.",
            parse_mode=ParseMode.HTML,
        )
        await _forced_sub_menu(update, ctx)

    elif mode == "admin:ban_input":
        st.awaiting = ""
        if not text or not text.isdigit():
            await _show_panel(update, ctx)
            return
        target = int(text)
        if storage.is_banned(target):
            storage.unban_user(target)
            act = "فك الحظر عن"
        else:
            storage.ban_user(target)
            act = "تم حظر"
        await update.effective_message.reply_text(
            f"✅ {act} {target}.",
            parse_mode=ParseMode.HTML,
        )
        await _bans_menu(update, ctx)

    elif mode == "admin:unban_input":
        st.awaiting = ""
        if not text or not text.isdigit():
            await _show_panel(update, ctx)
            return
        target = int(text)
        if storage.is_banned(target):
            storage.unban_user(target)
            await update.effective_message.reply_text(
                f"✅ تم فك الحظر عن {target}.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                f"⚠️ المستخدم {target} ليس محظوراً.",
                parse_mode=ParseMode.HTML,
            )
        await _bans_menu(update, ctx)

    elif mode == "admin:add_admin_input":
        st.awaiting = ""
        if not text or not text.isdigit():
            await _show_panel(update, ctx)
            return
        target = int(text)
        if storage.is_admin(target):
            storage.remove_admin(target)
            act = "تمت إزالة الأدمن"
        else:
            storage.add_admin(target)
            act = "تمت إضافة الأدمن"
        await update.effective_message.reply_text(
            f"✅ {act} {target}.",
            parse_mode=ParseMode.HTML,
        )
        await _admins_menu(update, ctx)

    elif mode == "admin:remove_admin_input":
        st.awaiting = ""
        if not text or not text.isdigit():
            await _show_panel(update, ctx)
            return
        target = int(text)
        if storage.is_admin(target) and target != ADMIN_ID:
            storage.remove_admin(target)
            await update.effective_message.reply_text(
                f"✅ تمت إزالة الأدمن {target}.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text(
                "⚠️ لا يمكن إزالة هذا الأدمن.",
                parse_mode=ParseMode.HTML,
            )
        await _admins_menu(update, ctx)


# -----------------------------------------------------------------------------
# Panel display helpers
# -----------------------------------------------------------------------------

async def _show_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    stats = storage.get_stats()
    text = (
        "🛡️ <b>لوحة تحكم الأدمن</b>\n\n"
        f"👥 المستخدمين: <b>{stats['total_users']}</b>\n"
        f"🖥️ السيرفرات النشطة: <b>{stats['active_sandboxes']}</b>\n"
        f"📦 إجمالي السيرفرات: <b>{stats['total_sandboxes']}</b>\n"
        f"🚫 المحظورين: <b>{stats['total_banned']}</b>\n"
        f"👤 الأدمن: <b>{stats['total_admins']}</b>\n"
        f"📢 قنوات الاشتراك: <b>{stats['forced_channels']}</b>\n"
    )
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=admin_main(),
    )


async def _forced_sub_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    channels = storage.get_forced_channels()
    if channels:
        text = "📢 <b>القنوات المفروضة للاشتراك</b>\n\n" + "\n".join(f"• @{c}" for c in channels)
    else:
        text = "📢 <b>القنوات المفروضة</b>\n\n<i>لا توجد قنوات. اضغط '➕ إضافة قناة'.</i>"
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=admin_forced_sub_keyboard(channels),
    )


async def _ask_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    sandbox_state.get(user_id).awaiting = "admin:channel_add"
    await safe_edit(
        q,
        "📢 أرسل يوزر القناة بدون @ (مثال: <code>my_channel</code>).\n\nاضغط /cancel للإلغاء.",
        reply_markup=admin_cancel_keyboard("admin:forced_sub"),
    )


async def _remove_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE, ch: str) -> None:
    storage.remove_forced_channel(ch)
    await _forced_sub_menu(update, ctx)


# -----------------------------------------------------------------------------
# Bans
# -----------------------------------------------------------------------------

async def _bans_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    banned = storage.get_banned_users()
    if banned:
        text = "🚫 <b>المستخدمين المحظورين</b>\n\n" + "\n".join(f"• <code>{b}</code>" for b in banned)
    else:
        text = "🚫 <b>إدارة الحظر</b>\n\n<i>لا يوجد محظورين.</i>"
    kb = [
        [InlineKeyboardButton("➕ حظر مستخدم", callback_data="admin:ban_user")],
        [InlineKeyboardButton("🔓 فك حظر", callback_data="admin:unban_user")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="admin:panel")],
    ]
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _ask_user_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    mode_map = {
        "ban": "admin:ban_input",
        "unban": "admin:unban_input",
        "add_admin": "admin:add_admin_input",
        "remove_admin": "admin:remove_admin_input",
    }
    labels = {
        "ban": "حظر — أرسل ID المستخدم (ولو محظور سيفك الحظر)",
        "unban": "فك حظر — أرسل ID المستخدم",
        "add_admin": "إضافة/إزالة أدمن — أرسل ID (ولو أدمن سيُزال)",
        "remove_admin": "إزالة أدمن — أرسل ID المستخدم",
    }
    back_map = {
        "ban": "admin:bans",
        "unban": "admin:bans",
        "add_admin": "admin:admins",
        "remove_admin": "admin:admins",
    }
    sandbox_state.get(user_id).awaiting = mode_map[action]
    await safe_edit(
        q,
        f"📝 {labels[action]}\n\nأرسل ID رقماً فقط أو اضغط /cancel.",
        reply_markup=admin_cancel_keyboard(back_map[action]),
    )


async def _do_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    storage.ban_user(target)
    await safe_answer(update.callback_query, f"✅ تم حظر {target}", show_alert=True)
    await _bans_menu(update, ctx)


async def _do_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    storage.unban_user(target)
    await safe_answer(update.callback_query, f"✅ فك حظر {target}", show_alert=True)
    await _bans_menu(update, ctx)


# -----------------------------------------------------------------------------
# Admins management
# -----------------------------------------------------------------------------

async def _admins_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    admins = storage.get_all_admins()
    if admins:
        text = "👤 <b>الأدمن الحاليين</b>\n\n" + "\n".join(f"• <code>{a}</code>" for a in admins)
    else:
        text = "👤 <b>إدارة الأدمن</b>\n\n<i>لا يوجد أدمن.</i>"
    kb = [
        [InlineKeyboardButton("➕ إضافة أدمن", callback_data="admin:add_admin")],
        [InlineKeyboardButton("➖ إزالة أدمن", callback_data="admin:remove_admin")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="admin:panel")],
    ]
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _do_add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    storage.add_admin(target)
    await safe_answer(update.callback_query, f"✅ أضيف الأدمن {target}", show_alert=True)
    await _admins_menu(update, ctx)


async def _do_remove_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, target: int) -> None:
    storage.remove_admin(target)
    await safe_answer(update.callback_query, f"✅ أُزيل الأدمن {target}", show_alert=True)
    await _admins_menu(update, ctx)


# -----------------------------------------------------------------------------
# Servers browser
# -----------------------------------------------------------------------------

async def _servers_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    admin_id = user_id
    servers = storage.get_all_users_with_sandboxes()

    # Split admin's own servers from other users' servers.
    admin_servers = [s for s in servers if s["user_id"] == admin_id]
    user_servers = [s for s in servers if s["user_id"] != admin_id]

    if not servers:
        await safe_edit(
            q,
            append_log("🖥️ <b>سيرفرات المستخدمين</b>\n\n<i>لا توجد سيرفرات حالياً.</i>", user_id),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 رجوع", callback_data="admin:panel")],
            ]),
        )
        return

    text = (
        f"🖥️ <b>سيرفرات المستخدمين</b>\n\n"
    )
    if admin_servers:
        text += (
            "<b>👤 حساباتي (الأدمن):</b>\n"
            + "\n".join(
                "• <code>" + (s["sandbox_id"] or "—") + "</code>"
                + " • <b>" + (s.get("status") or "—") + "</b>"
                for s in admin_servers
            )
            + "\n\n"
        )
    text += (
        f"<b>👥 مستخدمين آخرين ({len(user_servers)}):</b>\n"
        f"الصفحة {page + 1} — اضغط على سيرفر للدخول إليه."
    )
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=admin_servers_keyboard(user_servers + admin_servers, page),
    )


async def _enter_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        owner_id: int, sandbox_id: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    owner_state = storage.get_state(owner_id) or {}
    status = owner_state.get("status") or "—"
    host = owner_state.get("public_host") or "—"
    exp = owner_state.get("sandbox_exp") or "—"

    text = (
        f"🖥️ <b>سيرفر المستخدم {owner_id}</b>\n\n"
        f"• المعرّف: <code>{html.escape(sandbox_id)}</code>\n"
        f"• الحالة: <b>{html.escape(status)}</b>\n"
        f"• Host: <code>{html.escape(host)}</code>\n"
        f"• ينتهي: <code>{html.escape(str(exp))}</code>\n"
    )
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=admin_user_server_actions(owner_id, sandbox_id),
    )


async def _show_server_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                              owner_id: int, sandbox_id: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    owner_state = storage.get_state(owner_id) or {}
    status = owner_state.get("status") or "—"
    host = owner_state.get("public_host") or "—"
    exp = owner_state.get("sandbox_exp") or "—"

    text = (
        "📊 <b>حالة سيرفر المستخدم</b>\n\n"
        f"👤 المستخدم: <code>{owner_id}</code>\n"
        f"• المعرّف: <code>{html.escape(sandbox_id)}</code>\n"
        f"• الحالة: <b>{html.escape(status)}</b>\n"
        f"• Host: <code>{html.escape(host)}</code>\n"
        f"• ينتهي: <code>{html.escape(str(exp))}</code>\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع للسيرفر", callback_data=f"admin:enter_server:{owner_id}:{sandbox_id}")],
        [InlineKeyboardButton("🏠 لوحة الأدمن", callback_data="admin:panel")],
    ])
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=kb,
    )


async def _admin_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                          owner_id: int, sandbox_id: str) -> None:
    """Switch the admin to the target user's context and open terminal."""
    user_id = update.effective_user.id
    q = update.callback_query
    owner_state = storage.get_state(owner_id) or {}
    api_key = storage.get_api_key(owner_id)
    public_host = owner_state.get("public_host")
    jwt_val = owner_state.get("sandbox_jwt")
    if not api_key or not public_host or not jwt_val:
        await safe_edit(
            q,
            append_log(
                "⚠️ بيانات السيرفر غير مكتملة (JWT مفقود). لا يمكن فتح الترمنال.",
                user_id,
            ),
            reply_markup=admin_user_server_actions(owner_id, sandbox_id),
        )
        return
    from hopx_client import open_terminal as hopen_term
    from keyboards import terminal_keyboard

    st = sandbox_state.get(user_id)
    try:
        sess = await hopen_term(public_host, jwt_val)
    except Exception as e:
        await q.message.reply_text(
            f"❌ فشل فتح الترمنال: {e}",
            parse_mode=ParseMode.HTML,
        )
        return

    st.sandbox_id = sandbox_id
    st.public_host = public_host
    st.jwt = jwt_val
    st.api_key = api_key
    st.terminal_session = sess
    st.terminal_stdin = False
    st.awaiting = "terminal"
    storage.set_terminal_active(user_id, True)
    await safe_edit(
        q,
        append_log(
            "🖥️ <b>الترمنال مفتوح (سيرفر مستخدم آخر)</b>\nاكتب أوامرك هنا. استخدم الأزرار أدناه للتحكم.",
            user_id,
        ),
        reply_markup=terminal_keyboard(),
    )


async def _admin_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                       owner_id: int, sandbox_id: str) -> None:
    """Switch admin to target user and list files."""
    user_id = update.effective_user.id
    q = update.callback_query
    owner_state = storage.get_state(owner_id) or {}
    api_key = storage.get_api_key(owner_id)
    public_host = owner_state.get("public_host")
    jwt_val = owner_state.get("sandbox_jwt")
    if not api_key or not public_host or not jwt_val:
        await safe_edit(
            q,
            append_log("⚠️ بيانات غير مكتملة.", user_id),
            reply_markup=admin_user_server_actions(owner_id, sandbox_id),
        )
        return
    st = sandbox_state.get(user_id)
    st.sandbox_id = sandbox_id
    st.public_host = public_host
    st.jwt = jwt_val
    st.api_key = api_key
    from keyboards import files_keyboard
    from hopx_client import list_files

    try:
        items = await list_files(api_key, sandbox_id, "/workspace", user_id=user_id)
        msg = f"📁 <b>/workspace</b> — {len(items)} عنصر (المستخدم {owner_id})"
        await safe_edit(
            q,
            append_log(msg, user_id),
            reply_markup=files_keyboard(ctx, "/workspace", items),
        )
    except Exception as e:
        await safe_edit(
            q,
            append_log(f"❌ فشل: {e}", user_id),
            reply_markup=admin_user_server_actions(owner_id, sandbox_id),
        )