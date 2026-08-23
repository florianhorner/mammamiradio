#!/usr/bin/env bash
# Self-test for scripts/land-pr.sh
#
# Drives the landing wrapper with a mocked `gh` (PATH shim) and a mocked
# review-log reader (MMR_LAND_REVIEW_READER), asserting the squad code-state
# freshness check, committed evidence, review-thread disposition, the
# update-branch path, the conflict stop, and the head-pinned arming. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAND="$REPO_ROOT/scripts/land-pr.sh"
cd "$REPO_ROOT"

[[ -x "$LAND" ]] || chmod +x "$LAND"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
SAVE_REF="$(git rev-parse HEAD)"

write_evidence() {
  local head_sha
  mkdir -p proof/preship-review
  printf '{}\n' > proof/preship-review/pr-7.json
  git add proof/preship-review/pr-7.json
  git -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null \
    commit -q --no-verify -m "test: preship evidence fixture"
  head_sha="$(git rev-parse HEAD)"
  jq -cn --arg c "$head_sha" --arg h "$head_sha" \
    '{schema_version:"2.0.0",pr_number:7,pr_head_sha:$h,tree_sha:"abc",skill:"review",commit:$c,timestamp:"2026-07-30T12:00:00Z",status:"clean",emitted_at:"2026-07-30T12:00:01Z"}' \
    > proof/preship-review/pr-7.json
  git add proof/preship-review/pr-7.json
  git -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null \
    commit -q --amend --no-edit --no-verify
}

write_evidence
HEAD_FULL="$(git rev-parse HEAD)"
HEAD_SHORT="$(git rev-parse --short HEAD)"
git -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null \
  commit -q --allow-empty --no-verify -m "test: simulated post-update head"
AFTER_HEAD_FULL="$(git rev-parse HEAD)"
AFTER_HEAD_SHORT="$(git rev-parse --short HEAD)"
write_evidence

cleanup_fixture() {
  git reset --hard "$SAVE_REF" >/dev/null 2>&1 || true
  git clean -fd proof/preship-review >/dev/null 2>&1 || true
}
trap 'cleanup_fixture; rm -rf "$TMPDIR_T"' EXIT

ANC_SHORT="$(git rev-parse --short "${HEAD_FULL}~1" 2>/dev/null)" \
  || fail "HEAD_FULL~1 unavailable"
BOGUS_SHA="0000000"

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  OLD_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"
  VERY_OLD_ISO="$(date -u -v-6H +%Y-%m-%dT%H:%M:%SZ)"
else
  OLD_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
  VERY_OLD_ISO="$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)"
fi

MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
cat > "$MOCK_BIN/gh" <<'MOCK'
#!/usr/bin/env bash
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
  "api graphql")
    printf '%s\n' "${GH_MOCK_THREADS_JSON:-{\"data\":{\"repository\":{\"pullRequest\":{\"reviewThreads\":{\"nodes\":[]}}}}}}"
    ;;
  *) : ;;
esac
exit 0
MOCK
chmod +x "$MOCK_BIN/gh"

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

run_land() {
  local reader="$1"; shift
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  GH_MOCK_STATE_DIR="$(mktemp -d "$TMPDIR_T/state.XXXXXX")"
  RUN_RC=0
  RUN_OUT="$(env PATH="$MOCK_BIN:$PATH" \
      GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_STATE_DIR="$GH_MOCK_STATE_DIR" \
      GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
      MMR_LAND_REVIEW_READER="$reader" MMR_LAND_UPDATE_TIMEOUT=6 \
      MMR_LAND_SKIP_THREAD_CHECK=1 \
      "$@" bash "$LAND" 7 2>&1)" || RUN_RC=$?
}

merged_with() { grep -q "pr merge 7 --squash --auto --match-head-commit $1" "$GH_MOCK_LOG"; }
never_merged() { ! grep -q "pr merge" "$GH_MOCK_LOG"; }

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -eq 0 ] || { echo "$RUN_OUT"; fail "clean PR should arm"; }
merged_with "$HEAD_FULL" || fail "clean PR should arm pinned to head"
pass "clean PR arms --squash --auto --match-head-commit <head>"

run_land "$(make_reader review "$ANC_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -eq 0 ] || fail "ancestor entry within grace should arm"
merged_with "$HEAD_FULL" || fail "ancestor entry within grace should arm"
pass "ancestor entry within grace arms"

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" \
  GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_HEAD_AFTER="$AFTER_HEAD_FULL"
grep -q "pr update-branch 7" "$GH_MOCK_LOG" || fail "behind PR should call update-branch"
[ "$RUN_RC" -ne 0 ] || fail "behind PR without post-update re-review must deny"
never_merged || fail "behind PR without post-update re-review must not merge"
pass "behind PR without post-update re-review denies"

run_land "$(make_reader review "$AFTER_HEAD_SHORT" "$NOW_ISO")" \
  GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_HEAD_AFTER="$AFTER_HEAD_FULL"
