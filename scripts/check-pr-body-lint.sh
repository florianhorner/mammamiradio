#!/usr/bin/env bash
# PR-body editorial lint: applies the shared LINT_PATTERNS to a PR body file.
#
# Reuses the same patterns banned in public changelogs. Catches internal sprint
# labels, AI tool provenance, planning vocabulary, contributor archaeology, and
# process narrative in pull-request descriptions.
#
# Run locally:    bash scripts/check-pr-body-lint.sh <body-file>
# CI invocation:  see .github/workflows/pr-body-lint.yml
# Local hook:     ~/.claude/hooks/verify-proof-block.sh chains this in when present
#                 in the project's scripts/ directory at gh pr create time.

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <body-file>" >&2
  exit 2
fi

BODY_FILE="$1"

if [ ! -f "$BODY_FILE" ]; then
  echo "ERROR: body file '$BODY_FILE' not found." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lint-patterns.sh
source "$SCRIPT_DIR/lint-patterns.sh"

FAIL=0
HITS=0

for PAT in "${LINT_PATTERNS[@]}"; do
  if grep -nE "$PAT" "$BODY_FILE" 2>/dev/null | grep -q .; then
    MATCHES=$(grep -nE "$PAT" "$BODY_FILE")
    while IFS= read -r line; do
      echo "FAIL: PR body: $line  [pattern: $PAT]"
      HITS=$((HITS + 1))
    done <<< "$MATCHES"
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "Found $HITS editorial violation(s). PR bodies must not contain internal"
  echo "sprint labels, agent tool provenance, planning vocabulary, contributor"
  echo "archaeology, or process narrative. Rewrite the body to describe"
  echo "user-visible outcomes only. See CLAUDE.md \"Changelog editorial boundary\"."
  exit 1
fi

echo "PR body lint clean."
exit 0
