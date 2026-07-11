#!/usr/bin/env bash
# Shared editorial pattern source for changelog, PR-body, and public-doc lints.
#
# Sourced by:
#   - scripts/check-changelog-lint.sh   (CHANGELOG.md, ha-addon/.../CHANGELOG.md)
#   - scripts/check-pr-body-lint.sh     (PR body, via local hook + CI)
#   - scripts/check-docs-safety.sh      (public install and operator guides)
#
# Each pattern is a POSIX extended regex (grep -E). Patterns are word-anchored or
# specific multi-word phrases to minimize false positives on legitimate text.

# shellcheck disable=SC2034  # consumed by sourcing scripts (check-changelog-lint.sh, check-pr-body-lint.sh)
LINT_PATTERNS=(
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
  '\bPhase [A-Z][0-9]?\b'         # Phase A, Phase B1
  '\bPhase [0-9]+\b'              # Phase 1, Phase 2
  '\bTrack [A-Z]\b'               # Track A, Track B
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
  '\bred tests ride green\b'
  '\binformed the later\b'
  '\bsuperseded\b'
  '\bConductor setup fails\b'
  '\bCLAUDE\.md\b'

  # Process narrative (PR-body specific — observed in PR #422)
  '\b[0-9]+ commits ahead\b'      # "32 commits ahead — picked up cleanly"
  '\bpicked up cleanly\b'
  '\bauto-decided\b'              # "auto-decided during review"
  '\bsoak verification\b'         # "soak verification on edge addon after merge"
  '\bdual-voice review\b'
  '🤖 Generated with'             # Claude Code / Conductor / Codex footers
)

# Home Assistant renamed the user-facing add-on store surface to Apps. Keep
# developer/API "add-on" language where it remains accurate; ban only retired UI paths.
# shellcheck disable=SC2034  # consumed by scripts/check-docs-safety.sh
DOCS_RETIRED_INSTALL_PATTERNS=(
  'Settings[[:space:]]*(→|>|&gt;)[[:space:]]*Add-ons'
  '[Aa]dd-on [Ss]tore'
)

# Edge is a deliberate, manually cut channel. A merge to main may build an
# image, but Home Assistant sees no update until `make edge-release` advances
# the metadata pin. Keep this exact overclaim out of public channel copy.
# shellcheck disable=SC2016,SC2034  # literal backticks; consumed by scripts/check-docs-safety.sh
DOCS_RELEASE_TRUTH_PATTERNS=(
  '[Uu]pdates on every change merged to[[:space:]]+`?main`?'
)
