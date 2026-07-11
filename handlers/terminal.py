"""Interactive terminal handler — stateful WebSocket session per user.

Simple, classic flow:
- term:open opens a session and sends one "terminal opened" message.
- on_text sends the user's command to the WebSocket, receives output,
  and POSTS THE RESPONSE AS A NEW MESSAGE (one message per command).
- term:refresh opens a one-shot drain+edit cycle on the last command
  message so users can pull any pending output (e.g. for slow-running
  commands) without losing messages.
- term:close explicitly closes the session.
- menu:home does NOT close the session so users can navigate away
  and back without losing their terminal state.
"""
import asyncio
import logging
import html
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import storage
from hopx_client import open_terminal, HopXError, connect_sandbox
from keyboards import terminal_keyboard, main_menu
from ui import append_log
from utils.ansi import clean_for_telegram
from config import TELEGRAM_MAX_MSG_CHARS
from . import sandbox_state
from .sandbox import _ensure_creds
from .helpers import safe_edit, safe_answer, html_escape
from telegram.error import BadRequest

log = logging.getLogger(__name__)


async def _open_session(user_id: int) -> bool:
    creds = await _ensure_creds(user_id)
    if not creds:
        return False
    st, api_key, sandbox_id, public_host, jwt, info = creds
    if not public_host or not jwt:
        return False

    # Reuse live session if already open — preserves cwd + history.
    if st.terminal_session and st.terminal_session.is_open:
        st.awaiting = "terminal"
        storage.set_terminal_active(user_id, True)
        return True

    # Close stale session before opening a new one
    if st.terminal_session:
        try:
            await asyncio.wait_for(st.terminal_session.close(), timeout=2.0)
        except Exception:
            pass
        st.terminal_session = None

    # Try to open with the cached JWT. If HopX rejects it (HTTP 401),
    # call connect_sandbox which does a control-plane refresh of the
    # JWT, then retry. This handles the common case where the cached
    # JWT has aged out but the sandbox is still alive.
    sess = None
    for attempt in (1, 2):
        try:
            sess = await open_terminal(public_host, jwt)
            break
        except Exception as e:
            err = str(e).lower()
            if attempt == 1 and ("401" in err or "unauthorized" in err or "token" in err):
                log.warning("terminal JWT stale for user %s, refreshing", user_id)
                try:
                    info = await connect_sandbox(api_key, sandbox_id, user_id=user_id)
                    public_host = info.public_host
                    jwt = info.jwt
                    storage.set_sandbox(
                        user_id, info.sandbox_id, info.public_host, info.jwt,
                        info.expires_at, info.status,
                    )
                    st.public_host = public_host
                    st.jwt = jwt
                except Exception as ce:
                    log.warning("JWT refresh failed: %s", ce)
                    break
                continue
            log.warning("terminal open failed: %s", e)
            break
    if not sess:
        return False
    st.sandbox_id = sandbox_id
    st.jwt = jwt
    st.terminal_session = sess
    st.terminal_stdin = False
    st.awaiting = "terminal"
    storage.set_terminal_active(user_id, True)

    # Restore cwd: cd + pwd so the parser always finds an absolute path.
    last_cwd = storage.get_terminal_cwd(user_id) or "/workspace"
    try:
        await asyncio.wait_for(
            sess.send_input(f"cd {last_cwd}\npwd\n", execute=True),
            timeout=4.0,
        )
        out = await asyncio.wait_for(
            sess.recv_output(idle_s=0.4, grace_s=0.8),
            timeout=4.0,
        )
        new_cwd = _extract_pwd(out) or last_cwd
        if new_cwd and new_cwd != last_cwd and new_cwd.startswith("/"):
            storage.set_terminal_cwd(user_id, new_cwd)
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        log.debug("cwd restore failed: %s", e)
    return True


