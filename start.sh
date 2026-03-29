#!/bin/bash
# Start fakeitaliradio with go-librespot running independently
# go-librespot survives uvicorn restarts so Spotify stays connected

set -e
cd "$(dirname "$0")"
set -a
[ -f .env ] && source .env
set +a
mkdir -p tmp

# Resolve runtime settings from radio.toml + .env via the config helper
RUNTIME_JSON="$(python -m fakeitaliradio.config runtime-json 2>/dev/null || true)"
if [ -n "$RUNTIME_JSON" ]; then
    FIFO="$(echo "$RUNTIME_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["fifo_path"])')"
    GO_LIBRESPOT_BIN="$(echo "$RUNTIME_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["go_librespot_bin"])')"
    HOST="$(echo "$RUNTIME_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["bind_host"])')"
    PORT="$(echo "$RUNTIME_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["port"])')"
else
    echo "Warning: could not resolve runtime config, using defaults" >&2
    FIFO="${FAKEITALIRADIO_FIFO_PATH:-/tmp/fakeitaliradio.pcm}"
    GO_LIBRESPOT_BIN="${GO_LIBRESPOT_BIN:-go-librespot}"
    HOST="${FAKEITALIRADIO_BIND_HOST:-127.0.0.1}"
    PORT="${FAKEITALIRADIO_PORT:-8000}"
fi

DRAIN_PID_FILE="tmp/fifo-drain.pid"

# Ensure FIFO exists
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# Start go-librespot if not already running (tolerate missing binary)
if ! pgrep -f "go-librespot.*fakeitaliradio" > /dev/null 2>&1; then
    if [ -x "$GO_LIBRESPOT_BIN" ] || command -v "$GO_LIBRESPOT_BIN" > /dev/null 2>&1; then
        echo "Starting go-librespot..."
        "$GO_LIBRESPOT_BIN" \
            --config_dir go-librespot \
            > /dev/null 2>tmp/go-librespot.log &
        echo "go-librespot PID: $!"
        echo "Select 'fakeitaliradio' in your Spotify app"
    else
        echo "Warning: go-librespot not found at $GO_LIBRESPOT_BIN — running without Spotify" >&2
    fi
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
