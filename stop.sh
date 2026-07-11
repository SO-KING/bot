#!/bin/bash
# stop.sh — stop the running bot
cd "$(dirname "$0")"

if [ ! -f "bot.pid" ]; then
  echo "No PID file. Bot not running?"
  exit 1
fi

PID=$(cat bot.pid)
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "✅ Bot stopped (PID $PID)."
else
  echo "PID $PID not running."
fi
rm -f bot.pid
