"""Bot entrypoint: build Application, register handlers, init DB."""
import asyncio
import logging
import sys

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_LOCAL_API_URL, BASE_DIR
import storage
from handlers import start, menu, sandbox, terminal, files, processes, admin

log = logging.getLogger(__name__)


def build_app() -> Application:
    storage.init_db()

    # JobQueue disabled — we don't use scheduled jobs, and the queue
    # spawns a child process that competes for getUpdates (Conflict).
    #
    # Concurrent updates enabled: many users can be processed in parallel.
    # Per-user locking is handled in RuntimeState.lock; DB writes are
    # serialized in storage.py via _lock. Without concurrent updates the
    # bot freezes whenever one user blocks on a long-running operation
    # (terminal WebSocket, file upload, etc.) and all other users see
    # the bot as unresponsive.
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if TELEGRAM_LOCAL_API_URL:
        # Use a local telegram-bot-api server (supports >20MB files).
        builder = builder.base_url(TELEGRAM_LOCAL_API_URL)
    app = (
        builder
        .job_queue(None)
        .concurrent_updates(True)
        .build()
    )

    # Register a global error handler so transient errors (Conflict during
    # long-poll overlap, network blips) never crash the bot — they're just
    # logged and the event loop keeps running.
    async def _on_error(update, ctx):
        log.warning("handler error: %s", ctx.error)
    app.add_error_handler(_on_error)

    # /start, /help
    app.add_handler(CommandHandler("start", start.cmd_start))
    app.add_handler(CommandHandler("help", start.cmd_help))
    app.add_handler(CommandHandler("cancel", start.cmd_cancel))
    app.add_handler(CommandHandler("reset", start.cmd_reset))
    app.add_handler(CommandHandler("change_api", start.cmd_change_api))
    app.add_handler(CommandHandler("admin", admin.cmd_admin))

    # Fallback text handler — used for: API key capture, terminal stdin, files content
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu.text_router))

    # Document upload for files handler
    app.add_handler(MessageHandler(filters.Document.ALL, files.handle_document))

    # Central callback router
    app.add_handler(CallbackQueryHandler(menu.callback_router))

    return app


def main() -> None:
    # Mirror logs to stdout (if attached) plus a rotating log file
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        BASE_DIR / "bot.log",
        maxBytes=2_000_000,
        backupCount=2,
        encoding="utf-8",
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, stream_handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)

    app = build_app()
    logging.info("HopX Telegram Bot starting…")
    # drop_pending_updates drains any buffered getUpdates so we don't
    # replay stale events after a restart. allowed_updates keeps traffic
    # minimal (no inline_query, no chat_member churn).
    # poll_interval=1.0 adds a 1s gap between each long-poll request so that
    # the previous HTTP connection has fully closed before the next opens —
    # this eliminates the periodic "Conflict: terminated by other getUpdates"
    # warnings caused by overlapping requests on Windows.
    app.run_polling(
        poll_interval=1.0,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
