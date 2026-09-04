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
  set +e
  armed_out="$(MMR_REQUIRE_HA_RECEIPTS=1 bash "$SCRIPT" 2>&1)"
  armed_rc=$?
  set -e
  [[ "$armed_rc" -ne 0 ]] || fail "armed gate should fail with zero receipts, but passed"
  grep -q "\[FAIL\] HA Green release evidence" <<<"$armed_out" \
    || fail "armed run failed, but not on the receipt gate — exit status alone does not prove the gate ran"
  grep -q "\[WAIVED\]" <<<"$armed_out" \
    && fail "armed run still reported a waiver"
  pass "armed gate still fails on zero receipts, and fails on the gate itself"
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

# Case 5: both tag-path workflows must accept only 0 and 1, gate receipt
# validation on 1, gate the explicit waiver on 0, and define the variable at
# workflow scope. Two of the four enforcement sites sit on `push: tags`, so
# missing either one means the tag push fails after main is already frozen.
#
# The definition check is the load-bearing half. In GitHub Actions an
# `if: env.X == '1'` on an UNDEFINED variable evaluates against the empty
# string, so it is always false: the gate would be permanently disabled rather
# than opt-in, and a grep for the `if:` line alone would still pass.
for wf in .github/workflows/addon-release.yml .github/workflows/docker.yml; do
  grep -A2 "name: Validate physical HA Green release evidence" "$REPO_ROOT/$wf" \
    | grep -q "if: env.MMR_REQUIRE_HA_RECEIPTS == '1'" \
    || fail "$wf validation step is not conditioned on the variable"

  grep -A2 "name: Note waived HA Green release evidence" "$REPO_ROOT/$wf" \
    | grep -q "if: env.MMR_REQUIRE_HA_RECEIPTS == '0'" \
    || fail "$wf waiver step is not conditioned explicitly on 0"

  gate_script="$(python3 - "$REPO_ROOT/$wf" <<'PYEOF'
import sys, yaml

doc = yaml.safe_load(open(sys.argv[1]))
env = doc.get("env") or {}
value = env.get("MMR_REQUIRE_HA_RECEIPTS")
if value is None:
    sys.exit(1)
# It must default to waived, not to an empty string that reads as "off" by luck.
if "'0'" not in str(value) and '"0"' not in str(value):
    sys.exit(1)

for job in (doc.get("jobs") or {}).values():
    for step in job.get("steps") or []:
        if step.get("name") == "Validate HA Green receipt gate setting":
            print(step.get("run") or "")
            sys.exit(0)
sys.exit(1)
PYEOF
)" || fail "$wf does not define and validate MMR_REQUIRE_HA_RECEIPTS"

  for allowed in 0 1; do
    MMR_REQUIRE_HA_RECEIPTS="$allowed" bash -c "$gate_script" >/dev/null 2>&1 \
      || fail "$wf should accept MMR_REQUIRE_HA_RECEIPTS=$allowed"
  done

  for bad in true yes on TRUE 2; do
    set +e
    MMR_REQUIRE_HA_RECEIPTS="$bad" bash -c "$gate_script" >/dev/null 2>&1
    rc=$?
    set -e
    [[ "$rc" -eq 2 ]] \
      || fail "$wf should exit 2 for MMR_REQUIRE_HA_RECEIPTS=$bad, got $rc"
  done
done
pass "both tag-path workflows accept only 0 and 1"

echo "All receipt-gate opt-in scenarios passed."
