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

json_field() {
    printf '%s' "$1" | "$PYTHON_BIN" -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$2"
}

fail_runtime() {
    echo "FATAL: could not resolve runtime config: $1" >&2
    exit 1
}

# Resolve runtime settings from radio.toml + .env via the config helper
RUNTIME_ERR="$(mktemp)"
if ! RUNTIME_JSON="$("$PYTHON_BIN" -m mammamiradio.config runtime-json 2>"$RUNTIME_ERR")"; then
    fail_runtime "$(cat "$RUNTIME_ERR")"
fi
rm -f "$RUNTIME_ERR"

if ! FIFO="$(json_field "$RUNTIME_JSON" "fifo_path" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include fifo_path"
fi
if ! GO_LIBRESPOT_BIN="$(json_field "$RUNTIME_JSON" "go_librespot_bin" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include go_librespot_bin"
fi
if ! GO_LIBRESPOT_CONFIG_DIR="$(json_field "$RUNTIME_JSON" "go_librespot_config_dir" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include go_librespot_config_dir"
fi
if ! GO_LIBRESPOT_PORT="$(json_field "$RUNTIME_JSON" "go_librespot_port" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include go_librespot_port"
fi
if ! TMP_DIR="$(json_field "$RUNTIME_JSON" "tmp_dir" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include tmp_dir"
fi
if ! HOST="$(json_field "$RUNTIME_JSON" "bind_host" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include bind_host"
fi
if ! PORT="$(json_field "$RUNTIME_JSON" "port" 2>/dev/null)"; then
    fail_runtime "runtime-json did not include port"
fi

RUNTIME_ERR="$(mktemp)"
if ! GO_LIBRESPOT_RUNTIME_JSON="$("$PYTHON_BIN" -m mammamiradio.go_librespot_runtime describe \
    "$GO_LIBRESPOT_BIN" \
    "$GO_LIBRESPOT_CONFIG_DIR" \
    "$FIFO" \
    "$GO_LIBRESPOT_PORT" \
    "$TMP_DIR" 2>"$RUNTIME_ERR")"; then
    fail_runtime "$(cat "$RUNTIME_ERR")"
fi
rm -f "$RUNTIME_ERR"

if ! GO_LIBRESPOT_CONFIG_DIR="$(json_field "$GO_LIBRESPOT_RUNTIME_JSON" "config_dir" 2>/dev/null)"; then
    fail_runtime "go-librespot runtime did not include config_dir"
fi
if ! FIFO="$(json_field "$GO_LIBRESPOT_RUNTIME_JSON" "fifo_path" 2>/dev/null)"; then
    fail_runtime "go-librespot runtime did not include fifo_path"
fi
if ! TMP_DIR="$(json_field "$GO_LIBRESPOT_RUNTIME_JSON" "tmp_dir" 2>/dev/null)"; then
    fail_runtime "go-librespot runtime did not include tmp_dir"
fi
if ! GO_LIBRESPOT_FINGERPRINT="$(json_field "$GO_LIBRESPOT_RUNTIME_JSON" "fingerprint" 2>/dev/null)"; then
    fail_runtime "go-librespot runtime did not include fingerprint"
fi
if ! GO_LIBRESPOT_STATE_FILE="$(json_field "$GO_LIBRESPOT_RUNTIME_JSON" "state_file" 2>/dev/null)"; then
    fail_runtime "go-librespot runtime did not include state_file"
fi

mkdir -p "$TMP_DIR"
DRAIN_PID_FILE="$TMP_DIR/fifo-drain.pid"
GO_LIBRESPOT_LOG="$TMP_DIR/go-librespot.log"

echo "Using go-librespot config dir: $GO_LIBRESPOT_CONFIG_DIR"

# Ensure FIFO exists
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# Start go-librespot if not already running (tolerate missing binary)
golibrespot_pid="$("$PYTHON_BIN" -m mammamiradio.go_librespot_runtime owned-pid \
    "$GO_LIBRESPOT_STATE_FILE" \
    "$GO_LIBRESPOT_FINGERPRINT" 2>/dev/null || true)"
if [ -z "$golibrespot_pid" ]; then
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
    echo "go-librespot already running ($golibrespot_pid)"
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
