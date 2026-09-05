#!/usr/bin/env bash
# Contract + runtime test for the Dependabot cut-window gate:
#   .github/workflows/dependabot-automerge.yml and scripts/dependabot-window-hold.sh
#
# Static half: the workflow is PARSED (PyYAML), not grepped, so a comment or a key
# reorder cannot satisfy an assertion. Runtime half: the verdict step body runs
# under `bash -e` (what GitHub uses for `run:`) against a stub
# check-advertised-version.sh, and both script verbs run against a mocked `gh`
# that logs every call. No network.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"
WF="${WF_OVERRIDE:-.github/workflows/dependabot-automerge.yml}"
HOLD="scripts/dependabot-window-hold.sh"
fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[ -f "$WF" ] || fail "workflow missing"
[ -x "$HOLD" ] || chmod +x "$HOLD"

PY="python3"
[ -x .venv/bin/python ] && PY=".venv/bin/python"
"$PY" -c 'import yaml' 2>/dev/null || fail "PyYAML is required (pip install -r requirements-dev.txt)"

# ---- static: structure of the workflow -------------------------------------------
"$PY" - "$WF" <<'PYEOF' || fail "workflow structure check failed (see message above)"
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
on = wf.get(True) or wf.get("on")
def die(msg): print("structure: " + msg); sys.exit(1)

types = (on.get("pull_request_target") or {}).get("types") or []
for ev in ["opened", "reopened", "synchronize", "labeled", "unlabeled", "ready_for_review"]:
    ev in types or die(f"pull_request_target must keep the {ev} event (synchronize re-evaluates a pre-armed PR)")
push = on.get("push") or {}
push.get("branches") == ["main"] or die("sweep must run on push to main")
"ha-addon/mammamiradio/config.yaml" in (push.get("paths") or []) or die("sweep must fire on the cut commit (config.yaml path)")
on.get("schedule") or die("sweep needs a schedule so a disarmed PR re-arms after the window closes")
"workflow_dispatch" in on or die("sweep must be runnable on demand")

jobs = wf["jobs"]
job = jobs.get("enable-automerge") or die("enable-automerge job missing")
jif = job.get("if", "")
"github.event_name == 'pull_request_target'" in jif or die("enable-automerge must run only on pull_request_target")
"dependabot[bot]" in jif or die("enable-automerge must be limited to dependabot[bot] PRs (never touch human PRs armed by land-pr.sh)")

def step(job, pred, what):
    for s in job["steps"]:
        if pred(s): return s
    die(f"{what} step missing")

for name, j in (("enable-automerge", job), ("sweep", jobs.get("sweep") or die("sweep job missing"))):
    co = step(j, lambda s: str(s.get("uses", "")).startswith("actions/checkout@"), f"{name} checkout")
    (co.get("with") or {}).get("persist-credentials") is False or die(f"{name}: checkout must set persist-credentials: false")
    "ref" not in (co.get("with") or {}) or die(f"{name}: checkout must not select a ref (base branch only)")
    adv = step(j, lambda s: s.get("id") == "advertised", f"{name} verdict")
    run = adv.get("run", "")
    for needle in ["set +e", "bash scripts/check-advertised-version.sh", "sed -n 's/^VERDICT: //p'", '[ -n "$VERDICT" ] || VERDICT="unknown"', 'echo "verdict=$VERDICT" >> "$GITHUB_OUTPUT"']:
        needle in run or die(f"{name}: verdict step must contain {needle!r}")
    run.index("set +e") < run.index("bash scripts/check-advertised-version.sh") or die(f"{name}: set +e must precede the script call")

arm = step(job, lambda s: "Enable automerge" in str(s.get("name", "")), "arm")
aif = " ".join(str(arm.get("if", "")).split())
"steps.advertised.outputs.verdict == 'pass'" in aif or die("arm step must require verdict == pass")
"version-update:semver-patch" in aif and "version-update:semver-minor" in aif or die("arm step must stay limited to patch and minor")
"semver-major" not in aif or die("arm step must never include major updates")
"gh pr merge --squash --auto" in arm.get("run", "") or die("arm step must arm auto-merge")
"GH_TOKEN" in (arm.get("env") or {}) or die("arm step needs GH_TOKEN")

dis = step(job, lambda s: "Disarm automerge" in str(s.get("name", "")), "disarm")
"steps.advertised.outputs.verdict == 'fail'" in str(dis.get("if", "")) or die("disarm step must key on verdict == fail")
"scripts/dependabot-window-hold.sh disarm" in dis.get("run", "") or die("disarm step must call the hold script")
"|| true" not in dis.get("run", "") or die("disarm step must not swallow failures")
"GH_TOKEN" in (dis.get("env") or {}) or die("disarm step needs GH_TOKEN")

