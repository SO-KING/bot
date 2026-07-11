import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing in env")

FERNET_KEY = os.getenv("FERNET_KEY", "").strip()
if not FERNET_KEY:
    raise RuntimeError("FERNET_KEY missing in env (see README.md to generate one)")

DB_PATH = BASE_DIR / "hopx_bot.db"

HOPX_DEFAULT_TEMPLATE = os.getenv("HOPX_DEFAULT_TEMPLATE", "code-interpreter")
# 0 (or unset) means NO auto-destroy — sandbox lives until explicitly killed
# or paused/stopped. Per HopX docs: `timeout_seconds: undefined => no limit`.
HOPX_DEFAULT_TIMEOUT = int(os.getenv("HOPX_DEFAULT_TIMEOUT", "0"))
HOPX_USE_TIMEOUT = HOPX_DEFAULT_TIMEOUT > 0

HOPX_BASE_URL = "https://api.hopx.dev"

SANDBOX_RENEW_TIMEOUT_SECONDS = 3600
TERMINAL_COLS = 80
TERMINAL_ROWS = 24
TERMINAL_IDLE_FLUSH_MS = 350
TERMINAL_DONE_GRACE_MS = 1200
TELEGRAM_MAX_MSG_CHARS = 4000
TELEGRAM_MAX_FILE_BYTES = int(
    os.getenv("TELEGRAM_MAX_FILE_BYTES", str(200 * 1024 * 1024))
)
# Note: Telegram's public Bot API caps file downloads at 20 MB. To
# support files up to TELEGRAM_MAX_FILE_BYTES, run a local
# `telegram-bot-api` server (https://github.com/tdlib/telegram-bot-api)
# and point the bot at it via the `TELEGRAM_LOCAL_API_URL` env var.
TELEGRAM_LOCAL_API_URL = os.getenv("TELEGRAM_LOCAL_API_URL", "").strip()
