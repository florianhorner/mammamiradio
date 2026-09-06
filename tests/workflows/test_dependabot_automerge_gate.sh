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
assert wf['permissions'] == {'actions': 'read', 'contents': 'write', 'pull-requests': 'write'}
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
#!/usr/bin/env python3
import json, os, pathlib, sys

a = sys.argv[1:]
e = os.environ
with open(e['GH_MOCK_LOG'], 'a') as log:
    log.write(' '.join(a) + '\n')
p = pathlib.Path(e['GH_MOCK_DIR']) / 'freeze-state.json'
s = json.loads(p.read_text()) if p.exists() else {'workflow': e.get('WF_STATE', 'active'), 'disarmed': [], 'polls': 0, 'release_reads': 0}
def save():
    p.write_text(json.dumps(s))
def fail(point):
    if e.get('FAIL_AT') == point:
        sys.exit(1)
def output(value):
    print(json.dumps(value))
    save()
    sys.exit(0)

def runs():
    fail('runs')
    if e.get('BAD_RUNS'):
        output({'workflow_runs': []})
    s['polls'] += 1
    statuses = e.get('RUN_STATUSES', 'completed').split(',')
    if 'DRAIN_AFTER' in e and s['polls'] > int(e['DRAIN_AFTER']):
        statuses = ['completed']
        if e.get('LATE_ARM'):
            s['late_armed'] = True
    entries = [{'id': i+1, 'status': status} for i, status in enumerate(statuses)]
    count = len(entries) + int(e.get('INCOMPLETE_RUNS', '0'))
    if e.get('RUN_SECOND_PAGE'):
        output([{'total_count': count, 'workflow_runs': entries[:1]}, {'total_count': count, 'workflow_runs': entries[1:]}])
    output([{'total_count': count, 'workflow_runs': entries}])

if a[:2] == ['pr', 'view']:
    fail('view')
    if 'baseRefOid' in ' '.join(a):
        output({'state': e.get('PR_STATE', 'OPEN'), 'headRefOid': e['LAND_HEAD'], 'baseRefOid': e['LAND_BASE'],
                'mergeStateStatus': e.get('MERGE_STATE', 'CLEAN'), 'commits': [{'committedDate': '2026-09-06T12:00:00Z'}]})
    armed = 'false' if a[2] in s['disarmed'] else e.get('ARMED', 'true')
    if s.get('late_armed') and a[2] not in s['disarmed']:
        armed = 'true'
    print('\t'.join([e.get('PR_STATE', 'OPEN'), e.get('AUTHOR', 'app/dependabot'), e.get('BASE', 'main'), e.get('DRAFT', 'false'), e.get('HEAD_SHA', 'head1'), armed, e.get('HELD', 'false')]))
elif a[:2] == ['pr', 'merge']:
    if '--help' in a:
        print('--match-head-commit')
    else:
        if e.get('FAIL_AT') == 'merge' and (not e.get('FAIL_NUMBER') or a[-1] == e['FAIL_NUMBER']):
            sys.exit(1)
        if '--disable-auto' in a and not e.get('STUCK_ARMED'):
            s['disarmed'].append(a[-1])
elif a[:2] == ['pr', 'edit']:
    fail('edit')
elif a[:2] == ['label', 'create']:
    fail('label')
    if e.get('LABEL_EXISTS', 'false') != 'false':
        sys.exit(1)
elif a[:2] == ['label', 'list']:
    fail('label_list')
    print(e.get('LABEL_EXISTS', 'false'))
elif a[:2] == ['workflow', 'disable']:
    fail('disable')
    assert a[2:] == ['dependabot-automerge.yml', '--repo', 'florianhorner/mammamiradio']
    s['workflow'] = 'disabled_manually'
elif a[:2] == ['workflow', 'enable']:
    fail('enable')
    assert a[2:] == ['dependabot-automerge.yml', '--repo', 'florianhorner/mammamiradio']
    s['workflow'] = 'active'
    s['enabled'] = True
    if e.get('FAIL_AT') == 'enable_after_apply':
        save()
        sys.exit(1)
