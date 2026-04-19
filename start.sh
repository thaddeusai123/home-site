#!/bin/bash
# start.sh — Home Website launcher.
# Waitress on 0.0.0.0:8080. Nginx on the Pi proxies 80/443 → 8080.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

PORT="${HOMESITE_PORT:-8080}"
HOST="${HOMESITE_HOST:-0.0.0.0}"

if [ ! -d venv ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Creating virtualenv..."
    python3 -m venv venv
fi

STAMP_FILE="$ROOT_DIR/.deps.stamp"
REQ_FILE="$ROOT_DIR/requirements.txt"
if [ ! -f "$STAMP_FILE" ] || [ "$REQ_FILE" -nt "$STAMP_FILE" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Installing dependencies..."
    venv/bin/pip install -q -r "$REQ_FILE"
    touch "$STAMP_FILE"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Home Website on $HOST:$PORT"
exec venv/bin/waitress-serve --host="$HOST" --port="$PORT" --threads=8 app:app
