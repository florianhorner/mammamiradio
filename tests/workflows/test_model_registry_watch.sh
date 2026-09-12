#!/usr/bin/env bash
# Self-test for .github/workflows/model-registry-watch.yml
#
# Hermetic: every checker run below points --fixture-dir at the committed provider
# captures and pins --today, so no network and no calendar drift. The checker's own
# parsing is covered by tests/scripts/test_check_model_registry.py; this file proves
# the contract the WORKFLOW leans on: the three exit codes mean what the workflow
# assumes, the workflow branches on all three, and nothing wires --report into a
# PR or cut path where a calendar gate would get bumped reflexively.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHECKER="$REPO_ROOT/scripts/check_model_registry.py"
WF="$REPO_ROOT/.github/workflows/model-registry-watch.yml"
FIXTURES="$REPO_ROOT/tests/scripts/fixtures/model_registry"
REGISTRY="$REPO_ROOT/model_registry.toml"
# A date the committed fixtures and the live registry agree on. Pinned so the age
# half can never start failing this test just because months went by.
TODAY="2026-09-12"

# Same interpreter ladder as scripts/check-preship-evidence.sh: the checker needs
# tomllib (3.11+), and a bare `python3` outside the venv can be older. CI's runner
# Python is new enough; locally the venv usually is. Fail loud, never false-fail.
if [[ -n "${MAMMAMIRADIO_PYTHON:-}" ]]; then
  PYTHON_BIN="$MAMMAMIRADIO_PYTHON"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
  || { echo "FAIL: this self-test needs Python 3.11+ (set MAMMAMIRADIO_PYTHON)" >&2; exit 1; }

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMP="$(mktemp -d)"
trap 'rm -r "$TMP"' EXIT

report() {
  # report <registry> <fixture-dir> -> exit code, output to $TMP/out.
  # Never toggles errexit: callers capture the code with `rc=0; report … || rc=$?`,
  # which is errexit-safe. A `set -e` inside here would re-arm errexit for the
  # caller and abort the script on the very nonzero return the case is testing.
  "$PYTHON_BIN" "$CHECKER" --report --registry "$1" --fixture-dir "$2" --today "$TODAY" >"$TMP/out" 2>&1
}

# Case 1: the live registry against the committed captures is clean => exit 0.
# This also pins fixtures to pins: a bump that forgets to re-capture the provider
# docs goes red here, not in the first weekly run.
if ! report "$REGISTRY" "$FIXTURES"; then
  fail "live registry against committed fixtures should exit 0, got: $(cat "$TMP/out")"
fi
pass "live registry + committed fixtures exit 0 (the workflow's clean input is real)"

# Case 2: a pin the provider does not list is a finding => exit 1.
sed 's/^opus = "claude-opus-5"/opus = "claude-opus-99-fake"/' "$REGISTRY" > "$TMP/drift.toml"
grep -q 'claude-opus-99-fake' "$TMP/drift.toml" || fail "fixture setup: could not rewrite the opus pin"
rc=0; report "$TMP/drift.toml" "$FIXTURES" || rc=$?
[ "$rc" = "1" ] || fail "an unlisted pin should exit 1, got $rc"
grep -q "unlisted" "$TMP/out" || fail "drift output should classify the pin as unlisted"
pass "unlisted pin exits 1 (a finding, not a broken watcher)"

# Case 3: a stale review stamp is ALSO exit 1. Both halves feed the same verdict,
# so the workflow needs only one 'fail' branch for age and drift alike.
sed 's/^last_reviewed = .*/last_reviewed = "2026-01-01"/' "$REGISTRY" > "$TMP/stale.toml"
rc=0; report "$TMP/stale.toml" "$FIXTURES" || rc=$?
[ "$rc" = "1" ] || fail "a stale last_reviewed should exit 1, got $rc"
grep -q "last_reviewed" "$TMP/out" || fail "age failure should name last_reviewed"
pass "stale review stamp exits 1 (same branch as drift)"

