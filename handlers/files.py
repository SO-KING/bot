"""Files handler — list, view, download, upload, delete, mkdir.

All HopX VM operations route through `hopx_client`, which always
`Sandbox.connect()`s to get a fresh JWT before each operation, so
INVALID_TOKEN can never reproduce the kind of failure seen earlier.
"""
import html
import logging
import os
import shlex
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import storage
from config import TELEGRAM_MAX_FILE_BYTES
from hopx_client import (
    list_files, read_file_text, read_file_bytes,
    write_file_bytes, delete_file, make_dir, run_command,
    HopXError,
)
from keyboards import (
    files_keyboard, file_actions, confirm_delete, main_menu,
)
from ui import append_log
from utils.format import human_size
from . import sandbox_state
from .sandbox import _ensure_creds
from .helpers import safe_edit, safe_answer, html_escape

log = logging.getLogger(__name__)


def _is_safe_path(path: str) -> bool:
    return path.startswith("/workspace") or path.startswith("/tmp")


def _parent(path: str) -> str:
    parts = path.rstrip("/").split("/")
    if len(parts) <= 2:
        return "/workspace"
    return "/".join(parts[:-1])


# Telegram caps callback_data at 64 bytes. For long paths we route
# through ctx.user_data["files:idx"] — store the path, generate a
# compact `files:i:N` callback, and look it up on dispatch.
_MAX_CB_BYTES = 60  # leave margin for the prefix


def _path_key(ctx, path: str) -> str:
    """Return a short callback suffix for `path`. Builds (and caches)
    a path-to-index map in ctx.user_data. Result is always <= 12 bytes.
    """
    bucket = ctx.user_data.setdefault("files:idx", {})
    reverse = ctx.user_data.setdefault("files:idx_r", {})
    if path in bucket:
        return f"i:{bucket[path]}"
    idx = len(bucket) % 10000
    bucket[path] = idx
    reverse[idx] = path
    return f"i:{idx}"


def _resolve_path(ctx, suffix: str) -> str:
    """Inverse of `_path_key`: map `i:N` suffix back to the path."""
    if not suffix.startswith("i:"):
        return suffix  # literal path (short, no index needed)
    try:
        idx = int(suffix[2:])
    except Exception:
        return "/workspace"
    reverse = ctx.user_data.get("files:idx_r", {})
    return reverse.get(idx, "/workspace")


def _safe_cb(prefix: str, payload: str, ctx) -> str:
    """Build `prefix{path}` and ensure <=60 bytes. If the resulting
    callback data is too long, route through the index map.
    """
    direct = f"{prefix}{payload}"
    if len(direct.encode("utf-8")) <= _MAX_CB_BYTES:
        return direct
    return f"{prefix}{_path_key(ctx, payload)}"


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    parts = data.split(":", 2)
    sub = parts[1] if len(parts) > 1 else ""
    raw_path = parts[2] if len(parts) > 2 else "/workspace"
    path = _resolve_path(ctx, raw_path)

    if sub == "list":
        await _list(update, ctx, path)
    elif sub == "view":
        await _view(update, ctx, path)
    elif sub == "dl":
        await _download(update, ctx, path)
    elif sub == "del_ask":
        await safe_edit(
            q,
            append_log(f"⚠️ تأكيد حذف: <code>{html_escape(path)}</code>", user_id),
            reply_markup=confirm_delete(ctx, path),
        )
    elif sub == "del":
        await _delete(update, ctx, path)
    elif sub == "mkdir_ask":
        sandbox_state.get(user_id).awaiting = "files:mkdir"
        await q.message.reply_text(
            append_log(
                "📝 أرسل اسم/مسار المجلد الجديد "
                "(نسبي لـ /workspace أو مسار كامل يبدأ بـ /workspace أو /tmp).",
                user_id,
            ),
            parse_mode=ParseMode.HTML,
        )
    elif sub == "upload_ask":
        st = sandbox_state.get(user_id)
        st.awaiting = "files:upload"
        st.upload_filename = ""
        await q.message.reply_text(
            append_log(
                "📤 أرسل ملف كـ Document (وليس كصورة/فيديو) ليُرفع إلى /workspace.\n"
                "اختياري: أضِف في تعليق الملف المسار الكامل مثل "
                "<code>/workspace/sub/file.txt</code>.",
                user_id,
            ),
            parse_mode=ParseMode.HTML,
        )
    elif sub == "back":
        await _list(update, ctx, "/workspace")
    elif sub == "extract_ask":
        arch_path = html.escape(path)
        await safe_edit(
            q,
            append_log(
                "📦 <b>فك ضغط</b>\n\n"
                "الملف: <code>" + arch_path + "</code>\n\n"
                "سيُستخرج في نفس مجلد الملف. اضغط ✅ للتأكيد.",
                user_id,
            ),
            reply_markup=_confirm_extract(ctx, path),
        )
    elif sub == "extract":
        await _extract(update, ctx, path)
    else:
        log.warning("unknown files sub: %s", sub)


