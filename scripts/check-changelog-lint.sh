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

CHANGELOGS=(
  CHANGELOG.md
  ha-addon/mammamiradio/CHANGELOG.md
)

# Each pattern is a POSIX extended regex (grep -E). Patterns are word-anchored or
# specific multi-word phrases to minimize false positives on legitimate technical text.
PATTERNS=(
  # Internal sprint / workstream labels
  'PR-[A-Z][0-9/]*[A-Z0-9]*'      # PR-A, PR-B/5, PR-C, PR-D/5, PR-F
  '\bWS[0-9]+(-[A-Z0-9]+)?'       # WS2, WS3, WS3-A, WS3-B, WS5, WS6
  '\b[Ff]inding #[0-9]+'          # finding #8, finding #11, Finding #1
  '\b[Ii]tem [0-9]+'              # Item 1, Item 19, Item 21
  '\bP[0-9]-[0-9]+\b'             # P0-1, P1-2, P1-3
  '\b[HM][0-9]+/[HM][0-9]+\b'     # H2/H3
  '\b[HM][0-9]+\b(?: \()'         # M1 (used in (M1) context — covered by parens form below)
  '\([HM][0-9]+\)'                # (M1), (M4), (H2/H3)
  '\bsoak window\b'
  '\blive session\b'
  '\b[Aa]pproach [A-Z]\b'         # Approach A, Approach B
  '\bConcept [A-Z][a-z]'          # Concept A Time-Horizon Stack
  '\bphase [A-Z]\b'               # phase A, phase B (lowercase)
  '\bPhase [A-Z][0-9]?\b'         # Phase A, Phase 1, Phase B1
  '\bleadership principle\b'

  # Agent / tool provenance
  '/autoplan'
  '\bcodex review\b'
  '\bcodex independent review\b'
  '\bClaude review\b'
  '\bClaude Code\b'
  '\bConductor agent\b'
  '\bConductor session\b'
  '\boperator-honesty\b'

  # Cathedral / sacred vocabulary
  '\bcathedral\b'
  '\bsacred files?\b'
  '\bdomain naves?\b'
  '\bgod[- ]module\b'
  '\bnave\b'

  # Contributor archaeology
  '\bfirst outside contribution\b'
  '\bwork was superseded\b'
)

FAIL=0
HITS=0

for FILE in "${CHANGELOGS[@]}"; do
  if [ ! -f "$FILE" ]; then
    echo "SKIP: $FILE not found"
    continue
  fi
  for PAT in "${PATTERNS[@]}"; do
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
