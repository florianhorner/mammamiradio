#!/bin/bash
# Start mammamiradio with go-librespot running independently
# go-librespot survives uvicorn restarts so Spotify stays connected

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

# Resolve all runtime settings in a single Python invocation (avoids 16 separate spawns)
STARTUP_ERR="$(mktemp)"
if ! STARTUP_ENV="$("$PYTHON_BIN" -m mammamiradio.config startup-env 2>"$STARTUP_ERR")"; then
    echo "FATAL: could not resolve runtime config: $(cat "$STARTUP_ERR")" >&2
    rm -f "$STARTUP_ERR"
    exit 1
fi
rm -f "$STARTUP_ERR"
eval "$STARTUP_ENV"

mkdir -p "$TMP_DIR"
DRAIN_PID_FILE="$TMP_DIR/fifo-drain.pid"
GO_LIBRESPOT_LOG="$TMP_DIR/go-librespot.log"

echo "Using go-librespot config dir: $GO_LIBRESPOT_CONFIG_DIR"

# Ensure FIFO exists
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# Start go-librespot if not already running (tolerate missing binary)
if [ -z "$GOLIBRESPOT_OWNED_PID" ]; then
    if [ -x "$GO_LIBRESPOT_BIN" ] || command -v "$GO_LIBRESPOT_BIN" > /dev/null 2>&1; then
        echo "Starting go-librespot..."
        "$GO_LIBRESPOT_BIN" \
            --config_dir "$GO_LIBRESPOT_CONFIG_DIR" \
            > /dev/null 2>"$GO_LIBRESPOT_LOG" &
        GO_PID=$!
        "$PYTHON_BIN" -m mammamiradio.go_librespot_runtime claim \
            "$GO_LIBRESPOT_STATE_FILE" \
            "$GO_PID" \
            "$GO_LIBRESPOT_FINGERPRINT" \
            "$GO_LIBRESPOT_BIN" \
            "$GO_LIBRESPOT_CONFIG_DIR"
        echo "go-librespot PID: $GO_PID"
    else
        echo "Warning: go-librespot not found at $GO_LIBRESPOT_BIN — running without Spotify" >&2
    fi
else
    echo "go-librespot already running ($GOLIBRESPOT_OWNED_PID)"
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
exec python -m uvicorn mammamiradio.main:app \
    --host "$HOST" --port "$PORT" \
    --reload --reload-dir mammamiradio
