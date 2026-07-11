# HopX Telegram Bot — Project Guide

Telegram bot acting as a bridge to HopX sandboxes. Each user binds their own HopX API key, the bot provisions a sandbox, and gives full control via Telegram UI: file management, interactive terminal, background processes.

## Tech stack
- **Python 3.11+** (CPython)
- **python-telegram-bot v22.x** (fully async, asyncio) — installed with `[ext]` extras for job-queue, callback-data, rate-limiter
- **hopx-ai** SDK (sync API wrapped in async via `asyncio.to_thread`) — for control plane operations (sandbox create/kill/pause/resume)
- Raw HTTP via `httpx` for VM Agent API calls (sync, called from thread) — for execute, files, terminal WebSocket, processes
- **websockets** library for interactive WebSocket terminal sessions
- **SQLite** (`sqlite3`) for per-user state (api_key encrypted, sandbox_id, terminal sessions)
- **cryptography** (Fernet) to encrypt stored API keys at rest

## Architecture

```
C:\hopx-bot\
├── AGENTS.md                  # this file
├── README.md                  # user docs (Arabic)
├── requirements.txt
├── .env.example
├── .env                       # secrets (gitignored)
├── main.py                    # entrypoint — launches Application.run_polling()
├── config.py                  # load env, define constants (paths, timeouts)
├── storage.py                 # SQLite schema + UserStore + state helpers
├── crypto.py                  # Fernet encrypt/decrypt for API keys
├── hopx_client.py             # HopX SDK wrapper + VM Agent HTTP client (sync, wrapped async)
├── bot.py                     # Application builder: handlers, conversation, menus
├── keyboards.py               # ReplyMarkup factories (main menu, files, terminal)
├── ui.py                      # UI helpers: log bar under messages, ANSI rendering, pagination
├── handlers/
│   ├── __init__.py
│   ├── start.py               # /start, ask for API key, validate, store
│   ├── menu.py                # main menu, refresh, back button
│   ├── sandbox.py             # provision, status, kill, refresh timeout
│   ├── terminal.py            # interactive WebSocket terminal + ANSI rendering + stdin
│   ├── files.py               # list/upload/download/delete/mkdir
│   └── processes.py           # list running/background processes, kill by id
└── utils/
    ├── __init__.py
    ├── ansi.py                # ANSI escape parser → HTML for Telegram
    └── format.py              # size formatting, time delta, sanitize
```

## User flow

