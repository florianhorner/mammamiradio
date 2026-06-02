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

# --- PR-based bump contract (issue #384) -------------------------------------
# The edge version lands on protected main via a GitHub App PR that passes the
# normal quality+pi-smoke gate, NOT a direct push. These assertions lock that in.

grep -q 'create-github-app-token' "$WORKFLOW" \
  || fail "edge bump must mint a GitHub App token (create-github-app-token)"
pass "edge bump mints a GitHub App token"

# No direct push to main: the old 'git pull --rebase origin main && git push'
# direct-push path must be gone — the bump reaches main only through a PR merge.
if grep -q 'git pull --rebase origin main' "$WORKFLOW"; then
  fail "edge bump must NOT push directly to main (found direct-push rebase loop)"
fi
# Any push targeting main (origin main, HEAD:main, :main) is forbidden — the bump
# reaches main only through a PR merge. Catches future direct-push regressions,
# not just the removed rebase-loop idiom.
if grep -qE 'git push.*main' "$WORKFLOW"; then
  fail "edge bump must NOT push directly to main (found a git push targeting main)"
fi
# shellcheck disable=SC2016
grep -qF 'HEAD:$BRANCH' "$WORKFLOW" \
  || fail "edge bump must push the bump commit to a branch, not main"
pass "edge bump pushes to a branch, never directly to main"

grep -q 'gh pr create' "$WORKFLOW" \
  || fail "edge bump must open a PR (gh pr create)"
if ! grep -q 'gh pr merge' "$WORKFLOW" || ! grep -q -- '--squash' "$WORKFLOW"; then
  fail "edge bump must squash-merge (gh pr merge --squash) so the loop-guard subject matches"
fi
pass "edge bump opens and squash-merges a PR"

# The App token must be wired to gh via GH_TOKEN — otherwise gh falls back to the
# default GITHUB_TOKEN, the PR's required checks never fire, and the bump deadlocks
# (the exact failure this change fixes).
# shellcheck disable=SC2016
grep -qF 'GH_TOKEN: ${{ steps.app-token.outputs.token }}' "$WORKFLOW" \
  || fail "bump-edge must run gh as the App (GH_TOKEN = app-token), else required checks never fire"
pass "bump-edge wires the App token to gh (GH_TOKEN)"

# Least privilege: nothing in this workflow may hold contents: write — a re-grant
# would re-enable a direct push to protected main.
if grep -q 'contents: write' "$WORKFLOW"; then
  fail "addon-build.yml must not grant contents: write (would re-enable direct push to main)"
fi
pass "addon-build.yml grants no contents: write"

# Synchronous, required-checks-only wait + TOCTOU-safe merge + bounded runtime.
if ! grep -q 'gh pr checks' "$WORKFLOW" || ! grep -q -- '--required' "$WORKFLOW"; then
  fail "edge bump must wait on REQUIRED checks (gh pr checks --required)"
fi
grep -q -- '--match-head-commit' "$WORKFLOW" \
  || fail "edge bump must merge the validated SHA (--match-head-commit)"
grep -q 'timeout-minutes' "$WORKFLOW" \
  || fail "bump-edge job must be time-bounded (timeout-minutes) for the loud-fail path"
pass "edge bump waits on required checks, is TOCTOU-safe, and time-bounded"

# Loop guard: the bump-merge commit must not re-trigger the workflow.
grep -qF "github.actor != 'mammamiradio-edge-bumper[bot]'" "$WORKFLOW" \
  || fail "validate job must skip pushes from the edge-bump App (loop guard)"
grep -qF "startsWith(github.event.head_commit.message, 'chore(edge): bump')" "$WORKFLOW" \
  || fail "validate job must skip chore(edge): bump commits (loop guard fallback)"
pass "workflow loop guard skips edge-bump merges"

echo "All edge-bump monotonic guard scenarios passed."
