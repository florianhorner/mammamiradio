#!/usr/bin/env bash
# Self-test for scripts/land-pr.sh
#
# Drives the landing wrapper with a mocked `gh` (PATH shim) and a mocked
# review-log reader (MMR_LAND_REVIEW_READER), asserting the squad code-state
# freshness, v2 evidence, bot-thread gates, fail-closed paths, and pinned arming.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAND="$REPO_ROOT/scripts/land-pr.sh"
cd "$REPO_ROOT"

[[ -x "$LAND" ]] || chmod +x "$LAND"

PASS_COUNT=0
fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

HEAD_FULL="$(git rev-parse HEAD)"
HEAD_SHORT="$(git rev-parse --short HEAD)"
# Ancestor cases need HEAD~1 — a depth-1 shallow clone has no parent commit.
# CI checks out full history (quality.yml), which keeps HEAD~1 available.
ANC_FULL="$(git rev-parse HEAD~1 2>/dev/null)" \
  || fail "HEAD~1 unavailable (shallow clone?) — checkout with fetch-depth >= 2"
ANC_SHORT="$(git rev-parse --short HEAD~1)"
BOGUS_SHA="0000000"

BEHIND_BASE_FULL="$(git -c user.name='land-pr test' -c user.email='tests@example.com' commit-tree "$(git write-tree)" -p "$ANC_FULL" -m 'test: advanced base fixture')"
if git merge-base --is-ancestor "$BEHIND_BASE_FULL" "$HEAD_FULL"; then fail "invalid BEHIND fixture"; fi

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  OLD_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"      # BSD/macOS
  VERY_OLD_ISO="$(date -u -v-6H +%Y-%m-%dT%H:%M:%SZ)"
else
  OLD_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"  # GNU/Linux CI
  VERY_OLD_ISO="$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
fi

EMPTY_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}'
EMPTY_COMMENTS='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}'

# ---- mock gh ----------------------------------------------------------------
# Behavior is driven by env vars:
#   GH_MOCK_STATE         PR state (default OPEN)
#   GH_MOCK_MERGE_STATE   mergeStateStatus (default CLEAN)
#   GH_MOCK_HEAD          headRefOid (default real repo HEAD)
#   GH_MOCK_BASE          baseRefOid (default real repo HEAD~1)
#   GH_MOCK_COMMIT_DATE   committedDate of the newest PR commit (default NOW)
#   GH_MOCK_HELP_LINES    emit a large help stream for the capability probe
# Every invocation is appended to $GH_MOCK_LOG for assertions.
MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'MOCK'
#!/usr/bin/env bash
# The capability probe (`pr merge --help`) is answered without logging so the
# never-merged assertions only see real merge attempts.
if [[ "$*" == *"--help"* ]]; then
  printf '%s\n' "--match-head-commit"
  if [ -n "${GH_MOCK_HELP_LINES:-}" ]; then
    seq 1 "$GH_MOCK_HELP_LINES"
    exit $?
  fi
  exit 0
fi
echo "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  "pr view")
    head="${GH_MOCK_HEAD:?}"
    merge_state="${GH_MOCK_MERGE_STATE:-CLEAN}"
    commits="${GH_MOCK_COMMITS_JSON:-}"
    if [ -z "$commits" ]; then
      commits="[{\"committedDate\":\"${GH_MOCK_COMMIT_DATE:?}\"}]"
    fi
    printf '{"state":"%s","headRefOid":"%s","baseRefOid":"%s","mergeStateStatus":"%s","commits":%s}\n' \
      "${GH_MOCK_STATE:-OPEN}" "$head" "${GH_MOCK_BASE:?}" "$merge_state" "$commits"
    ;;
  "pr merge") : ;;
  "repo view")
    printf '{"nameWithOwner":"test-owner/test-repo"}\n'
    ;;
  "api graphql")
    [[ "$*" != *"isOutdated url comments"* ]] || exit 1
    if [[ "$*" == *"PullRequestReviewThread"* ]]; then
      if [[ "$*" == *"after=comment-page2"* ]]; then
        printf '%s\n' "${GH_MOCK_COMMENT_PAGE2:?}"
      else
        printf '%s\n' "${GH_MOCK_COMMENT_JSON:?}"
      fi
    elif [[ "$*" == *"after=thread-page2"* ]]; then
      printf '%s\n' "${GH_MOCK_GRAPHQL_PAGE2:?}"
    else
      printf '%s\n' "${GH_MOCK_GRAPHQL_JSON:?}"
    fi
    ;;
  *) : ;;
