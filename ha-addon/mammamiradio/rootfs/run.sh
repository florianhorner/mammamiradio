#!/usr/bin/env sh
# Home Assistant add-on entrypoint for mammamiradio
# Maps Supervisor environment and add-on options to app env vars.
set -e

echo "[mammamiradio] Starting add-on..."

# ---- Read add-on options from /data/options.json ----
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    for key in anthropic_api_key spotify_client_id spotify_client_secret \
               station_name claude_model playlist_spotify_url; do
        val=$(jq -r --arg k "$key" '.[$k] // empty' "$OPTIONS_FILE")
        if [ -n "$val" ]; then
            env_key=$(echo "$key" | tr '[:lower:]' '[:upper:]')
            export "$env_key=$val"
        fi
    done
fi

# ---- Map Supervisor token to HA_TOKEN ----
if [ -n "$SUPERVISOR_TOKEN" ]; then
    export HA_TOKEN="$SUPERVISOR_TOKEN"
    export HA_URL="http://supervisor/core/api"
    export HA_ENABLED="true"
    echo "[mammamiradio] Home Assistant API access configured via Supervisor"
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
mkdir -p /data/cache /data/music /data/tmp

echo "[mammamiradio] Station: ${STATION_NAME:-Radio Italì}"

# ---- Validate config before launching ----
cd /app
echo "[mammamiradio] Validating configuration..."
if ! python3 -c "from mammamiradio.config import load_config; load_config()"; then
    echo "[mammamiradio] ERROR: Configuration validation failed — check add-on logs above"
    exit 1
fi

echo "[mammamiradio] Starting uvicorn on 0.0.0.0:8000..."
exec python3 -m uvicorn mammamiradio.main:app \
    --host 0.0.0.0 --port 8000
