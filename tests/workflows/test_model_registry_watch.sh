#!/usr/bin/env bash
# Self-test for .github/workflows/model-registry-watch.yml
#
# Two halves. Cases 1-4 drive the real checker against the committed provider
# captures and prove the exit-code contract the workflow leans on. Cases 5-8
# take the workflow apart with the same tools test_dependabot_automerge_gate.sh
# uses: parse the YAML, assert the wiring, extract both `run:` blocks, and
# EXECUTE them with a stub checker and a mocked `gh` that logs every call. A grep
# for an `if` line proves a branch is written, not that it is taken; the first
# version of this file was all greps, and fifteen of sixteen behavior mutations
# survived it. No network anywhere.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHECKER="$REPO_ROOT/scripts/check_model_registry.py"
WF="$REPO_ROOT/.github/workflows/model-registry-watch.yml"
FIXTURES="$REPO_ROOT/tests/scripts/fixtures/model_registry"
REGISTRY="$REPO_ROOT/model_registry.toml"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

# TODAY is derived from the registry, never pinned. The checker rejects a stamp
# more than a day in the FUTURE relative to --today, so a fixed date here would
# false-fail on the next honest restamp. Reading it keeps case 1 at age zero for
# ever; case 3 supplies its own stale stamp relative to it.
TODAY="$(sed -n 's/^last_reviewed = "\([0-9][0-9-]*\)"/\1/p' "$REGISTRY" | head -1)"
[ -n "$TODAY" ] || fail "could not read last_reviewed from model_registry.toml"

# Same interpreter ladder as scripts/check-preship-evidence.sh: the checker needs
# tomllib (3.11+) and a bare python3 outside the venv can be older. Fail loud.
if [[ -n "${MAMMAMIRADIO_PYTHON:-}" ]]; then
  PYTHON_BIN="$MAMMAMIRADIO_PYTHON"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
  || { echo "FAIL: this self-test needs Python 3.11+ (set MAMMAMIRADIO_PYTHON)" >&2; exit 1; }
# A stamp 60 days behind TODAY is stale under the 45-day limit whatever TODAY is.
STALE="$("$PYTHON_BIN" -c 'import datetime as d, sys; print((d.date.fromisoformat(sys.argv[1]) - d.timedelta(days=60)).isoformat())' "$TODAY")"

TMP="$(mktemp -d)"
trap 'rm -r "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/steps" "$TMP/rt"

report() {
  # report <registry> <fixture-dir> -> exit code, output to $TMP/out. Never toggles
  # errexit; callers use `rc=0; report … || rc=$?`, which is errexit-safe.
  "$PYTHON_BIN" "$CHECKER" --report --registry "$1" --fixture-dir "$2" --today "$TODAY" >"$TMP/out" 2>&1
}

# ---------------------------------------------------------------------------
# The checker's exit-code contract, against the real registry and real captures.
# ---------------------------------------------------------------------------

# Case 1: live registry against committed fixtures is clean => exit 0. This also
# pins fixtures to pins: a bump that forgets to re-capture the provider docs goes
# red here, not in the first weekly run.
if ! report "$REGISTRY" "$FIXTURES"; then
  fail "live registry against committed fixtures should exit 0, got: $(cat "$TMP/out")"
fi
pass "live registry + committed fixtures exit 0 (the workflow's clean input is real)"

# Case 2: a pin the provider does not list is a finding => exit 1.
sed 's/^opus = ".*"/opus = "claude-opus-99-fake"/' "$REGISTRY" > "$TMP/drift.toml"
grep -q 'claude-opus-99-fake' "$TMP/drift.toml" || fail "fixture setup: could not rewrite the opus pin"
rc=0; report "$TMP/drift.toml" "$FIXTURES" || rc=$?
[ "$rc" = "1" ] || fail "an unlisted pin should exit 1, got $rc"
grep -q "unlisted" "$TMP/out" || fail "drift output should classify the pin as unlisted"
pass "unlisted pin exits 1 (a finding, not a broken watcher)"

