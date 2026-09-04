#!/usr/bin/env bash
# emit-review-evidence.sh — emit or reattest the immutable v2 pre-ship review receipt.
#
# Runtime-independent half of the pre-ship evidence gate. The Claude PreToolUse hook
# (scripts/hooks/require-preship-squad.sh) cannot fire in Codex — ~/.codex/config.toml has
# no hook layer at all — so CI needs evidence that travels WITH the PR. The evidence is a
# content-addressed receipt under proof/preship-reviews/v2/: emission requires a clean
# review-ledger entry for exactly HEAD's content, and CI re-verifies the binding from
# trusted base code (.github/workflows/preship-evidence.yml).
#
# Receipts are content-addressed additions in per-content directories, so concurrent PRs
# never conflict on evidence. The legacy fixed-name proof/preship-review.json is retired:
# 43 commits touched it, a guaranteed merge conflict between any two open PRs.
#
# Two verbs:
#   scripts/emit-review-evidence.sh                emit a receipt for freshly reviewed HEAD
#   scripts/emit-review-evidence.sh --reattest \
#     [--base origin/main]                         derive a receipt after a clean base
#                                                  integration (git merge / update-branch),
#                                                  with no re-review — refused unless HEAD
#                                                  is exactly the reviewed content merged
#                                                  with the base and nothing else
#
# --v2 is accepted as a no-op compatibility flag for callers written during the v1/v2
# phase. Run only from a clean, committed tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ACTION="emit"
if [[ "${1-}" == "--v2" ]]; then
  shift
fi
if [[ "${1-}" == "--reattest" ]]; then
  ACTION="reattest"
  shift
fi

if [[ -n "${MAMMAMIRADIO_PYTHON:-}" ]]; then
  PYTHON_BIN="$MAMMAMIRADIO_PYTHON"
elif [[ -x "$SOURCE_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$SOURCE_ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
if ! "$PYTHON_BIN" -S -P -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "emit-review-evidence: v2 requires Python 3.11+ (set MAMMAMIRADIO_PYTHON)" >&2
  exit 1
fi
export PYTHONPATH="$SOURCE_ROOT"
exec "$PYTHON_BIN" -S -P -m scripts.landing evidence "$ACTION" "$@"