1. User sends `/start` → bot greets in Arabic, asks for HopX API key (format `hopx_live_...`).
2. User sends the key (kept private via Telegram's privacy mode).
3. Bot validates by calling `Sandbox.list(limit=1)` via HopX SDK.
   - Invalid → ask again with hint.
   - Valid → encrypt and store in SQLite, show main menu.
4. User taps "إنشاء سيرفر" → bot calls `Sandbox.create(template="code-interpreter", timeout_seconds=3600)` using the user's API key, stores the sandbox_id, reports success/failure with a small log bar.
5. Once sandbox is `running`, the menu unlocks the controls:
   - **الترمنال** — opens an interactive WebSocket terminal; user types commands, output is streamed back with ANSI color rendering (parsed to Telegram HTML). Bot shows a prompt indicator and a "إدخال" mode for prompts requiring input. Stateful session (cd, exports, history persist).
   - **إدارة الملفات** — list `/workspace`, navigate folders, upload file (forward a Telegram document → bot uploads via HopX files API), download (bot fetches file and sends as document), delete (with confirm), mkdir.
   - **العمليات** — list system processes (max 200) and background executions; tap a process to kill it.
   - **حالة السيرفر** — show resources, uptime, expires_at; buttons: pause / resume / refresh timeout / kill sandbox.
6. Every bot message ends with a small log footer line: `⚙️ hopx • sandbox_abc123 • running • 3600s left` plus a "🏠 الرئيسية" inline button if not already on the main menu.

## HopX integration notes

### Sandbox lifecycle (Control Plane via hopx-ai SDK)
- Sync API is wrapped with `await asyncio.to_thread(...)` to stay non-blocking.
- `Sandbox.create(template=..., api_key=user_key, timeout_seconds=3600)`
- `Sandbox.connect(sandbox_id, api_key=user_key)` — auto-resumes paused + refreshes JWT
- `sandbox.pause()` / `resume()` / `kill()` — all wrapped async
- `sandbox.set_timeout(seconds)` — extend from now (absolute, not relative)

### VM Agent calls (per sandbox, JWT auth)
JWT comes from `Sandbox.connect()` response (`auth_token`). Stored in memory per session.
- `POST /execute` (sync code, max 300s) — used for short checks
- `POST /commands/run` (sync shell, default 30s but bumped to 300s)
- `POST /commands/background` — for long-running services the user wants to keep alive
- `POST /files/read` / `POST /files/write` / `GET /files/list` / `DELETE /files/remove` / `POST /files/mkdir`
- `GET /processes` (or `/processes/system`) — list system processes (max 200)
- `GET /execute/processes` — list background executions
- `DELETE /execute/kill?process_id=...`

### Interactive terminal (WebSocket, stateful)
We open a WebSocket to the sandbox's terminal endpoint (`wss://{sandbox_id}.hopx.dev/terminal`). HopX docs:
- `terminal.connect(timeout=30)` returns a WebSocket
- `terminal.send_input(ws, "command\n")` — `\n` executes
- `terminal.resize(ws, cols=80, rows=24)`
- `async for msg in terminal.iter_output(ws)`: `{"type":"stdout","data":"..."}` or `{"type":"done",...}`

We maintain a per-user `TerminalSession` (user_id → ws). Each user message in terminal mode is wrapped in `await ws.send_input(text + "\n")` and we accumulate the output until `done` or a short idle window (300ms). Output is then parsed (ANSI → HTML) and sent as a Telegram HTML message with `disable_web_page_preview=True`. When output exceeds 4096 chars, it's chunked.

If a process waits for input (e.g. `read` command, `sudo`, password prompt), HopX streams the prompt as stdout and waits — we detect by "no done event within 1.0s and command likely expects input" heuristics, then we keep the session in "stdin mode" where the next user message is sent WITHOUT trailing `\n` if they explicitly tapped the "إدخال" button, otherwise with `\n`.

### Path restrictions
Only `/workspace` and `/tmp` are writable/readable by the files API. We default to `/workspace` and prevent any attempt outside.

## Database schema (SQLite)

```sql
CREATE TABLE users (
  user_id      INTEGER PRIMARY KEY,
  api_key_enc  TEXT NOT NULL,           -- Fernet-encrypted HopX API key
  sandbox_id   TEXT,                    -- current sandbox id (NULL if none)
  sandbox_jwt  TEXT,                    -- current JWT (cleared on kill)
  sandbox_exp  TEXT,                    -- sandbox expires_at ISO8601
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE terminal_sessions (
  user_id      INTEGER PRIMARY KEY,
  ws_active    INTEGER DEFAULT 0,        -- 0/1 marker; actual WebSocket held in memory
  cwd_hint     TEXT DEFAULT '/workspace',
  started_at   TEXT
);
```

## Secrets handling
- BOT_TOKEN from `TELEGRAM_BOT_TOKEN` env
- Fernet key from `FERNET_KEY` env — generate once: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- User's HopX API key encrypted with Fernet before storing in DB; decrypted in memory only when calling HopX
- `.env` is gitignored; never committed
- Bot's privacy mode ENABLED (set via @BotFather) so user messages (API keys, password prompts) aren't visible to other chat members

## Logging bar under each message
Format:
```
⚙️ hopx • {sandbox_id_short} • {status} • {time_left}
```
With a "🏠 الرئيسية" button when not on the main menu. Time left is computed from `sandbox_exp - now` and refreshed on each interaction.

## How to run

```bash
cd C:\hopx-bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env with TELEGRAM_BOT_TOKEN and FERNET_KEY
python main.py
```

## Lint / typecheck
- `ruff check .` if ruff is installed
- No tests yet — manual validation against real HopX API and BotFather token.

## Security model
- All HopX operations execute against **the user's own sandbox**, never against the bot's host or other users' sandboxes.
- The bot host only proxies API calls using the user's encrypted key. A failed HopX call cannot affect the bot's host machine or other sandboxes.
- Per-user state isolated by `user_id` PRIMARY KEY in every handler.

### Verified absence of local-exec primitives
The project source was grep-audited for any local execution primitive and was confirmed clean:
- No `subprocess`, `os.system`, `os.popen`, `Popen`, or `shell=True` usage anywhere in the codebase.
- The only `import os` calls are in `config.py` (reading env vars) and `handlers/files.py` (`os.path.basename` / `os.path.join` / `os.remove` for the temporary local file that holds the bytes downloaded from the **sandbox** before forwarding to Telegram as a document — that file lives in the OS temp dir and is removed right after).
- All real command execution flows through one of:
  - `hopx_ai.Sandbox` (control plane + VM Agent via SDK)
  - `websockets.asyncio.client.connect` (raw WebSocket to the sandbox's `/terminal` endpoint)
- Therefore, anything the user types in the Telegram terminal is sent over the
  network to **their own** HopX sandbox and is never interpreted by the bot host.
- The bot itself only talks to:
  - `https://api.telegram.org` (Bot API)
  - `https://api.hopx.dev` (HopX control plane)
  - `wss://{sandbox_public_host}` (per-user sandbox VM agent)
