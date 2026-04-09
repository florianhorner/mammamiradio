#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Source credentials from ~/.config/mammamiradio/.env (safe, outside any repo).
# Falls back to CONDUCTOR_ROOT_PATH/.env for backwards compat.
_ENV_SAFE="$HOME/.config/mammamiradio/.env"
_ENV_REPO="${CONDUCTOR_ROOT_PATH:+$CONDUCTOR_ROOT_PATH/.env}"

_ENV_SOURCE=""
if [ -f "$_ENV_SAFE" ]; then
  _ENV_SOURCE="$_ENV_SAFE"
elif [ -n "$_ENV_REPO" ] && [ -f "$_ENV_REPO" ]; then
  _ENV_SOURCE="$_ENV_REPO"
fi

if [ -n "$_ENV_SOURCE" ]; then
  if [ -L .env ] || [ ! -e .env ]; then
    ln -sfn "$_ENV_SOURCE" .env
  fi
fi

if [ -z "${PYTHON_BIN:-}" ]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      export PYTHON_BIN="$candidate"
      break
    fi
  done
fi

"$ROOT/scripts/bootstrap-conductor.sh"

if [ -f requirements-dev.txt ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements-dev.txt
fi
