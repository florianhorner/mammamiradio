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

fail_runtime() {
    echo "FATAL: $1" >&2
    exit 1
}

run_python_or_fail() {
    local err_file output
    err_file="$(mktemp)"
    if ! output="$("$PYTHON_BIN" "$@" 2>"$err_file")"; then
        fail_runtime "$(cat "$err_file")"
    fi
    rm -f "$err_file"
    printf '%s' "$output"
}

cleanup_claim_failure() {
    local pid="$1"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f "$GO_LIBRESPOT_STATE_FILE"
    fail_runtime "failed to claim go-librespot ownership"
}

# Resolve all runtime settings in a single Python invocation (avoids 16 separate spawns)
STARTUP_ENV="$(run_python_or_fail -m mammamiradio.config startup-env)"
eval "$STARTUP_ENV"

mkdir -p "$TMP_DIR"
DRAIN_PID_FILE="$TMP_DIR/fifo-drain.pid"
GO_LIBRESPOT_LOG="$TMP_DIR/go-librespot.log"

echo "Using go-librespot config dir: $GO_LIBRESPOT_CONFIG_DIR"

# Ensure FIFO exists
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# Sync go-librespot config (ensures device_name is correct)
"$PYTHON_BIN" -m mammamiradio.go_librespot_config sync \
    go-librespot/config.yml "$GO_LIBRESPOT_CONFIG_DIR/config.yml" 2>/dev/null || true

# Patch runtime overrides (FIFO path and API port) into the workspace config
_GL_CFG="$GO_LIBRESPOT_CONFIG_DIR/config.yml"
if [ -f "$_GL_CFG" ]; then
    sed -i '' "s|audio_output_pipe:.*|audio_output_pipe: $FIFO|" "$_GL_CFG"
    if [ -n "${GO_LIBRESPOT_PORT:-}" ]; then
        sed -i '' "s|port:.*|port: $GO_LIBRESPOT_PORT|" "$_GL_CFG"
    fi
fi

# Start go-librespot if not already running (tolerate missing binary)
if [ -z "$GOLIBRESPOT_OWNED_PID" ]; then
    if [ -x "$GO_LIBRESPOT_BIN" ] || command -v "$GO_LIBRESPOT_BIN" > /dev/null 2>&1; then
        echo "Starting go-librespot..."
        "$GO_LIBRESPOT_BIN" \
            --config_dir "$GO_LIBRESPOT_CONFIG_DIR" \
            > /dev/null 2>"$GO_LIBRESPOT_LOG" &
        GO_PID=$!
        if ! "$PYTHON_BIN" -m mammamiradio.go_librespot_runtime claim \
            "$GO_LIBRESPOT_STATE_FILE" \
            "$GO_PID" \
            "$GO_LIBRESPOT_FINGERPRINT" \
            "$GO_LIBRESPOT_BIN" \
            "$GO_LIBRESPOT_CONFIG_DIR"; then
            cleanup_claim_failure "$GO_PID"
        fi
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
    --reload --reload-dir mammamiradio \
    --reload-include "*.toml" --reload-include "*.html"
