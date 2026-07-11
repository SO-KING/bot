"""Inline keyboard factories."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(has_sandbox: bool) -> InlineKeyboardMarkup:
    if has_sandbox:
        kb = [
            [InlineKeyboardButton("🖥️ الترمنال", callback_data="term:open")],
            [InlineKeyboardButton("📁 إدارة الملفات", callback_data="files:list:/workspace")],
            [InlineKeyboardButton("⚙️ العمليات", callback_data="proc:list")],
            [InlineKeyboardButton("📊 حالة السيرفر", callback_data="sb:status")],
            [InlineKeyboardButton("🗑️ إنهاء السيرفر", callback_data="sb:kill_ask")],
            [InlineKeyboardButton("🗂️ سيرفراتي السابقة", callback_data="sb:list_mine")],
            [InlineKeyboardButton("🔑 تغيير مفتاح API", callback_data="sb:change_api_ask")],
        ]
    else:
        kb = [
            [InlineKeyboardButton("🚀 إنشاء سيرفر جديد", callback_data="sb:create_ask")],
            [InlineKeyboardButton("🗂️ سيرفراتي السابقة", callback_data="sb:list_mine")],
            [InlineKeyboardButton("🔑 تغيير مفتاح API", callback_data="sb:change_api_ask")],
        ]
    return InlineKeyboardMarkup(kb)


def home_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("🏠 الرئيسية", callback_data="menu:home")


def with_home(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def cancel_button() -> InlineKeyboardButton:
    return InlineKeyboardButton("❌ إلغاء", callback_data="ui:cancel")


def with_cancel(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Append a single-row 'cancel' button that always returns to home."""
    rows.append([cancel_button(), home_button()])
    return InlineKeyboardMarkup(rows)


def api_key_prompt_keyboard() -> InlineKeyboardMarkup:
    """Shown when the bot is waiting for an API key.

    The single 'cancel' button goes back to home (or first-run greeting).
    """
    return InlineKeyboardMarkup([[cancel_button()]])


def confirm_create_sandbox() -> InlineKeyboardMarkup:
    """Shown after the user taps 'create new sandbox'. Lists the existing
    sandbox option so they don't lose context."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، أنشئ سيرفر جديد", callback_data="sb:create")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="menu:home")],
    ])


def confirm_change_api() -> InlineKeyboardMarkup:
    """Shown when the user taps 'change API key'. Two-step gate so a
    stray tap cannot wipe the credentials."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، غيّر المفتاح", callback_data="sb:change_api_go")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="menu:home")],
    ])


def terminal_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⌨️ وضع الإدخال (بدون Enter)", callback_data="term:stdin")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="term:refresh")],
        [InlineKeyboardButton("⟲ Ctrl+C", callback_data="term:ctrlc"),
         InlineKeyboardButton("↩️ Ctrl+D", callback_data="term:ctrld"),
         InlineKeyboardButton("🧹 مسح", callback_data="term:clear")],
        [InlineKeyboardButton("🚪 خروج/إغلاق", callback_data="term:close")],
        [home_button()],
    ]
    return InlineKeyboardMarkup(kb)