elif a[:3] == ['api', '--paginate', '--slurp']:
    url = a[3]
    if url == 'repos/florianhorner/mammamiradio/actions/workflows/dependabot-automerge.yml/runs?per_page=100':
        runs()
    elif url == 'repos/florianhorner/mammamiradio/pulls?state=open&base=main&per_page=100':
        fail('prs')
        if e.get('BAD_PRS'):
            output([None])
        output([[{'number': int(n)} for n in e.get('PR_NUMBERS', '10,11').split(',') if n]])
    elif url == 'repos/florianhorner/mammamiradio/actions/runs/123/attempts/1/jobs?per_page=100':
        fail('jobs')
        names = e.get('PROMOTE_NAMES', 'promote (amd64),promote (aarch64)').split(',')
        output([{'jobs': [{'name': n, 'status': e.get('JOB_STATUS', 'completed'), 'conclusion': e.get('PROMOTE_RESULT', 'success')} for n in names]}])
    else:
        sys.exit('Unexpected paginated mock URL: ' + url)
elif a[:2] == ['api', '--paginate']:
    fail('list')
    assert a[2] == 'repos/florianhorner/mammamiradio/pulls?state=open&base=main&per_page=100'
    assert a[4] == '.[] | select(.user.login == "dependabot[bot]") | .number'
    print('10\n11')
elif a[:2] == ['api', 'repos/florianhorner/mammamiradio/actions/workflows/dependabot-automerge.yml']:
    fail('state')
    if e.get('FAIL_AT') == 'state_after_enable' and s.get('enabled'):
        sys.exit(1)
    if e.get('REACTIVATE') and s['polls']:
        s['workflow'] = 'active'
    print(s['workflow'])
elif a[:2] == ['api', 'repos/florianhorner/mammamiradio/git/ref/heads/main']:
    fail('main')
    sha = e.get('MAIN_SHA', 'main1')
    if e.get('CHANGE_PROOF') == 'main' and s['release_reads'] > 1:
        sha = '3' * 40
    print(sha)
elif a[:2] == ['api', 'repos/florianhorner/mammamiradio/actions/workflows/addon-release.yml']:
    fail('release_workflow')
    print('456')
elif a[:2] == ['api', 'repos/florianhorner/mammamiradio/actions/runs/123']:
    fail('release')
    s['release_reads'] += 1
    release = {'id': 123, 'workflow_id': int(e.get('RELEASE_WORKFLOW', '456')), 'path': '.github/workflows/addon-release.yml',
               'event': 'push', 'head_branch': e.get('RELEASE_TAG', 'v2.18.0'), 'head_sha': '2' * 40,
               'run_attempt': 1, 'status': e.get('RELEASE_STATUS', 'completed'), 'conclusion': e.get('RELEASE_RESULT', 'success')}
    if e.get('CHANGE_PROOF') == 'attempt' and s['release_reads'] > 1:
        release['run_attempt'] = 2
    if '--jq' in a:
        output([release[k] for k in ['id', 'workflow_id', 'path', 'event', 'head_branch', 'head_sha', 'run_attempt', 'status', 'conclusion']])
    output(release)
elif a[:2] == ['api', 'repos/florianhorner/mammamiradio/commits/v2.18.0']:
    fail('tag')
    print('3' * 40 if e.get('WRONG_TAG_SHA') or (e.get('CHANGE_PROOF') == 'tag' and s['release_reads'] > 1) else '2' * 40)
elif len(a) > 1 and a[0] == 'api' and a[1].startswith('repos/florianhorner/mammamiradio/contents/ha-addon/mammamiradio/config.yaml?ref='):
    fail('config')
    print(e.get('CONFIG_BODY', 'version: "2.18.0"\nimage: "ghcr.io/florianhorner/mammamiradio-addon-{arch}"'))
elif a[:2] == ['repo', 'view']:
    print('florianhorner/mammamiradio')
else:
    sys.exit('Unexpected mock gh call: ' + ' '.join(a))
save()
MOCK
chmod +x "$TMP/bin/gh"
run_hold() {
  : > "$TMP/gh.log"; rm -f "$TMP/freeze-state.json"; RC=0
  OUT="$(PATH="$TMP/bin:$PATH" GH_REPO="${TEST_REPO-florianhorner/mammamiradio}" GH_MOCK_LOG="$TMP/gh.log" GH_MOCK_DIR="$TMP" bash "$HOLD" "$@" 2>&1)" || RC=$?
  LOG="$(cat "$TMP/gh.log")"
}
no_mutations() { ! grep -Eq '^(pr (merge|edit)|label create|workflow (disable|enable))' <<< "$LOG" || fail "unexpected mutation: $LOG"; }
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
for error in view main state merge edit; do
  UPDATE_TYPE=version-update:semver-patch HELD=true FAIL_AT="$error" run_hold arm 10 head1 main1; failed
  if [ "$error" = view ] || [ "$error" = main ] || [ "$error" = state ]; then no_mutations; fi
