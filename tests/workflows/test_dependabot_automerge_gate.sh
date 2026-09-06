#!/usr/bin/env bash
# Validate the workflow and run the helper with GitHub mocked.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"
WF="${WF_OVERRIDE:-.github/workflows/dependabot-automerge.yml}"
HOLD="${HOLD_OVERRIDE:-$REPO_ROOT/scripts/dependabot-window-hold.sh}"
PY=python3
[ ! -x .venv/bin/python ] || PY=.venv/bin/python
TMP="$(mktemp -d "${TMPDIR:-/tmp}/automerge-gate.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

# Compare full YAML authorization expressions to catch inverted guards and
# extra OR clauses.
"$PY" - "$WF" "$TMP" <<'PYEOF'
import pathlib, sys, yaml
wf = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
out = pathlib.Path(sys.argv[2])
on = wf.get('on', wf.get(True))
assert set(on['pull_request_target']['types']) == {'opened', 'reopened', 'synchronize', 'labeled', 'unlabeled', 'ready_for_review'}
assert on['push']['branches'] == ['main']
assert 'ha-addon/mammamiradio/config.yaml' in on['push']['paths']
assert on['schedule'] and 'workflow_dispatch' in on
assert wf['concurrency'] == {'group': 'dependabot-automerge', 'cancel-in-progress': False}
assert wf['permissions'] == {'contents': 'write', 'pull-requests': 'write'}
jobs = wf['jobs']
compact = lambda s: ' '.join(s.split())
assert compact(jobs['enable-automerge']['if']) == "github.repository == 'florianhorner/mammamiradio' && github.event_name == 'pull_request_target' && github.event.pull_request.user.login == 'dependabot[bot]' && github.event.pull_request.base.ref == 'main' && github.event.pull_request.draft == false"
assert compact(jobs['sweep']['if']) == "github.repository == 'florianhorner/mammamiradio' && github.event_name != 'pull_request_target'"
for name, job in jobs.items():
    steps = job['steps']
    checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
    assert checkout['with'] == {'ref': 'main', 'persist-credentials': False}
    verdict = next(s for s in steps if s.get('id') == 'advertised')
    (out / f'{name}-verdict.sh').write_text(verdict['run'])
    assert steps.index(checkout) < steps.index(verdict)
    for step in steps:
        if 'scripts/dependabot-window-hold.sh' in step.get('run', ''):
            assert step['env']['GH_TOKEN'] == '${{ secrets.GITHUB_TOKEN }}'
            assert step['env']['GH_REPO'] == '${{ github.repository }}'
pr = jobs['enable-automerge']['steps']
metadata = next(s for s in pr if s.get('id') == 'metadata')
assert metadata['if'] == "steps.advertised.outputs.verdict == 'pass'"
assert metadata.get('with') == {'github-token': '${{ secrets.GITHUB_TOKEN }}'}
assert metadata['uses'] == 'dependabot/fetch-metadata@25dd0e34f4fe68f24cc83900b1fe3fe149efef98'
arm = next(s for s in pr if s.get('name', '').startswith('Enable automerge'))
assert compact(arm['if']) == "steps.advertised.outputs.verdict == 'pass' && (steps.metadata.outputs.update-type == 'version-update:semver-patch' || steps.metadata.outputs.update-type == 'version-update:semver-minor')"
assert pr.index(next(s for s in pr if s.get('id') == 'advertised')) < pr.index(metadata) < pr.index(arm)
assert arm['env']['PR_HEAD_SHA'] == '${{ github.event.pull_request.head.sha }}'
assert arm['env']['UPDATE_TYPE'] == '${{ steps.metadata.outputs.update-type }}'
assert arm['run'].strip() == 'bash scripts/dependabot-window-hold.sh arm "$PR_NUMBER" "$PR_HEAD_SHA" "$(git rev-parse HEAD)"'
dis = next(s for s in pr if s.get('name', '').startswith('Disarm automerge'))
assert dis['if'] == "steps.advertised.outputs.verdict == 'fail'"
assert dis['run'].strip() == 'bash scripts/dependabot-window-hold.sh disarm "$PR_NUMBER"'
unknown = next(s for s in pr if s.get('if') == "steps.advertised.outputs.verdict == 'unknown'")
(out / 'unknown.sh').write_text(unknown['run'])
rec = jobs['sweep']['steps'][-1]
assert rec['env']['VERDICT'] == '${{ steps.advertised.outputs.verdict }}'
assert rec['run'].strip() == 'bash scripts/dependabot-window-hold.sh sweep "$VERDICT"'
quality = yaml.safe_load(pathlib.Path('.github/workflows/quality.yml').read_text())
assert any(s.get('run') == 'bash tests/workflows/test_dependabot_automerge_gate.sh' and s.get('if') == "needs.changes.outputs.workflows == 'true'" for s in quality['jobs']['invariants']['steps'])
PYEOF
pass "workflow authorization, current-main checkout, shared concurrency, metadata and CI wiring"

