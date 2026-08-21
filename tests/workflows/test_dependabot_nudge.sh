#!/usr/bin/env bash
# Self-test for scripts/nudge-dependabot-rebase.sh
#
# Drives the nudge script with a mocked `gh` (PATH shim), asserting it
# comments only on behind Dependabot PRs, skips PRs with an un-actioned
# nudge, keeps asking while GitHub still reports UNKNOWN mergeability,
# surfaces human-authored PRs that are armed-but-behind without ever
# confusing them with bot PRs, and degrades without failing when gh
# errors or the environment is hostile. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NUDGE="$REPO_ROOT/scripts/nudge-dependabot-rebase.sh"
cd "$REPO_ROOT"

# Assert, never repair: a `chmod +x` here would be a test writing into the repo,
# and it would silently mask a lost mode bit instead of failing on it.
[[ -x "$NUDGE" ]] || { echo "FAIL: $NUDGE is not executable" >&2; exit 1; }

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  OLD_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"      # BSD/macOS
else
  OLD_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"  # GNU/Linux CI
fi

# ---- mock gh ----------------------------------------------------------------
# The script makes ONE `gh pr list` call and partitions the reply by author,
# so the mock returns a single unfiltered array.
# Env-driven:
#   GH_MOCK_JSON        JSON array `gh pr list` returns
#   GH_MOCK_JSON_<N>    Nth reply (overrides the above), so a test can hand
#                       back UNKNOWN first and a settled answer after
#   GH_MOCK_LIST_FAIL   non-empty => `gh pr list` exits 1
#   GH_MOCK_VIEW_<N>    JSON body for `gh pr view N`
#   GH_MOCK_COMMENT_FAIL non-empty => `gh pr comment` exits 1
# Calls are appended to $GH_MOCK_LOG.
MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  "pr list")
    [ -n "${GH_MOCK_LIST_FAIL:-}" ] && exit 1
    n=$(( $(cat "$GH_MOCK_COUNTER" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$GH_MOCK_COUNTER"
    var="GH_MOCK_JSON_$n"
    if [ -n "${!var:-}" ]; then printf '%s\n' "${!var}"
    else printf '%s\n' "${GH_MOCK_JSON:-[]}"; fi
    ;;
  "pr view")
    var="GH_MOCK_VIEW_$3"
    printf '%s\n' "${!var:?no mock for PR $3}"
    ;;
  "pr comment")
    [ -n "${GH_MOCK_COMMENT_FAIL:-}" ] && exit 1
    ;;
esac
exit 0
MOCK
chmod +x "$MOCK_BIN/gh"

# run_nudge [env overrides...] -> RUN_RC, log at $GH_MOCK_LOG, stdout at $RUN_OUT
run_nudge() {
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  GH_MOCK_COUNTER="$TMPDIR_T/counter"; : > "$GH_MOCK_COUNTER"
  RUN_OUT="$TMPDIR_T/out.txt"; : > "$RUN_OUT"
  RUN_RC=0
  env PATH="$MOCK_BIN:$PATH" GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_COUNTER="$GH_MOCK_COUNTER" \
    NUDGE_MERGE_STATE_DELAY=0 "$@" bash "$NUDGE" >"$RUN_OUT" 2>&1 || RUN_RC=$?
}

commented_on() { grep -q "pr comment $1 " "$GH_MOCK_LOG"; }
never_commented() { ! grep -q "pr comment" "$GH_MOCK_LOG"; }
list_calls() { grep -c "^pr list" "$GH_MOCK_LOG"; }
reported() { grep -q "armed but behind" "$RUN_OUT"; }

# JSON entry builders. `j` joins entries into an array.
bot()   { printf '{"number":%s,"mergeStateStatus":"%s","autoMergeRequest":%s,"author":{"login":"app/dependabot"}}' "$1" "$2" "${3:-null}"; }
human() { printf '{"number":%s,"mergeStateStatus":"%s","autoMergeRequest":%s,"author":{"login":"florianhorner"}}' "$1" "$2" "${3:-null}"; }
j()     { local IFS=,; printf '[%s]' "$*"; }
ARMED='{"enabledAt":"2026-08-20T07:00:00Z"}'

VIEW_NO_NUDGE='{"commits":[{"committedDate":"'"$OLD_ISO"'"}],"comments":[]}'
VIEW_FRESH_NUDGE='{"commits":[{"committedDate":"'"$OLD_ISO"'"}],"comments":[{"body":"@dependabot rebase","createdAt":"'"$NOW_ISO"'"}]}'
VIEW_ACTIONED_NUDGE='{"commits":[{"committedDate":"'"$NOW_ISO"'"}],"comments":[{"body":"@dependabot rebase","createdAt":"'"$OLD_ISO"'"}]}'

# Case 1: behind bot PR, never nudged => comments
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)")" GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "behind PR without nudge should get a comment (exit code)"
commented_on 12 || fail "behind PR without nudge should get a comment"
pass "behind PR gets @dependabot rebase comment"