[ "$RUN_RC" -eq 0 ] || fail "behind PR with post-update re-review should arm"
merged_with "$AFTER_HEAD_FULL" || fail "behind PR should arm pinned to post-update head"
pass "behind PR updates then arms on re-reviewed new head"

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_MERGE_STATE=DIRTY
[ "$RUN_RC" -ne 0 ] || fail "dirty PR must stop"
never_merged || fail "dirty PR must stop"
pass "conflict stops cleanly with way-out"

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_MERGE_STATE=BEHIND GH_MOCK_UPDATE_FAIL=1
[ "$RUN_RC" -ne 0 ] || fail "failed update must stop"
never_merged || fail "failed update must stop"
pass "failed branch update stops cleanly"

run_land "$(empty_reader)"
[ "$RUN_RC" -ne 0 ] || fail "missing squad entry must deny"
never_merged || fail "missing squad entry must deny"
pass "missing squad entry denies"

run_land "$(make_reader review "$BOGUS_SHA" "$NOW_ISO")"
[ "$RUN_RC" -ne 0 ] || fail "bogus-commit entry must deny"
pass "bogus-commit entry denies"

run_land "$(make_reader review "$ANC_SHORT" "$VERY_OLD_ISO")" GH_MOCK_COMMIT_DATE="$OLD_ISO"
[ "$RUN_RC" -ne 0 ] || fail "post-review push must invalidate the entry"
pass "post-review push invalidates entry"

run_land "$(make_reader review "$HEAD_SHORT" "$OLD_ISO")" GH_MOCK_COMMIT_DATE="$VERY_OLD_ISO"
[ "$RUN_RC" -eq 0 ] || fail "old-but-unchanged entry should still arm"
merged_with "$HEAD_FULL" || fail "old-but-unchanged entry should still arm"
pass "soaked PR with unchanged head arms"

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_STATE=MERGED
[ "$RUN_RC" -ne 0 ] || fail "non-open PR must stop"
pass "non-open PR stops"

run_land "$(make_reader qa "$HEAD_SHORT" "$NOW_ISO")"
[ "$RUN_RC" -ne 0 ] || fail "wrong-skill entry must deny"
pass "wrong-skill entry denies"

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_MERGE_STATE=BEHIND
[ "$RUN_RC" -ne 0 ] || fail "stuck branch update must die after timeout"
never_merged || fail "stuck branch update must never arm"
pass "stuck branch update times out without arming"

run_land "$TMPDIR_T/nonexistent-reader"
[ "$RUN_RC" -ne 0 ] || fail "missing reader must deny"
never_merged || fail "missing reader must deny"
pass "missing review-log reader fails closed"

run_land "$(make_reader review "$HEAD_SHORT" "$NOW_ISO")" GH_MOCK_COMMITS_JSON='[]'
[ "$RUN_RC" -ne 0 ] || fail "empty commits array must die"
pass "empty commits array dies cleanly"

run_land "$(make_reader review "$ANC_SHORT" "$OLD_ISO")" \
  GH_MOCK_COMMITS_JSON='[{"committedDate":"'"$VERY_OLD_ISO"'"},{"committedDate":"'"$NOW_ISO"'"}]'
[ "$RUN_RC" -ne 0 ] || fail "newest commit after entry must deny"
pass "multi-commit freshness binds to newest commit"

GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
GH_MOCK_STATE_DIR="$(mktemp -d "$TMPDIR_T/state.XXXXXX")"
if env PATH="$MOCK_BIN:$PATH" GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_STATE_DIR="$GH_MOCK_STATE_DIR" \
    GH_MOCK_HEAD="$HEAD_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
    MMR_LAND_REVIEW_READER="$(empty_reader)" MMR_LAND_SKIP_THREAD_CHECK=1 \
    bash "$LAND" "7; rm -rf /" >/dev/null 2>&1; then
  fail "non-numeric PR arg must be rejected"
fi
never_merged || fail "non-numeric PR arg must never reach gh merge"
pass "non-numeric PR argument rejected"

GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
GH_MOCK_STATE_DIR="$(mktemp -d "$TMPDIR_T/state.XXXXXX")"
BLOCKING_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"nodes":[{"isResolved":false,"isOutdated":false,"comments":{"nodes":[{"author":{"login":"coderabbitai"},"body":"Major: example finding","url":"https://example.test/thread/1"}]}}]}}}}}'
RUN_RC=0
RUN_OUT="$(env PATH="$MOCK_BIN:$PATH" \
    GH_MOCK_LOG="$GH_MOCK_LOG" GH_MOCK_STATE_DIR="$GH_MOCK_STATE_DIR" \
    GH_MOCK_HEAD="$AFTER_HEAD_FULL" GH_MOCK_COMMIT_DATE="$NOW_ISO" \
    GH_MOCK_THREADS_JSON="$BLOCKING_THREADS" \
    MMR_LAND_REVIEW_READER="$(make_reader review "$AFTER_HEAD_SHORT" "$NOW_ISO")" \
    MMR_LAND_UPDATE_TIMEOUT=6 \
    bash "$LAND" 7 2>&1)" || RUN_RC=$?
[ "$RUN_RC" -ne 0 ] || fail "blocking bot thread must deny"
never_merged || fail "blocking bot thread must never merge"
printf '%s' "$RUN_OUT" | grep -q "unresolved Major/Critical" || fail "thread deny message wrong"
pass "unresolved Major/Critical bot thread denies"

echo
echo "All 17 land-pr cases passed."