unk = step(job, lambda s: "steps.advertised.outputs.verdict == 'unknown'" in str(s.get("if", "")), "unknown")
"gh pr" not in unk.get("run", "") or die("unknown verdict must not arm or disarm")

sw = jobs["sweep"]
"github.event_name != 'pull_request_target'" in str(sw.get("if", "")) or die("sweep must not run on PR events")
rec = step(sw, lambda s: "scripts/dependabot-window-hold.sh sweep" in str(s.get("run", "")), "reconcile")
"GH_TOKEN" in (rec.get("env") or {}) or die("reconcile step needs GH_TOKEN")
print("structure ok")
PYEOF
pass "workflow structure: base checkout, verdict step, arm/disarm/unknown branches, sweep job, triggers"

for f in "$WF" .github/workflows/advertised-version.yml; do
  grep -q "sed -n 's/^VERDICT: //p'" "$f" || fail "$f: VERDICT parsing drifted"
  # shellcheck disable=SC2016  # the literal $VERDICT text is what we look for
  grep -q '\[ -n "\$VERDICT" \] || VERDICT="unknown"' "$f" || fail "$f: empty-verdict default drifted"
done
pass "both VERDICT readers parse the same way"

# ---- runtime: the verdict step body under bash -e --------------------------------
STEP_BODY="$("$PY" - "$WF" <<'PYEOF'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1]))
for s in wf["jobs"]["enable-automerge"]["steps"]:
    if s.get("id") == "advertised":
        print(s["run"]); break
PYEOF
)"
[ -n "$STEP_BODY" ] || fail "could not extract the verdict step body"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/automerge-gate.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/scripts" "$TMP/bin"
run_step() { # <stub-stdout> <stub-exit> -> GITHUB_OUTPUT contents
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "%s"\nexit %s\n' "$1" "$2" > "$TMP/scripts/check-advertised-version.sh"
  chmod +x "$TMP/scripts/check-advertised-version.sh"
  : > "$TMP/out"
  ( cd "$TMP" && GITHUB_OUTPUT="$TMP/out" bash -e -c "$STEP_BODY" >/dev/null 2>&1 ) || echo "STEP_EXIT=$?" >> "$TMP/out"
  cat "$TMP/out"
}
[ "$(run_step 'VERDICT: pass' 0)" = "verdict=pass" ]       || fail "pass verdict must reach GITHUB_OUTPUT (got: $(run_step 'VERDICT: pass' 0))"
[ "$(run_step 'VERDICT: fail' 1)" = "verdict=fail" ]       || fail "a fail verdict exits 1; the step must survive it (got: $(run_step 'VERDICT: fail' 1))"
[ "$(run_step 'VERDICT: unknown' 0)" = "verdict=unknown" ] || fail "unknown verdict must reach GITHUB_OUTPUT"
[ "$(run_step 'no verdict line' 2)" = "verdict=unknown" ]  || fail "no VERDICT line must default to unknown (got: $(run_step 'no verdict line' 2))"
pass "verdict step survives a fail exit code and records pass/fail/unknown"

# ---- runtime: the hold script against a mocked gh --------------------------------
# Env: GH_MOCK_ARMED_<n>=true|false (pr view), GH_MOCK_DISABLE_FAIL=1 (disable errors),
# GH_MOCK_LIST (pre-rendered TSV the `pr list --jq` would print), GH_MOCK_LOG.
cat > "$TMP/bin/gh" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  "pr view")
    n="$3"; v="GH_MOCK_ARMED_$n"; echo "${!v:-false}" ;;
  "pr merge")
    if [ "$3" = "--disable-auto" ] && [ -n "${GH_MOCK_DISABLE_FAIL:-}" ]; then echo "HTTP 502: bad gateway" >&2; exit 1; fi ;;
  "pr edit") ;;
  "pr list") printf '%s\n' "${GH_MOCK_LIST:-}" ;;
  "label create") ;;
  *) echo "MOCK gh: unexpected '$*'" >&2; exit 99 ;;
esac
exit 0
MOCK
chmod +x "$TMP/bin/gh"
run_hold() { # args... ; env passed via the caller. Sets RC, OUT, LOG.
  : > "$TMP/gh.log"; RC=0
  OUT="$(PATH="$TMP/bin:$PATH" GH_MOCK_LOG="$TMP/gh.log" bash "$HOLD" "$@" 2>&1)" || RC=$?
  LOG="$(cat "$TMP/gh.log")"
}