# Case 2: behind PR with an UN-ACTIONED nudge (comment newer than last commit) => skip
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)")" GH_MOCK_VIEW_12="$VIEW_FRESH_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "un-actioned nudge must not be repeated (exit code)"
never_commented || fail "un-actioned nudge must not be repeated"
pass "un-actioned nudge is not repeated (idempotent)"

# Case 3: prior nudge was ACTIONED (commit newer than comment) => comments again
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)")" GH_MOCK_VIEW_12="$VIEW_ACTIONED_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "actioned nudge followed by new staleness should re-comment (exit code)"
commented_on 12 || fail "actioned nudge followed by new staleness should re-comment"
pass "re-nudges after the previous nudge was actioned"

# Case 4: no behind PRs => no comments, exit 0
run_nudge GH_MOCK_JSON="$(j "$(bot 12 CLEAN)")"
[ "$RUN_RC" -eq 0 ] || fail "no behind PRs should be a silent no-op (exit code)"
never_commented || fail "no behind PRs should be a silent no-op"
pass "no behind PRs is a no-op"

# Case 5: gh pr list fails => exit 0 (never fail the main-branch workflow)
run_nudge GH_MOCK_LIST_FAIL=1
[ "$RUN_RC" -eq 0 ] || fail "list failure must degrade to a no-op (exit code)"
never_commented || fail "list failure must degrade to a no-op"
pass "list failure degrades to no-op"

# Case 6: comment fails => script still exits 0 and continues
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)")" GH_MOCK_VIEW_12="$VIEW_NO_NUDGE" GH_MOCK_COMMENT_FAIL=1
[ "$RUN_RC" -eq 0 ] || fail "comment failure must not fail the run"
pass "comment failure is non-fatal"

# Case 7: multiple behind PRs => each gets exactly one comment
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)" "$(bot 34 BEHIND)")" \
  GH_MOCK_VIEW_12="$VIEW_NO_NUDGE" GH_MOCK_VIEW_34="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "every behind PR should be nudged (exit code)"
commented_on 12 || fail "every behind PR should be nudged"
commented_on 34 || fail "every behind PR should be nudged"
[ "$(grep -c "pr comment" "$GH_MOCK_LOG")" -eq 2 ] || fail "exactly one comment per PR"
pass "multiple behind PRs each nudged once"

# Case 8: non-numeric PR identifier (hostile input) => dropped before the shell.
# Asserting `never_commented` alone is inert: the mock has no view fixture for
# the junk key, so the PR would be skipped anyway and the assertion would pass
# with the sanitiser deleted. Pin the guard itself — the junk must never reach
# `gh` as an argument at all.
run_nudge GH_MOCK_JSON='[{"number":"12; rm -rf /","mergeStateStatus":"BEHIND","autoMergeRequest":null,"author":{"login":"app/dependabot"}}]'
[ "$RUN_RC" -eq 0 ] || fail "non-numeric PR identifiers must be ignored (exit code)"
never_commented || fail "non-numeric PR identifiers must be ignored"
grep -q "pr view 12;" "$GH_MOCK_LOG" && fail "junk identifier must never be passed to gh"
pass "non-numeric list entries never reach gh"

