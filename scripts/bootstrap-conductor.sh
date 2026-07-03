#!/usr/bin/env bash
# Bootstrap a Conductor workspace: create .venv, install -e, set up env.
# Usage: scripts/bootstrap-conductor.sh
set -euo pipefail

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/bootstrap-conductor.sh

Bootstrap a fresh Conductor workspace:
  - Create .venv with PYTHON_BIN (defaults to python3.11)
  - Install the package in editable mode (pip install -e .)
  - Wire up Conductor-specific env defaults

Env:
  PYTHON_BIN   Python interpreter to use (default: python3.11)

Options:
  -h, --help   Show this help and exit

Invoked by Conductor's `setup` hook (configured in `.conductor/settings.toml`).
EOF
    exit 0
    ;;
esac

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

if ! python -m pip --version >/dev/null 2>&1; then
  echo ".venv has no pip (likely created by a tool that skips seeding it, e.g. 'uv venv'). Bootstrapping pip via ensurepip..."
  if ! python -m ensurepip --upgrade; then
    echo "ensurepip failed to bootstrap pip in .venv. Recreate .venv with a Python interpreter that includes a working ensurepip (see PYTHON_BIN)." >&2
    exit 1
  fi
fi

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e . pytest

echo "Environment ready."
echo "Activate with: source .venv/bin/activate"
echo "Run tests with: python -m pytest"
