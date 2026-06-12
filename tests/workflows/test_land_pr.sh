#!/usr/bin/env bash
# Self-test for scripts/land-pr.sh
#
# Drives the landing wrapper with a mocked `gh` (PATH shim) and a mocked
# review-log reader (MMR_LAND_REVIEW_READER), asserting the squad code-state
# freshness check, the update-branch path, the conflict stop, and the
# head-pinned arming. No network. Exits non-zero on any mismatch.

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

HEAD_FULL="$(git rev-parse HEAD)"
HEAD_SHORT="$(git rev-parse --short HEAD)"
ANC_SHORT="$(git rev-parse --short HEAD~1)"
BOGUS_SHA="0000000"

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  OLD_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"      # BSD/macOS
  VERY_OLD_ISO="$(date -u -v-6H +%Y-%m-%dT%H:%M:%SZ)"
else
  OLD_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"  # GNU/Linux CI
  VERY_OLD_ISO="$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
fi

# ---- mock gh ----------------------------------------------------------------
# Behavior is driven by env vars:
#   GH_MOCK_STATE         PR state (default OPEN)
#   GH_MOCK_MERGE_STATE   mergeStateStatus (default CLEAN)
#   GH_MOCK_HEAD          headRefOid (default real repo HEAD)
#   GH_MOCK_HEAD_AFTER    headRefOid returned after `pr update-branch` ran
#   GH_MOCK_COMMIT_DATE   committedDate of the newest PR commit (default NOW)
#   GH_MOCK_UPDATE_FAIL   non-empty => `pr update-branch` exits 1
# Every invocation is appended to $GH_MOCK_LOG for assertions.
MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'MOCK'
#!/usr/bin/env bash
# The capability probe (`pr merge --help`) is answered without logging so the
# never-merged assertions only see real merge attempts.
if [[ "$*" == *"--help"* ]]; then
  echo "--match-head-commit"
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
      printf '{"state":"%s","headRefOid":"%s","mergeStateStatus":"%s","commits":%s}\n' \
        "${GH_MOCK_STATE:-OPEN}" "$head" "$merge_state" "$commits"
    fi
    ;;
  "pr update-branch")
    [ -n "${GH_MOCK_UPDATE_FAIL:-}" ] && exit 1
    touch "$GH_MOCK_STATE_DIR/updated"
    ;;
  "pr merge") : ;;
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

# run_land <reader> [env overrides...] -> sets RUN_RC, RUN_OUT, leaves log at $GH_MOCK_LOG
run_land() {
  local reader="$1"; shift
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  GH_MOCK_STATE_DIR="$(mktemp -d "$TMPDIR_T/state.XXXXXX")"
  RUN_RC=0
  RUN_OUT="$(env PATH="$MOCK_BIN:$PATH" \
      GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_STATE_DIR="$GH_MOCK_STATE_DIR" \
      GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
      MMR_LAND_REVIEW_READER="$reader" MMR_LAND_UPDATE_TIMEOUT=6 \
      "$@" bash "$LAND" 7 2>&1)" || RUN_RC=$?
}

merged_with() { grep -q "pr merge 7 --squash --auto --match-head-commit $1" "$GH_MOCK_LOG"; }
never_merged() { ! grep -q "pr merge" "$GH_MOCK_LOG"; }

# Case 1: CLEAN PR + fresh squad entry at HEAD => arms with pinned real head
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -eq 0 ] || fail "clean PR should arm auto-merge pinned to head (exit code)"
merged_with "$HEAD_FULL" || fail "clean PR should arm auto-merge pinned to head"
pass "clean PR arms --squash --auto --match-head-commit <head>"

# Case 2: entry commit is an ANCESTOR of head, push within grace => allow
run_land "$(make_reader review "$ANC_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -eq 0 ] || fail "ancestor entry within grace should arm (exit code)"
merged_with "$HEAD_FULL" || fail "ancestor entry within grace should arm"
pass "ancestor entry within grace arms"

# Case 3: BEHIND PR => update-branch first, then arm pinned to the NEW head
run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_HEAD_AFTER="deadbeefcafe"
grep -q "pr update-branch 7" "$GH_MOCK_LOG" || fail "behind PR should call update-branch"
[ "$RUN_RC" -eq 0 ] || fail "behind PR should arm pinned to post-update head (exit code)"
merged_with "deadbeefcafe" || fail "behind PR should arm pinned to post-update head"
pass "behind PR updates then arms on new head"

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
    GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
    MMR_LAND_REVIEW_READER="$(empty_reader)" \
    bash "$LAND" "7; rm -rf /" >/dev/null 2>&1; then
  fail "non-numeric PR arg must be rejected"
fi
never_merged || fail "non-numeric PR arg must never reach gh merge"
pass "non-numeric PR argument rejected"

echo
echo "All 16 land-pr cases passed."
