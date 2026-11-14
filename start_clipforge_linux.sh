#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/backend"
cd "$BACKEND"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install Python 3.10 or newer, then run this script again."
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "FFmpeg and FFprobe are required for local clip rendering."
  echo "On Debian/Ubuntu/ChromeOS Linux, run: sudo apt update && sudo apt install -y ffmpeg"
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
fi
if ! grep -q '^APP_ENCRYPTION_KEY=.' .env; then
  KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  if grep -q '^APP_ENCRYPTION_KEY=' .env; then
    sed -i "s|^APP_ENCRYPTION_KEY=.*|APP_ENCRYPTION_KEY=$KEY|" .env
  else
    printf '\nAPP_ENCRYPTION_KEY=%s\n' "$KEY" >> .env
  fi
  echo "Generated a private encryption key in backend/.env. Back up this file securely."
fi

echo "Starting ClipForge AI at http://127.0.0.1:8787"
echo "Keep this terminal open while using the Chrome extension. Press Ctrl+C to stop."
exec python -m uvicorn main:app --host 127.0.0.1 --port 8787