def files_keyboard(ctx, path: str, items: list) -> InlineKeyboardMarkup:
    rows = []
    # navigation
    if path != "/workspace" and path.startswith("/workspace/"):
        rows.append([InlineKeyboardButton("⬆️ الأعلى", callback_data=_safe_cb("files:list:", _parent(path), ctx))])
    rows.append([InlineKeyboardButton("📂 إنشاء مجلد", callback_data="files:mkdir_ask"),
                 InlineKeyboardButton("📤 رفع ملف", callback_data="files:upload_ask")])
    # listing (max 20 items to keep keyboard small)
    for f in items[:20]:
        if f.is_directory:
            # Folders now get the same triple-action row as files:
            # list (open folder), delete (works for folders too).
            rows.append([
                InlineKeyboardButton(f"📁 {f.name}/", callback_data=_safe_cb("files:list:", f.path, ctx)),
                InlineKeyboardButton("🗑️", callback_data=_safe_cb("files:del_ask:", f.path, ctx)),
            ])
        else:
            row = [
                InlineKeyboardButton(f"📄 {f.name} ({_hs(f.size)})", callback_data=_safe_cb("files:view:", f.path, ctx)),
                InlineKeyboardButton("⬇️", callback_data=_safe_cb("files:dl:", f.path, ctx)),
                InlineKeyboardButton("🗑️", callback_data=_safe_cb("files:del_ask:", f.path, ctx)),
            ]
            if _is_archive(f.name):
                row.append(InlineKeyboardButton(
                    "📦 فك ضغط",
                    callback_data=_safe_cb("files:extract_ask:", f.path, ctx),
                ))
            rows.append(row)
    if len(items) > 20:
        rows.append([InlineKeyboardButton(f"… +{len(items) - 20} عناصر أخرى", callback_data="noop")])
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


# Archive extensions supported by HopX VM agent (`unzip`, `tar`,
# `tar -xzf`, etc). Matched case-insensitively against the filename.
_ARCHIVE_EXTS = (
    ".zip", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst",
    ".tgz", ".tbz2", ".txz", ".gz", ".bz2", ".xz", ".zst",
    ".7z", ".rar", ".jar", ".war",
)


def _is_archive(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    for ext in _ARCHIVE_EXTS:
        if n.endswith(ext):
            return True
    return False


def file_actions(ctx, path: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⬇️ تحميل", callback_data=_safe_cb("files:dl:", path, ctx)),
         InlineKeyboardButton("🗑️ حذف", callback_data=_safe_cb("files:del_ask:", path, ctx))],
        [InlineKeyboardButton("📝 عرض المحتوى", callback_data=_safe_cb("files:view:", path, ctx))],
        [InlineKeyboardButton("⬆️ الأعلى", callback_data="files:back")],
        [home_button()],
    ]
    return InlineKeyboardMarkup(kb)


def confirm_delete(ctx, path: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ نعم احذف", callback_data=_safe_cb("files:del:", path, ctx))],
        [InlineKeyboardButton("❌ إلغاء", callback_data=_safe_cb("files:view:", path, ctx))],
    ]
    return InlineKeyboardMarkup(kb)


def _parent(path: str) -> str:
    parts = path.rstrip("/").split("/")
    if len(parts) <= 2:
        return "/workspace"
    return "/".join(parts[:-1])


def _safe_cb(prefix: str, payload: str, ctx) -> str:
    """Build a Telegram-safe callback_data. If the result exceeds 60
    bytes, the path is replaced with a compact `i:N` index that is
    resolved back to the original path via ctx.user_data.
    """
    direct = f"{prefix}{payload}"
    if len(direct.encode("utf-8")) <= 60:
        return direct
    bucket = ctx.user_data.setdefault("files:idx", {})
    reverse = ctx.user_data.setdefault("files:idx_r", {})
    if payload in bucket:
        return f"{prefix}i:{bucket[payload]}"
    idx = len(bucket) % 10000
    bucket[payload] = idx
    reverse[idx] = payload
    return f"{prefix}i:{idx}"


def processes_keyboard(items: list) -> InlineKeyboardMarkup:
    """Show user-background processes only.

    Each process has its own kill button. There's a Refresh button at
    the bottom to re-fetch the live list. No system-mode toggle —
    system processes are deliberately hidden from the user.
    """
    rows = []
    for p in items[:25]:
        label = f"{p.name or p.process_id} ({p.status})"
        rows.append([
            InlineKeyboardButton(label, callback_data="noop"),
            InlineKeyboardButton("🛑 إيقاف", callback_data=f"proc:kill:{p.process_id}"),
        ])
    rows.append([
        InlineKeyboardButton("🔄 تحديث", callback_data="proc:refresh"),
    ])
    rows.append([home_button()])
    return InlineKeyboardMarkup(rows)