esac
exit 0
MOCK
chmod +x "$MOCK_BIN/gh"

# make_reader <skill> <commit> <timestamp> -> path to a mock review-log reader
make_reader() {
  local f; f="$(mktemp "$TMPDIR_T/reader.XXXXXX")"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf 'cat <<'\''LINES'\''\n'
    printf '{"skill":"%s","commit":"%s","timestamp":"%s"}\n' "$1" "$2" "$3"
    printf '%s\n' '---CONFIG---'
    printf '%s\n' 'LINES'
  } > "$f"
  chmod +x "$f"
  echo "$f"
}

empty_reader() {
  local f; f="$(mktemp "$TMPDIR_T/reader.XXXXXX")"
  printf '%s\n' '#!/usr/bin/env bash' 'echo ---CONFIG---' > "$f"
  chmod +x "$f"
  echo "$f"
}

PASSING_EVIDENCE_CHECKER="$TMPDIR_T/evidence-pass"; printf 'exit 0\n' > "$PASSING_EVIDENCE_CHECKER"; FAILING_EVIDENCE_CHECKER="$TMPDIR_T/evidence-fail"; printf 'exit 1\n' > "$FAILING_EVIDENCE_CHECKER"

# run_land <reader> [env overrides...] -> sets RUN_RC, RUN_OUT, leaves log at $GH_MOCK_LOG
run_land() {
  local reader="$1"; shift
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  RUN_RC=0
  RUN_OUT="$(env PATH="$MOCK_BIN:$PATH" \
      GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_HEAD="$HEAD_FULL" \
      GH_MOCK_BASE="$ANC_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
      GH_MOCK_GRAPHQL_JSON="$EMPTY_THREADS" GH_MOCK_COMMENT_JSON="$EMPTY_COMMENTS" \
      MMR_LAND_REVIEW_READER="$reader" \
      MMR_LAND_SKIP_EVIDENCE_CHECK="${MMR_LAND_SKIP_EVIDENCE_CHECK:-1}" \
      MMR_LAND_SKIP_THREAD_CHECK="${MMR_LAND_SKIP_THREAD_CHECK:-1}" \
      "$@" bash "$LAND" 7 2>&1)" || RUN_RC=$?
}

merged_with() { grep -q "pr merge 7 --squash --auto --match-head-commit $1" "$GH_MOCK_LOG"; }
never_merged() { ! grep -q "pr merge" "$GH_MOCK_LOG"; }

# Case 0: drain a large help stream so grep does not SIGPIPE the gh producer.
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_HELP_LINES=100000
[ "$RUN_RC" -eq 0 ] || fail "large gh help output should not trip the capability probe"
merged_with "$HEAD_FULL" || fail "large gh help output should still arm auto-merge"
pass "capability probe drains gh help output"

# Case 1: CLEAN PR + fresh squad entry at HEAD => arms with pinned real head
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -eq 0 ] || fail "clean PR should arm auto-merge pinned to head (exit code)"
merged_with "$HEAD_FULL" || fail "clean PR should arm auto-merge pinned to head"
pass "clean PR arms --squash --auto --match-head-commit <head>"

# Case 1b: accept millisecond timestamps for the squad entry and newest PR
# commit.
FRACTIONAL_ISO="${NOW_ISO%Z}.300Z"
run_land "$(make_reader review "$HEAD_SHORT" "$FRACTIONAL_ISO")" \
  GH_MOCK_COMMIT_DATE="$FRACTIONAL_ISO"
