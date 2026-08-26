#!/usr/bin/env bash
# Self-test for scripts/check-merge-gate.sh
#
# Drives the drift guard with a mocked `gh` (PATH shim), asserting PASS/FAIL
# per setting and the loud CI skip. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$REPO_ROOT/scripts/check-merge-gate.sh"
cd "$REPO_ROOT"

[[ -x "$GATE" ]] || chmod +x "$GATE"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
  *"branches/main/protection/required_status_checks"*)
    [ -n "${GH_MOCK_PROT_FAIL:-}" ] && exit 1
    printf '%s\n' "${GH_MOCK_PROT:?}"
    ;;
  *"repos/{owner}/{repo}/rulesets/"*)
    [ -n "${GH_MOCK_RULESET_DETAIL_FAIL:-}" ] && exit 1
    printf '%s\n' "${GH_MOCK_RULESET_DETAIL:?}"
    ;;
  *"repos/{owner}/{repo}/rulesets"*)
    [ -n "${GH_MOCK_RULESETS_FAIL:-}" ] && exit 1
    printf '%s\n' "${GH_MOCK_RULESETS:?}"
    ;;
  *"repos/{owner}/{repo}"*)
    [ -n "${GH_MOCK_REPO_FAIL:-}" ] && exit 1
    printf '%s\n' "${GH_MOCK_REPO:?}"
    ;;
  *) exit 1 ;;
esac
MOCK
chmod +x "$MOCK_BIN/gh"

GOOD_REPO='{"allow_update_branch":true,"allow_auto_merge":true}'
GOOD_PROT='{"strict":true,"contexts":["quality","pi-smoke"]}'
GOOD_RULESETS='[[{"id":19667058}]]'
GOOD_RULESET_DETAIL='{"enforcement":"active","conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},"rules":[{"type":"pull_request","parameters":{"required_review_thread_resolution":true}}]}'

run_gate() { # [env overrides...]
  RUN_RC=0
  RUN_OUT="$(env -u CI PATH="$MOCK_BIN:$PATH" \
      GH_MOCK_REPO="$GOOD_REPO" GH_MOCK_PROT="$GOOD_PROT" \
      GH_MOCK_RULESETS="$GOOD_RULESETS" GH_MOCK_RULESET_DETAIL="$GOOD_RULESET_DETAIL" \
      "$@" bash "$GATE" 2>&1)" || RUN_RC=$?
}

# Case 1: all settings intact => exit 0, says intact
run_gate
[ "$RUN_RC" -eq 0 ] || fail "all-good settings should pass"
printf '%s' "$RUN_OUT" | grep -q "intact" || fail "all-good run should report intact"
printf '%s' "$RUN_OUT" | grep -q "review thread resolution" || fail "all-good run should report thread resolution"
pass "all settings intact passes"

# Case 2: strict=false => FAIL naming the setting
run_gate GH_MOCK_PROT='{"strict":false,"contexts":["quality","pi-smoke"]}'
[ "$RUN_RC" -ne 0 ] || fail "strict=false must fail"
printf '%s' "$RUN_OUT" | grep -q "strict" || fail "failure should name the strict setting"
pass "strict=false fails, names the setting"

# Case 3: allow_update_branch=false => FAIL
run_gate GH_MOCK_REPO='{"allow_update_branch":false,"allow_auto_merge":true}'
[ "$RUN_RC" -ne 0 ] || fail "allow_update_branch=false must fail"
printf '%s' "$RUN_OUT" | grep -q "allow_update_branch" || fail "failure should name allow_update_branch"
pass "allow_update_branch=false fails"

# Case 4: allow_auto_merge=false => FAIL
run_gate GH_MOCK_REPO='{"allow_update_branch":true,"allow_auto_merge":false}'
[ "$RUN_RC" -ne 0 ] || fail "allow_auto_merge=false must fail"
pass "allow_auto_merge=false fails"

# Case 5: required check context missing => FAIL naming it
run_gate GH_MOCK_PROT='{"strict":true,"contexts":["quality"]}'
[ "$RUN_RC" -ne 0 ] || fail "missing pi-smoke context must fail"
printf '%s' "$RUN_OUT" | grep -q "pi-smoke" || fail "failure should name the missing context"
pass "missing required context fails"

# Case 6: protection unreadable => FAIL loudly (never silently pass)
run_gate GH_MOCK_PROT_FAIL=1
[ "$RUN_RC" -ne 0 ] || fail "unreadable protection must fail"
pass "unreadable protection fails loudly"

# Case 7: CI set => loud SKIP, exit 0, no gh calls needed
RUN_RC=0
RUN_OUT="$(env CI=true PATH="$MOCK_BIN:$PATH" bash "$GATE" 2>&1)" || RUN_RC=$?
[ "$RUN_RC" -eq 0 ] || fail "CI run must skip with exit 0"
printf '%s' "$RUN_OUT" | grep -q "SKIPPED in CI" || fail "CI skip must be loud"
pass "CI skips loudly with exit 0"

# Case 8: ruleset missing thread resolution => FAIL
run_gate GH_MOCK_RULESET_DETAIL='{"enforcement":"active","conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},"rules":[{"type":"pull_request","parameters":{"required_review_thread_resolution":false}}]}'
[ "$RUN_RC" -ne 0 ] || fail "missing thread resolution must fail"
printf '%s' "$RUN_OUT" | grep -q "required_review_thread_resolution" || fail "failure should name thread resolution"
pass "missing review thread resolution fails"

# Case 9: inactive ruleset => FAIL even when thread resolution is enabled
run_gate GH_MOCK_RULESET_DETAIL='{"enforcement":"disabled","conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},"rules":[{"type":"pull_request","parameters":{"required_review_thread_resolution":true}}]}'
[ "$RUN_RC" -ne 0 ] || fail "inactive ruleset must fail"
pass "inactive ruleset fails"

# Case 10: non-main ruleset => FAIL
run_gate GH_MOCK_RULESET_DETAIL='{"enforcement":"active","conditions":{"ref_name":{"include":["refs/heads/develop"],"exclude":[]}},"rules":[{"type":"pull_request","parameters":{"required_review_thread_resolution":true}}]}'
[ "$RUN_RC" -ne 0 ] || fail "non-main ruleset must fail"
pass "non-main ruleset fails"

echo
echo "All 10 check-merge-gate cases passed."
