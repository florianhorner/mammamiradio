#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -n "${CONDUCTOR_ROOT_PATH:-}" ] && [ -f "$CONDUCTOR_ROOT_PATH/.env" ]; then
  if [ -L .env ]; then
    ln -sfn "$CONDUCTOR_ROOT_PATH/.env" .env
  elif [ ! -e .env ]; then
    ln -s "$CONDUCTOR_ROOT_PATH/.env" .env
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