GH_MOCK_ARMED_10=true run_hold disarm 10
[ "$RC" -eq 0 ] || fail "disarm of an armed PR should succeed (rc $RC): $OUT"
grep -q '^pr merge --disable-auto 10$' <<<"$LOG" || fail "disarm must call --disable-auto on the armed PR"
grep -q '^pr edit 10 --add-label cut-window-hold$' <<<"$LOG" || fail "disarm must label the PR it paused"
grep -q '::warning' <<<"$OUT" || fail "disarm must warn after it succeeded"
pass "disarm: armed PR is disarmed and labelled"

GH_MOCK_ARMED_11=false run_hold disarm 11
[ "$RC" -eq 0 ] || fail "disarm of an unarmed PR should be a clean no-op (rc $RC): $OUT"
grep -q 'pr merge' <<<"$LOG" && fail "disarm must not call the merge API on an unarmed PR"
grep -q '::notice' <<<"$OUT" || fail "disarm of an unarmed PR must say nothing to disarm"
pass "disarm: unarmed PR is a no-op, not an error"

GH_MOCK_ARMED_12=true GH_MOCK_DISABLE_FAIL=1 run_hold disarm 12
[ "$RC" -ne 0 ] || fail "a failed disable must fail the step, not report success"
grep -q '::error' <<<"$OUT" || fail "a failed disable must emit ::error"
grep -q 'add-label' <<<"$LOG" && fail "a failed disable must not label the PR as held"
pass "disarm: API failure is loud (no swallowed || true)"

run_hold sweep unknown
[ "$RC" -eq 0 ] && [ -z "$LOG" ] || fail "sweep unknown must touch nothing (rc $RC, calls: $LOG)"
pass "sweep: unknown verdict touches nothing"

LIST_FAIL=$'10\tarmed\tnohold\tchore(deps): bump ruff from 0.16.4 to 0.16.5\n11\toff\tnohold\tchore(deps): bump click from 8.4.2 to 8.5.0'
GH_MOCK_LIST="$LIST_FAIL" GH_MOCK_ARMED_10=true run_hold sweep fail
[ "$RC" -eq 0 ] || fail "sweep fail should succeed (rc $RC): $OUT"
grep -q '^pr merge --disable-auto 10$' <<<"$LOG" || fail "sweep fail must disarm the armed PR"
grep -q 'disable-auto 11' <<<"$LOG" && fail "sweep fail must leave unarmed PRs alone"
pass "sweep: open window disarms only armed PRs"

LIST_PASS=$'20\toff\thold\tchore(deps): bump ruff from 0.16.4 to 0.16.5\n21\toff\thold\tchore(deps): bump anthropic from 0.122.0 to 1.2.0\n22\toff\tnohold\tchore(deps): bump click from 8.4.2 to 8.5.0\n23\toff\thold\tchore(deps-dev): bump the dev-tools group with 2 updates'
GH_MOCK_LIST="$LIST_PASS" run_hold sweep pass
[ "$RC" -eq 0 ] || fail "sweep pass should succeed (rc $RC): $OUT"
grep -q '^pr merge --squash --auto 20$' <<<"$LOG" || fail "sweep pass must re-arm a held patch update"
grep -q '^pr edit 20 --remove-label cut-window-hold$' <<<"$LOG" || fail "sweep pass must drop the hold label it re-armed"
grep -q 'auto 21' <<<"$LOG" && fail "sweep pass must not arm a held MAJOR update"
grep -q 'auto 22' <<<"$LOG" && fail "sweep pass must not touch a PR it never held (a maintainer's manual disarm)"
grep -q 'auto 23' <<<"$LOG" && fail "sweep pass must not arm a group update it cannot classify"
pass "sweep: closed window re-arms only held patch/minor updates"

[ "$(bash "$HOLD" classify 'chore(deps): bump ruff from 0.16.4 to 0.16.5')" = patch ] || fail "classify patch"
[ "$(bash "$HOLD" classify 'chore(deps): bump websockets from 17.0.1 to 17.1')" = minor ] || fail "classify minor (two-part version)"
[ "$(bash "$HOLD" classify 'chore(deps): bump anthropic from 0.122.0 to 1.2.0')" = major ] || fail "classify major"
[ "$(bash "$HOLD" classify 'chore(deps): bump actions/checkout from v7.0.0 to v7.0.1')" = patch ] || fail "classify with v prefix"
[ "$(bash "$HOLD" classify 'chore(deps-dev): bump the dev-tools group with 2 updates')" = unknown ] || fail "classify group -> unknown"
pass "classify: patch/minor/major/unknown from Dependabot titles"

echo "All dependabot automerge gate cases passed."
