"""Sandbox lifecycle handlers.

Key entry points:
  - `receive_api_key(update, ctx)` — first-time /start flow storing the API key.
  - `on_callback(update, ctx, data)` — router for `sb:*` inline buttons.
  - `_ensure_creds(user_id)` — fetches a fresh JWT via connect-sandbox and
            persists it for the terminal/files/processes handlers.

All `q.edit_message_text` calls go through `helpers.safe_edit` so a
no-op edit never crashes with `BadRequest: Message is not modified`.
"""
import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import storage
from config import HOPX_DEFAULT_TEMPLATE, HOPX_DEFAULT_TIMEOUT, HOPX_USE_TIMEOUT
from hopx_client import (
    validate_api_key, create_sandbox, connect_sandbox,
    sandbox_lifecycle, extend_timeout,
    get_sandbox_info, list_plat_sandboxes,
    SandboxInfoDto, HopXError, _info_from_sb,
)
from . import sandbox_state as ss_lock
from keyboards import (
    main_menu, sandbox_status_keyboard, confirm_kill_sandbox,
    confirm_create_sandbox, home_button,
)
from ui import append_log
from . import sandbox_state
from .helpers import safe_edit, safe_answer, html_escape

log = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# API key capture
# -----------------------------------------------------------------------------

async def receive_api_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.effective_message.text.strip()
    st = sandbox_state.get(user_id)
    changing = st.awaiting == "api_key_change"
    st.awaiting = ""

    from keyboards import api_key_prompt_keyboard

    if not text.startswith("hopx_live_") or "." not in text:
        prompt = ("🔑 أرسل المفتاح الجديد (يبدأ بـ <code>hopx_live_</code>)."
                  if changing else
                  "❌ المفتاح غير صحيح. "
                  "الصيغة المتوقعة: "
                  "<code>hopx_live_xxxx.yyyy</code>\n"
                  "أرسل المفتاح أو اضغط ❌ إلغاء.")
        await update.effective_message.reply_text(
            prompt,
            reply_markup=api_key_prompt_keyboard(),
            parse_mode="HTML",
        )
        st.awaiting = "api_key_change" if changing else "api_key"
        return

    msg = await update.effective_message.reply_text("⏳ يتم التحقق من المفتاح…")
    try:
        ok = await validate_api_key(text)
    except Exception as e:
        err = html_escape(str(e))
        await msg.edit_text(
            f"⚠️ فشل التحقق: <code>{html_escape(str(e))}</code>\n"
            "حاول مجددًا أو اضغط ❌ إلغاء.",
            reply_markup=api_key_prompt_keyboard(),
            parse_mode="HTML",
        )
        st.awaiting = "api_key_change" if changing else "api_key"
        return

    if not ok:
        await msg.edit_text(
            "❌ المفتاح غير صالح أو غير مفعّل. "
            "حاول مجددًا أو اضغط ❌ إلغاء.",
            reply_markup=api_key_prompt_keyboard(),
            parse_mode="HTML",
        )
        st.awaiting = "api_key_change" if changing else "api_key"
        return

    if changing:
        storage.change_api_key(user_id, text)
        st.api_key = text
        await msg.edit_text(
            append_log(
                "✅ <b>تم تغيير المفتاح بنجاح</b>\n\n"
                "• المفتاح الحالي (المشفّر) تم استبداله.\n"
                "• بيانات السيرفر الناشط وسجل "
                "‘سيرفراتي السابقة’ تم مسحها "
                "من البوت.\n"
                "ابدأ بإنشاء سيرفر جديد "
                "باستخدام المفتاح الجديد.",
                user_id,
            ),
            reply_markup=main_menu(False),
            parse_mode="HTML",
        )
        return

    storage.set_api_key(user_id, text)
    st.api_key = text

    existing_ids = storage.list_sandboxes(user_id, limit=10)
    if existing_ids:
        await msg.edit_text(
            append_log(
                "✅ تم تسجيل مفتاحك. وجدت "
                f"{len(existing_ids)} سيرفر سابق لك. "
                "اضغط 📊 الحالة للدخول.",
                user_id,
            ),
            reply_markup=main_menu(False),
            parse_mode="HTML",
        )
    else:
        await msg.edit_text(
            append_log(
                "✅ تم تسجيل مفتاحك "
                "بنجاح. اضغط 🚀 لإنشاء سيرفر.",
                user_id,
            ),
            reply_markup=main_menu(False),
            parse_mode="HTML",
        )


# -----------------------------------------------------------------------------
# Callback router
# -----------------------------------------------------------------------------



