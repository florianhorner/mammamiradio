#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Load credentials from safe location (outside repo tree)
_ENV_SAFE="$HOME/.config/mammamiradio/.env"
if [ -f "$_ENV_SAFE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$_ENV_SAFE"
  set +a
elif [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ROOT/.env"
  set +a
fi

# yt-dlp is the default music source for local dev
export MAMMAMIRADIO_ALLOW_YTDLP="${MAMMAMIRADIO_ALLOW_YTDLP:-true}"

RUNTIME_ROOT="$ROOT/.context/conductor"
export MAMMAMIRADIO_BIND_HOST="${MAMMAMIRADIO_BIND_HOST:-127.0.0.1}"
export MAMMAMIRADIO_PORT="${MAMMAMIRADIO_PORT:-${CONDUCTOR_PORT:-8000}}"
export MAMMAMIRADIO_TMP_DIR="${MAMMAMIRADIO_TMP_DIR:-$RUNTIME_ROOT/tmp}"
export MAMMAMIRADIO_CACHE_DIR="${MAMMAMIRADIO_CACHE_DIR:-$RUNTIME_ROOT/cache}"
mkdir -p "$MAMMAMIRADIO_TMP_DIR" "$MAMMAMIRADIO_CACHE_DIR"

exec ./start.sh
