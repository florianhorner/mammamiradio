#!/usr/bin/with-contenv sh
# shellcheck shell=sh
# Home Assistant add-on entrypoint for mammamiradio
# Maps Supervisor environment and add-on options to app env vars.
set -e

echo "[mammamiradio] Starting add-on..."

# ---- Read add-on options from /data/options.json ----
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    OPTS_LOG="/tmp/opts-parse.log"
    if ! OPTS_EXPORT=$(python3 -c "
import json, shlex, sys
try:
    with open('$OPTIONS_FILE') as f:
        opts = json.load(f)
except (json.JSONDecodeError, OSError) as e:
    print(f'FATAL: corrupt options.json: {e}', file=sys.stderr)
    sys.exit(1)
for key in (
    'anthropic_api_key',
    'openai_api_key',
    'azure_speech_key',
    'azure_speech_region',
    'elevenlabs_api_key',
    'station_name',
    'admin_token',
    'jamendo_client_id',
):
    val = opts.get(key, '')
    if val:
        env_key = key.upper()
        print(f'export {env_key}={shlex.quote(str(val))}')
# Quality dial → model profile. Missing/blank defaults to 'balanced'. Existing
# add-ons may still have the removed claude_model option in /data/options.json;
# keep honoring it as the legacy fast-role override until the operator saves the
# new quality_profile option.
quality = opts.get('quality_profile') or 'balanced'
print('export MAMMAMIRADIO_QUALITY=' + shlex.quote(str(quality)))
legacy_claude_model = opts.get('claude_model') if not opts.get('quality_profile') else ''
if legacy_claude_model:
    print('export CLAUDE_MODEL=' + shlex.quote(str(legacy_claude_model)))
enabled = opts.get('enable_home_assistant', True)
ha_val = 'true' if enabled else 'false'
print('export HA_ENABLED=' + ha_val)
super_italian = opts.get('super_italian_mode', False)
si_val = 'true' if super_italian else 'false'
print('export MAMMAMIRADIO_SUPER_ITALIAN=' + si_val)
chaos = opts.get('chaos_mode_active', False)
chaos_val = 'true' if chaos else 'false'
print('export MAMMAMIRADIO_CHAOS_MODE=' + chaos_val)
festival = opts.get('festival_mode', False)
festival_val = 'true' if festival else 'false'
print('export MAMMAMIRADIO_FESTIVAL_MODE=' + festival_val)
broadcast_chain = opts.get('broadcast_chain', False)
bc_val = 'true' if broadcast_chain else 'false'
print('export MAMMAMIRADIO_BROADCAST_CHAIN=' + bc_val)
media_player_push = opts.get('ha_media_player_push', True)
mpp_val = 'true' if media_player_push else 'false'
print('export MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH=' + mpp_val)
" 2>"$OPTS_LOG"); then
        echo "[mammamiradio] WARNING: Failed to parse options.json, continuing with defaults"
        cat "$OPTS_LOG" 2>/dev/null
    else
        eval "$OPTS_EXPORT"
    fi
fi

# ---- Map Supervisor token to HA_TOKEN ----
if [ "${HA_ENABLED:-true}" != "false" ]; then
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
else
    unset HA_TOKEN
    unset HA_URL
    echo "[mammamiradio] Home Assistant integration disabled by add-on option"
fi

# ---- Enable yt-dlp as primary music source ----
export MAMMAMIRADIO_ALLOW_YTDLP="true"

# ---- Enable provenance ledger (records per-segment production data to cache/ledger/) ----
export MAMMAMIRADIO_LEDGER_ENABLED="true"

# ---- Bind to all interfaces (required for ingress) ----
export MAMMAMIRADIO_BIND_HOST="0.0.0.0"
export MAMMAMIRADIO_PORT="8000"

# ---- Point cache/tmp at persistent /data ----
export MAMMAMIRADIO_CACHE_DIR="/data/cache"
export MAMMAMIRADIO_TMP_DIR="/data/tmp"

# ---- Ensure directories exist ----
if ! mkdir -p /data/cache /data/music /data/tmp 2>/tmp/mammamiradio-data-mkdir.err; then
    FALLBACK_BASE="/tmp/mammamiradio-data"
    echo "[mammamiradio] WARNING: /data is not writable ($(cat /tmp/mammamiradio-data-mkdir.err 2>/dev/null || echo unknown error))"
    echo "[mammamiradio] WARNING: Falling back to $FALLBACK_BASE (state will not persist across restarts)"
    export MAMMAMIRADIO_CACHE_DIR="$FALLBACK_BASE/cache"
    export MAMMAMIRADIO_TMP_DIR="$FALLBACK_BASE/tmp"
    mkdir -p "$MAMMAMIRADIO_CACHE_DIR" "$FALLBACK_BASE/music" "$MAMMAMIRADIO_TMP_DIR"
fi

# ---- Validate critical files exist ----
if [ ! -f /app/radio.toml ]; then
    echo "[mammamiradio] ERROR: /app/radio.toml not found — image may be corrupt"
    exit 1
fi

echo "[mammamiradio] Station: ${STATION_NAME:-Mamma Mi Radio}"
echo "[mammamiradio] Starting uvicorn on 0.0.0.0:8000..."

cd /app
exec python3 -m uvicorn mammamiradio.main:app \
    --host 0.0.0.0 --port 8000 --no-access-log
