#!/usr/bin/env sh
# Home Assistant add-on entrypoint for mammamiradio
# Maps Supervisor environment and add-on options to app env vars.
set -e

echo "[mammamiradio] Starting add-on..."

# ---- Read add-on options from /data/options.json ----
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    # Extract options using Python (always available in the image)
    # Uses shlex.quote to prevent shell injection from user-provided values
    # stderr goes to log file (NOT into eval'd variable) to prevent injection
    OPTS_LOG="/tmp/opts-parse.log"
    if ! OPTS_EXPORT=$(python3 -c "
import json, shlex, sys
try:
    with open('$OPTIONS_FILE') as f:
        opts = json.load(f)
except (json.JSONDecodeError, OSError) as e:
    print(f'FATAL: corrupt options.json: {e}', file=sys.stderr)
    sys.exit(1)
for key in ('anthropic_api_key', 'openai_api_key', 'spotify_client_id', 'spotify_client_secret',
            'station_name', 'claude_model', 'playlist_spotify_url'):
    val = opts.get(key, '')
    if val:
        env_key = key.upper()
        print(f'export {env_key}={shlex.quote(str(val))}')
" 2>"$OPTS_LOG"); then
        echo "[mammamiradio] WARNING: Failed to parse options.json, continuing with defaults"
        cat "$OPTS_LOG" 2>/dev/null
    else
        eval "$OPTS_EXPORT"
    fi
fi

# ---- Map Supervisor token to HA_TOKEN ----
# Keep SUPERVISOR_TOKEN so _is_addon() detects addon mode
# Note: HA_URL must NOT include /api — ha_context.py appends it
if [ -n "$SUPERVISOR_TOKEN" ]; then
    export HA_TOKEN="$SUPERVISOR_TOKEN"
    export HA_URL="http://supervisor/core"
    export HA_ENABLED="true"
    echo "[mammamiradio] Home Assistant API access configured via Supervisor"
elif [ -n "$HASSIO_TOKEN" ]; then
    export HA_TOKEN="$HASSIO_TOKEN"
    export HA_URL="http://supervisor/core"
    export HA_ENABLED="true"
    echo "[mammamiradio] Home Assistant API access configured via Supervisor (legacy token)"
fi

# ---- Bind to all interfaces (required for ingress) ----
export MAMMAMIRADIO_BIND_HOST="0.0.0.0"
export MAMMAMIRADIO_PORT="8000"

# ---- Auto-generate ADMIN_TOKEN for non-loopback bind ----
if [ -z "$ADMIN_TOKEN" ]; then
    export ADMIN_TOKEN="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
    echo "[mammamiradio] Auto-generated ADMIN_TOKEN for non-loopback bind"
fi

# ---- Point cache/tmp at persistent /data ----
export MAMMAMIRADIO_CACHE_DIR="/data/cache"
export MAMMAMIRADIO_TMP_DIR="/data/tmp"
export MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR="/data/go-librespot"

# ---- Ensure directories exist ----
if ! mkdir -p /data/cache /data/music /data/tmp /data/go-librespot 2>/tmp/mammamiradio-data-mkdir.err; then
    FALLBACK_BASE="/tmp/mammamiradio-data"
    echo "[mammamiradio] WARNING: /data is not writable ($(cat /tmp/mammamiradio-data-mkdir.err 2>/dev/null || echo unknown error))"
    echo "[mammamiradio] WARNING: Falling back to $FALLBACK_BASE (state will not persist across restarts)"
    export MAMMAMIRADIO_CACHE_DIR="$FALLBACK_BASE/cache"
    export MAMMAMIRADIO_TMP_DIR="$FALLBACK_BASE/tmp"
    export MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR="$FALLBACK_BASE/go-librespot"
    mkdir -p "$MAMMAMIRADIO_CACHE_DIR" "$FALLBACK_BASE/music" "$MAMMAMIRADIO_TMP_DIR" "$MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR"
fi

# ---- Initialize go-librespot config and keep device_name aligned with the shipped default ----
SYNC_MSG="$(python3 -m mammamiradio.go_librespot_config sync \
    /defaults/go-librespot-config.yml \
    "$MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR/config.yml" 2>&1)" || {
    echo "[mammamiradio] ERROR: go-librespot config sync failed: $SYNC_MSG"
    exit 1
}
echo "[mammamiradio] $SYNC_MSG"

# ---- Validate critical files exist ----
if [ ! -f /app/radio.toml ]; then
    echo "[mammamiradio] ERROR: /app/radio.toml not found — image may be corrupt"
    exit 1
fi

echo "[mammamiradio] Station: ${STATION_NAME:-Malamie Radio}"
echo "[mammamiradio] Starting uvicorn on 0.0.0.0:8000..."

cd /app
exec python3 -m uvicorn mammamiradio.main:app \
    --host 0.0.0.0 --port 8000 --no-access-log