mkdir -p "$TMP/scripts" "$TMP/bin"
for job in enable-automerge sweep; do
  for test_case in pass fail unknown missing; do
    case "$test_case" in
      pass) result=pass; code=0; message='VERDICT: pass' ;;
      fail) result=fail; code=1; message='VERDICT: fail' ;;
      unknown) result=unknown; code=0; message='VERDICT: unknown' ;;
      missing) result=unknown; code=2; message='no verdict' ;;
    esac
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "%s"\nexit %s\n' "$message" "$code" > "$TMP/scripts/check-advertised-version.sh"
    : > "$TMP/out"
    (cd "$TMP" && GITHUB_OUTPUT="$TMP/out" bash -e "$TMP/$job-verdict.sh" >/dev/null) || fail "$job: $test_case aborted the verdict step"
    [ "$(cat "$TMP/out")" = "verdict=$result" ] || fail "$job: wrong $test_case verdict"
  done
done
pass "both workflow verdict steps survive fail/unknown/error exits"

cat > "$TMP/bin/gh" <<'MOCK'
#!/usr/bin/env bash
set -eu
echo "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  'pr view')
    [ "${FAIL_AT:-}" != view ] || exit 1
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${PR_STATE:-OPEN}" "${AUTHOR:-app/dependabot}" "${BASE:-main}" "${DRAFT:-false}" "${HEAD_SHA:-head1}" "${ARMED:-true}" "${HELD:-false}" ;;
  'pr merge')
    if [ "${FAIL_AT:-}" = merge ] && { [ -z "${FAIL_NUMBER:-}" ] || [ "${!#}" = "$FAIL_NUMBER" ]; }; then exit 1; fi ;;
  'pr edit')
    [ "${FAIL_AT:-}" != edit ] || exit 1 ;;
  'label create')
    [ "${FAIL_AT:-}" != label ] && [ "${LABEL_EXISTS:-false}" = false ] || exit 1 ;;
  'label list')
    echo "${LABEL_EXISTS:-false}" ;;
  'api --paginate')
    [ "$3" = 'repos/florianhorner/mammamiradio/pulls?state=open&base=main&per_page=100' ] || exit 99
    [ "$5" = '.[] | select(.user.login == "dependabot[bot]") | .number' ] || exit 99
    [ "${FAIL_AT:-}" != list ] || exit 1
    printf '10\n11\n' ;;
  'api repos/florianhorner/mammamiradio/git/ref/heads/main')
    [ "${FAIL_AT:-}" != main ] || exit 1
    echo "${MAIN_SHA:-main1}" ;;
  *) echo "Unexpected gh call: $*" >&2; exit 99 ;;
esac
MOCK
chmod +x "$TMP/bin/gh"
run_hold() {
  : > "$TMP/gh.log"; RC=0
  OUT="$(PATH="$TMP/bin:$PATH" GH_REPO="${TEST_REPO-florianhorner/mammamiradio}" GH_MOCK_LOG="$TMP/gh.log" bash "$HOLD" "$@" 2>&1)" || RC=$?
  LOG="$(cat "$TMP/gh.log")"
}
no_mutations() { ! grep -Eq '^(pr (merge|edit)|label create)' <<< "$LOG" || fail "unexpected mutation: $LOG"; }
succeeded() { [ "$RC" -eq 0 ] || fail "rc=$RC: $OUT"; }
failed() { [ "$RC" -ne 0 ] || fail "expected failure: $OUT"; }

