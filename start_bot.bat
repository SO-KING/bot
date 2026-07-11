@echo off
chcp 65001 >nul
cd /d %~dp0

REM Setup: create venv and install deps on first run
if not exist "venv\Scripts\python.exe" (
    echo First run setup...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
)

REM Create .env if missing
if not exist ".env" (
    copy .env.example .env >nul
    echo.
    echo ==========================================
    echo    EDIT .env BEFORE STARTING THE BOT
    echo ==========================================
    echo  Set TELEGRAM_BOT_TOKEN and FERNET_KEY
)

REM Stop previous instance if any
if exist bot.pid (
    for /f %%i in (bot.pid) do (
        taskkill /F /PID %%i >nul 2>&1
    )
    del bot.pid >nul 2>&1
)

REM Start bot hidden
echo Starting HopX Bot...
start "" /B "venv\Scripts\pythonw.exe" main.py

REM Save PID after a moment
ping 127.0.0.1 -n 3 >nul
for /f %%j in ('wmic process where "name='pythonw.exe' and commandline like '%%main.py%%'" get processid ^| findstr /R [0-9]') do (
    echo %%j > bot.pid
)

echo Bot started. Logs: bot.log, bot.stdout.log
echo To stop: stop_bot.bat
