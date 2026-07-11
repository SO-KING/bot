#!/bin/bash
# start.sh — start the bot in background, write PID, log to bot.log
set -e

cd "$(dirname "$0")"

# Stop any previous instance
if [ -f "bot.pid" ]; then
  OLD=$(cat bot.pid)
  if kill -0 "$OLD" 2>/dev/null; then
    echo "Stopping previous instance (PID $OLD)..."
    kill "$OLD"
    sleep 2
  fi
  rm -f bot.pid
fi

# Validate .env
if [ ! -f ".env" ]; then
  echo "❌ .env not found. Run ./setup.sh first."
  exit 1
fi

# Start bot detached
echo "Starting HopX Bot..."
nohup ./venv/bin/python main.py > bot.stdout.log 2> bot.stderr.log < /dev/null &
PID=$!
echo $PID > bot.pid
disown

sleep 3
if kill -0 "$PID" 2>/dev/null; then
  echo "✅ Bot started (PID $PID). Live logs:"
  echo "   tail -f bot.log            (rotating log file)"
  echo "   tail -f bot.stdout.log     (stdout)"
  echo "   ./stop.sh                  (stop the bot)"
else
  echo "❌ Bot crashed on startup. Last output:"
  tail -20 bot.stderr.log bot.stdout.log 2>/dev/null
  exit 1
fi