[ "$RUN_RC" -eq 0 ] || fail "fractional timestamps should arm auto-merge (exit code)"
merged_with "$HEAD_FULL" || fail "fractional timestamps should arm auto-merge"
pass "millisecond timestamps are accepted"

# Case 2: entry commit is an ANCESTOR of head, push within grace => allow
run_land "$(make_reader review "$ANC_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -eq 0 ] || fail "ancestor entry within grace should arm (exit code)"
merged_with "$HEAD_FULL" || fail "ancestor entry within grace should arm"
pass "ancestor entry within grace arms"

# Case 3: a real divergent BEHIND graph stops before evidence or branch mutation.
run_land "$(empty_reader)" GH_MOCK_MERGE_STATE=BEHIND \
  GH_MOCK_BASE="$BEHIND_BASE_FULL" MMR_LAND_SKIP_EVIDENCE_CHECK=0
[ "$RUN_RC" -ne 0 ] || fail "behind PR must deny (exit code)"
never_merged || fail "behind PR must never merge"
! grep -q "pr update-branch" "$GH_MOCK_LOG" || fail "landing seat must not update a behind branch"
printf '%s' "$RUN_OUT" | grep -q "feature workspace" || fail "deny message should name the owning workspace"
printf '%s' "$RUN_OUT" | grep -q -- "--reattest" || fail "deny message should give the reattest command"
! printf '%s' "$RUN_OUT" | grep -q "committed v2 pre-ship evidence does not cover" || fail "behind handling must run before evidence verification"
pass "real divergent BEHIND graph parks before evidence or mutation"

# Case 4: DIRTY (conflict) => stop with way-out, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_MERGE_STATE=DIRTY
[ "$RUN_RC" -ne 0 ] || fail "dirty PR must stop before merging (exit code)"
never_merged || fail "dirty PR must stop before merging"
printf '%s' "$RUN_OUT" | grep -qi "conflict" || fail "dirty PR message should name the conflict"
pass "conflict stops cleanly with way-out"

# Case 6: no squad entry and no v2 evidence => deny, never merge
run_land "$(empty_reader)" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$FAILING_EVIDENCE_CHECKER"
[ "$RUN_RC" -ne 0 ] || fail "missing review proof must deny (exit code)"
never_merged || fail "missing review proof must deny"
printf '%s' "$RUN_OUT" | grep -q "v2 pre-ship evidence" || fail "deny message should name v2 evidence"
pass "missing review proof denies"

# Case 7: bogus ledger commit is ignored when v2 evidence covers the head
run_land "$(make_reader review "$BOGUS_SHA" "$NOW_ISO")" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$PASSING_EVIDENCE_CHECKER"
[ "$RUN_RC" -eq 0 ] || fail "valid v2 evidence should arm despite bogus ledger entry (exit code)"
merged_with "$HEAD_FULL" || fail "valid v2 evidence should arm despite bogus ledger entry"
pass "valid v2 evidence ignores bogus ledger entry"

# Case 8: commits pushed AFTER the entry (beyond grace) => deny when v2 is absent.
# Entry is 6h old; newest PR commit is 3h old.
run_land "$(make_reader review "$ANC_SHORT" "$VERY_OLD_ISO")" \
  GH_MOCK_COMMIT_DATE="$OLD_ISO" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$FAILING_EVIDENCE_CHECKER"
[ "$RUN_RC" -ne 0 ] || fail "stale ledger without v2 evidence must deny (exit code)"
never_merged || fail "stale ledger without v2 evidence must deny"
pass "stale ledger without v2 evidence denies"

# Case 9: OLD entry, no commits since (newest commit predates entry) => allow.
# Wall-clock age alone must NOT deny — soak windows are days long by design.
run_land "$(make_reader review "$HEAD_SHORT" "$OLD_ISO")" \
  GH_MOCK_COMMIT_DATE="$VERY_OLD_ISO" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$PASSING_EVIDENCE_CHECKER"
