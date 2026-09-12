#!/usr/bin/env bash
# Execute both ratchet steps from the workflow with hermetic git/python/gh stubs.
# No credentials, network writes, or mutations to this checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/quality.yml"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

# PyYAML comes from the project's uvicorn[standard] dependency.
python3 -c 'import yaml' 2>/dev/null || fail \
  "pyyaml is required; run inside the venv: source .venv/bin/activate"
python3 - "$WORKFLOW" "$TMP" <<'PY'
import pathlib, sys, yaml
job = yaml.safe_load(open(sys.argv[1]))["jobs"]["coverage-ratchet"]
steps = job["steps"]
ratchets = [s for s in steps if s.get("id") == "ratchet"]
reports = [s for s in steps if s.get("name") == "Report unlanded coverage floors"]
assert len(ratchets) == len(reports) == 1, "expected exactly one ratchet and reporter"
report = reports[0]
assert report["if"] == "always() && !cancelled()", "report failures, suppress cancellation"
assert job["if"] == "github.ref == 'refs/heads/main' && github.event_name == 'push'"
assert job["permissions"]["issues"] == "write"
for name, value in {
    "GH_REPO": "${{ github.repository }}",
    "SOURCE_SHA": "${{ github.sha }}",
    "PUSHED_SHA": "${{ steps.ratchet.outputs.pushed_sha }}",
    "LANDED": "${{ steps.ratchet.outputs.landed }}",
    "RATCHET_OUTCOME": "${{ steps.ratchet.outcome }}",
    "RUN_URL": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
}.items():
    assert report["env"][name] == value, f"incorrect {name} wiring"
for name, step in [("ratchet", ratchets[0]), ("report", report)]:
    pathlib.Path(sys.argv[2], name + ".sh").write_text(step["run"])
PY
pass "workflow wiring reports failures, excludes cancellation, and binds the run"

mkdir "$TMP/bin"
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env bash
set -eu
printf 'gh %s\n' "$*" >> "$GH_STUB_LOG"
if [ "${GH_STUB_FAIL:-}" = "$1 ${2:-}" ]; then exit 1; fi
case "$1 ${2:-}" in
  "api repos/example/radio/git/ref/heads/main") printf '%s' "$GH_STUB_MAIN" ;;
  "issue list")
    case " $* " in
      *" --label coverage-ratchet-broken "*) printf '%s' "${GH_STUB_CRASH:-}" ;;
      *" --label coverage-ratchet-stale "*) printf '%s' "${GH_STUB_STALE:-}" ;;
      *) exit 2 ;;
    esac ;;
  "label create"|"issue create"|"issue close") : ;;
  *) exit 2 ;;
esac
SH
cat > "$TMP/bin/git" <<'SH'
#!/usr/bin/env bash
set -eu
printf 'git %s\n' "$*" >> "$GIT_STUB_LOG"
case "$1" in
  diff)
    if [ "${2:-}" = "--quiet" ]; then exit "$GIT_STUB_CHANGED"; fi
    printf 'diff --git a/.coverage-floors.json b/.coverage-floors.json\n+  "x": 90,\n' ;;
  push) echo "${GIT_STUB_ERROR:-}" >&2; exit "$GIT_STUB_PUSH" ;;
  commit) exit "$GIT_STUB_COMMIT" ;;
  config|add) : ;;
  rev-parse) printf '%s\n' 'pushed-main' ;;
  *) exit 2 ;;
esac
SH
cat > "$TMP/bin/python" <<'SH'
#!/usr/bin/env bash
exit "$PYTHON_STUB_EXIT"
SH
chmod +x "$TMP/bin/gh" "$TMP/bin/git" "$TMP/bin/python"

export GH_REPO=example/radio SOURCE_SHA=source-main
export RUN_URL=https://github.com/example/radio/actions/runs/123
export RUNNER_TEMP="$TMP" GITHUB_OUTPUT="$TMP/outputs"
export GH_STUB_LOG="$TMP/gh.log" GIT_STUB_LOG="$TMP/git.log"
export LABEL=coverage-ratchet-stale CRASH_LABEL=coverage-ratchet-broken
export TITLE="Coverage floors could not be pushed" CRASH_TITLE="The coverage ratchet step failed"

run_report() {
  export LANDED="$1" GH_STUB_STALE="$2" RATCHET_OUTCOME="${3:-success}"
  export GH_STUB_CRASH="${4:-}" GH_STUB_FAIL="${5:-}"
  export GH_STUB_MAIN="${6:-source-main}" PUSHED_SHA="${7:-}"
  : > "$GH_STUB_LOG"
  report_exit=0
  PATH="$TMP/bin:$PATH" bash "$TMP/report.sh" > "$TMP/report.log" 2>&1 || report_exit=$?
}
ok() { [ "$report_exit" -eq 0 ] || fail "report failed: $(cat "$TMP/report.log")"; }
has() { grep -qF "$1" "$GH_STUB_LOG" || fail "missing operation: $1"; }
lacks() { if grep -qF "$1" "$GH_STUB_LOG"; then fail "unexpected operation: $1"; fi; }
no_mutations() {
  if grep -qE '^gh (issue (create|close)|label create)' "$GH_STUB_LOG"; then
    fail "unexpected mutation: $(cat "$GH_STUB_LOG")"
  fi
}

