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
# A broken pattern source is a gate error (exit 2) — never a violation (1),
# never clean (0). The readability check must come first: bash aborts a
# non-interactive shell outright when source targets a missing file, so an
# if-guard alone can never catch that case.
if [ ! -r "$SCRIPT_DIR/lint-patterns.sh" ]; then
  echo "ERROR: pattern source '$SCRIPT_DIR/lint-patterns.sh' is missing or unreadable." >&2
  exit 2
fi
# shellcheck source=scripts/lint-patterns.sh
if ! source "$SCRIPT_DIR/lint-patterns.sh"; then
  echo "ERROR: could not load '$SCRIPT_DIR/lint-patterns.sh'." >&2
  exit 2
fi
if [ "${#LINT_PATTERNS[@]}" -eq 0 ]; then
  echo "ERROR: LINT_PATTERNS is empty — nothing would be linted." >&2
  exit 2
fi

FAIL=0
HITS=0

for PAT in "${LINT_PATTERNS[@]}"; do
  # grep exit codes: 0 = match, 1 = no match, >1 = error. Only 1 may pass
  # silently — a pattern or read error must fail loudly, never read as clean.
  GREP_RC=0
  MATCHES=$(grep -nE "$PAT" "$BODY_FILE") || GREP_RC=$?
  if [ "$GREP_RC" -eq 1 ]; then
    continue
  fi
  if [ "$GREP_RC" -gt 1 ]; then
    echo "ERROR: pattern '$PAT' failed against '$BODY_FILE' (grep exit $GREP_RC)." >&2
    exit 2
  fi
  while IFS= read -r line; do
    echo "FAIL: PR body: $line  [pattern: $PAT]"
    HITS=$((HITS + 1))
  done <<< "$MATCHES"
  FAIL=1
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
