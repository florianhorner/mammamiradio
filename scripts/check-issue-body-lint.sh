#!/usr/bin/env bash
# Issue-body editorial lint: applies the shared LINT_PATTERNS to an issue body file.
#
# Same patterns banned in public changelogs and PR bodies. Issues on the public
# tracker are product copy: internal sprint labels, agent tool provenance,
# planning vocabulary, workspace archaeology, and process narrative belong in a
# private durable system, never in a public issue.
#
# Scope: maintainer-authored issues only. Outside contributors' bug reports are
# never linted (the CI workflow carries the author guard).
#
# Run locally:    bash scripts/check-issue-body-lint.sh <body-file>
# CI invocation:  see .github/workflows/issue-body-lint.yml
# Local hook:     ~/.claude/hooks/verify-issue-body.sh chains this in when present
#                 in the project's scripts/ directory at gh issue create/edit time.

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
      echo "FAIL: issue body: $line  [pattern: $PAT]"
      HITS=$((HITS + 1))
    done <<< "$MATCHES"
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "Found $HITS editorial violation(s). Public issues must not contain internal"
  echo "sprint labels, agent tool provenance, planning vocabulary, workspace"
  echo "archaeology, or process narrative. Describe the user-visible problem and"
  echo "the desired outcome only; internal context goes to the private durable"
  echo "system instead. See the project editorial boundary docs."
  exit 1
fi

echo "Issue body lint clean."
exit 0