# Case 3: a stale review stamp is ALSO exit 1, so the workflow needs one 'fail'
# branch for age and drift alike.
sed "s/^last_reviewed = .*/last_reviewed = \"$STALE\"/" "$REGISTRY" > "$TMP/stale.toml"
rc=0; report "$TMP/stale.toml" "$FIXTURES" || rc=$?
[ "$rc" = "1" ] || fail "a stale last_reviewed should exit 1, got $rc"
grep -q "last_reviewed" "$TMP/out" || fail "age failure should name last_reviewed"
pass "stale review stamp exits 1 (same branch as drift)"

# Case 4: unreadable provider docs are exit 2, distinct from a finding. This is
# the code the workflow maps to 'broken', where it diverges from
# advertised-version.yml on purpose.
rc=0; report "$REGISTRY" "$TMP/no-such-dir" || rc=$?
[ "$rc" = "2" ] || fail "unreadable fixtures should exit 2, got $rc"
pass "unreadable provider docs exit 2 (broken, not a finding)"

# ---------------------------------------------------------------------------
# The workflow itself. Parse it, assert the wiring, and extract the two run:
# blocks so the cases below can EXECUTE them.
# ---------------------------------------------------------------------------

# Case 5: structure and wiring, read from the parsed YAML rather than grepped, so a
# commented-out line cannot satisfy it. Also extracts report.sh and issue.sh.
"$PYTHON_BIN" - "$WF" "$TMP/steps" "$REPO_ROOT/.github/workflows/quality.yml" <<'PYEOF'
import pathlib, sys, yaml
wf = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
out = pathlib.Path(sys.argv[2])
on = wf.get('on') or wf.get(True)   # PyYAML reads a bare `on:` key as the boolean True
assert on['schedule'] == [{'cron': '20 9 * * 1'}], on.get('schedule')
assert 'workflow_dispatch' in on
assert wf['permissions'] == {'contents': 'read'}
assert wf['concurrency'] == {'group': 'model-registry-watch', 'cancel-in-progress': False}
job = wf['jobs']['watch']
assert job['if'] == "github.repository == 'florianhorner/mammamiradio'", job.get('if')
assert job['permissions'] == {'contents': 'read', 'issues': 'write'}, job.get('permissions')
assert job.get('timeout-minutes') == 10, job.get('timeout-minutes')
steps = job['steps']
co = steps[0]
assert co['uses'].startswith('actions/checkout@') and co['with']['persist-credentials'] is False
rep = next(s for s in steps if s.get('id') == 'report')
inv = [l for l in rep['run'].splitlines() if 'check_model_registry.py' in l and not l.strip().startswith('#')]
assert len(inv) == 1, inv
assert '--report' in inv[0], inv[0]
# The weekly run must be LIVE: pinning the date or the docs would make it green for ever.
for flag in ('--today', '--fixture-dir', '--registry'):
    assert flag not in inv[0], f"weekly invocation must not carry {flag}: {inv[0]}"
(out / 'report.sh').write_text(rep['run'])
iss = steps[-1]
assert iss['env']['GH_TOKEN'] == '${{ secrets.GITHUB_TOKEN }}', iss['env']
assert iss['env']['VERDICT'] == '${{ steps.report.outputs.verdict }}', iss['env']
assert iss['env']['DETAIL'] == '${{ steps.report.outputs.output }}', iss['env']
assert iss['env']['LABEL'] == 'model-registry-watch'
assert 'TITLE' not in iss['env'], 'title is chosen per verdict inside the step'
(out / 'issue.sh').write_text(iss['run'])
q = yaml.safe_load(pathlib.Path(sys.argv[3]).read_text())
assert any(s.get('run') == 'bash tests/workflows/test_model_registry_watch.sh'
           and s.get('if') == "needs.changes.outputs.workflows == 'true'"
           for s in q['jobs']['invariants']['steps']), "self-test not registered in quality.yml"
