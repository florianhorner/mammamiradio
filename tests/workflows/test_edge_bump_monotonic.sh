#!/usr/bin/env bash
# Self-test for scripts/edge-version-newer.sh — the monotonic guard that the
# addon-build.yml bump-edge job uses before advancing the edge add-on version.
#
# Exit 0 from the script means "bump"; exit 1 means "skip". Runs a set of
# version-comparison scenarios plus workflow-contract assertions. No network.
# Exits non-zero on any mismatch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/edge-version-newer.sh"
WORKFLOW="$REPO_ROOT/.github/workflows/addon-build.yml"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

# should_bump <current> <candidate>  -> echoes "bump" or "skip"
should_bump() {
  if bash "$SCRIPT" "$1" "$2" >/dev/null 2>&1; then echo "bump"; else echo "skip"; fi
}

# Case 1: seed 0.0.0 -> first calver => bump
[[ "$(should_bump "0.0.0" "2026.5.17.842")" == "bump" ]] \
  || fail "seed 0.0.0 -> calver should bump"
pass "seed 0.0.0 -> calver bumps"

# Case 2: same-day later run => bump
[[ "$(should_bump "2026.5.17.842" "2026.5.17.843")" == "bump" ]] \
  || fail "later run_number same day should bump"
pass "same-day later run bumps"

# Case 3: older candidate (raced build finishing last) => skip
[[ "$(should_bump "2026.5.17.843" "2026.5.17.842")" == "skip" ]] \
  || fail "older candidate should skip"
pass "older candidate skips (race guard)"

# Case 4: identical version (rerun) => skip
[[ "$(should_bump "2026.5.17.842" "2026.5.17.842")" == "skip" ]] \
  || fail "identical version should skip"
pass "identical version skips (rerun guard)"

# Case 5: day rollover => bump
[[ "$(should_bump "2026.5.17.999" "2026.5.18.1")" == "bump" ]] \
  || fail "day rollover should bump"
pass "day rollover bumps"

# Case 6: year rollover => bump
[[ "$(should_bump "2025.12.31.5" "2026.1.1.1")" == "bump" ]] \
  || fail "year rollover should bump"
pass "year rollover bumps"

# Case 7: malformed current (e.g. 'latest') => skip
[[ "$(should_bump "latest" "2026.5.17.1")" == "skip" ]] \
  || fail "malformed current should skip"
pass "malformed current skips"

# Case 8: malformed candidate => skip
[[ "$(should_bump "2026.5.17.1" "garbage")" == "skip" ]] \
  || fail "malformed candidate should skip"
pass "malformed candidate skips"

# Case 9: empty current (e.g. version line absent) => skip
[[ "$(should_bump "" "2026.5.17.1")" == "skip" ]] \
  || fail "empty current should skip"
pass "empty current skips"

# Case 10: empty candidate => skip
[[ "$(should_bump "2026.5.17.1" "")" == "skip" ]] \
  || fail "empty candidate should skip"
pass "empty candidate skips"

# Case 11: 3-segment seed -> 4-segment calver => bump (regex allows 3 or 4)
[[ "$(should_bump "2026.5.17" "2026.5.17.1")" == "bump" ]] \
  || fail "3-segment current vs 4-segment candidate should bump"
pass "3-segment vs 4-segment bumps"

# Case 12: 4-segment vs lower 3-segment => skip
[[ "$(should_bump "2026.5.17.1" "2026.5.17")" == "skip" ]] \
  || fail "4-segment current vs lower 3-segment candidate should skip"
pass "4-segment vs lower 3-segment skips"

# Case 13: cross-day stale candidate (prior day arriving late) => skip
[[ "$(should_bump "2026.5.18.1" "2026.5.17.999")" == "skip" ]] \
  || fail "prior-day candidate after rollover should skip"
pass "cross-day stale candidate skips"

# Workflow contract: the calver must be deterministic and timestamp-free
# (commit-count ordered), and the bump job must not mutate main after another
# commit has already advanced it.
grep -q 'edge-calver.sh' "$WORKFLOW" \
  || fail "validate job must compute the calver via scripts/edge-calver.sh"
pass "workflow computes calver via edge-calver.sh"

# shellcheck disable=SC2016
grep -q 'git rev-list --count HEAD' "$REPO_ROOT/scripts/edge-calver.sh" \
  || fail "edge-calver.sh must order by commit count (timestamp-free)"
pass "calver ordering is the commit count"

# SC2016: these grep patterns intentionally match literal workflow text
# containing $(...) — they must not be expanded.
# shellcheck disable=SC2016
grep -q 'git fetch origin main:refs/remotes/origin/main' "$WORKFLOW" \
  || fail "edge bump job must refresh origin/main before checking staleness"
# shellcheck disable=SC2016
grep -q 'MAIN_SHA="$(git rev-parse origin/main)"' "$WORKFLOW" \
  || fail "edge bump job must compare origin/main to the triggering SHA"
# shellcheck disable=SC2016
grep -q 'MAIN_SHA.*GITHUB_SHA' "$WORKFLOW" \
  || fail "edge bump job must skip when main advanced beyond the triggering SHA"
pass "workflow skips stale main bumps"

echo "All edge-bump monotonic guard scenarios passed."
