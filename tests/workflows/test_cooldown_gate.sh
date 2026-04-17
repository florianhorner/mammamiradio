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
set +e
bash "$SCRIPT" "" "$NOW" >/dev/null 2>&1
rc=$?
set -e
if (( rc == 1 )); then
  fail "empty-prior should not be treated as a cooldown block (exit=${rc})"
fi
pass "empty-prior not blocked (exit=${rc})"

# Case 6: MIN_COOLDOWN_HOURS=0 disables the gate
if ! MIN_COOLDOWN_HOURS=0 bash "$SCRIPT" "2026-04-17T11:59:59Z" "$NOW" >/dev/null 2>&1; then
  fail "MIN_COOLDOWN_HOURS=0 should disable the gate"
fi
pass "MIN_COOLDOWN_HOURS=0 disables gate"

# Case 7: MIN_COOLDOWN_HOURS=48 with a 25h-old prior => blocked
if MIN_COOLDOWN_HOURS=48 bash "$SCRIPT" "2026-04-16T11:00:00Z" "$NOW" >/dev/null 2>&1; then
  fail "MIN_COOLDOWN_HOURS=48 with 25h-old prior should block"
fi
pass "MIN_COOLDOWN_HOURS=48 blocks 25h-old prior"

# Case 8: malformed ISO => exit 2 (lookup / parse error, not block)
set +e
bash "$SCRIPT" "not-an-iso" "$NOW" >/dev/null 2>&1
rc=$?
set -e
if (( rc != 2 )); then
  fail "malformed ISO should exit 2, got ${rc}"
fi
pass "malformed ISO returns exit 2"

# Case 9: clock skew (prior in the future) => allowed with warning, not blocked
if ! bash "$SCRIPT" "2026-04-17T13:00:00Z" "$NOW" >/dev/null 2>&1; then
  fail "clock-skew (prior in future) should be allowed, not blocked"
fi
pass "clock-skew prior-in-future allowed"

echo
echo "All 9 cooldown gate cases passed."