async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    q = update.callback_query
    user_id = update.effective_user.id

    if data == "sb:create_ask":
        await _create_ask(update, ctx)
    elif data == "sb:create":
        await _create(update, ctx)
    elif data == "sb:status":
        await _status(update, ctx)
    elif data == "sb:kill_ask":
        await safe_edit(
            q,
            append_log("⚠️ هل أنت متأكد من إنهاء السيرفر؟ سيُحذف بكل ما عليه.", user_id),
            reply_markup=confirm_kill_sandbox(),
        )
    elif data == "sb:kill":
        await _kill(update, ctx)
    elif data == "sb:pause":
        await _lifecycle(update, ctx, "pause")
    elif data == "sb:resume":
        await _lifecycle(update, ctx, "resume")
    elif data == "sb:change_api_ask":
        await _change_api_ask(update, ctx)
    elif data == "sb:change_api_go":
        await _change_api_go(update, ctx)
    elif data == "sb:list_mine":
        await _list_mine(update, ctx)
    elif data.startswith("sb:attach:"):
        sid = data.split(":", 2)[2]
        await _attach(update, ctx, sid)
    elif data.startswith("sb:extend:"):
        secs = int(data.split(":")[2])
        await _extend(update, ctx, secs)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

async def _ensure_creds(user_id: int):
    """Returns a 6-tuple (state, api_key, sandbox_id, public_host, jwt, info)
    or None if the user has no sandbox or no API key.

    Uses stored JWT/public_host from DB whenever possible; only calls
    `connect_sandbox` (which does a full Control Plane refresh) when
    the stored data is missing or stale.  Avoiding a refresh on every
    call prevents INVALID_TOKEN errors from the VM agent.
    """
    st = sandbox_state.get(user_id)
    api_key = st.api_key or storage.get_api_key(user_id)
    if not api_key:
        return None
    state = storage.get_state(user_id)
    if not state or not state.get("sandbox_id"):
        return None
    sandbox_id = state["sandbox_id"]

    # Prefer stored credentials — no API call needed.
    db_jwt = state.get("sandbox_jwt")
    db_host = state.get("public_host")
    if db_jwt and db_host:
        # Re-seed the SDK's in-memory token cache so future VM ops
        # (files, processes) find the token immediately.
        try:
            from hopx_ai._token_cache import _token_cache, TokenData
            from datetime import datetime, timezone, timedelta
            if sandbox_id not in _token_cache:
                _token_cache[sandbox_id] = TokenData(
                    token=db_jwt,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                )
        except Exception:
            pass
        st.sandbox_id = sandbox_id
        st.public_host = db_host
        st.jwt = db_jwt
        return st, api_key, sandbox_id, db_host, db_jwt, None

    # No stored JWT — do a one-time connect to fetch fresh credentials.
    try:
        info = await connect_sandbox(api_key, sandbox_id, user_id=user_id)
    except Exception as e:
        log.warning("connect failed for %s: %s; trying get_info", sandbox_id, e)
        try:
            info = await get_sandbox_info(api_key, sandbox_id, user_id=user_id)
        except Exception as e2:
            log.exception("fallback get_info failed: %s", e2)
            return None

    try:
        storage.set_sandbox(
            user_id, info.sandbox_id, info.public_host, info.jwt,
            info.expires_at, info.status,
        )
    except Exception as e:
        log.warning("set_sandbox persist failed: %s", e)

    try:
        st.sandbox_id = info.sandbox_id
        st.public_host = info.public_host
        st.jwt = info.jwt
    except Exception as e:
        log.warning("runtime state update failed: %s", e)

    return st, api_key, sandbox_id, info.public_host, info.jwt, info


# -----------------------------------------------------------------------------
# Actions
# -----------------------------------------------------------------------------

