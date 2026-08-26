#!/usr/bin/env bash
# Cloud Conductor workspace bootstrap (Amazon Linux / CONDUCTOR_IS_LOCAL=0).
# Machine install provides a supported Python interpreter; this script builds .venv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -L .env ] || [ ! -e .env ]; then
  if [ -n "${CONDUCTOR_ROOT_PATH:-}" ] && [ -f "$CONDUCTOR_ROOT_PATH/.env" ]; then
    ln -sfn "$CONDUCTOR_ROOT_PATH/.env" .env
  fi
fi

if [ -z "${PYTHON_BIN:-}" ]; then
  for candidate in python3.11 python3.12 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1; then
      export PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [ -z "${PYTHON_BIN:-}" ] || ! command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    >/dev/null 2>&1; then
  echo "Missing a Python 3.11+ interpreter. Install Python 3.11+ or set PYTHON_BIN." >&2
  exit 1
fi

"$ROOT/scripts/bootstrap-conductor.sh"

if [ -f requirements-dev.txt ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements-dev.txt
fi

echo "[conductor-cloud-bootstrap] workspace ready (.venv, dev deps)"
