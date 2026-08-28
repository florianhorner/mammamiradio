#!/usr/bin/env bash
# Self-test for the MMR_REQUIRE_HA_RECEIPTS opt-in on the physical HA Green
# receipt gate.
#
# The gate is off by default and must stay honest in both directions: waived it
# reports the waiver rather than a pass, armed it still fails on missing
# receipts, and a typo is a hard error instead of a silent skip. It also asserts
# both tag-path workflows gate their validation step on the same variable —
# without that, a green cut PR still dies at `git push origin vX.Y.Z`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/pre-release-check.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

cd "$REPO_ROOT"

# Case 1: unset => section 9 is waived, not passed, and does not fail the run.
out="$(env -u MMR_REQUIRE_HA_RECEIPTS bash "$SCRIPT" 2>&1)" || true
grep -q "\[WAIVED\]" <<<"$out" || fail "unset should report a WAIVED gate"
grep -q "WITHOUT physical Home Assistant Green" <<<"$out" \
  || fail "the waiver must name the missing evidence in plain words"
grep -qE "Waived: [1-9]" <<<"$out" || fail "the summary must count the waiver"
grep -q "\[PASS\] at least 20 cold" <<<"$out" \
  && fail "a waived gate must never be counted as a PASS"
pass "unset waives the gate and says so"

# Case 2: 0 behaves exactly like unset.
out0="$(MMR_REQUIRE_HA_RECEIPTS=0 bash "$SCRIPT" 2>&1)" || true
grep -q "\[WAIVED\]" <<<"$out0" || fail "0 should behave like unset"
pass "0 waives the gate"

# Case 3: armed => the gate still fails with no receipts on disk. This is the
# case that proves the opt-in did not quietly retire the gate.
if [[ -d "$REPO_ROOT/proof/media/ha-green-release-evidence" ]] \
   && compgen -G "$REPO_ROOT/proof/media/ha-green-release-evidence/run-*.json" >/dev/null; then
  echo "SKIP: receipts exist, cannot assert the empty-evidence failure"
else
  if MMR_REQUIRE_HA_RECEIPTS=1 bash "$SCRIPT" >/dev/null 2>&1; then
    fail "armed gate should fail with zero receipts, but passed"
  fi
  pass "armed gate still fails on zero receipts"
fi

# Case 4: anything else is a hard error, never a silent skip. A gate that
# disables itself on MMR_REQUIRE_HA_RECEIPTS=true is worse than no gate.
for bad in true yes on TRUE 2; do
  set +e
  MMR_REQUIRE_HA_RECEIPTS="$bad" bash "$SCRIPT" >/dev/null 2>&1
  rc=$?
  set -e
  [[ "$rc" -eq 2 ]] || fail "MMR_REQUIRE_HA_RECEIPTS=$bad should exit 2, got $rc"
done
pass "invalid values are a hard error"

# Case 5: both tag-path workflows must gate the validation step on the variable.
# Two of the four enforcement sites sit on `push: tags`, so missing either one
# means the tag push fails after main is already frozen.
for wf in .github/workflows/addon-release.yml .github/workflows/docker.yml; do
  grep -q "MMR_REQUIRE_HA_RECEIPTS" "$REPO_ROOT/$wf" \
    || fail "$wf does not gate HA Green validation on MMR_REQUIRE_HA_RECEIPTS"
  grep -A2 "name: Validate physical HA Green release evidence" "$REPO_ROOT/$wf" \
    | grep -q "if: env.MMR_REQUIRE_HA_RECEIPTS == '1'" \
    || fail "$wf validation step is not conditioned on the variable"
done
pass "both tag-path workflows gate on the variable"

echo "All receipt-gate opt-in scenarios passed."
