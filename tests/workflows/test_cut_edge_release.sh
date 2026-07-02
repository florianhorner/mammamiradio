#!/usr/bin/env bash
# Self-test for scripts/cut-edge-release.sh
#
# Fully hermetic: PATH-shimmed `gh` and `git` mocks intercept EVERY git verb the
# script uses (including `rev-parse origin/main`), so the test does not depend on
# the runner's real branch topology, on `origin/main` being locally resolvable, or
# on a particular checkout shape. SHAs are synthetic. The git mock no-ops + logs
# the mutating verbs and HARD-FAILS on any unrecognised verb, so a future mutating
# verb can never run against the real repo. The real edge config is backed up and
# restored around every run; a final assertion proves the real repo/branch/config
# were never touched. No network. Exits non-zero on any mismatch.
#
# Scenarios cover the two failure modes this hardening exists to kill:
#   - blind-HEAD pin: HEAD is tests-only, newest green build is an older commit
#     (cases "older-pin" + "off-main-skipped") -> pin the OLDER built SHA.
#   - soft-pass on an unverifiable image: gh query fails / gh absent -> HARD-fail,
#     no PR (cases "gh-query-fail" + "gh-absent"); plus the image-drift guard.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/cut-edge-release.sh"
cd "$REPO_ROOT"

[[ -x "$SCRIPT" ]] || chmod +x "$SCRIPT"
BASH_BIN="$(command -v bash)"   # absolute, so the restricted-PATH cases still find bash

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"
EDGE_ORIG="$TMPDIR_T/edge-config.orig"
cp "$EDGE_CONFIG" "$EDGE_ORIG"
# Restore the real edge config and clean up no matter how the test exits.
restore() {
  cp "$EDGE_ORIG" "$EDGE_CONFIG" 2>/dev/null || true
  rm -rf "$TMPDIR_T"
}
trap restore EXIT

# Real anchors (captured with real git, before any mock is on PATH) to prove at the
# end that the test never mutated the real repo.
ORIG_HEAD="$(git rev-parse HEAD)"
ORIG_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Synthetic SHAs — decoupled from the runner's real topology.
MAIN_FULL="1111111111111111111111111111111111111111"; MAIN_SHORT="1111111"  # origin/main HEAD
OLDER_FULL="2222222222222222222222222222222222222222"; OLDER_SHORT="2222222" # an older built commit
OFFMAIN_FULL="3333333333333333333333333333333333333333"                       # green run NOT on main
REVLIST="$MAIN_FULL"$'\n'"$OLDER_FULL"   # origin/main topology, newest first

# ---- mock gh ----------------------------------------------------------------
# Env: GH_MOCK_RUN_SHAS (newline list `run list` returns), GH_MOCK_RUN_FAIL (=>
# `run list` exits 1), GH_MOCK_PR_URL (`pr list` result; empty => no open PR).
MOCK_BIN="$TMPDIR_T/bin"
mkdir -p "$MOCK_BIN"
# Absolute-bash shebang so the mock launches even under the restricted PATH of the
# gh-absent case (where `#!/usr/bin/env bash` could not resolve bash).
{ printf '#!%s\n' "$BASH_BIN"; cat <<'MOCK'
echo "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  "run list")
    [ -n "${GH_MOCK_RUN_FAIL:-}" ] && exit 1
    printf '%s\n' "${GH_MOCK_RUN_SHAS:-}" ;;
  "pr list")  printf '%s\n' "${GH_MOCK_PR_URL:-}" ;;
  "pr create") : ;;
  *) : ;;
esac
exit 0
MOCK
} > "$MOCK_BIN/gh"
chmod +x "$MOCK_BIN/gh"

# ---- mock python3 -----------------------------------------------------------
# The release-beat validator is tested in pytest. Here we only assert that the
# edge-release workflow invokes it with the selected target SHA, without reading
# or mutating the real source manifest.
{ printf '#!%s\n' "$BASH_BIN"; cat <<'MOCK'
echo "$*" >> "$PYTHON_MOCK_LOG"
case "$1" in
  "scripts/validate-release-beat.py") exit "${PYTHON_MOCK_RC:-0}" ;;
  *) echo "MOCK python3: unexpected invocation '$*'" >&2; exit 99 ;;
esac
MOCK
} > "$MOCK_BIN/python3"
chmod +x "$MOCK_BIN/python3"

