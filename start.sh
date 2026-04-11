#!/bin/bash
# Start mammamiradio local dev server

set -e
cd "$(dirname "$0")"
set -a
[ -f .env ] && source .env
set +a

PYTHON_BIN=".venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    echo "FATAL: $PYTHON_BIN not found. Create the project virtualenv before running start.sh." >&2
    exit 1
fi

# Resolve runtime settings
RT_JSON="$("$PYTHON_BIN" -m mammamiradio.config runtime-json)"
HOST="$(echo "$RT_JSON" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["bind_host"])')"
PORT="$(echo "$RT_JSON" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["port"])')"

# Pre-flight: check if the port is already in use by a stale process
if command -v lsof > /dev/null 2>&1; then
    STALE_PID="$(lsof -ti :"$PORT" 2>/dev/null | head -1 || true)"
    if [ -n "$STALE_PID" ]; then
        echo "WARNING: Port $PORT held by PID $STALE_PID — reclaiming..." >&2
        kill -TERM "$STALE_PID" 2>/dev/null || true
        sleep 1
        kill -0 "$STALE_PID" 2>/dev/null && kill -KILL "$STALE_PID" 2>/dev/null || true
    fi
fi

# Start uvicorn with reload
echo "Starting uvicorn with --reload..."
source .venv/bin/activate
exec python -m uvicorn mammamiradio.main:app \
    --host "$HOST" --port "$PORT" \
    --reload --reload-dir mammamiradio \
    --reload-include "*.toml" --reload-include "*.html"