done
pass "arming requires verified update type, exact head, fresh main, eligible PR and successful APIs"

# Preserve the historical cut race: a pre-cut metadata event must not undo the
# hold installed by the cut sweep after main advances.
run_hold sweep fail; succeeded
UPDATE_TYPE=version-update:semver-patch ARMED=false HELD=true MAIN_SHA=cut-main run_hold arm 10 head1 main1
succeeded; no_mutations
pass "delayed pre-cut arming cannot undo the cut sweep"


# A disabled workflow is the durable pause. Freeze drains prior write authority
# before disarming; cut admission proves that quiet state instead of racing a
# final main/flag read.
for state in disabled_manually disabled_inactivity disabled_fork deleted; do
  WF_STATE="$state" UPDATE_TYPE=version-update:semver-patch run_hold arm 10 head1 main1
  succeeded; no_mutations
done
WF_STATE=garbage UPDATE_TYPE=version-update:semver-patch run_hold arm 10 head1 main1
failed; no_mutations
WF_STATE=disabled_manually ARMED=false run_hold check-freeze; succeeded; no_mutations
ARMED=false run_hold check-freeze; failed; no_mutations
WF_STATE=disabled_manually run_hold check-freeze; failed; no_mutations
for status in in_progress queued requested waiting pending unknown; do
  WF_STATE=disabled_manually ARMED=false RUN_STATUSES="completed,$status" RUN_SECOND_PAGE=1 run_hold check-freeze
  failed; no_mutations
done
for invalid in BAD_RUNS=1 INCOMPLETE_RUNS=1 BAD_PRS=1 REACTIVATE=1 PR_STATE=garbage AUTHOR=null; do
  export "${invalid?}"
  WF_STATE=disabled_manually ARMED=false run_hold check-freeze; failed; no_mutations
  unset "${invalid%%=*}"
done
for error in state runs prs view; do
  WF_STATE=disabled_manually ARMED=false FAIL_AT="$error" run_hold check-freeze; failed; no_mutations
done
WF_STATE=disabled_manually PR_NUMBERS='' run_hold check-freeze; succeeded; no_mutations
pass "freeze admission rejects active, armed, incomplete, malformed and unreadable state"

# No wall-clock wait in the simulated drain; the mock changes state at the next poll.
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/bin/sleep"
chmod +x "$TMP/bin/sleep"
RUN_STATUSES=in_progress DRAIN_AFTER=1 LATE_ARM=1 ARMED=false run_hold freeze 30; succeeded
"$PY" - "$TMP/gh.log" <<'PYEOF'
import pathlib, sys
calls = pathlib.Path(sys.argv[1]).read_text().splitlines()
assert calls[0] == 'workflow disable dependabot-automerge.yml --repo florianhorner/mammamiradio'
polls = [i for i, s in enumerate(calls) if '/runs?per_page=100' in s]
disarm = calls.index('pr merge --disable-auto 10')
assert len(polls) >= 3 and polls[1] < disarm < polls[-1]
assert 'pr merge --disable-auto 11' in calls
assert not any('--auto ' in c or c.startswith('workflow enable') for c in calls)
PYEOF
WF_STATE=disabled_manually run_hold freeze 0; succeeded
for error in disable state runs list view merge label edit prs; do
  FAIL_AT="$error" run_hold freeze 0; failed
  ! grep -q '^workflow enable' <<< "$LOG" || fail "failed freeze must remain paused"
done
RUN_STATUSES=pending run_hold freeze 0; failed
! grep -q '^pr merge' <<< "$LOG" || fail "must not disarm before pending runs drain"
STUCK_ARMED=1 run_hold freeze 0; failed
REACTIVATE=1 run_hold freeze 0; failed
for timeout in -1 601 invalid; do
  run_hold freeze "$timeout"; failed; no_mutations