[ "$RUN_RC" -eq 0 ] || fail "old-but-unchanged entry should still arm (no wall-clock staleness) (exit code)"
merged_with "$HEAD_FULL" || fail "old-but-unchanged entry should still arm (no wall-clock staleness)"
pass "soaked PR with unchanged head arms (no wall-clock denial)"

# Case 10: closed PR => stop, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_STATE=MERGED
[ "$RUN_RC" -ne 0 ] || fail "non-open PR must stop (exit code)"
never_merged || fail "non-open PR must stop"
pass "non-open PR stops"

# Case 11: wrong-skill ledger entry is ignored when v2 evidence covers the head
run_land "$(make_reader qa "$HEAD_SHORT" "$NOW_ISO")" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$PASSING_EVIDENCE_CHECKER"
[ "$RUN_RC" -eq 0 ] || fail "valid v2 evidence should arm despite wrong-skill ledger entry (exit code)"
merged_with "$HEAD_FULL" || fail "valid v2 evidence should arm despite wrong-skill ledger entry"
pass "valid v2 evidence ignores wrong-skill ledger entry"

# Case 13: missing ledger reader is OK when committed v2 evidence covers head
run_land "$TMPDIR_T/nonexistent-reader" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$PASSING_EVIDENCE_CHECKER"
[ "$RUN_RC" -eq 0 ] || fail "missing ledger reader should arm when v2 evidence passes (exit code)"
merged_with "$HEAD_FULL" || fail "missing ledger reader should arm when v2 evidence passes"
pass "missing ledger reader is OK when v2 evidence covers head"

# Case 14: PR with an empty commits array => clean die, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_COMMITS_JSON='[]'
[ "$RUN_RC" -ne 0 ] || fail "empty commits array must die cleanly (exit code)"
never_merged || fail "empty commits array must never reach gh merge"
pass "empty commits array dies cleanly"

# Case 15: multi-commit PR — ledger freshness binds to the NEWEST commit.
# Entry is 3h old; an older commit predates it but the newest commit is NOW =>
# deny without v2 evidence.
run_land "$(make_reader review "$ANC_SHORT" "$OLD_ISO")" \
  GH_MOCK_COMMITS_JSON='[{"committedDate":"'"$VERY_OLD_ISO"'"},{"committedDate":"'"$NOW_ISO"'"}]' \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$FAILING_EVIDENCE_CHECKER"
[ "$RUN_RC" -ne 0 ] || fail "stale ledger without v2 evidence must deny on newest commit (exit code)"
never_merged || fail "stale ledger without v2 evidence must deny on newest commit"
pass "ledger freshness without v2 evidence denies on newest commit"

# Case 16: non-numeric PR argument => usage error, never calls gh merge
GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
if env PATH="$MOCK_BIN:$PATH" GH_MOCK_LOG="$GH_MOCK_LOG" \
    GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_BASE="$ANC_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
    MMR_LAND_REVIEW_READER="$(empty_reader)" \
    bash "$LAND" "7; rm -rf /" >/dev/null 2>&1; then
  fail "non-numeric PR arg must be rejected"
fi
never_merged || fail "non-numeric PR arg must never reach gh merge"
pass "non-numeric PR argument rejected"

# Case 17: unresolved Major/Critical bot thread => deny
BLOCKING_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"id":"T1","isResolved":false,"isOutdated":false}]}}}}}'
BLOCKING_COMMENTS='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"author":{"login":"coderabbitai"},"body":"Major: fix the race","url":"https://example.test/comment/1"}]}}}}'
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  MMR_LAND_SKIP_THREAD_CHECK=0 GH_MOCK_GRAPHQL_JSON="$BLOCKING_THREADS" GH_MOCK_COMMENT_JSON="$BLOCKING_COMMENTS"
[ "$RUN_RC" -ne 0 ] || fail "blocking bot thread must deny (exit code)"
never_merged || fail "blocking bot thread must never merge"
printf '%s' "$RUN_OUT" | grep -q "https://example.test/comment/1" || fail "deny message should link the blocking comment"
pass "unresolved Major/Critical bot thread denies"