# ---- mock git ---------------------------------------------------------------
# Intercepts EVERY verb the script uses; mutating verbs are logged + no-op'd; any
# unrecognised verb HARD-FAILS (exit 99) so a new mutating verb can never run real.
# Env: GIT_MOCK_TOPLEVEL, GIT_MOCK_MAIN_SHORT, GIT_MOCK_REVLIST, GIT_MOCK_DIFF,
# GIT_MOCK_DIRTY.
{ printf '#!%s\n' "$BASH_BIN"; cat <<'MOCK'
case "$1" in
  rev-parse)
    _last=""; for _a in "$@"; do _last="$_a"; done
    case "$_last" in
      --show-toplevel) echo "${GIT_MOCK_TOPLEVEL:-$PWD}" ;;
      origin/main)     echo "${GIT_MOCK_MAIN_SHORT:-0000000}" ;;
      *)               echo "${_last:0:7}" ;;            # --short=7 <sha>
    esac ;;
  status)   [ -n "${GIT_MOCK_DIRTY:-}" ] && echo " M somefile" || echo "" ;;
  fetch)    : ;;
  rev-list) printf '%s\n' "${GIT_MOCK_REVLIST:-}" ;;
  show)     echo "version: ${GIT_MOCK_SHOW_VERSION:-aeafa99}" ;;   # origin/main:edge-config
  diff)     [ -n "${GIT_MOCK_DIFF_FAIL:-}" ] && exit 1; printf '%s\n' "${GIT_MOCK_DIFF:-}" ;;
  remote)   echo "https://github.com/florianhorner/mammamiradio.git" ;;
  checkout|add|commit|push) echo "$*" >> "$GIT_MOCK_LOG" ;;
  *) echo "MOCK git: unexpected verb '$1' (args: $*) — would have run real git" >&2; exit 99 ;;
esac
exit 0
MOCK
} > "$MOCK_BIN/git"
chmod +x "$MOCK_BIN/git"

# git-only mock dir (no gh) for the gh-absent case. The script reaches its
# `command -v gh` guard using only git+shell builtins, before any coreutils.
MOCK_NOGH="$TMPDIR_T/nogh"
mkdir -p "$MOCK_NOGH"
cp "$MOCK_BIN/git" "$MOCK_NOGH/git"

# run_cut ENV=val... -> sets RUN_RC, RUN_OUT, WROTE_VERSION; logs at $GH_MOCK_LOG /
# $GIT_MOCK_LOG. Resets the edge config before and after. The shell var _PATH_OVERRIDE
# (restricted PATH) tweaks a run without reaching the script. The idempotency version
# is driven by GIT_MOCK_SHOW_VERSION (the mock for `git show origin/main:<config>`).
run_cut() {
  cp "$EDGE_ORIG" "$EDGE_CONFIG"
  GH_MOCK_LOG="$TMPDIR_T/gh.log"; : > "$GH_MOCK_LOG"
  GIT_MOCK_LOG="$TMPDIR_T/git.log"; : > "$GIT_MOCK_LOG"
  PYTHON_MOCK_LOG="$TMPDIR_T/python.log"; : > "$PYTHON_MOCK_LOG"
  RUN_RC=0
  RUN_OUT="$(env PATH="${_PATH_OVERRIDE:-$MOCK_BIN:$PATH}" \
      GH_MOCK_LOG="$GH_MOCK_LOG" GIT_MOCK_LOG="$GIT_MOCK_LOG" \
      PYTHON_MOCK_LOG="$PYTHON_MOCK_LOG" \
      GIT_MOCK_TOPLEVEL="$REPO_ROOT" GIT_MOCK_MAIN_SHORT="$MAIN_SHORT" \
      GIT_MOCK_REVLIST="$REVLIST" \
      "$@" "$BASH_BIN" "$SCRIPT" 2>&1)" || RUN_RC=$?
  WROTE_VERSION="$(grep '^version:' "$EDGE_CONFIG" | awk '{print $2}')"
  cp "$EDGE_ORIG" "$EDGE_CONFIG"
}

created_pr()       { grep -q "pr create" "$GH_MOCK_LOG"; }
never_created_pr() { ! grep -q "pr create" "$GH_MOCK_LOG"; }
never_committed()  { ! grep -q "^commit" "$GIT_MOCK_LOG"; }
never_pushed()     { ! grep -q "^push" "$GIT_MOCK_LOG"; }
validator_called_for() { grep -q "scripts/validate-release-beat.py --channel edge --target-sha $1" "$PYTHON_MOCK_LOG"; }

# Case 1: happy path — HEAD itself is the newest green build => pin HEAD, open PR.
run_cut GH_MOCK_RUN_SHAS="$MAIN_FULL"
[ "$RUN_RC" -eq 0 ]                  || fail "happy path should exit 0 (got $RUN_RC): $RUN_OUT"
created_pr                           || fail "happy path should open a PR"
[ "$WROTE_VERSION" = "$MAIN_SHORT" ] || fail "happy path should write version: $MAIN_SHORT (got $WROTE_VERSION)"
grep -q "cut edge release $MAIN_SHORT" "$GIT_MOCK_LOG" || fail "commit message should pin $MAIN_SHORT"
grep -q "edge-release/$MAIN_SHORT" "$GIT_MOCK_LOG"     || fail "should branch on $MAIN_SHORT"
validator_called_for "$MAIN_SHORT"                    || fail "happy path should validate release beat for $MAIN_SHORT"
pass "happy path pins HEAD and opens PR"