run_ratchet() {
  export GIT_STUB_CHANGED="$1" GIT_STUB_PUSH="$2" PYTHON_STUB_EXIT="${3:-0}"
  export GIT_STUB_COMMIT="${4:-0}" GIT_STUB_ERROR="${5:-}"
  : > "$GITHUB_OUTPUT"; : > "$GIT_STUB_LOG"
  ratchet_exit=0
  PATH="$TMP/bin:$PATH" bash "$TMP/ratchet.sh" > "$TMP/ratchet.log" 2>&1 || ratchet_exit=$?
  landed="$(sed -n 's/^landed=//p' "$GITHUB_OUTPUT")"
  pushed_sha="$(sed -n 's/^pushed_sha=//p' "$GITHUB_OUTPUT")"
}

run_ratchet 1 1 0 0 'fatal: Authentication failed'
[ "$ratchet_exit" -eq 0 ] && [ "$landed" = false ] || fail "push rejection did not emit false"
run_report "$landed" ""; ok
has 'gh issue create --title Coverage floors could not be pushed'
[ "$(grep -c '^gh issue create' "$GH_STUB_LOG")" -eq 1 ] || fail "duplicate issue created"
grep -qF 'coverage-floors.json' "$TMP/coverage-ratchet-issue.md" || fail "missing computed diff"
grep -qF 'make coverage-ratchet' "$TMP/coverage-ratchet-issue.md" || fail "missing remedy"
grep -qF "$RUN_URL" "$TMP/coverage-ratchet-issue.md" || fail "missing diagnostic run"
grep -qF 'does not determine which' "$TMP/coverage-ratchet-issue.md" || fail "push cause inferred"
grep -qF 'may also remove obsolete' "$TMP/coverage-ratchet-issue.md" || fail "report assumes every update raises coverage"
grep -qF 'Fixes #<this issue number>' "$TMP/coverage-ratchet-issue.md" || fail "missing resolution path"
pass "actual push rejection opens one issue with diff, diagnostic, and remedy"

run_ratchet 1 0
[ "$ratchet_exit" -eq 0 ] && [ "$landed" = true ] || fail "successful push not reported"
[ "$pushed_sha" = pushed-main ] || fail "successful push lacks its commit identity"
run_report "$landed" "" success 907 "" pushed-main "$pushed_sha"; ok
has 'gh issue close 907'
lacks 'gh issue create'
pass "successful pushed commit is current and resolves a previous compute failure"

run_ratchet 0 1
[ "$ratchet_exit" -eq 0 ] && [ "$landed" = unchanged ] || fail "no diff was reported as landing"
if grep -qF 'git push' "$GIT_STUB_LOG"; then fail "unchanged floors attempted a push"; fi
run_report "$landed" ""; ok; no_mutations
pass "unchanged snapshot without outstanding drift does nothing"

# The earlier 80 -> 90 candidate must survive a later 80 -> 80 no-op, or even
# a different successful increase: neither proves the original 90 floor landed.
for result in unchanged true false; do
  run_report "$result" 412; ok; no_mutations
done
pass "existing missed increase stays open until its candidate is verified"

for result in unchanged true false; do
  run_report "$result" 412 success 907; ok
  has 'gh issue close 907'; lacks 'gh issue close 412'; lacks 'gh issue create'
done
pass "compute recovery closes only the broken issue, independently of push status"

for failure in python commit; do
  if [ "$failure" = python ]; then run_ratchet 1 0 1; else run_ratchet 1 0 0 1; fi
  [ "$ratchet_exit" -ne 0 ] && [ -z "$landed" ] || fail "$failure failure looked successful"
  run_report "$landed" "" failure; ok
  has 'gh issue create --title The coverage ratchet step failed'
  lacks 'gh issue create --title Coverage floors could not be pushed'
  grep -qF "$RUN_URL" "$TMP/coverage-ratchet-crash.md" || fail "crash lacks diagnostic run"
done
pass "computation and commit failures actually create the distinct crash report"

for outcome in failure skipped; do
  run_report "" "" "$outcome" 907; ok; no_mutations
done
pass "failed or skipped work does not duplicate its existing report"

for result in "" unexpected; do
  run_report "$result" "" success; ok
  has 'gh issue create --title The coverage ratchet step failed'
  lacks 'gh issue create --title Coverage floors could not be pushed'
done
pass "missing and invalid outputs remain unknown rather than push failures"

for outcome in success failure; do
  run_report false 412 "$outcome" 907 "" newer-main; ok; no_mutations
done
pass "obsolete snapshots cannot open or resolve current tracking issues"

for failure in 'api repos/example/radio/git/ref/heads/main' 'issue list' 'label create' 'issue create'; do
  run_report false "" success "" "$failure"
  [ "$report_exit" -ne 0 ] || fail "$failure failure was swallowed"
done
run_report unchanged "" success 907 'issue close'
[ "$report_exit" -ne 0 ] || fail "issue close failure was swallowed"
pass "lookup, label, creation, and closure API failures fail the reporting step"

echo "All coverage-ratchet reporting cases passed."
