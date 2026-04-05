#!/bin/bash
# Double-click this file in Finder to start Malamie Radio.
# Drag it to your Dock for one-click launch.
cd "$(dirname "$0")"

# Use login shell environment so homebrew/pyenv/etc. are on PATH
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"
[ -f ~/.zprofile ] && source ~/.zprofile 2>/dev/null
[ -f ~/.zshrc ] && source ~/.zshrc 2>/dev/null

set -a
[ -f .env ] && source .env
set +a
PORT="${MAMMAMIRADIO_PORT:-8000}"

# Bootstrap: install dependencies and venv if missing
if [ ! -d .venv ]; then
    echo "First run — setting up everything..."

    # Install brew dependencies if needed
    if command -v brew > /dev/null 2>&1; then
        for pkg in python@3.13 ffmpeg; do
            if ! brew list "$pkg" &>/dev/null; then
                echo "Installing $pkg..."
                brew install "$pkg"
            fi
        done
        eval "$(brew shellenv)"
    fi

    PY=""
    for p in python3.13 python3.12 python3.11 python3; do
        if command -v "$p" > /dev/null 2>&1; then PY="$p"; break; fi
    done
    if [ -z "$PY" ]; then
        echo "ERROR: Python 3 not found and could not auto-install."
        echo "Press any key to close."
        read -n 1
        exit 1
    fi
    echo "Using $("$PY" --version)..."
    "$PY" -m venv .venv
    .venv/bin/pip install --upgrade pip setuptools --quiet
    .venv/bin/pip install -e . --quiet || {
        echo "ERROR: pip install failed. See output above."
        echo "Press any key to close."
        read -n 1
        exit 1
    }
    echo "Setup complete!"
    echo ""
fi

# Start if not already running
if pgrep -f "uvicorn mammamiradio" > /dev/null 2>&1; then
    echo "Radio already running."
else
    echo "Starting Malamie Radio..."
    ./start.sh &
    # Wait for server
    for i in $(seq 1 30); do
        curl -sf -o /dev/null "http://localhost:${PORT}/listen" && break
        sleep 1
    done
fi

# Open dashboard
open "http://localhost:${PORT}/"
echo ""
echo "Dashboard: http://localhost:${PORT}/"
echo "Listener:  http://localhost:${PORT}/listen  (share this with friends)"
echo ""
echo "Close this window to stop the radio."
wait
