#!/usr/bin/env bash
# UI copy lint: Principle #5 guard for human-facing product strings.
#
# Scans listener ui_copy, admin operator tables, listener.js fallbacks, HA addon
# option descriptions, and streamer setup errors. Fails on NEW violations outside
# .config/ui-copy-baseline.json until the backlog is cleared.
#
# Run locally:
#   bash scripts/check-ui-copy-lint.sh          # CI mode (baseline-aware)
#   bash scripts/check-ui-copy-lint.sh --audit  # full report
#
# Refresh baseline after fixing violations:
#   python3 scripts/ui_copy_lint.py --write-baseline

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--audit" ]; then
  exec python3 "$SCRIPT_DIR/ui_copy_lint.py" --audit
fi

exec python3 "$SCRIPT_DIR/ui_copy_lint.py"
