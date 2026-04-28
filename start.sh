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
RT_JSON="$("$PYTHON_BIN" -m mammamiradio.core.config runtime-json)"
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

# Start app with optional stream-alive reload proxy
source .venv/bin/activate

if command -v caddy > /dev/null 2>&1; then
    INTERNAL_PORT=$((PORT + 1))
    INTERNAL_HOST="127.0.0.1"
    CADDYFILE=$(mktemp /tmp/mammamiradio-Caddyfile-XXXXXX)
    cat > "$CADDYFILE" <<EOF
{
    admin off
}
$HOST:$PORT {
    reverse_proxy $INTERNAL_HOST:$INTERNAL_PORT {
        flush_interval -1
        transport http {
            read_body_timeout 0
        }
        lb_try_duration 30s
        lb_try_interval 500ms
    }
}
EOF

    CADDY_PID=""
    UVICORN_PID=""
    cleanup() {
        [ -n "$CADDY_PID" ] && kill "$CADDY_PID" 2>/dev/null || true
        [ -n "$UVICORN_PID" ] && kill "$UVICORN_PID" 2>/dev/null || true
        rm -f "$CADDYFILE"
        wait 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM

    if command -v lsof > /dev/null 2>&1; then
        STALE_INTERNAL="$(lsof -ti :"$INTERNAL_PORT" 2>/dev/null | head -1 || true)"
        if [ -n "$STALE_INTERNAL" ]; then
            STALE_CMD="$(ps -p "$STALE_INTERNAL" -o command= 2>/dev/null || true)"
            if ! echo "$STALE_CMD" | grep -q 'mammamiradio.main:app'; then
                echo "ERROR: Port $INTERNAL_PORT is used by non-mammamiradio PID $STALE_INTERNAL; refusing to kill." >&2
                exit 1
            fi
            echo "WARNING: Port $INTERNAL_PORT held by PID $STALE_INTERNAL — reclaiming..." >&2
            kill -TERM "$STALE_INTERNAL" 2>/dev/null || true
            sleep 1
            kill -0 "$STALE_INTERNAL" 2>/dev/null && kill -KILL "$STALE_INTERNAL" 2>/dev/null || true
        fi
    fi

    echo "Starting mammamiradio with stream-alive proxy (caddy)..."
    echo "  caddy   → :$PORT  (listeners connect here)"
    echo "  uvicorn → :$INTERNAL_PORT  (hot-reload backend)"

    python -m uvicorn mammamiradio.main:app \
        --host "$INTERNAL_HOST" --port "$INTERNAL_PORT" \
        --reload --reload-dir mammamiradio \
        --reload-include "*.toml" --reload-include "*.html" &
    UVICORN_PID=$!

    caddy run --config "$CADDYFILE" --adapter caddyfile &
    CADDY_PID=$!

    sleep 1
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[mammamiradio] WARNING: caddy failed to start — falling back to bare uvicorn on :$PORT" >&2
        kill "$UVICORN_PID" 2>/dev/null || true
        wait "$UVICORN_PID" 2>/dev/null || true
        CADDY_PID=""
        UVICORN_PID=""
        exec python -m uvicorn mammamiradio.main:app \
            --host "$HOST" --port "$PORT" \
            --reload --reload-dir mammamiradio \
            --reload-include "*.toml" --reload-include "*.html"
    fi

    while kill -0 "$UVICORN_PID" 2>/dev/null && kill -0 "$CADDY_PID" 2>/dev/null; do
        sleep 1
    done
    if ! kill -0 "$CADDY_PID" 2>/dev/null; then
        echo "[mammamiradio] ERROR: caddy exited unexpectedly; stopping uvicorn." >&2
        kill "$UVICORN_PID" 2>/dev/null || true
        wait "$UVICORN_PID" 2>/dev/null || true
        exit 1
    fi
    wait "$UVICORN_PID"
else
    echo "" >&2
    echo "[mammamiradio] NOTE: Install caddy for stream-alive reload — active streams survive file saves." >&2
    echo "[mammamiradio]       brew install caddy   (macOS)   |   apt install caddy   (Ubuntu/Debian)" >&2
    echo "" >&2
    exec python -m uvicorn mammamiradio.main:app \
        --host "$HOST" --port "$PORT" \
        --reload --reload-dir mammamiradio \
        --reload-include "*.toml" --reload-include "*.html"
fi
