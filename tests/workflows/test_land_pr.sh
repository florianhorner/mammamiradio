#!/usr/bin/env bash
# Self-test for scripts/land-pr.sh
#
# Drives the landing wrapper with a mocked `gh` (PATH shim) and a mocked
# review-log reader (MMR_LAND_REVIEW_READER), asserting the squad code-state
# freshness check, v2 evidence and bot-thread gates (skipped by default here),
# the update-branch path with post-update strict recheck, the conflict stop,
# and the head-pinned arming. No network. Exits non-zero on any mismatch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAND="$REPO_ROOT/scripts/land-pr.sh"
cd "$REPO_ROOT"

[[ -x "$LAND" ]] || chmod +x "$LAND"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

SAVE_REF="$(git rev-parse HEAD)"
HEAD_FULL="$SAVE_REF"
HEAD_SHORT="$(git rev-parse --short HEAD)"
# Ancestor cases need HEAD~1 — a depth-1 shallow clone has no parent commit.
# CI checks out full history (quality.yml), which keeps HEAD~1 available.
ANC_FULL="$(git rev-parse HEAD~1 2>/dev/null)" \
  || fail "HEAD~1 unavailable (shallow clone?) — checkout with fetch-depth >= 2"
ANC_SHORT="$(git rev-parse --short HEAD~1)"
BOGUS_SHA="0000000"

# Post-update re-review case needs a second commit object without mutating HEAD.
HEAD2_FULL="$(git commit-tree "$(git write-tree)" -p "$HEAD_FULL" -m "test: land-pr HEAD2 fixture")"
HEAD2_SHORT="$(git rev-parse --short "$HEAD2_FULL")"

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  OLD_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"      # BSD/macOS
  VERY_OLD_ISO="$(date -u -v-6H +%Y-%m-%dT%H:%M:%SZ)"
else
  OLD_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"  # GNU/Linux CI
  VERY_OLD_ISO="$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
fi

EMPTY_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[]}}}}}'

# ---- mock gh ----------------------------------------------------------------
# Behavior is driven by env vars:
#   GH_MOCK_STATE         PR state (default OPEN)
#   GH_MOCK_MERGE_STATE   mergeStateStatus (default CLEAN)
#   GH_MOCK_HEAD          headRefOid (default real repo HEAD)
#   GH_MOCK_BASE          baseRefOid (default real repo HEAD~1)
#   GH_MOCK_HEAD_AFTER    headRefOid returned after `pr update-branch` ran
#   GH_MOCK_COMMIT_DATE   committedDate of the newest PR commit (default NOW)
#   GH_MOCK_GRAPHQL_JSON  GraphQL response for review-thread query
#   GH_MOCK_HELP_LINES    emit a large help stream for the capability probe
#   GH_MOCK_UPDATE_FAIL   non-empty => `pr update-branch` exits 1
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
    if [ -f "$GH_MOCK_STATE_DIR/updated" ] && [ -n "${GH_MOCK_HEAD_AFTER:-}" ]; then
      head="$GH_MOCK_HEAD_AFTER"; merge_state="CLEAN"
    else
      head="${GH_MOCK_HEAD:?}"; merge_state="${GH_MOCK_MERGE_STATE:-CLEAN}"
    fi
    if [[ "$*" == *"--jq"* ]]; then
      printf '%s\n' "$head"
    else
      commits="${GH_MOCK_COMMITS_JSON:-}"
      if [ -z "$commits" ]; then
        commits="[{\"committedDate\":\"${GH_MOCK_COMMIT_DATE:?}\"}]"
      fi
      printf '{"state":"%s","headRefOid":"%s","baseRefOid":"%s","mergeStateStatus":"%s","commits":%s}\n' \
        "${GH_MOCK_STATE:-OPEN}" "$head" "${GH_MOCK_BASE:?}" "$merge_state" "$commits"
    fi
    ;;
  "pr update-branch")
    [ -n "${GH_MOCK_UPDATE_FAIL:-}" ] && exit 1
    touch "$GH_MOCK_STATE_DIR/updated"
    ;;
  "pr merge") : ;;
  "repo view")
    printf '{"nameWithOwner":"test-owner/test-repo"}\n'
    ;;
  "api graphql")
    printf '%s\n' "${GH_MOCK_GRAPHQL_JSON:-{\"data\":{\"repository\":{\"pullRequest\":{\"reviewThreads\":{\"nodes\":[]}}}}}}"
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

