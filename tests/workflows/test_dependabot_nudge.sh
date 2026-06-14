#!/usr/bin/env bash
# Self-test for scripts/nudge-dependabot-rebase.sh
#
# Drives the nudge script with a mocked `gh` (PATH shim), asserting it
# comments only on behind Dependabot PRs, skips PRs with an un-actioned
# nudge, and degrades without failing when gh errors. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
NUDGE="$REPO_ROOT/scripts/nudge-dependabot-rebase.sh"
cd "$REPO_ROOT"

[[ -x "$NUDGE" ]] || chmod +x "$NUDGE"

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
# Env-driven:
#   GH_MOCK_LIST       newline-separated PR numbers `gh pr list` emits (post-jq)
#   GH_MOCK_LIST_FAIL  non-empty => `gh pr list` exits 1
#   GH_MOCK_VIEW_<N>   JSON body for `gh pr view N`
#   GH_MOCK_COMMENT_FAIL  non-empty => `gh pr comment` exits 1
# Calls are appended to $GH_MOCK_LOG.
MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'MOCK'
#!/usr/bin/env bash
echo "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  "pr list")
    [ -n "${GH_MOCK_LIST_FAIL:-}" ] && exit 1
    [ -n "${GH_MOCK_LIST:-}" ] && printf '%s\n' "$GH_MOCK_LIST"
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

# run_nudge [env overrides...] -> RUN_RC, log at $GH_MOCK_LOG
run_nudge() {
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  RUN_RC=0
  env PATH="$MOCK_BIN:$PATH" GH_MOCK_LOG="$GH_MOCK_LOG" "$@" bash "$NUDGE" >/dev/null 2>&1 || RUN_RC=$?
}

commented_on() { grep -q "pr comment $1 " "$GH_MOCK_LOG"; }
never_commented() { ! grep -q "pr comment" "$GH_MOCK_LOG"; }

VIEW_NO_NUDGE='{"commits":[{"committedDate":"'$OLD_ISO'"}],"comments":[]}'
VIEW_FRESH_NUDGE='{"commits":[{"committedDate":"'$OLD_ISO'"}],"comments":[{"body":"@dependabot rebase","createdAt":"'$NOW_ISO'"}]}'
VIEW_ACTIONED_NUDGE='{"commits":[{"committedDate":"'$NOW_ISO'"}],"comments":[{"body":"@dependabot rebase","createdAt":"'$OLD_ISO'"}]}'

# Case 1: behind PR, never nudged => comments
run_nudge GH_MOCK_LIST="12" GH_MOCK_VIEW_12="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "behind PR without nudge should get a comment (exit code)"
commented_on 12 || fail "behind PR without nudge should get a comment"
pass "behind PR gets @dependabot rebase comment"

# Case 2: behind PR with an UN-ACTIONED nudge (comment newer than last commit) => skip
run_nudge GH_MOCK_LIST="12" GH_MOCK_VIEW_12="$VIEW_FRESH_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "un-actioned nudge must not be repeated (exit code)"
never_commented || fail "un-actioned nudge must not be repeated"
pass "un-actioned nudge is not repeated (idempotent)"

# Case 3: prior nudge was ACTIONED (commit newer than comment) => comments again
run_nudge GH_MOCK_LIST="12" GH_MOCK_VIEW_12="$VIEW_ACTIONED_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "actioned nudge followed by new staleness should re-comment (exit code)"
commented_on 12 || fail "actioned nudge followed by new staleness should re-comment"
pass "re-nudges after the previous nudge was actioned"

# Case 4: no behind PRs => no comments, exit 0
run_nudge GH_MOCK_LIST=""
[ "$RUN_RC" -eq 0 ] || fail "no behind PRs should be a silent no-op (exit code)"
never_commented || fail "no behind PRs should be a silent no-op"
pass "no behind PRs is a no-op"

# Case 5: gh pr list fails => exit 0 (never fail the main-branch workflow)
run_nudge GH_MOCK_LIST_FAIL=1
[ "$RUN_RC" -eq 0 ] || fail "list failure must degrade to a no-op (exit code)"
never_commented || fail "list failure must degrade to a no-op"
pass "list failure degrades to no-op"

# Case 6: comment fails => script still exits 0 and continues
run_nudge GH_MOCK_LIST="12" GH_MOCK_VIEW_12="$VIEW_NO_NUDGE" GH_MOCK_COMMENT_FAIL=1
[ "$RUN_RC" -eq 0 ] || fail "comment failure must not fail the run"
pass "comment failure is non-fatal"

# Case 7: multiple behind PRs => each gets exactly one comment
run_nudge GH_MOCK_LIST="$(printf '12\n34')" \
  GH_MOCK_VIEW_12="$VIEW_NO_NUDGE" GH_MOCK_VIEW_34="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "every behind PR should be nudged (exit code)"
commented_on 12 || fail "every behind PR should be nudged"
commented_on 34 || fail "every behind PR should be nudged"
[ "$(grep -c "pr comment" "$GH_MOCK_LOG")" -eq 2 ] || fail "exactly one comment per PR"
pass "multiple behind PRs each nudged once"

# Case 8: non-numeric junk in the list (hostile input) => ignored, no comment
run_nudge GH_MOCK_LIST='12; rm -rf /'
[ "$RUN_RC" -eq 0 ] || fail "non-numeric PR identifiers must be ignored (exit code)"
never_commented || fail "non-numeric PR identifiers must be ignored"
pass "non-numeric list entries ignored"

# Case 9: `gh pr view` fails for a listed PR (no mock set => mock errors)
# => that PR is skipped non-fatally, others still processed
run_nudge GH_MOCK_LIST="$(printf '12\n34')" GH_MOCK_VIEW_34="$VIEW_NO_NUDGE"
[ "$RUN_RC" -eq 0 ] || fail "per-PR view failure must be non-fatal (exit code)"
never_commented_on_12() { ! grep -q "pr comment 12 " "$GH_MOCK_LOG"; }
never_commented_on_12 || fail "unreadable PR must be skipped"
commented_on 34 || fail "later PRs must still be processed after a view failure"
pass "per-PR view failure skips that PR, continues"

echo
echo "All 9 dependabot nudge cases passed."