# Case 4: unreadable provider docs are exit 2, distinct from a finding. This is the
# code the workflow maps to 'broken', the one case where it diverges from
# advertised-version.yml on purpose.
rc=0; report "$REGISTRY" "$TMP/no-such-dir" || rc=$?
[ "$rc" = "2" ] || fail "unreadable fixtures should exit 2, got $rc"
pass "unreadable provider docs exit 2 (broken, not a finding)"

# Case 5: the workflow runs --report, not one half of the watch. Match the
# invocation, not a mention: the header comment names --report too, and a grep
# that counts comments let a mutant that ran --providers instead survive.
grep -v '^[[:space:]]*#' "$WF" | grep -q -- 'python3 scripts/check_model_registry.py --report' \
  || fail "workflow must invoke --report (not merely mention it)"
pass "workflow invokes --report"

# Case 6: all three exit codes reach a named verdict.
grep -q '0) VERDICT=pass' "$WF" || fail "exit 0 must map to pass"
grep -q '1) VERDICT=fail' "$WF" || fail "exit 1 must map to fail"
grep -q '\*) VERDICT=broken' "$WF" || fail "any other exit must map to broken"
pass "exit codes 0/1/other map to pass/fail/broken"

# Case 7: close only on pass; open or comment on everything else. A broken watcher
# must not read as clean, and a clean week must not leave a stale issue open.
# shellcheck disable=SC2016  # literal shell text inside the YAML
grep -q 'if \[ "\$VERDICT" = "pass" \]; then' "$WF" || fail "workflow must branch on a pass verdict"
# shellcheck disable=SC2016
grep -q 'if \[ "\$VERDICT" = "broken" \]; then' "$WF" || fail "workflow must give broken its own body"
grep -q 'gh issue close' "$WF" || fail "workflow must close the issue on pass"
grep -q 'gh issue comment' "$WF" || fail "workflow must comment this week's report on an open issue"
grep -q 'gh issue create' "$WF" || fail "workflow must open the issue when none is open"
pass "closes on pass, opens or comments otherwise"

# Case 8: posture cloned from advertised-version.yml: weekly, fork-guarded, and the
# job (not the workflow) holds issues: write.
grep -q '"20 9 \* \* 1"' "$WF" || fail "schedule should be weekly (Monday 09:20 UTC)"
grep -q "github.repository == 'florianhorner/mammamiradio'" "$WF" || fail "fork guard missing"
grep -q 'issues: write' "$WF" || fail "job needs issues: write to file the nag"
grep -q 'persist-credentials: false' "$WF" || fail "checkout must not persist credentials"
pass "weekly, fork-guarded, issues: write on the job, no persisted credentials"

# Case 9: this self-test is registered, so it cannot be dropped silently.
grep -q 'tests/workflows/test_model_registry_watch.sh' "$REPO_ROOT/.github/workflows/quality.yml" \
  || fail "self-test must be registered in quality.yml"
pass "registered in quality.yml"

# Case 10: --report stays out of every PR and cut path. The checker's own contract:
# a calendar gate on unrelated PRs gets bumped reflexively. Match invocations, not
# mentions; comments explaining why it lives elsewhere must stay legal.
for rel in .github/workflows/quality.yml scripts/pre-release-check.sh scripts/model-registry-gate.sh; do
  [ -f "$REPO_ROOT/$rel" ] || continue
  if grep -v '^[[:space:]]*#' "$REPO_ROOT/$rel" | grep -q -- 'check_model_registry.py --report'; then
    fail "--report must not run on a PR or cut path ($rel); it belongs to the weekly workflow only"
  fi
done
if awk '/^pre-release:/{inrecipe=1; next} /^[^\t]/{inrecipe=0} inrecipe' "$REPO_ROOT/Makefile" \
   | grep -v '^[[:space:]]*#' | grep -q -- '--report'; then
  fail "--report must not be in the make pre-release recipe"
fi
pass "--report absent from the PR and cut paths"

CASE_COUNT="$(grep -c '^pass ' "$0")"
echo
echo "All $CASE_COUNT model-registry-watch cases passed."
