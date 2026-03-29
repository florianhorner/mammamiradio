#!/bin/bash
# Start fakeitaliradio with go-librespot running independently
# go-librespot survives uvicorn restarts so Spotify stays connected

set -e
cd "$(dirname "$0")"
set -a
[ -f .env ] && source .env
set +a
mkdir -p tmp

# Ensure FIFO exists
FIFO="/tmp/fakeitaliradio.pcm"
DRAIN_PID_FILE="tmp/fifo-drain.pid"
HOST="${FAKEITALIRADIO_BIND_HOST:-127.0.0.1}"
PORT="${FAKEITALIRADIO_PORT:-8000}"
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# Start go-librespot if not already running
if ! pgrep -f "go-librespot.*fakeitaliradio" > /dev/null 2>&1; then
    echo "Starting go-librespot..."
    /opt/homebrew/opt/go-librespot/bin/go-librespot \
        --config_dir go-librespot \
        > /dev/null 2>tmp/go-librespot.log &
    echo "go-librespot PID: $!"
    echo "Select 'fakeitaliradio' in your Spotify app"
else
    echo "go-librespot already running ($(pgrep -f 'go-librespot.*fakeitaliradio'))"
fi

# Start fallback FIFO drain. The app will reclaim this on startup and restore it
# during reload/shutdown when attaching to an externally managed go-librespot.
drain_pid=""
if [ -f "$DRAIN_PID_FILE" ]; then
    drain_pid="$(cat "$DRAIN_PID_FILE" 2>/dev/null || true)"
fi
if [ -z "$drain_pid" ]; then
    drain_pid="$(pgrep -f "cat .*${FIFO}" | head -n 1 || true)"
fi
if [ -n "$drain_pid" ] && ps -p "$drain_pid" -o command= 2>/dev/null | grep -F "$FIFO" > /dev/null; then
    echo "$drain_pid" > "$DRAIN_PID_FILE"
    echo "FIFO drain already running ($drain_pid)"
else
    rm -f "$DRAIN_PID_FILE"
    cat "$FIFO" > /dev/null &
    echo "$!" > "$DRAIN_PID_FILE"
    echo "FIFO drain PID: $!"
fi

# Start uvicorn with reload (restarts on code changes, doesn't kill go-librespot)
echo "Starting uvicorn with --reload..."
source .venv/bin/activate
exec python -m uvicorn fakeitaliradio.main:app \
    --host "$HOST" --port "$PORT" \
    --reload --reload-dir fakeitaliradio