PYEOF
pass "weekly, fork-guarded, issues: write on the job, live invocation, outputs wired, registered in quality.yml"

# Case 6: EXECUTE the report step. A stub checker on PATH exits 0/1/2/3 with a
# two-line report; the step must survive every exit (it sets +e itself, the
# runner's shell is -e), map it to the right verdict, and round-trip both lines
# through the GITHUB_OUTPUT heredoc.
# shape=report prints a real two-line report; shape=trace prints a traceback with no
# '== ' header, the way a crash before main() looks. Exit 1 with no report is the
# watcher broken, not a pin finding with a traceback for evidence.
for tc in "0:pass:report" "1:fail:report" "2:broken:report" "3:broken:report" "1:broken:trace"; do
  IFS=: read -r code want shape <<<"$tc"
  if [ "$shape" = report ]; then
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "== Review age ==" "anthropic.opus stub-model unlisted"\nexit %s\n' "$code" > "$TMP/bin/python3"
  else
    printf '#!/usr/bin/env bash\nprintf "%%s\\n" "Traceback (most recent call last):" "ModuleNotFoundError: No module named tomllib" >&2\nexit %s\n' "$code" > "$TMP/bin/python3"
  fi
  chmod +x "$TMP/bin/python3"
  : > "$TMP/gh-out"
  (cd "$REPO_ROOT" && PATH="$TMP/bin:$PATH" GITHUB_OUTPUT="$TMP/gh-out" bash -e "$TMP/steps/report.sh" >/dev/null) \
    || fail "report step aborted on checker exit $code/$shape (lost its set +e?)"
  # Decode the runner's key=value and key<<delimiter forms. Finding the right
  # lines is not enough: an unterminated output block prevents the next step.
  "$PYTHON_BIN" - "$TMP/gh-out" "$want" "$shape" <<'PYEOF'
import pathlib, sys
lines = iter(pathlib.Path(sys.argv[1]).read_text().splitlines())
values = {}
for line in lines:
    if '<<' in line:
        key, delimiter = line.split('<<', 1)
        assert key and delimiter, f"invalid output header: {line!r}"
        body = []
        for part in lines:
            if part == delimiter:
                break
            body.append(part)
        else:
            raise AssertionError(f"unterminated output block: {key}")
        value = '\n'.join(body)
    else:
        key, separator, value = line.partition('=')
        assert key and separator, f"invalid output assignment: {line!r}"
    assert key not in values, f"duplicate output: {key}"
    values[key] = value
expected_output = (
    '== Review age ==\nanthropic.opus stub-model unlisted'
    if sys.argv[3] == 'report'
    else 'Traceback (most recent call last):\nModuleNotFoundError: No module named tomllib'
)
assert values == {'output': expected_output, 'verdict': sys.argv[2]}, values
PYEOF
done
rm -f "$TMP/bin/python3"
pass "report step: 0/1/2/3 map to pass/fail/broken/broken, a traceback on exit 1 is broken, output round-trips"

# Case 7: EXECUTE the issue step against a gh mock that logs every call, across
# verdict x existing-issue. Exact call lines, ordering, and body contents.
cat > "$TMP/bin/gh" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_MOCK_LOG"
if [ "$1 $2" = "${GH_FAIL_ACTION:-}" ]; then
  exit 23
fi
case "$1 $2" in
  "issue list")   printf '%s' "${GH_EXISTING:-}"; exit 0 ;;
  "label create") exit "${GH_LABEL_RC:-0}" ;;
  "issue create"|"issue comment"|"issue close") echo "https://example.invalid/issues/${GH_EXISTING:-99}"; exit 0 ;;
esac
echo "gh mock: unexpected call: $*" >&2
exit 1
MOCK
chmod +x "$TMP/bin/gh"
DETAIL_TEXT=$'== Providers (drift) ==\nanthropic.opus claude-opus-99-fake unlisted (current: claude-opus-5)'
BODY_FILE="$TMP/rt/model-registry-watch.md"

