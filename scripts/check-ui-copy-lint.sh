#!/usr/bin/env bash
# UI copy lint: Principle #5 guard for human-facing product strings.
#
# Scans listener ui_copy, admin operator tables, listener.js fallbacks, HA addon
# option descriptions, and streamer setup errors. Fails on NEW violations outside
# .config/ui-copy-baseline.json until the backlog is cleared.
#
# Run locally (every ui_copy_lint.py flag is forwarded):
#   bash scripts/check-ui-copy-lint.sh                   # CI mode (baseline-aware)
#   bash scripts/check-ui-copy-lint.sh --audit           # full report
#   bash scripts/check-ui-copy-lint.sh --write-baseline  # refresh baseline after fixes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${MAMMAMIRADIO_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "$SCRIPT_DIR/../.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/../.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "check-ui-copy-lint: no Python at '$PYTHON_BIN' — install Python 3.11+ or set MAMMAMIRADIO_PYTHON to one" >&2
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "check-ui-copy-lint: '$PYTHON_BIN' is older than 3.11 — point MAMMAMIRADIO_PYTHON at a newer one" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/ui_copy_lint.py" "$@"
