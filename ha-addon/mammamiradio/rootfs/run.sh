#!/usr/bin/with-contenv sh
# shellcheck shell=sh
# Home Assistant add-on entrypoint for mammamiradio
# Maps Supervisor environment and add-on options to app env vars.
set -e

echo "[mammamiradio] Starting add-on..."

# ---- Read add-on options plus file-backed provider secrets ----
OPTIONS_FILE="/data/options.json"
SECRETS_FILE="/config/secrets.env"
SUPERVISOR_API="${SUPERVISOR_API:-http://supervisor}"
RECOVERY_MARKER_FILE="${RECOVERY_MARKER_FILE:-/data/.provider_recovery_checked}"
if [ -f "$OPTIONS_FILE" ] || [ -f "$SECRETS_FILE" ]; then
    OPTS_LOG="/tmp/opts-parse.log"
    if ! OPTS_EXPORT=$(python3 -c "
import json, os, shlex, sys, tempfile

opts = {}
if os.path.exists('$OPTIONS_FILE'):
    try:
        with open('$OPTIONS_FILE') as f:
            opts = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f'WARNING: ignoring corrupt options.json: {e}', file=sys.stderr)
        opts = {}

# Provider key fields were removed from the add-on schema (secrets moved to
# /config/secrets.env). Supervisor validates stored options against the new
# schema on start and drops unknown keys from /data/options.json, so this
# legacy fallback covers only transitional states; the real upgrade path for
# keys saved through the old Configuration-tab fields is the Supervisor-API
# recovery below. secrets.env values win over any legacy source.
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

# One-time recovery for installs upgraded from the era when provider keys were
# Supervisor options: on the first start after the schema removal, Supervisor
# rewrites /data/options.json through schema validation and drops the unknown
# provider keys, so the saved values survive only in Supervisor's own store.
# Fetch them once via the Supervisor API and persist them into secrets.env so
# every later boot is file-first. Best-effort: any failure just means the
# station boots without those keys, same as before recovery existed.
#
# RECOVERY_MARKER makes this genuinely one-time: written only after a
# *successful* Supervisor response (an authoritative answer, whether or not it
# contained any of the missing keys — post-removal there is no way for a new
# legacy value to appear in Supervisor's store, since the schema fields that
# fed it are gone). A network error or timeout does NOT write the marker, so a
# transient failure keeps retrying on later boots instead of losing the
# recovery chance permanently.
RECOVERY_MARKER = '$RECOVERY_MARKER_FILE'
missing_keys = [k for k in secret_keys if not provider_values.get(k)]
supervisor_token = os.environ.get('SUPERVISOR_TOKEN') or os.environ.get('HASSIO_TOKEN') or ''
recovered = {}
if missing_keys and supervisor_token and not os.path.exists(RECOVERY_MARKER):
    try:
        import urllib.request
        req = urllib.request.Request(
            '$SUPERVISOR_API/addons/self/info',
            headers={'Authorization': 'Bearer ' + supervisor_token},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.load(resp)
        stored = (info.get('data') or {}).get('options') or {}
        if isinstance(stored, dict):
            for opt_key, env_key in provider_option_map:
                if env_key in missing_keys:
                    val = stored.get(opt_key, '')
                    if val:
                        recovered[env_key] = str(val)
        provider_values.update(recovered)
        try:
            with open(RECOVERY_MARKER, 'w', encoding='utf-8') as marker_file:
                marker_file.write('checked\n')
        except OSError:
            pass
    except Exception as exc:
        warning(f'could not check Supervisor for legacy provider keys: {exc}')
if recovered:
    try:
        existing_lines = []
        if os.path.exists('$SECRETS_FILE'):
            with open('$SECRETS_FILE', encoding='utf-8') as secret_file:
                existing_lines = secret_file.read().splitlines()
        new_lines = existing_lines + [k + '=' + shlex.quote(v) for k, v in sorted(recovered.items())]
        secrets_dir = os.path.dirname('$SECRETS_FILE') or '.'
        fd, tmp_name = tempfile.mkstemp(prefix='.secrets-', dir=secrets_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as tmp_file:
                tmp_file.write('\n'.join(new_lines) + '\n')
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, '$SECRETS_FILE')
            warning('moved legacy provider keys from the old add-on options into /config/secrets.env')
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception as exc:
        warning(f'could not persist recovered provider keys to secrets.env: {exc}')

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
guest_host = opts.get('guest_host', True)
gh_val = 'true' if guest_host else 'false'
print('export MAMMAMIRADIO_GUEST_HOST=' + gh_val)
# Pacing sliders. Only export when the operator actually set a value (addon
# config, or the admin slider persisting into /data/options.json) so an unset
# option leaves radio.toml's default in charge. A non-int value is skipped, not
# fatal, so one bad key can't drop every export.
for _pace_opt, _pace_env in (
    ('songs_between_banter', 'MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER'),
    ('songs_between_ads', 'MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS'),
    ('ad_spots_per_break', 'MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK'),
):
    _pace_val = opts.get(_pace_opt)
    if _pace_val is None:
        continue
    try:
        _pace_int = int(_pace_val)
    except (TypeError, ValueError):
        continue
    print('export ' + _pace_env + '=' + shlex.quote(str(_pace_int)))
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