# Case 9: `gh pr view` fails for a listed PR (no mock set => mock errors)
# => that PR is skipped non-fatally, others still processed
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)" "$(bot 34 BEHIND)")" GH_MOCK_VIEW_34="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "per-PR view failure must be non-fatal (exit code)"
grep -q "pr comment 12 " "$GH_MOCK_LOG" && fail "unreadable PR must be skipped"
commented_on 34 || fail "later PRs must still be processed after a view failure"
pass "per-PR view failure skips that PR, continues"

# ---- the 2026-08-20 deadlock: GitHub answers UNKNOWN right after a push ------

# Case 10: UNKNOWN first, BEHIND on the retry => re-asks and nudges.
# Without the retry this is the silent "nothing to do" that parked #944/#908.
run_nudge \
  GH_MOCK_JSON_1="$(j "$(bot 12 UNKNOWN)")" \
  GH_MOCK_JSON_2="$(j "$(bot 12 BEHIND)")" \
  GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "UNKNOWN then BEHIND should nudge (exit code)"
commented_on 12 || fail "UNKNOWN mergeability must be re-asked, not treated as 'not behind'"
[ "$(list_calls)" -eq 2 ] || fail "should have asked twice: UNKNOWN then BEHIND (got $(list_calls))"
pass "re-asks while GitHub reports UNKNOWN, then nudges"

# Case 11: UNKNOWN forever => bounded, exits 0, comments on nobody
run_nudge GH_MOCK_JSON="$(j "$(bot 12 UNKNOWN)")" NUDGE_MERGE_STATE_TRIES=3
[ "$RUN_RC" -eq 0 ] || fail "permanent UNKNOWN must not fail the workflow"
never_commented || fail "permanent UNKNOWN must not be guessed as BEHIND"
[ "$(list_calls)" -eq 3 ] || fail "permanent UNKNOWN should exhaust its retries (got $(list_calls))"
grep -q "still reports UNKNOWN" "$RUN_OUT" \
  || fail "giving up on UNKNOWN must say so — it is the only operator-visible signal"
pass "permanent UNKNOWN is bounded, announced, and never guessed"

# Case 11b: a non-numeric delay must fall back, not degenerate the settle loop
# into a zero-wait spin — that is the 2026-08-20 silent no-op wearing a hat.
run_nudge NUDGE_MERGE_STATE_DELAY=abc NUDGE_MERGE_STATE_TRIES=2 \
  GH_MOCK_JSON_1="$(j "$(bot 12 UNKNOWN)")" \
  GH_MOCK_JSON_2="$(j "$(bot 12 BEHIND)")" \
  GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "non-numeric delay must not fail the run"
grep -q "invalid time interval" "$RUN_OUT" && fail "non-numeric delay must fall back, not reach sleep"
commented_on 12 || fail "non-numeric delay must still complete the settle loop"
pass "non-numeric retry delay falls back to the default"

# Case 11c: no open PRs at all — the genuinely empty reply, not a CLEAN one
run_nudge GH_MOCK_JSON='[]'
[ "$RUN_RC" -eq 0 ] || fail "an empty PR list must be a clean no-op"
never_commented || fail "an empty PR list must comment on nobody"
reported && fail "an empty PR list must report nobody"
pass "zero open PRs is a clean no-op"

# Case 12: a settled reply costs exactly one list call — no retry, no double fetch
run_nudge GH_MOCK_JSON="$(j "$(bot 12 CLEAN)" "$(human 990 BEHIND "$ARMED")")" NUDGE_MERGE_STATE_TRIES=6
[ "$RUN_RC" -eq 0 ] || fail "settled reply should not retry (exit code)"
[ "$(list_calls)" -eq 1 ] || fail "one list call must serve both bot and human paths (got $(list_calls))"
pass "one settled list call serves both audiences"