done
pass "freeze disables, drains a late arming run, disarms afterward and refuses timeout/API errors"

# Execute the real registry verdict reader with a local tag probe. No network.
cat > "$TMP/tag-probe" <<'PROBE'
#!/usr/bin/env bash
printf '%s %s\n' "$1" "$2" >> "$GH_MOCK_LOG"
echo "${TAG_VERDICT:-present}"
PROBE
chmod +x "$TMP/tag-probe"
run_thaw() {
  WF_STATE="${THAW_WORKFLOW:-disabled_manually}" ARMED="${THAW_ARMED:-false}" \
    MAIN_SHA=1111111111111111111111111111111111111111 \
    MAMMAMIRADIO_TAG_PROBE="$TMP/tag-probe" run_hold thaw 123
}
run_thaw; succeeded
grep -q '^workflow enable dependabot-automerge.yml --repo florianhorner/mammamiradio$' <<< "$LOG" || fail "published cut should resume"
grep -q '^florianhorner/mammamiradio-addon-aarch64 2.18.0$' <<< "$LOG" || fail "must verify both current images"
CONFIG_BODY=$'version: "2.18.0"\nimage: "ghcr.io/florianhorner/current-image-{arch}"' run_thaw; succeeded
grep -q '^florianhorner/current-image-aarch64 2.18.0$' <<< "$LOG" || fail "registry probe must use pinned main config, not the local checkout"
for invalid in THAW_WORKFLOW=active THAW_ARMED=true RELEASE_RESULT=failure RELEASE_STATUS=in_progress RELEASE_TAG=v3.0.0 RELEASE_WORKFLOW=789 PROMOTE_RESULT=failure JOB_STATUS=in_progress WRONG_TAG_SHA=1 CHANGE_PROOF=main CHANGE_PROOF=tag CHANGE_PROOF=attempt REACTIVATE=1; do
  export "${invalid?}"
  run_thaw; failed
  ! grep -q '^workflow enable' <<< "$LOG" || fail "invalid release/freeze state must not resume: $invalid"
  unset "${invalid%%=*}"
done
AUTHOR=human THAW_ARMED=true run_thaw; failed
! grep -q '^workflow enable' <<< "$LOG" || fail "queued human cut must prevent old-release thaw"
for names in 'promote (amd64)' 'promote (aarch64)' 'promote (amd64),promote (amd64)'; do
  PROMOTE_NAMES="$names" run_thaw; failed
  ! grep -q '^workflow enable' <<< "$LOG" || fail "missing/duplicate promotion must not resume"
done
for verdict in absent unknown; do
  TAG_VERDICT="$verdict" run_thaw; failed
  ! grep -q '^workflow enable' <<< "$LOG" || fail "$verdict registry verdict must not resume"
done
for error in state runs prs view main config release_workflow release tag jobs enable; do
  FAIL_AT="$error" run_thaw; failed
done
for error in enable_after_apply state_after_enable; do
  FAIL_AT="$error" run_thaw; failed
  grep -q 'Inspect its state\|inspect the workflow state' <<< "$OUT" || fail "ambiguous enable must explain the state check"
  ! grep -q 'Dependabot workflow enabled' <<< "$OUT" || fail "ambiguous enable must not claim success"
done
CONFIG_BODY='version: broken' run_thaw; failed
! grep -q '^workflow enable' <<< "$LOG" || fail "malformed version must not resume"
bash "$REPO_ROOT/scripts/check-advertised-version.sh" --config >/dev/null 2>&1 && fail "missing --config path must refuse"
pass "thaw validates current release proof, blocks pending cuts and reports ambiguous enable without claiming success"