def sandbox_status_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⏱️ +1 ساعة", callback_data="sb:extend:3600"),
         InlineKeyboardButton("⏱️ +3 ساعات", callback_data="sb:extend:10800")],
        [InlineKeyboardButton("⏸️ إيقاف مؤقت", callback_data="sb:pause"),
         InlineKeyboardButton("▶️ استئناف", callback_data="sb:resume")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="sb:status")],
        [InlineKeyboardButton("🗑️ إنهاء السيرفر", callback_data="sb:kill_ask")],
        [home_button()],
    ]
    return InlineKeyboardMarkup(kb)


def confirm_kill_sandbox() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ نعم أنهِ", callback_data="sb:kill")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="sb:status")],
    ]
    return InlineKeyboardMarkup(kb)


def _hs(n: int) -> str:
    from utils.format import human_size
    return human_size(n)


# -----------------------------------------------------------------------------
# Admin panel keyboards
# -----------------------------------------------------------------------------

def admin_main() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="admin:stats")],
        [InlineKeyboardButton("📢 الاشتراك الإجباري", callback_data="admin:forced_sub")],
        [InlineKeyboardButton("🚫 إدارة الحظر", callback_data="admin:bans")],
        [InlineKeyboardButton("👤 إدارة الأدمن", callback_data="admin:admins")],
        [InlineKeyboardButton("🖥️ سيرفرات المستخدمين", callback_data="admin:servers:0")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="menu:home")],
    ]
    return InlineKeyboardMarkup(kb)


def admin_forced_sub_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        rows.append([
            InlineKeyboardButton(f"🔗 @{ch}", callback_data="noop"),
            InlineKeyboardButton("❌ حذف", callback_data=f"admin:remove_ch:{ch}"),
        ])
    rows.append([InlineKeyboardButton("➕ إضافة قناة", callback_data="admin:add_ch")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)


def admin_cancel_keyboard(callback_target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ إلغاء", callback_data=callback_target)],
    ])


def admin_single_button(target: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=target)],
    ])


def admin_servers_keyboard(servers: list[dict], page: int = 0,
                           per_page: int = 3) -> InlineKeyboardMarkup:
    total = len(servers)
    start = page * per_page
    end = min(start + per_page, total)
    page_servers = servers[start:end]

    rows = []
    for s in page_servers:
        uid = s["user_id"]
        sid = s["sandbox_id"] or "—"
        status = s.get("status") or "—"
        short_id = str(sid)[:14] + "…" if len(str(sid)) > 15 else str(sid)
        rows.append([
            InlineKeyboardButton(
                f"🆔 {uid} • {short_id} • {status}",
                callback_data=f"admin:enter_server:{uid}:{sid}",
            )
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"admin:servers:{page - 1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton("التالي ➡️", callback_data=f"admin:servers:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin:panel")])
    return InlineKeyboardMarkup(rows)


def admin_user_server_actions(user_id: int, sandbox_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥️ فتح ترمنال", callback_data=f"admin:term:{user_id}:{sandbox_id}")],
        [InlineKeyboardButton("📁 ملفات", callback_data=f"admin:files:{user_id}:{sandbox_id}")],
        [InlineKeyboardButton("📊 حالة", callback_data=f"admin:status:{user_id}:{sandbox_id}")],
        [InlineKeyboardButton("🔙 رجوع للسيرفرات", callback_data="admin:servers:0")],
        [InlineKeyboardButton("🏠 لوحة الأدمن", callback_data="admin:panel")],
    ])


# -----------------------------------------------------------------------------
# Forced subscription keyboard (shown to unsubscribed users)
# -----------------------------------------------------------------------------

def forced_sub_keyboard(channels: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        rows.append([InlineKeyboardButton(f"📢 اشترك في @{ch}", url=f"https://t.me/{ch}")])
    rows.append([InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="check_sub")])
    return InlineKeyboardMarkup(rows)
