@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist bot.pid (
    echo No PID file. Bot not running?
    exit /b 1
)

for /f %%i in (bot.pid) do (
    taskkill /F /PID %%i >nul 2>&1
)

del bot.pid >nul 2>&1
echo Bot stopped.
