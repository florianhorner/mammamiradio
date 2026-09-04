#!/usr/bin/env bash
# check-preship-evidence.sh — verify the committed pre-ship review evidence.
#
# Shared checker for the runtime-independent half of the pre-ship squad gate: the local
# Claude hook (require-preship-squad.sh) cannot fire in Codex (no hook layer exists
# there), so CI verifies the content-addressed v2 receipt committed under
# proof/preship-reviews/v2/ — written by scripts/emit-review-evidence.sh after the squad
# runs. PR mode binds the new receipt to its reviewed commit AND to the PR head's exact
# content; main mode finds a surviving receipt matching the landed content, so squash/GC
# does not break a landed receipt.
#
# Honest scope, same words as land-pr.sh: this is a guard for tired humans and parallel
# agents, not a security boundary. The agent that would skip the squad also writes the
# evidence. What this changes: skipping the squad from ANY runtime now requires
# fabricating a diffable, content-bound artifact instead of being silently invisible.
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
