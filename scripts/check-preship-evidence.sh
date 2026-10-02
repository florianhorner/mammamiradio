#!/usr/bin/env bash
# check-preship-evidence.sh — standalone verifier for historical v2 review receipts.
# RETIRED ADMISSION: manual/legacy only; no active hook, gate, queue, or workflow invokes this.
#
# Verify historical content-addressed receipts. PR mode accepts an exact
# content digest or a clean three-way merge witness with a landed base. Conflicts
# and post-review drift fail closed. Main mode requires an exact digest match.
# This checks evidence consistency; it is not a security boundary.
#
# Usage: scripts/check-preship-evidence.sh --v2 --target SHA [--base SHA] --mode pr|main
#
# The legacy positional v1 form (evidence-file, target-head) is retired: it verified the
# fixed-name proof/preship-review.json, which no longer exists. Old branches whose
# report-only workflow still invokes that form get a no-op notice and exit 0, so their
# annotations stay quiet until they integrate main.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "${1-}" == "--v2" ]]; then
  shift
  if [[ -n "${MAMMAMIRADIO_PYTHON:-}" ]]; then
    PYTHON_BIN="$MAMMAMIRADIO_PYTHON"
  elif [[ -x "$SOURCE_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$SOURCE_ROOT/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
  if ! "$PYTHON_BIN" -S -P -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo "check-preship-evidence: v2 requires Python 3.11+ (set MAMMAMIRADIO_PYTHON)" >&2
    exit 1
  fi
  export PYTHONPATH="$SOURCE_ROOT"
  exec "$PYTHON_BIN" -S -P -m scripts.landing evidence verify "$@"
fi

echo "check-preship-evidence: v1 evidence is retired — nothing to check here." \
  "The v2 receipts under proof/preship-reviews/v2/ are the only evidence layer;" \
  "verify them with: scripts/check-preship-evidence.sh --v2 --target <sha> --base <sha> --mode pr"
exit 0
