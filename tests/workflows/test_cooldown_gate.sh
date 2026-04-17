#!/usr/bin/env bash
# Self-test for scripts/check-release-cooldown.sh
#
# Runs 5 scenarios with mocked ISO timestamps (no network, no gh CLI) and
# asserts the gate blocks/allows as expected. Exits non-zero on any mismatch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/check-release-cooldown.sh"

if [[ ! -x "$SCRIPT" ]]; then
  chmod +x "$SCRIPT"
fi

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

NOW="2026-04-17T12:00:00Z"

# Case 1: prior release 1h ago => blocked
if bash "$SCRIPT" "2026-04-17T11:00:00Z" "$NOW" >/dev/null 2>&1; then
  fail "1h-ago prior release should be blocked, was allowed"
fi
pass "1h-ago prior blocked"

# Case 2: prior release 23h59m ago => blocked (still under 24h)
if bash "$SCRIPT" "2026-04-16T12:00:01Z" "$NOW" >/dev/null 2>&1; then
  fail "23h59m-ago prior release should be blocked, was allowed"
fi
pass "23h59m-ago prior blocked"

# Case 3: prior release exactly 24h ago => allowed (boundary)
if ! bash "$SCRIPT" "2026-04-16T12:00:00Z" "$NOW" >/dev/null 2>&1; then
  fail "24h-ago prior release should be allowed (boundary), was blocked"
fi
pass "24h-ago prior allowed (boundary)"

# Case 4: prior release 25h ago => allowed
if ! bash "$SCRIPT" "2026-04-16T11:00:00Z" "$NOW" >/dev/null 2>&1; then
  fail "25h-ago prior release should be allowed, was blocked"
fi
pass "25h-ago prior allowed"

# Case 5: empty prior (first release ever) => allowed
if ! bash "$SCRIPT" "" "$NOW" 2>/dev/null; then
  # Empty prior with no gh CLI could exit 2 (lookup error). Allow both 0 and 2
  # as "not a cooldown block" outcomes for local testing. In CI, gh CLI is
  # available so this path returns 0.
  rc=$?
  if (( rc == 1 )); then
    fail "empty-prior should not be treated as a cooldown block"
  fi
fi
pass "empty-prior not blocked"

echo
echo "All 5 cooldown gate cases passed."
