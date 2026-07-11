"""Handlers for /start, /help, /cancel, /reset, /change_api."""
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import storage
from keyboards import main_menu, api_key_prompt_keyboard, forced_sub_keyboard
from ui import append_log
from . import sandbox_state
from handlers.helpers import safe_edit


WELCOME = (
    "👋 <b>أهلاً بك في HopX Bot</b>\n\n"
    "هذا البوت يشنئ لك سيرفر (sandbox) سحابي على <a href=\"https://hopx.ai\">HopX</a> "
    "باستخدام مفتاح الـ API الخاص بك، ويمنحك تحكماً كاملاً عبر تيليجرام:\n\n"
    "• ترمنال تفاعلي حقيقي مثل SSH\n"
    "• إدارة ملفات (رفع/تحميل/حذف/إنشاء مجلدات)\n"
    "• إدارة العمليات (عرض/إيقاف)\n"
    "• متابعة حالة السيرفر وتمديد المهلة\n\n"
    "للبدء، أرسل لي <b>مفتاح HopX API</b> الخاص بك (يبدأ بـ <code>hopx_live_</code>).\n"
    "يُمكنك إنشاؤه من <a href=\"https://console.hopx.dev\">console.hopx.dev</a>.\n\n"
    "🔐 مفتاحك يُخزّن مشفّراً (Fernet) ولن يعرض لأحد."
)


async def _check_subscription_if_needed(user_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns False if user is not an admin and not subscribed to all forced channels."""
    if storage.is_admin(user_id):
        return True
    channels = storage.get_forced_channels()
    if not channels:
        return True
    try:
        for ch in channels:
            member = await ctx.bot.get_chat_member(chat_id=f"@{ch}", user_id=user_id)
            if member.status in ("left", "kicked", "restricted"):
                return False
        return True
    except Exception:
        return True


async def _send_sub_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    channels = storage.get_forced_channels()
    text = (
        "⚠️ <b>الاشتراك الإجباري</b>\n\n"
        "يجب عليك الاشتراك في القنوات التالية لاستخدام البوت:\n\n"
        + "\n".join(f"• @{ch}" for ch in channels)
        + "\n\nبعد الاشتراك اضغط '✅ تحقق من الاشتراك'."
    )
    await update.effective_message.reply_text(
        text,
        reply_markup=forced_sub_keyboard(channels),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _has_sb(user_id: int) -> bool:
    return bool(
        storage.get_state(user_id) and storage.get_state(user_id).get("sandbox_id")
    )


async def _ask_api_key(update, ctx, *, changing: bool) -> None:
    """Put the user into 'awaiting=api_key[_change]' mode.

    If `changing` is True the next received key REPLACES the existing
    one (used by /change_api and the 'change key' button). Otherwise the
    flow is first-time setup.

    Works for both Message-triggered (start, reset) and
    CallbackQuery-triggered (change key button) updates because both
    expose `effective_message`/`effective_user`.
    """
    user_id = update.effective_user.id
    sandbox_state.get(user_id).awaiting = (
        "api_key_change" if changing else "api_key"
    )

    if changing:
        text = (
            "🔑 <b>تغيير مفتاح HopX API</b>\n\n"
            "أرسل المفتاح الجديد (يبدأ بـ <code>hopx_live_</code>).\n\n"
            "سيُحذف:\n"
            "• المفتاح الحالي (المشفّر) في هذا البوت\n"
            "• بيانات السيرفر النشط المرتبطة به\n"
            "• سجل 'سيرفراتي السابقة' (السيرفرات القديمة لن يُسمح "
            "للمفتاح الجديد بالوصول إليها على منصة HopX)\n\n"
            "لو غلطت اضغط '❌ إلغاء' للرجوع دون تعديل."
        )
    else:
        text = WELCOME

    if update.callback_query:
        try:
            await safe_edit(
                update.callback_query, text, api_key_prompt_keyboard()
            )
            return
        except Exception:
            pass
    await update.effective_message.reply_text(
        text,
        reply_markup=api_key_prompt_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if storage.is_banned(user_id):
        return
    sub_ok = await _check_subscription_if_needed(user_id, ctx)
    if not sub_ok:
        await _send_sub_prompt(update, ctx)
        return
    if storage.has_api_key(user_id):
        state = storage.get_state(user_id)
        has_sb = bool(state and state.get("sandbox_id"))
        text = (
            f"👋 أهلاً <b>{update.effective_user.first_name}</b>!\n\n"
            "مفتاحك مسجّل لدينا بالفعل."
        )
        await update.effective_message.reply_text(
            append_log(text, user_id),
            reply_markup=main_menu(has_sb),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await _ask_api_key(update, ctx, changing=False)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if storage.is_banned(user_id):
        return
    sub_ok = await _check_subscription_if_needed(user_id, ctx)
    if not sub_ok:
        await _send_sub_prompt(update, ctx)
        return
    text = (
        "📖 <b>الأوامر</b>\n\n"
        "/start — بدء العمل وعرض القائمة الرئيسية\n"
        "/change_api — استبدال مفتاح HopX API بمفتاح جديد\n"
        "/cancel — إلغاء العملية الحالية والرجوع للقائمة\n"
        "/reset — حذف مفتاحك والبدء من جديد\n\n"
        "من القائمة يمكنك: إنشاء/إنهاء السيرفر، الترمنال، الملفات، "
        "العمليات، تغيير المفتاح، سيرفراتي السابقة."
    )
    await update.effective_message.reply_text(
        append_log(text, user_id),
        reply_markup=main_menu(_has_sb(user_id)),
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if storage.is_banned(user_id):
        return
    st = sandbox_state.get(user_id)
    st.awaiting = ""
    st.files_pending = ""
    await update.effective_message.reply_text(
        append_log("تم الإلغاء — عُدنا للقائمة الرئيسية.", user_id),
        reply_markup=main_menu(_has_sb(user_id)),
        parse_mode=ParseMode.HTML,
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if storage.is_banned(user_id):
        return
    sub_ok = await _check_subscription_if_needed(user_id, ctx)
    if not sub_ok:
        await _send_sub_prompt(update, ctx)
        return
    await sandbox_state.drop(user_id)
    storage.delete_api_key(user_id)
    await _ask_api_key(update, ctx, changing=False)


async def cmd_change_api(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if storage.is_banned(user_id):
        return
    sub_ok = await _check_subscription_if_needed(user_id, ctx)
    if not sub_ok:
        await _send_sub_prompt(update, ctx)
        return
    if not storage.has_api_key(user_id):
        await _ask_api_key(update, ctx, changing=False)
        return
    from keyboards import confirm_change_api
    await update.effective_message.reply_text(
        append_log(
            "🔑 <b>هل أنت متأكد من تغيير مفتاح API؟</b>\n\n"
            "سيُمسح:\n"
            "• المفتاح الحالي (المشفّر)\n"
            "• بيانات السيرفر النشط في هذا البوت\n"
            "• سجل 'سيرفراتي السابقة' (المفتاح الجديد لن يصل إليها)\n\n"
            "اضغط ✅ لتأكيد التغيير أو ❌ إلغاء للتراجع.",
            user_id,
        ),
        reply_markup=confirm_change_api(),
        parse_mode="HTML",
    )