#!/bin/bash
# Double-click this file in Finder to start Mamma Mi Radio.
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
        if command -v "$p" > /dev/null 2>&1 \
            && "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' > /dev/null 2>&1; then
            PY="$p"; break
        fi
    done
    if [ -z "$PY" ]; then
        echo "Mamma Mi Radio needs Python 3.11 or newer, and none was found."
        echo "Install it from python.org (or with Homebrew), then double-click again."
        echo "Press any key to close."
        read -n 1
        exit 1
    fi
    echo "Using $("$PY" --version)..."
    "$PY" -m venv .venv
    .venv/bin/pip install --upgrade pip setuptools --quiet
    # Install the hash-locked runtime, then the app without resolving its
    # dependencies, so a first run gets exactly the tested versions.
    if ! .venv/bin/pip install --force-reinstall --require-hashes -r requirements.txt --quiet \
        || ! .venv/bin/pip install --no-deps -e . --quiet \
        || ! .venv/bin/pip check; then
        echo "ERROR: pip install failed. See output above."
        echo "Press any key to close."
        read -n 1
        exit 1
    fi
    echo "Setup complete!"
    echo ""
fi

# Start if not already running
if pgrep -f "uvicorn mammamiradio" > /dev/null 2>&1; then
    echo "Radio already running."
else
    echo "Starting Mamma Mi Radio..."
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
