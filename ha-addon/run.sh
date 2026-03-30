#!/usr/bin/with-contenv bashio
# Entrypoint for the fakeitaliradio Home Assistant addon
set -e
cd /app

# --- Read addon options and export as environment variables ---
if bashio::config.exists 'spotify_client_id' && bashio::config.has_value 'spotify_client_id'; then
    export SPOTIFY_CLIENT_ID="$(bashio::config 'spotify_client_id')"
fi
if bashio::config.exists 'spotify_client_secret' && bashio::config.has_value 'spotify_client_secret'; then
    export SPOTIFY_CLIENT_SECRET="$(bashio::config 'spotify_client_secret')"
fi
if bashio::config.exists 'anthropic_api_key' && bashio::config.has_value 'anthropic_api_key'; then
    export ANTHROPIC_API_KEY="$(bashio::config 'anthropic_api_key')"
fi
if bashio::config.exists 'admin_password' && bashio::config.has_value 'admin_password'; then
    export ADMIN_PASSWORD="$(bashio::config 'admin_password')"
fi

# --- Bind to all interfaces (addon runs behind Supervisor proxy) ---
export FAKEITALIRADIO_BIND_HOST="0.0.0.0"
export FAKEITALIRADIO_PORT="8099"

# --- Persistent storage ---
mkdir -p /data/cache /data/tmp /data/go-librespot

# --- Custom radio.toml from addon config ---
if [ -f /config/radio.toml ]; then
    bashio::log.info "Using custom radio.toml from addon config"
    cp /config/radio.toml /app/radio.toml
fi

# --- go-librespot credentials persistence ---
if [ ! -f /data/go-librespot/config.yml ]; then
    cp /app/go-librespot/config.yml /data/go-librespot/config.yml
fi

# --- FIFO setup ---
FIFO="/tmp/fakeitaliradio.pcm"
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# --- Persistent FIFO drain (prevents ENXIO) — must start BEFORE go-librespot ---
cat "$FIFO" > /dev/null &

# --- Start go-librespot if available ---
if command -v go-librespot > /dev/null 2>&1; then
    bashio::log.info "Starting go-librespot..."
    go-librespot --config_dir /data/go-librespot > /dev/null 2>/data/tmp/go-librespot.log &
    bashio::log.info "go-librespot started (PID $!) — select 'fakeitaliradio' in your Spotify app"
else
    bashio::log.warning "go-librespot not available — Spotify will use fallback audio"
fi

# --- Launch the radio ---
bashio::log.info "Starting Fake Italian Radio on port 8099..."
exec python -m uvicorn fakeitaliradio.main:app \
    --host 0.0.0.0 --port 8099
