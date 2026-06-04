#!/usr/bin/env bash
# Changelog content lint: fails on internal process/agent language in public release notes.
#
# Public changelogs (CHANGELOG.md, ha-addon/mammamiradio/CHANGELOG.md) describe what
# changed for users and developers. They must not contain internal sprint labels,
# AI tool provenance, planning vocabulary, or contributor archaeology.
#
# Run locally:    bash scripts/check-changelog-lint.sh
# CI invocation:  see .github/workflows/quality.yml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lint-patterns.sh
source "$SCRIPT_DIR/lint-patterns.sh"

CHANGELOGS=(
  CHANGELOG.md
  ha-addon/mammamiradio/CHANGELOG.md
)

FAIL=0
HITS=0

for FILE in "${CHANGELOGS[@]}"; do
  if [ ! -f "$FILE" ]; then
    echo "SKIP: $FILE not found"
    continue
  fi
  for PAT in "${LINT_PATTERNS[@]}"; do
    if grep -nE "$PAT" "$FILE" 2>/dev/null | grep -q .; then
      MATCHES=$(grep -nE "$PAT" "$FILE")
      while IFS= read -r line; do
        echo "FAIL: $FILE: $line  [pattern: $PAT]"
        HITS=$((HITS + 1))
      done <<< "$MATCHES"
      FAIL=1
    fi
  done
done

if [ "$FAIL" -ne 0 ]; then
  echo ""
  echo "Found $HITS violation(s). Public changelogs must not contain internal sprint labels,"
  echo "agent tool provenance, or planning vocabulary. Edit the entries above to describe"
  echo "user-visible outcomes only."
  exit 1
fi

echo "Changelog lint clean."
exit 0