# Case 2: tests-only HEAD — newest green build is the OLDER commit => pin OLDER.
# Blind-HEAD-pin regression guard. The trailing-SHA note must print (SHA != HEAD).
run_cut GH_MOCK_RUN_SHAS="$OLDER_FULL"
[ "$RUN_RC" -eq 0 ]                   || fail "older-pin should exit 0 (got $RUN_RC): $RUN_OUT"
created_pr                            || fail "older-pin should open a PR"
[ "$WROTE_VERSION" = "$OLDER_SHORT" ] || fail "older-pin should write the OLDER built SHA $OLDER_SHORT (got $WROTE_VERSION)"
grep -q "edge-release/$OLDER_SHORT" "$GIT_MOCK_LOG"  || fail "older-pin should branch on $OLDER_SHORT"
grep -q "edge-release/$MAIN_SHORT" "$GIT_MOCK_LOG"   && fail "older-pin must NOT pin tests-only HEAD $MAIN_SHORT"
validator_called_for "$OLDER_SHORT"                  || fail "older-pin should validate release beat for $OLDER_SHORT"
printf '%s' "$RUN_OUT" | grep -q "latest BUILT main commit" || fail "older-pin should print the trailing-SHA note"
pass "tests-only HEAD pins the older built SHA (blind-HEAD-pin guard)"

# Case 3: a green run exists for a SHA not on main (rebased-away) — skip it, fall to
# the newest green build that IS on main.
run_cut GH_MOCK_RUN_SHAS="$OFFMAIN_FULL"$'\n'"$OLDER_FULL"
[ "$RUN_RC" -eq 0 ]                   || fail "off-main case should exit 0 (got $RUN_RC): $RUN_OUT"
[ "$WROTE_VERSION" = "$OLDER_SHORT" ] || fail "off-main case should pick the on-main built SHA $OLDER_SHORT (got $WROTE_VERSION)"
pass "green run for an off-main SHA is skipped (topology-correct selection)"

# Case 4: image-affecting file changed since the built commit => HARD-fail, no PR.
run_cut GH_MOCK_RUN_SHAS="$OLDER_FULL" GIT_MOCK_DIFF="mammamiradio/audio/normalizer.py"
[ "$RUN_RC" -ne 0 ]  || fail "image-drift must hard-fail (got $RUN_RC)"
never_created_pr     || fail "image-drift must not open a PR"
never_committed      || fail "image-drift must not commit"
never_pushed         || fail "image-drift must not push"
printf '%s' "$RUN_OUT" | grep -q "image files changed" || fail "image-drift message should name the drift"
pass "image-affecting drift since built commit hard-fails (no stale-image pin)"

# Case 5: no successful build run anywhere => HARD-fail, no PR.
run_cut GH_MOCK_RUN_SHAS=""
[ "$RUN_RC" -ne 0 ]  || fail "no-build must hard-fail (got $RUN_RC)"
never_created_pr     || fail "no-build must not open a PR"
never_committed      || fail "no-build must not commit"
never_pushed         || fail "no-build must not push"
printf '%s' "$RUN_OUT" | grep -q "no successful" || fail "no-build message should say so"
pass "no successful build run hard-fails"

# Case 6: every green run is off-main (all rebased away) => HARD-fail, no PR.
run_cut GH_MOCK_RUN_SHAS="$OFFMAIN_FULL"
[ "$RUN_RC" -ne 0 ]  || fail "all-off-main must hard-fail (got $RUN_RC)"
never_created_pr     || fail "all-off-main must not open a PR"
never_pushed         || fail "all-off-main must not push"
printf '%s' "$RUN_OUT" | grep -q "no successful" || fail "all-off-main message should say so"
pass "all green runs off main hard-fails"

# Case 7: gh query fails => HARD-fail, no PR. Soft-pass regression guard (the old
# script warned-and-continued on an unverifiable image).
run_cut GH_MOCK_RUN_SHAS="$MAIN_FULL" GH_MOCK_RUN_FAIL=1
[ "$RUN_RC" -ne 0 ]  || fail "gh-query-fail must hard-fail (got $RUN_RC)"
never_created_pr     || fail "gh-query-fail must not open a PR"
never_committed      || fail "gh-query-fail must not commit"
never_pushed         || fail "gh-query-fail must not push"
printf '%s' "$RUN_OUT" | grep -q "could not query" || fail "gh-query-fail message should say so"
pass "gh query failure hard-fails, never soft-passes (soft-pass guard)"