def _extract_pwd(text: str) -> str:
    """Return the first absolute path found in text, or empty string."""
    if not text:
        return ""
    scrubbed = (
        text.replace("\x1b[?2004h", "")
        .replace("\x1b[?2004l", "")
    )
    for line in scrubbed.splitlines():
        line = line.strip()
        if line.startswith("/") and " " not in line and len(line) < 4096:
            return line
    return ""


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    parts = data.split(":")
    sub = parts[1] if len(parts) > 1 else ""

    if sub == "open":
        # Cancel any pending edit of an old refresh message before we open
        # a brand-new terminal session.
        msg = await q.message.reply_text(
            append_log("⏳ جاري فتح ترمنال تفاعلي…", user_id),
            parse_mode=ParseMode.HTML,
        )
        try:
            ok = await asyncio.wait_for(_open_session(user_id), timeout=15.0)
        except asyncio.TimeoutError:
            ok = False
        if not ok:
            try:
                await msg.edit_text(
                    append_log("❌ لا يوجد سيرفر نشط. ابدأ سيرفر أولاً.", user_id),
                    reply_markup=main_menu(False),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await q.message.reply_text(
                    append_log("❌ لا يوجد سيرفر نشط. ابدأ سيرفر أولاً.", user_id),
                    reply_markup=main_menu(False),
                    parse_mode=ParseMode.HTML,
                )
            return
        try:
            await msg.edit_text(
                append_log(
                    "🖥️ <b>الترمنال مفتوح</b>\n"
                    "اكتب أوامرك هنا كأنك على SSH.",
                    user_id,
                ),
                reply_markup=terminal_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await q.message.reply_text(
                append_log(
                    "🖥️ <b>الترمنال مفتوح</b>\n"
                    "اكتب أوامرك هنا كأنك على SSH.",
                    user_id,
                ),
                reply_markup=terminal_keyboard(),
                parse_mode=ParseMode.HTML,
            )

    elif sub == "stdin":
        st = sandbox_state.get(user_id)
        if not st.terminal_session:
            await q.message.reply_text(
                append_log("⚠️ الترمنال مغلق. افتحه أولاً.", user_id),
                parse_mode=ParseMode.HTML,
            )
            return
        st.terminal_stdin = not st.terminal_stdin
        mode = "🔇 عادي (Enter)" if not st.terminal_stdin else "⌨️ إدخال (بدون Enter)"
        await safe_answer(q, f"الوضع: {mode}", show_alert=False)

    elif sub == "ctrlc":
        st = sandbox_state.get(user_id)
        if not st.terminal_session:
            return
        try:
            await st.terminal_session.send_input("\x03", execute=False)
            out = await st.terminal_session.recv_output(idle_s=0.4)
            await _flush_output(update, out)
        except Exception as e:
            await q.message.reply_text(
                append_log(f"⚠️ {e}", user_id),
                parse_mode=ParseMode.HTML,
            )

    elif sub == "ctrld":
        st = sandbox_state.get(user_id)
        if not st.terminal_session:
            return
        try:
            await st.terminal_session.send_input("\x04", execute=False)
            out = await st.terminal_session.recv_output(idle_s=0.4)
            await _flush_output(update, out)
        except Exception as e:
            await q.message.reply_text(
                append_log(f"⚠️ {e}", user_id),
                parse_mode=ParseMode.HTML,
            )

    elif sub == "clear":
        st = sandbox_state.get(user_id)
        if not st.terminal_session:
            return
        try:
            await st.terminal_session.send_input("\x1b[?2004l", execute=False)
            await st.terminal_session.send_input("clear\n", execute=True)
            await asyncio.sleep(0.15)
            await st.terminal_session.recv_output(idle_s=0.2)
        except Exception:
            pass
        try:
            await q.delete_message()
        except Exception:
            pass
        await q.message.reply_text(
            append_log("🧹 تم مسح الشاشة.", user_id),
            reply_markup=terminal_keyboard(),
            parse_mode=ParseMode.HTML,
        )

    elif sub == "refresh":
        st = sandbox_state.get(user_id)
        if not st.terminal_session or not st.terminal_session.is_open:
            await q.message.reply_text(
                append_log("⚠️ الترمنال مغلق.", user_id),
                reply_markup=terminal_keyboard(),
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            out = await asyncio.wait_for(
                st.terminal_session.recv_output(idle_s=0.3, grace_s=0.6),
                timeout=2.5,
            )
        except asyncio.TimeoutError:
            out = ""
        except Exception as e:
            out = f"[recv error: {e}]"
        if out.strip():
            await _flush_output(update, out)
        else:
            await safe_answer(q, "🔄 لا توجد مخرجات جديدة", show_alert=False)

    elif sub == "close":
        st = sandbox_state.get(user_id)
        if st.terminal_session:
            await st.terminal_session.close()
            st.terminal_session = None
        storage.set_terminal_active(user_id, False)
        st.awaiting = ""
        has_sb = bool(
            storage.get_state(user_id)
            and storage.get_state(user_id).get("sandbox_id")
        )
        await safe_edit(
            q,
            append_log("🚪 تم إغلاق الترمنال.", user_id),
            reply_markup=main_menu(has_sb),
        )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = sandbox_state.get(user_id)

    # Per-user lock — telegram sends one update at a time per chat, but
    # during reconnect storms two can interleave. Always serialize.
    await st.lock.acquire()
    try:
        await _on_text_locked(update, ctx)
    finally:
        st.lock.release()


async def _on_text_locked(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = sandbox_state.get(user_id)

    if not st.terminal_session or not st.terminal_session.is_open:
        # Auto-reopen
        if st.terminal_session:
            try:
                await asyncio.wait_for(st.terminal_session.close(), timeout=2.0)
            except Exception:
                pass
            st.terminal_session = None
        try:
            ok = await asyncio.wait_for(_open_session(user_id), timeout=15.0)
        except asyncio.TimeoutError:
            await update.effective_message.reply_text(
                append_log("⏳ فتح الترمنال أخذ وقتاً طويلاً، حاول مجدداً.", user_id),
                parse_mode=ParseMode.HTML,
            )
            return
        if not ok:
            await update.effective_message.reply_text(
                append_log("❌ لا يوجد سيرفر/ترمنال نشط.", user_id),
                reply_markup=main_menu(False),
                parse_mode=ParseMode.HTML,
            )
            return

    text = update.effective_message.text or ""
    execute = not st.terminal_stdin

    try:
        await asyncio.wait_for(
            st.terminal_session.send_input(text, execute=execute),
            timeout=3.0,
        )
    except HopXError as e:
        st.terminal_session = None
        try:
            ok = await asyncio.wait_for(_open_session(user_id), timeout=10.0)
        except asyncio.TimeoutError:
            ok = False
        if ok:
            try:
                await st.terminal_session.send_input(text, execute=execute)
            except Exception as e2:
                await update.effective_message.reply_text(
                    append_log(f"❌ {e2}", user_id),
                    parse_mode=ParseMode.HTML,
                )
                return
        else:
            await update.effective_message.reply_text(
                append_log(f"❌ الترمنال مغلق: {e}", user_id),
                parse_mode=ParseMode.HTML,
            )
            return
    except Exception as e:
        await update.effective_message.reply_text(
            append_log(f"❌ {e}", user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    # Receive output (bounded to 5s total so a runaway process can't hang us)
    try:
        out = await asyncio.wait_for(
            st.terminal_session.recv_output(idle_s=0.5, grace_s=1.0),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        out = "[- الترمنال لم يستجب خلال 5 ثوانٍ -]"
    except Exception as e:
        out = f"[recv error: {e}]"

    new_cwd = _extract_pwd(out)
    if not new_cwd:
        try:
            await asyncio.wait_for(
                st.terminal_session.send_input("pwd\n", execute=True),
                timeout=2.0,
            )
            pwd_out = await asyncio.wait_for(
                st.terminal_session.recv_output(idle_s=0.3, grace_s=0.6),
                timeout=2.0,
            )
            new_cwd = _extract_pwd(pwd_out)
        except (asyncio.TimeoutError, Exception):
            pass
    if new_cwd:
        try:
            storage.set_terminal_cwd(user_id, new_cwd)
        except Exception:
            pass

    if out.strip():
        await _flush_output(update, out)
    elif st.terminal_stdin:
        await update.effective_message.reply_text(
            append_log("<i>… تم إرسال إدخال (بدون Enter</i>", user_id),
            reply_markup=terminal_keyboard(),
            parse_mode=ParseMode.HTML,
        )


async def _flush_output(update: Update, text: str) -> None:
    if not text:
        return
    chunks = clean_for_telegram(text, TELEGRAM_MAX_MSG_CHARS)
    for chunk in chunks:
        try:
            await update.effective_message.reply_text(
                append_log(chunk, update.effective_user.id),
                reply_markup=terminal_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning("send chunk failed: %s", e)