# Case 13: `--limit` is passed AND carries the intended value, so >30 open PRs
# are not silently truncated. Asserting the bare flag would let `--limit 1` pass.
run_nudge GH_MOCK_JSON="$(j "$(bot 12 CLEAN)")"
grep -q -- "--limit 100" "$GH_MOCK_LOG" || fail "pr list must pass --limit 100 (CLI default 30 truncates)"
pass "list call passes --limit 100"

# Case 13b: a non-numeric limit falls back rather than truncating to nothing
run_nudge NUDGE_LIST_LIMIT=abc GH_MOCK_JSON="$(j "$(bot 12 CLEAN)")"
[ "$RUN_RC" -eq 0 ] || fail "non-numeric limit must not fail the run"
grep -q -- "--limit 100" "$GH_MOCK_LOG" || fail "non-numeric limit must fall back to 100"
pass "non-numeric list limit falls back to the default"

# ---- human-authored PRs: the gap that let #990 sit green and parked ---------

# Case 14: human PR armed + behind => surfaced, never commented on
run_nudge GH_MOCK_JSON="$(j "$(human 990 BEHIND "$ARMED")")"
[ "$RUN_RC" -eq 0 ] || fail "human-PR reporting must not fail the workflow"
never_commented || fail "human PRs must never receive an @dependabot comment"
reported || fail "armed-but-behind human PR must be reported"
grep -q "990" "$RUN_OUT" || fail "the report must name the PR number"
grep -q "::warning" "$RUN_OUT" || fail "armed-but-behind should raise a workflow warning"
pass "armed-but-behind human PR is surfaced, not silently skipped"

# Case 14b: hostile input on the HUMAN path. This is the path that interpolates
# ids into operator-facing text, so one odd id must not become several bogus
# "run land-pr.sh on this" rows.
# shellcheck disable=SC2016  # the $(...) must stay literal — it IS the hostile input
run_nudge GH_MOCK_JSON='[{"number":"990 $(touch /tmp/nudge-pwned)","mergeStateStatus":"BEHIND","autoMergeRequest":{"enabledAt":"2026-08-20T07:00:00Z"},"author":{"login":"florianhorner"}}]'
[ "$RUN_RC" -eq 0 ] || fail "a non-numeric human PR id must not fail the run"
reported && fail "a non-numeric human PR id must not be reported"
[ ! -e /tmp/nudge-pwned ] || fail "hostile id must never be evaluated"
pass "non-numeric human PR id is dropped, not rendered"

# Case 15: human PR behind but NOT armed => not reported (nobody asked GitHub
# to merge it, so it is not deadlocked)
run_nudge GH_MOCK_JSON="$(j "$(human 991 BEHIND)")"
[ "$RUN_RC" -eq 0 ] || fail "un-armed behind PR must be a no-op (exit code)"
reported && fail "un-armed behind PR must not be reported"
pass "behind but un-armed PR is not reported"

# Case 16: a BOT PR that is armed and behind must be NUDGED, never escalated
# to a human. Regression guard: an unfiltered human query would double-count it.
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND "$ARMED")")" GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "armed behind bot PR should just be nudged"
commented_on 12 || fail "armed behind bot PR must still be nudged"
reported && fail "a bot PR must never be escalated as needing a human"
pass "armed behind bot PR is nudged, never escalated to a human"

# Case 17: bot nudge and human report coexist in one run, on disjoint PRs
run_nudge GH_MOCK_JSON="$(j "$(bot 12 BEHIND)" "$(human 990 BEHIND "$ARMED")")" \
  GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "combined run should succeed"
commented_on 12 || fail "bot PR should still be nudged when a human PR is also stuck"
grep -q "990" "$RUN_OUT" || fail "human PR should still be reported when a bot PR was nudged"
grep -q "pr comment 990" "$GH_MOCK_LOG" && fail "the human PR must not be commented on"
pass "bot nudge and human report coexist on disjoint sets"