# Two-step confirm for creating new sandbox
async def _create_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation before creating a new sandbox.

    Idempotent: if the user already has a running sandbox attached in
    the local DB we skip the "are you sure?" flow entirely and route
    them straight to the live sandbox status. Repeated taps on
    "🚀 create" then become a no-op rather than a duplicate-spawn
    tool. If the cached sandbox turns out to be dead on HopX we clear
    it and fall through to the standard confirm screen.
    """
    user_id = update.effective_user.id
    q = update.callback_query
    api_key = sandbox_state.get(user_id).api_key or storage.get_api_key(user_id)
    if not api_key:
        sandbox_state.get(user_id).awaiting = "api_key"
        await safe_edit(
            q,
            "⚠️ لا يوجد مفتاح مسجّل. أرسل مفتاحك الآن (يبدأ بـ <code>hopx_live_</code>).",
        )
        return

    existing_id = storage.get_active_sandbox_id(user_id)
    if existing_id:
        try:
            from keyboards import main_menu
            from hopx_client import connect_sandbox
            async with sandbox_state.get(user_id).lock:
                info = await connect_sandbox(api_key, existing_id, user_id=user_id)
            if info and info.status in ("running", "paused"):
                storage.set_sandbox(
                    user_id, info.sandbox_id, info.public_host,
                    info.jwt, info.expires_at, info.status,
                )
                st = sandbox_state.get(user_id)
                st.sandbox_id = info.sandbox_id
                st.public_host = info.public_host
                st.jwt = info.jwt
                await safe_edit(
                    q,
                    append_log(
                        "🟢 <b>لديك سيرفر نشط بالفعل</b>\n\n"
                        f"المعرّف: <code>{info.sandbox_id}</code>\n"
                        f"الحالة: <b>{info.status}</b>\n\n"
                        "لإنشاء سيرفر جديد، أنهِ هذا أولاً من '🗑️ إنهاء السيرفر'.",
                        user_id,
                    ),
                    reply_markup=main_menu(True),
                )
                return
            # Locally tracked but dead on HopX -> clear and proceed
            storage.clear_sandbox(user_id)
        except Exception as e:
            log.warning("reattach in create_ask failed: %s", e)
            storage.clear_sandbox(user_id)

    await safe_edit(
        q,
        append_log(
            "🚀 <b>هل أنت متأكد من إنشاء سيرفر جديد؟</b>\n\n"
            "• السيرفر القديم (إن كان متوقفا) سيُستبدل.\n"
            "• ضغط زر '✅ نعم' عدة مرات لن ينشئ سيرفرات مكررة.\n\n"
            "اضغط ✅ نعم أو ❌ إلغاء.",
            user_id,
        ),
        reply_markup=confirm_create_sandbox(),
    )



# Confirm before changing API key
async def _change_api_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    if not storage.has_api_key(user_id):
        sandbox_state.get(user_id).awaiting = "api_key"
        await safe_edit(q, "⚠️ لا يوجد مفتاح مسجّل. أرسل مفتاحك الآن (يبدأ بـ <code>hopx_live_</code>).")
        return
    from keyboards import confirm_change_api
    await safe_edit(
        q,
        append_log(
            "🔑 <b>هل أنت متأكد من تغيير مفتاح API؟</b>\n\n"
            "سيُمسح المفتاح الحالي وبيانات السيرفر النشط وسجل 'سيرفراتي السابقة'.\n\n"
            "اضغط ✅ للتأكيد أو ❌ إلغاء للتراجع.",
            user_id,
        ),
        reply_markup=confirm_change_api(),
    )


async def _change_api_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.start import _ask_api_key
    await _ask_api_key(update, ctx, changing=True)


async def _create(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    st = sandbox_state.get(user_id)
    api_key = st.api_key or storage.get_api_key(user_id)
    if not api_key:
        st.awaiting = "api_key"
        await safe_edit(
            q,
            "⚠️ لا يوجد مفتاح مسجّل. أرسل مفتاحك الآن "
            "(يبدأ بـ <code>hopx_live_</code>).",
        )
        return

    # Anti-duplicate-create. Within the TTL window (6s), repeated taps on
    # "إنشاء سيرفر جديد" become no-ops so a double-click can't spawn
    # two sandboxes on HopX.
    if not ss_lock.acquire_create_lock(user_id):
        await safe_answer(
            q, "⏳ إنشاء جارٍ، انتظر قليلاً…", show_alert=False,
        )
        return

    # If user already has a sandbox, reattach instead of creating new.
    existing_id = storage.get_active_sandbox_id(user_id)
    if existing_id:
        try:
            info = await connect_sandbox(api_key, existing_id, user_id=user_id)
        except Exception:
            storage.clear_sandbox(user_id)
            info = None
        if info and info.status in ("running", "paused"):
            storage.set_sandbox(user_id, info.sandbox_id, info.public_host,
                                info.jwt, info.expires_at, info.status)
            st.sandbox_id = info.sandbox_id
            st.public_host = info.public_host
            st.jwt = info.jwt
            ss_lock.clear_create_lock(user_id)
            await _status(update, ctx)
            return

    await safe_edit(
        q,
        append_log("⏳ جاري إنشاء سيرفر جديد على HopX…", user_id),
    )
    ttl = HOPX_DEFAULT_TIMEOUT if HOPX_USE_TIMEOUT else None
    try:
        info = await create_sandbox(api_key, HOPX_DEFAULT_TEMPLATE, ttl, user_id)
    except Exception as e:
        ss_lock.clear_create_lock(user_id)
        log.exception("create_sandbox failed: %s", e)
        err = html_escape(str(e))
        await safe_edit(
            q,
            append_log(
                "❌ فشل الإنشاء: "
                + "<code>" + err + "</code>"
                + "\n\n"
                + "جرّب مرة أخرى أو تحقق من اتصالك.",
                user_id,
            ),
            reply_markup=main_menu(False),
        )
        return

    storage.set_sandbox(user_id, info.sandbox_id, info.public_host, info.jwt,
                        info.expires_at, info.status)
    st.sandbox_id = info.sandbox_id
    st.public_host = info.public_host
    st.jwt = info.jwt
    # Drop any leftover terminal session bound to the previous sandbox so
    # the next `term:open` performs a clean handshake against the new VM.
    st.terminal_session = None
    ss_lock.clear_create_lock(user_id)

    timeout_txt = (
        f"<code>{ttl // 60} دقيقة</code>" if ttl
        else "<b>دائم</b> (لن يُدمَّر تلقائياً)"
    )
    text = (
        "✅ <b>تم إنشاء السيرفر بنجاح</b>\n\n"
        f"• المعرّف: <code>{info.sandbox_id}</code>\n"
        f"• القالب: <code>{info.template_name or HOPX_DEFAULT_TEMPLATE}</code>\n"
        f"• الحالة: <b>{info.status}</b>\n"
        f"• المنطقة: <code>{info.region or 'default'}</code>\n"
        f"• ينتهي خلال: {timeout_txt}\n"
        f"• Host: <code>{info.public_host}</code>\n"
    )
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=main_menu(True),
    )


async def _status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    creds = await _ensure_creds(user_id)
    if not creds:
        mine = storage.list_sandboxes(user_id, limit=6)
        if mine:
            await _list_mine(update, ctx)
            return
        await safe_edit(
            q,
            append_log("⚠️ لا يوجد سيرفر نشط. اضغط 🚀 لإنشاء سيرفر جديد.", user_id),
            reply_markup=main_menu(False),
        )
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    if info is None:
        # Fast path: creds came from DB-only (no live `connect` call).
        # Render status from what we know — without this branch the
        # .sandbox_id access on `None` blew up the handler.
        db_state = storage.get_state(user_id) or {}
        db_status = db_state.get("status") or "unknown"
        db_exp = db_state.get("sandbox_exp") or "— (دائم)"
        text = (
            "📊 <b>حالة السيرفر</b>\n\n"
            f"• المعرّف: <code>{html.escape(sandbox_id)}</code>\n"
            f"• الحالة: <b>{html.escape(db_status)}</b>\n"
            f"• ينتهي في: <code>{html.escape(str(db_exp))}</code>\n"
            f"• Host: <code>{html.escape(public_host)}</code>\n"
        )
    else:
        text = (
            "📊 <b>حالة السيرفر</b>\n\n"
            f"• المعرّف: <code>{html.escape(info.sandbox_id)}</code>\n"
            f"• الحالة: <b>{html.escape(info.status)}</b>\n"
            f"• ينتهي في: <code>{html.escape(str(info.expires_at or '— (دائم)'))}</code>\n"
            f"• Host: <code>{html.escape(info.public_host)}</code>\n"
            f"• المنطقة: <code>{html.escape(info.region or '—')}</code>\n"
        )
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=sandbox_status_keyboard(),
    )



async def _kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    api_key = sandbox_state.get(user_id).api_key or storage.get_api_key(user_id)
    state = storage.get_state(user_id) or {}
    sandbox_id = state.get("sandbox_id")
    if not api_key or not sandbox_id:
        await safe_edit(
            q,
            append_log("⚠️ لا يوجد سيرفر لإنهائه.", user_id),
            reply_markup=main_menu(False),
        )
        return

    await safe_edit(
        q,
        append_log("⏳ جارٍ إنهاء السيرفر…", user_id),
    )
    try:
        await sandbox_lifecycle(api_key, sandbox_id, "kill", user_id=user_id)
    except Exception as e:
        log.warning("kill failed: %s", e)

    storage.clear_sandbox(user_id)
    try:
        await sandbox_state.drop(user_id)
    except Exception:
        pass
    sandbox_state.get(user_id)
    await safe_edit(
        q,
        append_log("🗑️ تم إنهاء السيرفر وكافة الموارد.", user_id),
        reply_markup=main_menu(False),
    )


async def _lifecycle(update: Update, ctx: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    creds = await _ensure_creds(user_id)
    if not creds:
        await safe_edit(
            q,
            append_log("⚠️ لا يوجد سيرفر.", user_id),
            reply_markup=main_menu(False),
        )
        return
    st, api_key, sandbox_id, *_ = creds
    await safe_edit(
        q,
        append_log(f"⏳ جارٍ {action}…", user_id),
    )
    try:
        info = await sandbox_lifecycle(api_key, sandbox_id, action, user_id=user_id)
        storage.set_sandbox(user_id, info.sandbox_id, info.public_host, info.jwt,
                            info.expires_at, info.status)
        st.public_host = info.public_host
        st.jwt = info.jwt
    except Exception as e:
        await safe_edit(
            q,
            append_log(f"❌ فشل: <code>{html_escape(str(e))}</code>", user_id),
        )
        return
    await _status(update, ctx)


async def _extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE, seconds: int) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    creds = await _ensure_creds(user_id)
    if not creds:
        await safe_edit(
            q,
            append_log("⚠️ لا يوجد سيرفر.", user_id),
            reply_markup=main_menu(False),
        )
        return
    st, api_key, sandbox_id, *_ = creds
    try:
        await extend_timeout(api_key, sandbox_id, seconds)
    except Exception as e:
        await q.message.reply_text(
            append_log(f"❌ فشل التمديد: <code>{html_escape(str(e))}</code>", user_id),
            parse_mode="HTML",
        )
    await _status(update, ctx)


async def _list_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    api_key = sandbox_state.get(user_id).api_key or storage.get_api_key(user_id)
    if not api_key:
        await safe_edit(
            q,
            "⚠️ لا يوجد مفتاح مسجّل. أرسل مفتاحك أولاً.",
        )
        return

    text = "🗂️ <b>سيرفراتك المعروفة</b>\n\nاختر سيرفر للدخول إليه (اتصال جديد + استعادة JWT):\n"

    local = storage.list_sandboxes(user_id, limit=30)
    live = []
    try:
        live = await list_plat_sandboxes(api_key, limit=50)
    except Exception as e:
        log.warning("list_plat_sandboxes failed: %s", e)

    merged: dict[str, dict] = {}
    for s in local:
        merged[s["sandbox_id"]] = {
            "sandbox_id": s["sandbox_id"],
            "status": s.get("status") or "?",
            "template_name": s.get("template_name"),
            "region": s.get("region"),
        }
    for s in live:
        merged[s.sandbox_id] = {
            "sandbox_id": s.sandbox_id,
            "status": s.status,
            "template_name": s.template_name,
            "region": s.region,
        }

    if not merged:
        await safe_edit(
            q,
            append_log(
                text + "\n<i>لا يوجد أي سيرفر سابق — اضغط 🚀 للإنشاء</i>",
                user_id,
            ),
            reply_markup=main_menu(False),
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for sid, info in merged.items():
        tag = info["status"] or "?"
        label = f"• {sid[:18]}"
        if info.get("region"):
            label += f" • {info['region']}"
        label += f" • {tag}"
        rows.append([
            InlineKeyboardButton(label, callback_data=f"sb:attach:{sid}")
        ])
    rows.append([InlineKeyboardButton("🚀 سيرفر جديد", callback_data="sb:create_ask")])
    rows.append([home_button()])
    await safe_edit(
        q,
        append_log(text, user_id),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _attach(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sandbox_id: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    api_key = sandbox_state.get(user_id).api_key or storage.get_api_key(user_id)
    if not api_key:
        await safe_edit(q, "⚠️ لا يوجد مفتاح مسجّل.")
        return
    await safe_edit(
        q,
        append_log(f"⏳ الاتصال بالسيرفر <code>{sandbox_id}</code>…", user_id),
    )
    try:
        info = await connect_sandbox(api_key, sandbox_id, user_id=user_id)
    except Exception as e:
        await safe_edit(
            q,
            append_log(
                f"❌ فشل الاتصال: <code>{html_escape(str(e))}</code>\n"
                "السيرفر قد يكون محذوفاً من HopX. اختر غيره أو أنشئ جديداً.",
                user_id,
            ),
            reply_markup=main_menu(False),
        )
        return
    storage.set_sandbox(user_id, info.sandbox_id, info.public_host, info.jwt,
                        info.expires_at, info.status)
    st = sandbox_state.get(user_id)
    st.sandbox_id = info.sandbox_id
    st.public_host = info.public_host
    st.jwt = info.jwt
    await _status(update, ctx)