run_issue() { # run_issue <verdict> <existing> [GH_LABEL_RC]
  : > "$TMP/gh.log"; rm -f "$BODY_FILE"
  (cd "$REPO_ROOT" && PATH="$TMP/bin:$PATH" GH_TOKEN=x GH_MOCK_LOG="$TMP/gh.log" GH_EXISTING="$2" \
     GH_LABEL_RC="${3:-0}" RUNNER_TEMP="$TMP/rt" VERDICT="$1" DETAIL="$DETAIL_TEXT" \
     LABEL="model-registry-watch" \
     bash -e "$TMP/steps/issue.sh" >/dev/null) || fail "issue step aborted for verdict=$1 existing='$2'"
}
calls() { grep -c "^$1" "$TMP/gh.log" || true; }

# Every cell lists issues by LABEL, never by title or unfiltered.
run_issue pass ""
grep -q '^issue list .*--label model-registry-watch' "$TMP/gh.log" || fail "issue list must filter on the watch label"
[ "$(wc -l < "$TMP/gh.log" | tr -d ' ')" = "1" ] || fail "pass with no open issue must make no mutation, got: $(cat "$TMP/gh.log")"
[ ! -f "$BODY_FILE" ] || fail "pass must not write an issue body"
pass "pass + no issue: one list call, nothing mutated"

run_issue pass 42
grep -q '^issue close 42' "$TMP/gh.log" || fail "pass with an open issue must close it"
{ [ "$(calls 'issue create')" = 0 ] && [ "$(calls 'issue comment')" = 0 ]; } || fail "pass must not create or comment"
pass "pass + open issue: closed, nothing else"

run_issue fail ""
LBL="$(grep -n '^label create model-registry-watch --force' "$TMP/gh.log" | cut -d: -f1)"
CRT="$(grep -n '^issue create ' "$TMP/gh.log" | cut -d: -f1)"
{ [ -n "$LBL" ] && [ -n "$CRT" ] && [ "$LBL" -lt "$CRT" ]; } || fail "fail must create the label BEFORE the issue (first-ever finding dies on a missing label otherwise)"
grep -q '^issue create --title Model registry needs a decision --label model-registry-watch --body-file' "$TMP/gh.log" \
  || fail "issue create must carry the title, the label, and a body file"
{ [ "$(calls 'issue comment')" = 0 ] && [ "$(calls 'issue close')" = 0 ]; } || fail "fail with no issue must not comment or close"
grep -q 'found something to decide' "$BODY_FILE" || fail "fail body must be the decision copy"
grep -qF 'claude-opus-99-fake unlisted' "$BODY_FILE" || fail "fail body must include the checker report"
grep -q 'closes itself once a weekly run comes back clean' "$BODY_FILE" || fail "body must say how it resolves"
pass "fail + no issue: label then create, decision body carries the report"

run_issue fail 42
grep -q '^issue comment 42 --body-file' "$TMP/gh.log" || fail "fail with an open issue must comment on it"
{ [ "$(calls 'issue create')" = 0 ] && [ "$(calls 'issue close')" = 0 ]; } || fail "fail with an open issue must not create or close"
grep -qF 'claude-opus-99-fake unlisted' "$BODY_FILE" || fail "comment body must include this week's report"
pass "fail + open issue: comment with the report, no duplicate"

run_issue broken ""
[ "$(calls 'issue create')" = 1 ] || fail "broken must open an issue (a silent dead watcher is the failure this exists to catch)"
grep -q '^issue create --title Model registry watch could not complete --label' "$TMP/gh.log" || fail "broken must open under the watcher title, not the decision title"
grep -q 'could not complete its checks' "$BODY_FILE" || fail "broken body must name the watcher, not a pin"
grep -qF 'python3 scripts/check_model_registry.py --report' "$BODY_FILE" || fail "broken recovery must rerun age and providers together"
grep -q 'found something to decide' "$BODY_FILE" && fail "broken must not reuse the decision copy"
grep -qF 'claude-opus-99-fake unlisted' "$BODY_FILE" || fail "broken body must still include the raw output"
pass "broken + no issue: opened with the watcher body"

