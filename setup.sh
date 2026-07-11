#!/bin/bash
# setup.sh — works on Ubuntu/Debian VPS out of the box
set -e

echo "=== HopX Bot — one-time VPS setup ==="

# 1. Install Python 3.11+ if missing
if ! command -v python3.11 >/dev/null 2>&1; then
  echo "Installing Python 3.11..."
  sudo apt-get update -qq
  sudo apt-get install -y -qq software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip
fi

cd "$(dirname "$0")"

# 2. Create virtualenv if missing
if [ ! -d "venv" ]; then
  echo "Creating venv..."
  python3 -m venv venv
fi

# 3. Install requirements
echo "Installing dependencies..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

# 4. Create .env if missing
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "=========================================="
  echo "  ⚠️  EDIT .env BEFORE STARTING THE BOT  "
  echo "=========================================="
  echo "Set:"
  echo "  TELEGRAM_BOT_TOKEN=<your bot token>"
  echo "  FERNET_KEY=<generate with: ./venv/bin/python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\">"
  echo ""
fi

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. nano .env        (set TELEGRAM_BOT_TOKEN and FERNET_KEY)"
echo "  2. ./start.sh       (start the bot in background)"
echo "  3. tail -f bot.log  (see live output)"
