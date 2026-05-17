#!/usr/bin/env bash
# Self-test for scripts/edge-calver.sh — the edge add-on calendar-version
# generator used by the addon-build.yml validate job.
#
# Asserts: output format, determinism, and that the ordering segment is the
# git commit count (not a timestamp — the property that makes it clock-skew
# immune). No network. Exits non-zero on any mismatch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/edge-calver.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

cd "$REPO_ROOT"

# Case 1: output matches YYYY.M.D.<count>
OUT="$(bash "$SCRIPT")"
if echo "$OUT" | grep -qE '^[0-9]{4}\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  pass "calver format: $OUT"
else
  fail "calver '$OUT' does not match YYYY.M.D.<count>"
fi

# Case 2: deterministic — two runs on the same HEAD produce the same value
OUT2="$(bash "$SCRIPT")"
[ "$OUT" = "$OUT2" ] || fail "calver not deterministic: '$OUT' vs '$OUT2'"
pass "calver is deterministic"

# Case 3: the ordering segment is the commit count (timestamp-free)
EXPECTED_COUNT="$(git rev-list --count HEAD)"
ACTUAL_COUNT="${OUT##*.}"
[ "$ACTUAL_COUNT" = "$EXPECTED_COUNT" ] \
  || fail "ordering segment '$ACTUAL_COUNT' != git rev-list --count HEAD ($EXPECTED_COUNT)"
pass "ordering segment is the commit count ($EXPECTED_COUNT)"

# Case 4: the date segment is the CI/runner UTC date, not a commit timestamp
EXPECTED_DATE="$(date -u +%Y.%-m.%-d)"
ACTUAL_DATE="${OUT%.*}"
[ "$ACTUAL_DATE" = "$EXPECTED_DATE" ] \
  || fail "date segment '$ACTUAL_DATE' != runner UTC date ($EXPECTED_DATE)"
pass "date segment is the runner UTC date"

echo "All edge-calver scenarios passed."