# Case 18: step summary is written when GitHub provides one, and names the
# landing contract's command rather than a bare update-branch
SUMMARY="$TMPDIR_T/summary.md"; : > "$SUMMARY"
run_nudge GH_MOCK_JSON="$(j "$(human 990 BEHIND "$ARMED")")" GITHUB_STEP_SUMMARY="$SUMMARY"
[ "$RUN_RC" -eq 0 ] || fail "step summary write must not fail the run"
grep -q "land-pr.sh 990" "$SUMMARY" || fail "step summary must name the landing-contract command"
pass "step summary names the landing-contract command"

# Case 19: an UNWRITABLE step summary must not fail the step. Reporting is the
# last thing the happy path does; a reporting bug must never redden a good run.
run_nudge GH_MOCK_JSON="$(j "$(human 990 BEHIND "$ARMED")")" \
  GITHUB_STEP_SUMMARY="$TMPDIR_T/no/such/dir/summary.md"
[ "$RUN_RC" -eq 0 ] || fail "an unwritable step summary must not fail the run (got rc=$RUN_RC)"
reported || fail "the stdout report should still be emitted"
# Two guards, each pinned separately: the caller's `|| true` keeps the exit 0
# above, and the redirect's own 2>/dev/null keeps the runner log clean.
grep -qi "no such file" "$RUN_OUT" && fail "an unwritable summary must not leak shell noise into the log"
pass "unwritable step summary degrades quietly and silently"

# Case 20: a non-numeric retry budget falls back to the default instead of
# silently disabling the loop (`for ((try=1; try<=abc; ...))` never runs)
run_nudge NUDGE_MERGE_STATE_TRIES=abc \
  GH_MOCK_JSON_1="$(j "$(bot 12 UNKNOWN)")" \
  GH_MOCK_JSON_2="$(j "$(bot 12 BEHIND)")" \
  GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "non-numeric retry budget must not fail the run"
commented_on 12 || fail "non-numeric retry budget must fall back, not disable the loop"
pass "non-numeric retry budget falls back to the default"

# Case 21: a zero retry budget is clamped to at least one attempt
run_nudge NUDGE_MERGE_STATE_TRIES=0 GH_MOCK_JSON="$(j "$(bot 12 BEHIND)")" GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "zero retry budget must not fail the run"
[ "$(list_calls)" -ge 1 ] || fail "zero retry budget must still make one attempt"
commented_on 12 || fail "zero retry budget must still nudge a known-behind PR"
pass "zero retry budget is clamped to one attempt"

# Case 22: a missing dependency is a clean skip, not a red step. Both early
# exits are the difference between "this runner lacks a tool" and "the build
# is broken", and neither had an assertion.
BARE_BIN="$TMPDIR_T/bare"; mkdir -p "$BARE_BIN"
for t in bash date grep sed tr cat printf env sleep; do
  src="$(command -v "$t" 2>/dev/null)" && ln -sf "$src" "$BARE_BIN/$t"
done
ln -sf "$(command -v jq)" "$BARE_BIN/jq"
out_no_gh="$(env -i PATH="$BARE_BIN" HOME="$HOME" bash "$NUDGE" 2>&1)"; rc_no_gh=$?
[ "$rc_no_gh" -eq 0 ] || fail "a missing gh must exit 0, not fail the workflow"
printf '%s' "$out_no_gh" | grep -q "gh CLI not found" || fail "a missing gh must say so"
pass "missing gh degrades to a clean skip"

rm -f "$BARE_BIN/jq"; ln -sf "$MOCK_BIN/gh" "$BARE_BIN/gh"
out_no_jq="$(env -i PATH="$BARE_BIN" HOME="$HOME" GH_MOCK_LOG=/dev/null bash "$NUDGE" 2>&1)"; rc_no_jq=$?
[ "$rc_no_jq" -eq 0 ] || fail "a missing jq must exit 0, not fail the workflow"
printf '%s' "$out_no_jq" | grep -q "jq not found" || fail "a missing jq must say so"
pass "missing jq degrades to a clean skip"

echo
echo "All 26 dependabot nudge cases passed."
