#!/usr/bin/env bash
# Cloud Conductor workspace bootstrap (Amazon Linux / CONDUCTOR_IS_LOCAL=0).
# Machine install provides python3.11; this script builds .venv.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -L .env ] || [ ! -e .env ]; then
  if [ -n "${CONDUCTOR_ROOT_PATH:-}" ] && [ -f "$CONDUCTOR_ROOT_PATH/.env" ]; then
    ln -sfn "$CONDUCTOR_ROOT_PATH/.env" .env
  fi
fi

export PYTHON_BIN="${PYTHON_BIN:-python3.11}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Missing $PYTHON_BIN. Add python3.11 to the Cloud Computer install script." >&2
  exit 1
fi

"$ROOT/scripts/bootstrap-conductor.sh"

if [ -f requirements-dev.txt ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements-dev.txt
fi

echo "[conductor-cloud-bootstrap] workspace ready (.venv, dev deps)"