run_issue broken 42
{ [ "$(calls 'issue comment 42')" = 1 ] && [ "$(calls 'issue close')" = 0 ]; } || fail "broken with an open issue must comment, never close"
grep -q 'could not complete its checks' "$BODY_FILE" || fail "broken comment must name the watcher"
pass "broken + open issue: comment, never close"

# --force makes an existing label a no-op, so the only way `label create` fails is
# a real one (token cannot manage labels). That must go red HERE, with gh's own
# message, not one line later as "could not add label" on the issue create.
: > "$TMP/gh.log"; rm -f "$BODY_FILE"
if (cd "$REPO_ROOT" && PATH="$TMP/bin:$PATH" GH_TOKEN=x GH_MOCK_LOG="$TMP/gh.log" GH_EXISTING="" GH_LABEL_RC=1 \
      RUNNER_TEMP="$TMP/rt" VERDICT=fail DETAIL="$DETAIL_TEXT" LABEL="model-registry-watch" \
      bash -e "$TMP/steps/issue.sh" >/dev/null 2>&1); then
  fail "a real label-create failure must abort the step, not be swallowed"
fi
[ "$(calls 'issue create')" = 0 ] || fail "after a label failure no issue must be created"
pass "a real label-create failure aborts loudly before the issue is filed"

# Any GitHub error must fail the run and stop further issue actions. Exercise
# the same error on finding/broken paths, because both must remain visible.
for tc in "list:fail:" "list:pass:42" "create:fail:" "create:broken:" "comment:fail:42" "comment:broken:42" "close:pass:42"; do
  IFS=: read -r action verdict existing <<<"$tc"
  : > "$TMP/gh.log"; rm -f "$BODY_FILE"
  rc=0
  (cd "$REPO_ROOT" && PATH="$TMP/bin:$PATH" GH_TOKEN=x GH_MOCK_LOG="$TMP/gh.log" GH_EXISTING="$existing" \
      GH_FAIL_ACTION="issue $action" RUNNER_TEMP="$TMP/rt" VERDICT="$verdict" DETAIL="$DETAIL_TEXT" \
      LABEL="model-registry-watch" \
      bash -e "$TMP/steps/issue.sh" >/dev/null 2>&1) || rc=$?
  [ "$rc" = 23 ] || fail "issue $action error must propagate for $verdict, got $rc"
  [ "$(calls "issue $action")" = 1 ] || fail "issue $action must be attempted exactly once"
  case "$(tail -1 "$TMP/gh.log")" in
    "issue $action"*) ;;
    *) fail "no further GitHub action may follow a failed issue $action" ;;
  esac
done
pass "list/create/comment/close errors fail the run without follow-up actions (7 scenarios)"

# Case 8: --report stays out of every PR and cut path. The checker's own contract:
# a calendar gate on unrelated PRs gets bumped reflexively. Match invocations, not
# mentions; comments explaining why it lives elsewhere must stay legal.
"$PYTHON_BIN" - "$REPO_ROOT" <<'PYEOF'
import pathlib, shlex, sys, yaml

def fold_shell_lines(source):
    # Delete shell continuations before tokenizing, even inside a word. Preserve
    # single-quoted text and escaped backslashes. Comments retain their newline
    # boundary: a backslash at the end of a comment does not continue that line.
    result, quote, in_word, index = [], None, False, 0
    while index < len(source):
        char = source[index]
        if char == '#' and quote is None and not in_word:
            newline = source.find('\n', index)
            index = newline if newline >= 0 else len(source)
            continue
        if char == '\\' and quote != "'" and index + 1 < len(source):
            escaped = source[index + 1]
            if escaped != '\n':
                result.extend((char, escaped))
                in_word = True
            index += 2
            continue
        if char in ('"', "'"):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            in_word = True
        elif quote is None:
            in_word = char not in ' \t\r\n;&|()'
        result.append(char)
        index += 1
    return ''.join(result)