# Case 17b: an unresolved lower-severity thread is not blocking
NONBLOCKING_COMMENTS='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"author":{"login":"coderabbitai"},"body":"P2: follow-up","url":"https://example.test/comment/2"}]}}}}'
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  MMR_LAND_SKIP_THREAD_CHECK=0 GH_MOCK_GRAPHQL_JSON="$BLOCKING_THREADS" GH_MOCK_COMMENT_JSON="$NONBLOCKING_COMMENTS"
[ "$RUN_RC" -eq 0 ] || fail "lower-severity bot thread should not deny (exit code)"
merged_with "$HEAD_FULL" || fail "lower-severity bot thread should still arm"
pass "unresolved lower-severity bot thread does not block"

# Case 18: blocking thread on page 2 of paginated reviewThreads => deny
PAGE1='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":true,"endCursor":"thread-page2"},"nodes":[]}}}}}'
PAGE2='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"id":"T2","isResolved":false,"isOutdated":false}]}}}}}'
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  MMR_LAND_SKIP_THREAD_CHECK=0 GH_MOCK_GRAPHQL_JSON="$PAGE1" GH_MOCK_GRAPHQL_PAGE2="$PAGE2" \
  GH_MOCK_COMMENT_JSON="$BLOCKING_COMMENTS"
[ "$RUN_RC" -ne 0 ] || fail "page-2 blocking thread must deny (exit code)"
never_merged || fail "page-2 blocking thread must never merge"
pass "paginated reviewThreads scan finds blocking thread on page 2"

# Case 19: blocking bot comment on page 2 of a thread => deny
COMMENT_PAGE1='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":true,"endCursor":"comment-page2"},"nodes":[]}}}}'
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  MMR_LAND_SKIP_THREAD_CHECK=0 GH_MOCK_GRAPHQL_JSON="$BLOCKING_THREADS" \
  GH_MOCK_COMMENT_JSON="$COMMENT_PAGE1" GH_MOCK_COMMENT_PAGE2="$BLOCKING_COMMENTS"
[ "$RUN_RC" -ne 0 ] || fail "page-2 blocking comment must deny (exit code)"
never_merged || fail "page-2 blocking comment must never merge"
pass "paginated comment scan finds blocking bot debt"

# Case 20: no local ledger, committed v2 evidence covers head => arm
run_land "$(empty_reader)" \
  MMR_LAND_SKIP_EVIDENCE_CHECK=0 \
  MMR_LAND_EVIDENCE_CHECKER="$PASSING_EVIDENCE_CHECKER"
[ "$RUN_RC" -eq 0 ] || fail "v2-only landing should arm when ledger is absent (exit code)"
merged_with "$HEAD_FULL" || fail "v2-only landing should arm auto-merge"
printf '%s' "$RUN_OUT" | grep -q "committed v2 evidence covers" \
  || fail "v2-only landing should note v2 evidence satisfied the review gate"
pass "v2 evidence satisfies landing without a local ledger"

# Case 21: skipping v2 evidence still requires a qualifying local ledger
run_land "$(empty_reader)" MMR_LAND_SKIP_EVIDENCE_CHECK=1
[ "$RUN_RC" -ne 0 ] || fail "skipped v2 evidence without ledger must deny (exit code)"
never_merged || fail "skipped v2 evidence without ledger must never merge"
printf '%s' "$RUN_OUT" | grep -q "committed evidence was skipped" \
  || fail "deny message should explain that skipped evidence requires a ledger"
pass "evidence skip without local ledger denies"

echo
echo "All $PASS_COUNT land-pr cases passed."
