#!/usr/bin/env bash
# Self-test for the MMR_REQUIRE_HA_RECEIPTS opt-in on the physical HA Green
# receipt gate.
#
# The gate is off by default and must stay honest in both directions: waived it
# reports the waiver rather than a pass, armed evidence is validated, and a typo
# is a hard error instead of a silent skip. It also asserts
# both tag-path workflows gate their validation step on the same variable —
# without that, a green cut PR still dies at `git push origin vX.Y.Z`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/pre-release-check.sh"
GATE="$REPO_ROOT/scripts/ha-green-receipt-gate.sh"
PYTHON="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

cd "$REPO_ROOT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/validator.py" <<'PY'
import os
import sys

print("validator args:", " ".join(sys.argv[1:]))
raise SystemExit(int(os.environ.get("MMR_TEST_VALIDATOR_RC", "0")))
PY

run_gate() {  # $1 gate value ("unset" removes it), $2 validator exit code
  local mode="$1" validator_rc="$2"
  local env_args=("MMR_TEST_VALIDATOR_RC=$validator_rc")
  [ "$mode" = unset ] || env_args+=("MMR_REQUIRE_HA_RECEIPTS=$mode")
  set +e
  # The single-quoted body is intentionally expanded by the child bash.
  # shellcheck disable=SC2016
  out="$(env -u MMR_REQUIRE_HA_RECEIPTS "${env_args[@]}" bash -c '
    source "$0"
    ok() { echo "  [PASS] $*"; }
    fail() { echo "  [FAIL] $*"; }
    waive() { echo "  [WAIVED] $*"; }
    ha_green_receipt_gate_validate || exit $?
    ha_green_receipt_gate "$1" "$2" 1.2.3
  ' "$GATE" "$PYTHON" "$TMP/validator.py" 2>&1)"
  rc=$?
  set -e
}

# Case 1: unset => section 9 is waived, not passed, and does not fail the run.
run_gate unset 99
[[ "$rc" -eq 0 ]] || fail "unset gate returned $rc"
grep -q "\[WAIVED\]" <<<"$out" || fail "unset should report a WAIVED gate"
grep -q "WITHOUT physical Home Assistant Green" <<<"$out" \
  || fail "the waiver must name the missing evidence in plain words"
grep -q "\[PASS\] at least 20 cold" <<<"$out" \
  && fail "a waived gate must never be counted as a PASS"
grep -q "validator args" <<<"$out" && fail "waived gate invoked the validator"
pass "unset waives the gate and says so"

# Case 2: 0 behaves exactly like unset.
run_gate 0 99
grep -q "\[WAIVED\]" <<<"$out" || fail "0 should behave like unset"
grep -q "validator args" <<<"$out" && fail "0 gate invoked the validator"
pass "0 waives the gate"

# Case 3: armed => validator success passes and validator failure fails.
run_gate 1 0
grep -q "\[PASS\] at least 20 cold" <<<"$out" || fail "armed valid evidence did not pass"
grep -q -- "--release-version 1.2.3" <<<"$out" || fail "release version did not reach the validator"
run_gate 1 1
grep -q "\[FAIL\] HA Green release evidence" <<<"$out" || fail "armed invalid evidence did not fail"
grep -q "\[WAIVED\]" <<<"$out" && fail "armed run reported a waiver"
pass "armed gate reports validator success and failure honestly"

# Case 4: anything else is a hard error, never a silent skip. A gate that
# disables itself on MMR_REQUIRE_HA_RECEIPTS=true is worse than no gate.
for bad in true yes on TRUE 2; do
  run_gate "$bad" 0
  [[ "$rc" -eq 2 ]] || fail "MMR_REQUIRE_HA_RECEIPTS=$bad should exit 2, got $rc"
done
pass "invalid values are a hard error"

# The whole release script must source the helper and pass canonical paths.
source_wiring="source \"\$SCRIPT_DIR/ha-green-receipt-gate.sh\""
validator_wiring="\"\$SCRIPT_DIR/validate-ha-green-release-evidence.py\""
grep -Fq "$source_wiring" "$SCRIPT" \
  || fail "pre-release check does not source the HA Green receipt helper"
grep -Fq "$validator_wiring" "$SCRIPT" \
  || fail "pre-release check does not pass the canonical validator"
pass "pre-release check wires the sourced helper to the canonical validator"

# One whole release-check case keeps the helper wired to the production
# PASS/FAIL counters and final exit status. The PATH shim delegates every other
# Python invocation and rejects only the canonical HA evidence validator.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/python3" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == */validate-ha-green-release-evidence.py ]]; then
  echo "forced invalid HA Green evidence"
  exit 1
fi
exec "$MMR_TEST_REAL_PYTHON" "$@"
SH
chmod +x "$TMP/bin/python3"
set +e
whole_out="$(PATH="$TMP/bin:$PATH" MMR_TEST_REAL_PYTHON="$PYTHON" \
  MMR_REQUIRE_HA_RECEIPTS=1 bash "$SCRIPT" 2>&1)"
whole_rc=$?
set -e
[[ "$whole_rc" -ne 0 ]] || fail "whole release check passed rejected HA evidence"
grep -q "\[FAIL\] HA Green release evidence" <<<"$whole_out" \
  || fail "whole release check did not report the HA evidence failure"
grep -qE "Failed: [1-9][0-9]*( |$)" <<<"$whole_out" \
  || fail "whole release summary did not count the HA evidence failure"
pass "whole release check counts rejected HA evidence and exits nonzero"

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