# Exercise the actual landing wrapper against immutable version fixtures.
# Only GitHub, the evidence checker and the ledger reader are mocked.
FIXTURE="$TMP/landing"
mkdir -p "$FIXTURE/ha-addon/mammamiradio"
git -C "$FIXTURE" init -q
git -C "$FIXTURE" config user.name 'Automerge gate tests'
git -C "$FIXTURE" config user.email 'tests@example.com'
git -C "$FIXTURE" config core.hooksPath /dev/null
git -C "$FIXTURE" config commit.gpgsign false
fixture_commit() {
  printf '%s\n' "$1" > "$FIXTURE/ha-addon/mammamiradio/config.yaml"
  git -C "$FIXTURE" add .
  git -C "$FIXTURE" commit -qm 'chore: version fixture'
  git -C "$FIXTURE" rev-parse HEAD
}
BASE_COMMIT="$(fixture_commit $'version: "2.18.0"\nname: Old')"
ORDINARY_COMMIT="$(fixture_commit $'version: "2.18.0"\nname: New')"
CUT_COMMIT="$(fixture_commit $'version: "3.0.0"\nname: New')"
BAD_COMMIT="$(fixture_commit $'version: "3.0.0"\nversion: "2.18.0"')"
MISSING_COMMIT="$(fixture_commit 'name: Missing version')"
printf 'version: "99.0.0"\n' > "$FIXTURE/ha-addon/mammamiradio/config.yaml"
cat > "$TMP/evidence" <<'EVIDENCE'
#!/usr/bin/env bash
echo "evidence $*" >> "$GH_MOCK_LOG"
exit "${EVIDENCE_RC:-0}"
EVIDENCE
printf '#!/usr/bin/env bash\necho ---CONFIG---\n' > "$TMP/reader"
chmod +x "$TMP/evidence" "$TMP/reader"
run_landing() {
  : > "$TMP/gh.log"; rm -f "$TMP/freeze-state.json"; RC=0
  OUT="$(cd "$FIXTURE" && PATH="$TMP/bin:$PATH" GH_REPO=florianhorner/mammamiradio \
    GH_MOCK_LOG="$TMP/gh.log" GH_MOCK_DIR="$TMP" LAND_BASE="$BASE_COMMIT" LAND_HEAD="$1" \
    MMR_LAND_REVIEW_READER="$TMP/reader" MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
    MMR_LAND_EVIDENCE_CHECKER="$TMP/evidence" MMR_LAND_SKIP_THREAD_CHECK=1 \
    bash "$REPO_ROOT/scripts/land-pr.sh" 7 2>&1)" || RC=$?
  LOG="$(cat "$TMP/gh.log")"
}
no_merge() { ! grep -q '^pr merge .*--auto' <<< "$LOG" || fail "blocked landing attempted merge"; }
run_landing "$ORDINARY_COMMIT"; succeeded
grep -q "^pr merge 7 --squash --auto --match-head-commit $ORDINARY_COMMIT$" <<< "$LOG" || fail "ordinary PR must still land"
! grep -q '/actions/' <<< "$LOG" || fail "ordinary config change must not read freeze state"
run_landing "$CUT_COMMIT"; failed; no_merge
WF_STATE=disabled_manually ARMED=false run_landing "$CUT_COMMIT"; succeeded
"$PY" - "$TMP/gh.log" "$CUT_COMMIT" <<'PYEOF'
import pathlib, sys
calls = pathlib.Path(sys.argv[1]).read_text().splitlines()
evidence = next(i for i, s in enumerate(calls) if s.startswith('evidence '))
admission = next(i for i, s in enumerate(calls) if '/actions/workflows/dependabot-automerge.yml' in s)
merge = calls.index(f'pr merge 7 --squash --auto --match-head-commit {sys.argv[2]}')
assert evidence < admission < merge
PYEOF
for invalid in EVIDENCE_RC=1 MERGE_STATE=BEHIND; do
  export "${invalid?}"
  WF_STATE=disabled_manually ARMED=false run_landing "$CUT_COMMIT"; failed; no_merge
  ! grep -q '/actions/' <<< "$LOG" || fail "existing gate failure must precede freeze admission"
  unset "${invalid%%=*}"
done
for invalid in "$BAD_COMMIT" "$MISSING_COMMIT"; do
  WF_STATE=disabled_manually ARMED=false run_landing "$invalid"; failed; no_merge
done
# Failed object reads are tested directly: ensure_head_local would try to fetch
# an unavailable PR head in the wrapper, which is intentionally not simulated.
(cd "$FIXTURE" && GH_REPO=florianhorner/mammamiradio bash "$HOLD" check-cut missing "$CUT_COMMIT" >/dev/null 2>&1) && fail "missing base object must refuse"
pass "real landing path gates pinned version changes after evidence; ordinary PRs retain existing behavior"
echo "All dependabot automerge gate cases passed."
