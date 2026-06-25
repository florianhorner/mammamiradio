#!/usr/bin/with-contenv sh
# shellcheck shell=sh
# Home Assistant add-on entrypoint for mammamiradio
# Maps Supervisor environment and add-on options to app env vars.
set -e

echo "[mammamiradio] Starting add-on..."

# ---- Read add-on options and provider secrets ----
OPTIONS_FILE="/data/options.json"
SECRETS_FILE="/config/secrets.env"
if [ -f "$OPTIONS_FILE" ] || [ -f "$SECRETS_FILE" ]; then
    OPTS_LOG="/tmp/opts-parse.log"
    if ! OPTS_EXPORT=$(python3 -c "
import json, shlex, sys
from pathlib import Path

PROVIDER_OPTION_ENV = {
    'anthropic_api_key': 'ANTHROPIC_API_KEY',
    'openai_api_key': 'OPENAI_API_KEY',
    'azure_speech_key': 'AZURE_SPEECH_KEY',
    'azure_speech_region': 'AZURE_SPEECH_REGION',
    'elevenlabs_api_key': 'ELEVENLABS_API_KEY',
}
PROVIDER_ENV_KEYS = set(PROVIDER_OPTION_ENV.values())

options_path = Path('$OPTIONS_FILE')
if options_path.exists():
    try:
        opts = json.loads(options_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f'FATAL: corrupt options.json: {e}', file=sys.stderr)
        sys.exit(1)
else:
    opts = {}

exports = {}
for key, env_key in PROVIDER_OPTION_ENV.items():
    val = opts.get(key, '')
    if val:
        exports[env_key] = str(val)
for key in ('station_name', 'admin_token', 'jamendo_client_id'):
    val = opts.get(key, '')
    if val:
        exports[key.upper()] = str(val)
# Quality dial → model profile. Missing/blank defaults to 'balanced'. Existing
# add-ons may still have the removed claude_model option in /data/options.json;
# keep honoring it as the legacy fast-role override until the operator saves the
# new quality_profile option.
quality = opts.get('quality_profile') or 'balanced'
exports['MAMMAMIRADIO_QUALITY'] = str(quality)
legacy_claude_model = opts.get('claude_model') if not opts.get('quality_profile') else ''
if legacy_claude_model:
    exports['CLAUDE_MODEL'] = str(legacy_claude_model)
enabled = opts.get('enable_home_assistant', True)
ha_val = 'true' if enabled else 'false'
exports['HA_ENABLED'] = ha_val
super_italian = opts.get('super_italian_mode', False)
si_val = 'true' if super_italian else 'false'
exports['MAMMAMIRADIO_SUPER_ITALIAN'] = si_val
chaos = opts.get('chaos_mode_active', False)
chaos_val = 'true' if chaos else 'false'
exports['MAMMAMIRADIO_CHAOS_MODE'] = chaos_val
festival = opts.get('festival_mode', False)
festival_val = 'true' if festival else 'false'
exports['MAMMAMIRADIO_FESTIVAL_MODE'] = festival_val
broadcast_chain = opts.get('broadcast_chain', False)
bc_val = 'true' if broadcast_chain else 'false'
exports['MAMMAMIRADIO_BROADCAST_CHAIN'] = bc_val
media_player_push = opts.get('ha_media_player_push', True)
mpp_val = 'true' if media_player_push else 'false'
exports['MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH'] = mpp_val

secrets_path = Path('$SECRETS_FILE')
if secrets_path.exists():
    try:
        for raw_line in secrets_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                parsed = shlex.split(line, comments=False, posix=True)
            except ValueError as e:
                # One bad line must not discard every other export (incl. the
                # options.json-derived station_name/toggles). Skip it like
                # config.py does, keeping the good keys and the options.
                print(f'WARNING: skipping corrupt secrets.env line: {e}', file=sys.stderr)
                continue
            if len(parsed) != 1 or '=' not in parsed[0]:
                print(f'WARNING: skipping invalid secrets.env line: {raw_line}', file=sys.stderr)
                continue
            key, value = parsed[0].split('=', 1)
            key = key.strip()
            if key in PROVIDER_ENV_KEYS and value:
                exports[key] = value
    except OSError as e:
        print(f'WARNING: cannot read secrets.env, skipping: {e}', file=sys.stderr)

for env_key, value in exports.items():
    print(f'export {env_key}={shlex.quote(str(value))}')
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
