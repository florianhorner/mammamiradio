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
    OPTS_EXPORT=$(python3 -c "
import json, shlex, sys
try:
    with open('$OPTIONS_FILE') as f:
        opts = json.load(f)
except (json.JSONDecodeError, OSError) as e:
    print(f'[mammamiradio] FATAL: corrupt options.json: {e}', file=sys.stderr)
    sys.exit(1)
for key in ('anthropic_api_key', 'spotify_client_id', 'spotify_client_secret',
            'station_name', 'claude_model', 'playlist_spotify_url'):
    val = opts.get(key, '')
    if val:
        env_key = key.upper()
        print(f'export {env_key}={shlex.quote(str(val))}')
" 2>/dev/null)
    if [ $? -ne 0 ]; then
        echo "[mammamiradio] FATAL: Failed to parse options.json — check addon configuration"
        exit 1
    fi
    eval "$OPTS_EXPORT"
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

# ---- Ensure directories exist ----
mkdir -p /data/cache /data/music /data/tmp /data/go-librespot

# ---- Initialize go-librespot config (only on first install, preserves auth state) ----
if [ ! -f /data/go-librespot/config.yml ]; then
    cp /defaults/go-librespot-config.yml /data/go-librespot/config.yml
    echo "[mammamiradio] Initialized go-librespot config"
fi

# ---- Validate critical files exist ----
if [ ! -f /app/radio.toml ]; then
    echo "[mammamiradio] ERROR: /app/radio.toml not found — image may be corrupt"
    exit 1
fi

echo "[mammamiradio] Station: ${STATION_NAME:-Radio Italì}"
echo "[mammamiradio] Starting uvicorn on 0.0.0.0:8000..."

cd /app
exec python3 -m uvicorn mammamiradio.main:app \
    --host 0.0.0.0 --port 8000 --no-access-log