for mode in disarm sweep; do
  arg=10; [ "$mode" != sweep ] || arg=fail
  run_hold "$mode" "$arg"; succeeded
  grep -q '^pr merge --disable-auto 10$' <<< "$LOG" || fail "$mode did not disarm"
  grep -q '^pr edit 10 --add-label cut-window-hold$' <<< "$LOG" || fail "$mode did not record hold"
  if [ "$mode" = sweep ]; then
    grep -q '^pr merge --disable-auto 11$' <<< "$LOG" || fail "sweep stopped after the first PR"
    grep -q '^pr edit 11 --add-label cut-window-hold$' <<< "$LOG" || fail "sweep did not record the second hold"
  fi
  for error in view merge label edit; do
    FAIL_AT="$error" run_hold "$mode" "$arg"; failed
    ! grep -q 'is disarmed and labelled' <<< "$OUT" || fail "$mode falsely claimed success after $error failure"
  done
  LABEL_EXISTS=true run_hold "$mode" "$arg"; succeeded
  for unsafe in ARMED=false AUTHOR=human BASE=release PR_STATE=CLOSED; do
    # Export only a fixed test-case assignment, never repository/user content.
    export "${unsafe?}"
    run_hold "$mode" "$arg"; succeeded; no_mutations
    unset "${unsafe%%=*}"
  done
done
FAIL_AT=merge FAIL_NUMBER=10 run_hold sweep fail; failed
! grep -q '^pr edit 10 ' <<< "$LOG" || fail "failed PR must not be labelled"
grep -q '^pr merge --disable-auto 11$' <<< "$LOG" || fail "one API failure must not prevent disarming the next PR"
grep -q '^pr edit 11 --add-label cut-window-hold$' <<< "$LOG" || fail "next PR hold must still be recorded"
FAIL_AT=list run_hold sweep fail; failed; no_mutations
for repo in unowned/mammamiradio ''; do
  TEST_REPO="$repo" run_hold sweep fail; failed
  [ -z "$LOG" ] || fail "unowned/missing repository must be rejected before GitHub reads or writes"
done
ARMED=garbage run_hold disarm 10; failed; no_mutations
pass "direct and sweep disarm: eligible PRs only, idempotent labels, every API failure visible"

for verdict in pass unknown; do
  run_hold sweep "$verdict"; succeeded
  [ -z "$LOG" ] || fail "$verdict sweep must not grant merge authority or call GitHub"
done
run_hold sweep invalid; failed; no_mutations
: > "$TMP/gh.log"
PATH="$TMP/bin:$PATH" GH_MOCK_LOG="$TMP/gh.log" bash -e "$TMP/unknown.sh" >/dev/null
[ ! -s "$TMP/gh.log" ] || fail "unknown PR branch must not call GitHub"
pass "pass/unknown sweeps and unknown PR verdict do not mutate state"

for kind in version-update:semver-patch version-update:semver-minor; do
  UPDATE_TYPE="$kind" HELD=true run_hold arm 10 head1 main1; succeeded
  grep -q '^pr merge --squash --auto --match-head-commit head1 10$' <<< "$LOG" || fail "arm must pin the metadata head"
  grep -q '^pr edit 10 --remove-label cut-window-hold$' <<< "$LOG" || fail "successful arm must clear an existing hold"
done
UPDATE_TYPE=version-update:semver-patch HELD=false run_hold arm 10 head1 main1; succeeded
grep -q '^pr merge --squash --auto --match-head-commit head1 10$' <<< "$LOG" || fail "unheld eligible PR should arm"
! grep -q '^pr edit' <<< "$LOG" || fail "unheld PR should not need a label edit"
for kind in version-update:semver-major unknown ''; do
  UPDATE_TYPE="$kind" run_hold arm 10 head1 main1; succeeded; no_mutations
done
for unsafe in HEAD_SHA=new-head MAIN_SHA=cut-main AUTHOR=human BASE=release DRAFT=true PR_STATE=CLOSED; do
  export "${unsafe?}"
  UPDATE_TYPE=version-update:semver-patch run_hold arm 10 head1 main1; succeeded; no_mutations
  unset "${unsafe%%=*}"
done
for error in view main merge edit; do
  UPDATE_TYPE=version-update:semver-patch HELD=true FAIL_AT="$error" run_hold arm 10 head1 main1; failed
  if [ "$error" = view ] || [ "$error" = main ]; then no_mutations; fi
done
pass "arming requires verified update type, exact head, fresh main, eligible PR and successful APIs"

# Preserve the historical cut race: a pre-cut metadata event must not undo the
# hold installed by the cut sweep after main advances.
run_hold sweep fail; succeeded
UPDATE_TYPE=version-update:semver-patch ARMED=false HELD=true MAIN_SHA=cut-main run_hold arm 10 head1 main1
succeeded; no_mutations
pass "delayed pre-cut arming cannot undo the cut sweep"
echo "All dependabot automerge gate cases passed."