def has_report(words):
    if not words or words[0] in ('echo', 'printf'):
        return False
    return '--report' in words and any(
        word in ('check_model_registry.py', '$checker', '${checker}')
        or word.endswith('/check_model_registry.py') for word in words
    )

def forbidden_report(source):
    # A static guard for literal commands, not a Bash evaluator. YAML folding
    # and shell continuations are resolved before matching command arguments.
    lexer = shlex.shlex(fold_shell_lines(source), posix=True, punctuation_chars=';&|()\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    lexer.commenters = ''  # comments were removed without losing newlines above
    command = []
    for token in lexer:
        if token and all(char in ';&|()\n' for char in token):
            if has_report(command):
                return True
            command = []
        else:
            command.append(token)
    return has_report(command)

reject = [
    'python3 scripts/check_model_registry.py --report',
    'python3 scripts/check_model_registry.py \\\n  --report',
    'python3 scripts/check_model_registry.py\\\n  --report',
    'python3 scripts/check_model_registry.py --re\\\nport',
    'python3 "scripts/check_model_\\\nregistry.py" --report',
    'python3   "scripts/check_model_registry.py"   --report',
    'python3 scripts/check_model_registry.py --today 2026-09-10 --report',
    '"$python" "$checker" --report',
    '# comment \\\npython3 scripts/check_model_registry.py --report',
    yaml.safe_load('run: >-\n  python3 scripts/check_model_registry.py\n  --report\n')['run'],
]
allow = [
    '# python3 scripts/check_model_registry.py --report',
    '# comment \\\n',
    'python3 scripts/check_model_registry.py \\\\\nother --report',
    "echo 'scripts/check_model_registry.py \\\n--report'",
    'python3 other.py # scripts/check_model_registry.py --report',
    'echo "python3 scripts/check_model_registry.py --report"',
    "printf '%s\\n' 'python3 scripts/check_model_registry.py --report'",
    '"$python" "$checker" --providers --gate liveness \\\n  "${args[@]}"',
    'python3 scripts/check_model_registry.py --age # comment\nother --report',
    *[f'python3 scripts/check_model_registry.py --age{sep}other --report'
      for sep in ('\n', '; ', ' && ')],
]
for source in reject:
    assert forbidden_report(source), f'guard missed a live report: {source!r}'
for source in allow:
    assert not forbidden_report(source), f'guard rejected a non-report: {source!r}'

root = pathlib.Path(sys.argv[1])
quality = yaml.safe_load((root / '.github/workflows/quality.yml').read_text())
sources = [(f'quality.yml:{job_name}:{index}', step['run'])
           for job_name, job in quality['jobs'].items()
           for index, step in enumerate(job.get('steps', [])) if 'run' in step]
for rel in ('scripts/pre-release-check.sh', 'scripts/model-registry-gate.sh'):
    sources.append((rel, (root / rel).read_text()))
recipe = []
in_recipe = False
for line in (root / 'Makefile').read_text().splitlines(keepends=True):
    if line.startswith('pre-release:'):
        in_recipe = True
    elif in_recipe and line.startswith('\t'):
        recipe.append(line)
    elif line.strip():
        in_recipe = False
sources.append(('Makefile:pre-release', ''.join(recipe)))
for name, source in sources:
    assert not forbidden_report(source), f'--report must stay out of PR/cut paths: {name}'
print(f'Checked {len(reject) + len(allow)} report-guard regression examples.')
PYEOF
pass "--report absent from the PR and cut paths"

CASE_COUNT="$(grep -c '^pass ' "$0")"
echo
echo "All $CASE_COUNT model-registry-watch cases passed."
