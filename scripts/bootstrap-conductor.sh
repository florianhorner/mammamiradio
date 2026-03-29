#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Missing $PYTHON_BIN. Set PYTHON_BIN to a Python 3.11+ interpreter." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  echo ".venv is not using Python 3.11+. Remove .venv and rerun with PYTHON_BIN set correctly." >&2
  exit 1
fi

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e . pytest

echo "Environment ready."
echo "Activate with: source .venv/bin/activate"
echo "Run tests with: python -m pytest"