async def _list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    if not _is_safe_path(path):
        await q.message.reply_text(
            append_log("⚠️ مسارات غير مسموحة (فقط /workspace و /tmp).", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    sandbox_state.get(user_id).files_path = path
    creds = await _ensure_creds(user_id)
    if not creds:
        await safe_edit(
            q,
            append_log("⚠️ لا يوجد سيرفر نشط. ابدأ سيرفر أولاً.", user_id),
            reply_markup=main_menu(False),
        )
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    try:
        items = await list_files(api_key, sandbox_id, path, user_id=user_id)
    except HopXError as e:
        await safe_edit(
            q,
            append_log(f"❌ {html_escape(str(e))}", user_id),
            reply_markup=main_menu(True),
        )
        return
    msg = f"📁 <b>{html.escape(path)}</b> — {len(items)} عنصر"
    await safe_edit(
        q,
        append_log(msg, user_id),
        reply_markup=files_keyboard(ctx, path, items),
    )


async def _view(update: Update, ctx: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    if not _is_safe_path(path):
        return
    creds = await _ensure_creds(user_id)
    if not creds:
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    try:
        data = await read_file_bytes(api_key, sandbox_id, path, user_id=user_id)
    except HopXError as e:
        await q.message.reply_text(
            append_log(f"❌ {html.escape(str(e))}", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = f"<binary {len(data)} bytes>"
    if len(text) > 3500:
        text = text[:3500] + "\n… (truncated)"
    body = f"📄 <b>{html.escape(path)}</b>\n\n<pre>{html.escape(text)}</pre>"
    await q.message.reply_text(
        append_log(body, user_id),
        reply_markup=file_actions(ctx, path),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _download(update: Update, ctx: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    if not _is_safe_path(path):
        return
    creds = await _ensure_creds(user_id)
    if not creds:
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    msg = await q.message.reply_text(
        append_log("⏳ تحميل الملف…", user_id),
        parse_mode=ParseMode.HTML,
    )
    try:
        data = await read_file_bytes(api_key, sandbox_id, path, user_id=user_id)
    except HopXError as e:
        await msg.edit_text(
            append_log(f"❌ {html.escape(str(e))}", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    name = os.path.basename(path) or "file"
    tmp = os.path.join(tempfile.gettempdir(), f"hopx_{user_id}_{name}")
    with open(tmp, "wb") as f:
        f.write(data)
    with open(tmp, "rb") as f:
        await q.message.reply_document(
            document=f,
            filename=name,
            caption=append_log(f"⬇️ {name} ({human_size(len(data))})", user_id),
            parse_mode=ParseMode.HTML,
        )
    try:
        os.remove(tmp)
    except Exception:
        pass
    try:
        await msg.delete()
    except Exception:
        pass


async def _delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    if not _is_safe_path(path):
        return
    creds = await _ensure_creds(user_id)
    if not creds:
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    try:
        await delete_file(api_key, sandbox_id, path, user_id=user_id)
    except HopXError as e:
        await q.message.reply_text(
            append_log(f"❌ {html.escape(str(e))}", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    parent = _parent(path)
    await q.message.reply_text(
        append_log(f"🗑️ حُذف: <code>{html.escape(path)}</code>", user_id),
        parse_mode=ParseMode.HTML,
    )
    await _list(update, ctx, parent)


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not update.effective_message or not update.effective_message.document:
        return
    st = sandbox_state.get(user_id)
    if st.awaiting != "files:upload":
        # If user already has a sandbox, accept documents even without the
        # explicit "upload_ask" prompt — convenient UX.
        state = storage.get_state(user_id)
        if not state or not state.get("sandbox_id"):
            # No sandbox → ignore document silently.
            return
        st.awaiting = "files:upload"

    creds = await _ensure_creds(user_id)
    if not creds:
        await update.effective_message.reply_text(
            append_log("⚠️ لا يوجد سيرفر نشط.", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds

    doc = update.effective_message.document
    caption = update.effective_message.caption or ""
    remote_name = caption.strip() or doc.file_name or "file.bin"
    if "/" in remote_name:
        remote_path = remote_name if remote_name.startswith("/") else "/workspace/" + remote_name
    else:
        remote_path = "/workspace/" + remote_name
    if not _is_safe_path(remote_path):
        await update.effective_message.reply_text(
            append_log("⚠️ مسار غير مسموح (استخدم /workspace أو /tmp).", user_id),
            parse_mode=ParseMode.HTML,
        )
        return

    msg = await update.effective_message.reply_text(
        append_log(
            f"⏳ رفع <code>{html.escape(remote_path)}</code> "
            f"({human_size(doc.file_size or 0)})…",
            user_id,
        ),
        parse_mode=ParseMode.HTML,
    )
    # Soft cap on file size. Telegram's public Bot API limits downloads
    # to 20 MB; for >20 MB you must run a local telegram-bot-api server
    # and set TELEGRAM_LOCAL_API_URL. If no local server is configured,
    # Telegram will surface its own error — we still attempt and let
    # the user know about the limit.
    if doc.file_size and doc.file_size > TELEGRAM_MAX_FILE_BYTES:
        await msg.edit_text(
            append_log(
                f"❌ الملف أكبر من الحد ({human_size(doc.file_size)} > "
                f"{human_size(TELEGRAM_MAX_FILE_BYTES)}).",
                user_id,
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
        await write_file_bytes(
            api_key, sandbox_id, remote_path, bytes(data), user_id=user_id,
        )
    except Exception as e:
        # Even on HopX errors, refresh_next_retry is handled inside
        # `_vm_op_blocking`. Surface the actual message to the user.
        await msg.edit_text(
            append_log(f"❌ فشل الرفع: {html.escape(str(e))}", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    st.awaiting = ""
    await msg.edit_text(
        append_log(
            f"✅ رُفع <code>{html.escape(remote_path)}</code> "
            f"({human_size(len(data))})",
            user_id,
        ),
        reply_markup=main_menu(True),
        parse_mode=ParseMode.HTML,
    )


async def on_upload_path_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """User was prompted to upload; their response is plain text — treat it
    as the upload target filename, then ask for the actual file."""
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()
    if text:
        sandbox_state.get(user_id).upload_filename = text
    sandbox_state.get(user_id).awaiting = "files:upload"
    await update.effective_message.reply_text(
        append_log(
            "📤 الآن أرسل الملف كـ Document ليُرفع إلى <code>"
            f"{html.escape(text or '/workspace')}</code>.",
            user_id,
        ),
        parse_mode=ParseMode.HTML,
    )


async def on_mkdir_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = sandbox_state.get(user_id)
    text = (update.effective_message.text or "").strip()
    st.awaiting = ""
    if not text:
        await update.effective_message.reply_text(
            append_log("❌ اسم غير صالح.", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    if not text.startswith("/"):
        text = "/workspace/" + text
    if not _is_safe_path(text):
        await update.effective_message.reply_text(
            append_log("⚠️ فقط /workspace أو /tmp.", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    creds = await _ensure_creds(user_id)
    if not creds:
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds
    try:
        await make_dir(api_key, sandbox_id, text, user_id=user_id)
    except HopXError as e:
        await update.effective_message.reply_text(
            append_log(f"❌ {html.escape(str(e))}", user_id),
            parse_mode=ParseMode.HTML,
        )
        return
    await update.effective_message.reply_text(
        append_log(f"✅ أُنشئ: <code>{html.escape(text)}</code>", user_id),
        reply_markup=main_menu(True),
        parse_mode=ParseMode.HTML,
    )


# -----------------------------------------------------------------------------
# Archive extraction (via shell)
# -----------------------------------------------------------------------------


def _confirm_extract(ctx, path: str) -> InlineKeyboardMarkup:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ فك الضغط", callback_data=_safe_cb("files:extract:", path, ctx)),
         InlineKeyboardButton("❌ إلغاء", callback_data=_safe_cb("files:view:", path, ctx))],
    ])


async def _extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE, path: str) -> None:
    user_id = update.effective_user.id
    q = update.callback_query
    if not _is_safe_path(path):
        return
    creds = await _ensure_creds(user_id)
    if not creds:
        return
    st, api_key, sandbox_id, public_host, jwt, info = creds

    dirname = _parent(path)
    basename = os.path.basename(path)
    ext = "." + basename.split(".", 1)[1] if "." in basename else ""
    ext = ext.lower()
    # Extract into the same directory as the source archive — preserves
    # the zip's natural structure (one folder deep), no extra wrapper.
    # e.g. /workspace/SOLO-test (2).zip containing "SOLO-test/foo" →
    # /workspace/SOLO-test/foo (one level, matches user expectation).
    dest = dirname
    cmd = _build_extract_cmd(path, dest, ext)
    decoded_path = path + ".b64decoded"

    await safe_edit(
        q,
        append_log("⏳ جارٍ فك ضغط الملف…", user_id),
    )
    try:
        await make_dir(api_key, sandbox_id, dest, user_id=user_id)
    except HopXError:
        pass

    try:
        res = await run_command(
            api_key, sandbox_id, cmd, timeout=120,
            working_dir=dirname, user_id=user_id,
        )
    except HopXError as e:
        await safe_edit(
            q,
            append_log(
                "❌ فشل فك الضغط: <code>" + html.escape(str(e)) + "</code>",
                user_id,
            ),
            reply_markup=main_menu(True),
        )
        return

    # If the original tool failed (e.g. .zip that's really a tarball),
    # always retry once with `tar` which auto-detects gz/bzip2/xz/zstd/
    # tar content by magic bytes — covers the most common misnamed-
    # archive case.
    if not res.success:
        try:
            retry = await run_command(
                api_key, sandbox_id,
                "tar -xf " + shlex.quote(path) + " -C " + shlex.quote(dest),
                timeout=120, working_dir=dirname, user_id=user_id,
            )
            if retry.success:
                await safe_edit(
                    q,
                    append_log(
                        "تم فك الضغط (auto-detect) إلى:\n"
                        + "<code>" + html.escape(dest) + "</code>",

                        user_id,
                    ),
                )
                await _list(update, ctx, dirname)
                return
        except HopXError:
            pass

    # If everything above failed AND the file content looks like base64
    # text that decodes into a known archive magic, decode it and try
    # again on the decoded copy. Covers the common workflow where users
    # base64-encode archives to push them safely through copy/paste
    # channels, then save the decoded copy before extracting.
    if not res.success:
        try:
            qp = shlex.quote(path)
            sniff_cmd = (
                "decoded_magic=$("
                "head -c 4096 " + qp + " | tr -d '\\n\\r ' "
                "| base64 -d 2>/dev/null | head -c 8 | xxd -p"
                "); "
                "case \"$decoded_magic\" in "
                "504b0304*|1f8b*|425a68*|377abcaf*|52617221*|75737461*) "
                "echo BASE64_ARCHIVE=\"$decoded_magic\";; "
                "*) echo NOT_BASE64 ;; "
                "esac"
            )
            sniff = await run_command(
                api_key, sandbox_id,
                sniff_cmd,
                timeout=15, working_dir=dirname, user_id=user_id,
            )
            sniff_out = (sniff.stdout or "").strip()
            if sniff_out.startswith("BASE64_ARCHIVE="):
                decode_cmd = (
                    "base64 -d " + qp + " > " + shlex.quote(decoded_path)
                )
                await run_command(
                    api_key, sandbox_id,
                    decode_cmd,
                    timeout=60, working_dir=dirname, user_id=user_id,
                )
                retry = await run_command(
                    api_key, sandbox_id,
                    _build_extract_cmd(decoded_path, dest, ext),
                    timeout=120, working_dir=dirname, user_id=user_id,
                )
                # Always clean up the decoded temp file regardless of
                # success — keeping it would clutter /workspace.
                await _try_rm(api_key, sandbox_id, decoded_path,
                              working_dir=dirname, user_id=user_id)
                if retry.success:
                    await safe_edit(
                        q,
                        append_log(
                            "✅ تم فك التشفير + الضغط إلى:\n"
                            + "<code>" + html.escape(dest) + "</code>",
                            user_id,
                        ),
                    )
                    await _list(update, ctx, dirname)
                    return
        except HopXError:
            pass

    if res.success:
        await safe_edit(
            q,
            append_log(
                "✅ تم فك الضغط بنجاح إلى:\n"
                + "<code>" + html.escape(dest) + "</code>",

                user_id,
            ),
        )
        await _list(update, ctx, dirname)
        return

    stderr_short = ((res.stderr or res.stdout or "").splitlines()[:3])
    stderr_short = "\n".join(stderr_short)[:400]
    help_msg = (
        "\n\n💡 إذا الملف تالف أو مو مضغوط صالح، من الترمنال يمكنك:\n"
        + "file " + path + "</code>",
        " لمعرفة النوع الفعلي"
    )
    await safe_edit(
        q,
        append_log(
            "fشل فك الضغط (exit " + str(res.exit_code) + ")."
            + "<code>" + html.escape(stderr_short) + "</code>",
            + help_msg,
            user_id,
        ),
        reply_markup=main_menu(True),
    )

def _build_extract_cmd(filepath: str, dest: str, ext: str) -> str:
    "Build a shell command to extract an archive. All paths are "
    "quoted with shlex.quote so filenames with spaces or "
    "special chars don't break shell tokenisation."
    qp = shlex.quote(filepath)
    qd = shlex.quote(dest)
    if ext in ('.zip', '.jar', '.war'):
        return "unzip -o " + qp + " -d " + qd
    if ext == '.7z':
        return "7z x " + qp + " -o" + qd + " -y"
    if ext == '.rar':
        return "unrar x -o+ " + qp + " " + qd
    if ext == '.gz':
        return "gunzip -c " + qp + " > " + qd + "/content"
    if ext == '.bz2':
        return "bunzip2 -c " + qp + " > " + qd + "/content"
    if ext == '.xz':
        return "xz -d -c " + qp + " > " + qd + "/content"
    if ext == '.zst':
        return "zstd -d -c " + qp + " -o " + qd + "/content"
    "tar handles .tar, .tar.gz, .tgz, .tar.bz2, .tbz2, "
    ".tar.xz, .txz, .tar.zst via auto-detection"
    return "tar -xf " + qp + " -C " + qd


async def _try_rm(api_key, sandbox_id, target_path: str,
                   working_dir: str = "/workspace", user_id: int = 0) -> None:
    """Best-effort delete a file inside the sandbox. Swallows any error
    so callers can use it as a fire-and-forget cleanup."""
    try:
        await run_command(
            api_key, sandbox_id,
            "rm -f " + shlex.quote(target_path),
            timeout=10, working_dir=working_dir, user_id=user_id,
        )
    except HopXError:
        pass
