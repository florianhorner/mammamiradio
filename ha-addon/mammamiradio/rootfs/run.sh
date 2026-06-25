#!/usr/bin/with-contenv sh
# shellcheck shell=sh
# Home Assistant add-on entrypoint for mammamiradio
# Maps Supervisor environment and add-on options to app env vars.
set -e

echo "[mammamiradio] Starting add-on..."

# ---- Read add-on options plus file-backed provider secrets ----
OPTIONS_FILE="/data/options.json"
SECRETS_FILE="/config/secrets.env"
if [ -f "$OPTIONS_FILE" ] || [ -f "$SECRETS_FILE" ]; then
    OPTS_LOG="/tmp/opts-parse.log"
    if ! OPTS_EXPORT=$(python3 -c "
import json, os, shlex, sys

opts = {}
if os.path.exists('$OPTIONS_FILE'):
    try:
        with open('$OPTIONS_FILE') as f:
            opts = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'WARNING: ignoring corrupt options.json: {e}', file=sys.stderr)
        opts = {}

provider_option_map = (
    ('anthropic_api_key', 'ANTHROPIC_API_KEY'),
    ('openai_api_key', 'OPENAI_API_KEY'),
    ('azure_speech_key', 'AZURE_SPEECH_KEY'),
    ('azure_speech_region', 'AZURE_SPEECH_REGION'),
    ('elevenlabs_api_key', 'ELEVENLABS_API_KEY'),
)
provider_values = {}
for key, env_key in provider_option_map:
    val = opts.get(key, '')
    if val:
        provider_values[env_key] = str(val)

def warning(message):
    print('echo ' + shlex.quote('[mammamiradio] WARNING: ' + message) + ' >&2')

def parse_secret_value(raw_value, line_no):
    value = raw_value.strip()
    if not value:
        return ''
    quote = value[0]
    if quote in (chr(34), chr(39)):
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            warning(f'secrets.env line {line_no} ignored: invalid quoting')
            return None
        if len(parts) != 1:
            warning(f'secrets.env line {line_no} ignored: invalid quoted value')
            return None
        return parts[0].strip()
    return value

secret_keys = tuple(env_key for _, env_key in provider_option_map)
if os.path.exists('$SECRETS_FILE'):
    try:
        with open('$SECRETS_FILE', encoding='utf-8') as secret_file:
            for line_no, raw_line in enumerate(secret_file, 1):
                line = raw_line.rstrip('\n').rstrip('\r')
                if line_no == 1:
                    line = line.lstrip('\ufeff')
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                if stripped.startswith('export '):
                    stripped = stripped[7:].lstrip()
                if '=' not in stripped:
                    warning(f'secrets.env line {line_no} ignored: missing KEY=VALUE')
                    continue
                key, raw_value = stripped.split('=', 1)
                key = key.strip()
                if key not in secret_keys:
                    warning(f'secrets.env line {line_no} ignored: unsupported key')
                    continue
                value = parse_secret_value(raw_value, line_no)
                if value is None:
                    continue
                if value:
                    provider_values[key] = value
    except OSError:
        warning('could not read /config/secrets.env')

for env_key in secret_keys:
    val = provider_values.get(env_key, '')
    if val:
        print(f'export {env_key}={shlex.quote(str(val))}')

for key in (
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
        echo "[mammamiradio] WARNING: Failed to parse add-on config, continuing with defaults"
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