# Case 8: gh CLI not on PATH => HARD-fail at the first guard, no PR.
_PATH_OVERRIDE="$MOCK_NOGH" run_cut
[ "$RUN_RC" -ne 0 ]  || fail "gh-absent must hard-fail (got $RUN_RC)"
never_created_pr     || fail "gh-absent must not open a PR"
printf '%s' "$RUN_OUT" | grep -q "gh CLI not available" || fail "gh-absent message should say so"
pass "missing gh CLI hard-fails at the first guard"

# Case 9: working tree not clean => HARD-fail before anything, no PR.
run_cut GIT_MOCK_DIRTY=1 GH_MOCK_RUN_SHAS="$MAIN_FULL"
[ "$RUN_RC" -ne 0 ]  || fail "dirty-tree must hard-fail (got $RUN_RC)"
never_created_pr     || fail "dirty-tree must not open a PR"
never_committed      || fail "dirty-tree must not commit"
never_pushed         || fail "dirty-tree must not push"
printf '%s' "$RUN_OUT" | grep -q "working tree not clean" || fail "dirty-tree message should say so"
pass "unclean working tree hard-fails"

# Case 10: edge already at the target SHA on origin/main => no-op exit 0, no
# commit/PR/push. The version is read from git show origin/main (GIT_MOCK_SHOW_VERSION),
# NOT the local tree (which is the original aeafa99 here) — so this also guards that
# the idempotency check sources origin/main, not the caller's checked-out branch.
run_cut GH_MOCK_RUN_SHAS="$MAIN_FULL" GIT_MOCK_SHOW_VERSION="$MAIN_SHORT"
[ "$RUN_RC" -eq 0 ]  || fail "idempotent case should exit 0 (got $RUN_RC): $RUN_OUT"
never_committed      || fail "idempotent case must not commit"
never_created_pr     || fail "idempotent case must not open a PR"
never_pushed         || fail "idempotent case must not push"
printf '%s' "$RUN_OUT" | grep -q "already at" || fail "idempotent message should say 'already at'"
pass "edge already at target SHA (read from origin/main) is a clean no-op"

# Case 11: an edge PR for the target is already open => no-op exit 0, no commit/PR.
run_cut GH_MOCK_RUN_SHAS="$MAIN_FULL" GH_MOCK_PR_URL="https://github.com/florianhorner/mammamiradio/pull/999"
[ "$RUN_RC" -eq 0 ]  || fail "existing-PR case should exit 0 (got $RUN_RC): $RUN_OUT"
never_committed      || fail "existing-PR case must not commit"
never_created_pr     || fail "existing-PR case must not open a second PR"
never_pushed         || fail "existing-PR case must not push"
printf '%s' "$RUN_OUT" | grep -q "already open" || fail "existing-PR message should say 'already open'"
pass "existing open edge PR is a clean no-op"

# Case 12: the drift check itself errors (git diff fails, not "no drift") => HARD-fail,
# no PR. Regression guard for the `|| true` that once turned an unverifiable drift
# check into a release-proceed — the exact soft-pass this script exists to remove.
run_cut GH_MOCK_RUN_SHAS="$OLDER_FULL" GIT_MOCK_DIFF_FAIL=1
[ "$RUN_RC" -ne 0 ]  || fail "drift-check failure must hard-fail (got $RUN_RC)"
never_created_pr     || fail "drift-check failure must not open a PR"
never_committed      || fail "drift-check failure must not commit"
never_pushed         || fail "drift-check failure must not push"
printf '%s' "$RUN_OUT" | grep -q "could not verify" || fail "drift-check-fail message should say so"
pass "unverifiable drift check hard-fails, never soft-passes"

# Case 13: release-beat target validation fails => HARD-fail after branch prep,
# before commit/push/PR.
run_cut GH_MOCK_RUN_SHAS="$MAIN_FULL" PYTHON_MOCK_RC=7
[ "$RUN_RC" -ne 0 ]  || fail "release-beat validation failure must hard-fail (got $RUN_RC)"
validator_called_for "$MAIN_SHORT" || fail "release-beat failure case should call validator for $MAIN_SHORT"
never_committed      || fail "release-beat validation failure must not commit"
never_created_pr     || fail "release-beat validation failure must not open a PR"
never_pushed         || fail "release-beat validation failure must not push"
pass "release-beat validation failure blocks edge cut before commit/push/PR"

# Safety: the test must never have mutated the real repo, branch, or edge config.
[ "$(git rev-parse HEAD)" = "$ORIG_HEAD" ]                 || fail "test mutated real HEAD"
[ "$(git rev-parse --abbrev-ref HEAD)" = "$ORIG_BRANCH" ]  || fail "test changed the real branch"
diff -q "$EDGE_CONFIG" "$EDGE_ORIG" >/dev/null             || fail "test left the real edge config modified"
pass "real repo / branch / edge config untouched"

echo
echo "All 13 cut-edge-release cases passed."