# make_multi_reader <skill> <commit> <timestamp> [<skill> <commit> <timestamp> ...]
make_multi_reader() {
  local f skill commit ts
  f="$(mktemp "$TMPDIR_T/reader.XXXXXX")"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf 'cat <<'\''LINES'\''\n'
    while [ "$#" -ge 3 ]; do
      skill="$1"; commit="$2"; ts="$3"; shift 3
      printf '{"skill":"%s","commit":"%s","timestamp":"%s"}\n' "$skill" "$commit" "$ts"
    done
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

# run_land <reader> [env overrides...] -> sets RUN_RC, RUN_OUT, leaves log at $GH_MOCK_LOG
run_land() {
  local reader="$1"; shift
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  GH_MOCK_STATE_DIR="$(mktemp -d "$TMPDIR_T/state.XXXXXX")"
  RUN_RC=0
  RUN_OUT="$(env PATH="$MOCK_BIN:$PATH" \
      GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_STATE_DIR="$GH_MOCK_STATE_DIR" \
      GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_BASE="$ANC_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
      GH_MOCK_GRAPHQL_JSON="$EMPTY_THREADS" \
      MMR_LAND_REVIEW_READER="$reader" MMR_LAND_UPDATE_TIMEOUT=6 \
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

# Case 3: BEHIND PR => update-branch, then deny without exact-head re-review
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_HEAD_AFTER="$HEAD2_FULL"
grep -q "pr update-branch 7" "$GH_MOCK_LOG" || fail "behind PR should call update-branch"
[ "$RUN_RC" -ne 0 ] || fail "behind PR without post-update re-review must deny (exit code)"
never_merged || fail "behind PR without post-update re-review must never merge"
printf '%s' "$RUN_OUT" | grep -q "exact PR head" || fail "deny message should mention exact-head recheck"
pass "behind PR denies without post-update exact-head review"

# Case 3b: BEHIND PR with review on the new head => update then arm
run_land "$(make_multi_reader review "$HEAD_SHORT" "$NOW_ISO" review "$HEAD2_SHORT" "$NOW_ISO")" \
  GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_HEAD_AFTER="$HEAD2_FULL"
grep -q "pr update-branch 7" "$GH_MOCK_LOG" || fail "behind PR should call update-branch"
[ "$RUN_RC" -eq 0 ] || fail "behind PR with post-update review should arm (exit code)"
merged_with "$HEAD2_FULL" || fail "behind PR with post-update review should arm on new head"
pass "behind PR with post-update review arms on new head"

# Case 4: DIRTY (conflict) => stop with way-out, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_MERGE_STATE=DIRTY
[ "$RUN_RC" -ne 0 ] || fail "dirty PR must stop before merging (exit code)"
never_merged || fail "dirty PR must stop before merging"
printf '%s' "$RUN_OUT" | grep -qi "conflict" || fail "dirty PR message should name the conflict"
pass "conflict stops cleanly with way-out"

# Case 5: update-branch fails => stop cleanly, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_UPDATE_FAIL=1
[ "$RUN_RC" -ne 0 ] || fail "failed update must stop before merging (exit code)"
never_merged || fail "failed update must stop before merging"
pass "failed branch update stops cleanly"

# Case 6: no squad entry => deny, never merge
run_land "$(empty_reader)"
[ "$RUN_RC" -ne 0 ] || fail "missing squad entry must deny (exit code)"
never_merged || fail "missing squad entry must deny"
printf '%s' "$RUN_OUT" | grep -q "squad" || fail "deny message should name the squad"
pass "missing squad entry denies"

# Case 7: entry for a bogus commit => deny
run_land "$(make_reader review "$BOGUS_SHA" "$NOW_ISO")"
[ "$RUN_RC" -ne 0 ] || fail "bogus-commit entry must deny (exit code)"
never_merged || fail "bogus-commit entry must deny"
pass "bogus-commit entry denies"

# Case 8: commits pushed AFTER the entry (beyond grace) => deny — the review
# saw older code. Entry is 6h old; newest PR commit is 3h old.
run_land "$(make_reader review "$ANC_SHORT" "$VERY_OLD_ISO")" GH_MOCK_COMMIT_DATE="$OLD_ISO"
[ "$RUN_RC" -ne 0 ] || fail "post-review push must invalidate the entry (exit code)"
never_merged || fail "post-review push must invalidate the entry"
pass "post-review push invalidates entry (code-state freshness)"

# Case 9: OLD entry, no commits since (newest commit predates entry) => allow.
# Wall-clock age alone must NOT deny — soak windows are days long by design.
run_land "$(make_reader review "$HEAD_SHORT" "$OLD_ISO")" GH_MOCK_COMMIT_DATE="$VERY_OLD_ISO"
[ "$RUN_RC" -eq 0 ] || fail "old-but-unchanged entry should still arm (no wall-clock staleness) (exit code)"
merged_with "$HEAD_FULL" || fail "old-but-unchanged entry should still arm (no wall-clock staleness)"
pass "soaked PR with unchanged head arms (no wall-clock denial)"

# Case 10: closed PR => stop, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_STATE=MERGED
[ "$RUN_RC" -ne 0 ] || fail "non-open PR must stop (exit code)"
never_merged || fail "non-open PR must stop"
pass "non-open PR stops"

# Case 11: wrong-skill entry (qa) => deny
run_land "$(make_reader qa "$HEAD_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -ne 0 ] || fail "non-review skill must not satisfy the gate (exit code)"
never_merged || fail "non-review skill must not satisfy the gate"
pass "wrong-skill entry denies"

# Case 12: BEHIND, update succeeds, but the head NEVER changes (rebase stuck)
# => die after the timeout, never arm. Regression guard for the fall-through
# that armed auto-merge pinned to the pre-update head (GitHub would then
# silently never fire the merge).
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_MERGE_STATE=BEHIND
[ "$RUN_RC" -ne 0 ] || fail "stuck branch update must die after timeout (exit code)"
never_merged || fail "stuck branch update must never arm a merge"
printf '%s' "$RUN_OUT" | grep -q "did not surface" || fail "timeout message should say the head did not surface"
pass "stuck branch update times out without arming"

# Case 13: review-log reader missing/non-executable => hard DENY (unlike the
# create-path hook, the landing wrapper fails CLOSED — it cannot verify, so
# it does not land).
run_land "$TMPDIR_T/nonexistent-reader"
[ "$RUN_RC" -ne 0 ] || fail "missing reader must deny the landing (exit code)"
never_merged || fail "missing reader must never reach gh merge"
printf '%s' "$RUN_OUT" | grep -q "cannot verify" || fail "missing-reader message should say it cannot verify"
pass "missing review-log reader fails closed"

# Case 14: PR with an empty commits array => clean die, never merge
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_COMMITS_JSON='[]'
[ "$RUN_RC" -ne 0 ] || fail "empty commits array must die cleanly (exit code)"
never_merged || fail "empty commits array must never reach gh merge"
pass "empty commits array dies cleanly"

# Case 15: multi-commit PR — freshness binds to the NEWEST commit. Entry is
# 3h old; an older commit predates it but the newest commit is NOW => deny.
run_land "$(make_reader review "$ANC_SHORT" "$OLD_ISO")" \
  GH_MOCK_COMMITS_JSON='[{"committedDate":"'"$VERY_OLD_ISO"'"},{"committedDate":"'"$NOW_ISO"'"}]'
[ "$RUN_RC" -ne 0 ] || fail "newest commit after entry must deny even when older commits predate it (exit code)"
never_merged || fail "newest commit after entry must never merge"
pass "multi-commit freshness binds to newest commit"

# Case 16: non-numeric PR argument => usage error, never calls gh merge
GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
GH_MOCK_STATE_DIR="$(mktemp -d "$TMPDIR_T/state.XXXXXX")"
if env PATH="$MOCK_BIN:$PATH" GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_STATE_DIR="$GH_MOCK_STATE_DIR" \
    GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_BASE="$ANC_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
    MMR_LAND_REVIEW_READER="$(empty_reader)" \
    bash "$LAND" "7; rm -rf /" >/dev/null 2>&1; then
  fail "non-numeric PR arg must be rejected"
fi
never_merged || fail "non-numeric PR arg must never reach gh merge"
pass "non-numeric PR argument rejected"

# Case 17: unresolved Major/Critical bot thread => deny
BLOCKING_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"url":"https://example.test/thread/1","comments":{"nodes":[{"author":{"login":"coderabbitai"},"body":"Major: fix the race"}]}}]}}}}}'
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  MMR_LAND_SKIP_THREAD_CHECK=0 GH_MOCK_GRAPHQL_JSON="$BLOCKING_THREADS"
[ "$RUN_RC" -ne 0 ] || fail "blocking bot thread must deny (exit code)"
never_merged || fail "blocking bot thread must never merge"
printf '%s' "$RUN_OUT" | grep -q "bot thread" || fail "deny message should name bot threads"
pass "unresolved Major/Critical bot thread denies"

echo
echo "All 18 land-pr cases passed."
