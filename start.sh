#!/bin/bash
# Start fakeitaliradio with go-librespot running independently
# go-librespot survives uvicorn restarts so Spotify stays connected

set -e
cd "$(dirname "$0")"

# Ensure FIFO exists
FIFO="/tmp/fakeitaliradio.pcm"
[ -p "$FIFO" ] || (rm -f "$FIFO" && mkfifo "$FIFO")

# Start go-librespot if not already running
if ! pgrep -f "go-librespot.*fakeitaliradio" > /dev/null 2>&1; then
    echo "Starting go-librespot..."
    mkdir -p tmp
    /opt/homebrew/opt/go-librespot/bin/go-librespot \
        --config_dir go-librespot \
        > /dev/null 2>tmp/go-librespot.log &
    echo "go-librespot PID: $!"
    echo "Select 'fakeitaliradio' in your Spotify app"
else
    echo "go-librespot already running ($(pgrep -f 'go-librespot.*fakeitaliradio'))"
fi

# Start persistent FIFO drain (keeps reader open so go-librespot never gets ENXIO)
if ! pgrep -f "cat.*fakeitaliradio.pcm" > /dev/null 2>&1; then
    cat "$FIFO" > /dev/null &
    echo "FIFO drain PID: $!"
fi

# Start uvicorn with reload (restarts on code changes, doesn't kill go-librespot)
echo "Starting uvicorn with --reload..."
source .venv/bin/activate
exec python -m uvicorn fakeitaliradio.main:app \
    --host 0.0.0.0 --port 8000 \
    --reload --reload-dir fakeitaliradio
