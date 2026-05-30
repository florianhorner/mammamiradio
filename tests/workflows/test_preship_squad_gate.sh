#!/usr/bin/env bash
# Self-test for scripts/hooks/require-preship-squad.sh
#
# Drives the PreToolUse(Bash) guard with mocked stdin payloads and a mocked
# review-log reader (via MMR_PRESHIP_REVIEW_READER), asserting it blocks/allows
# as expected. No network, no gh CLI, no gstack. Exits non-zero on any mismatch.
#
# The guard outputs a deny JSON on stdout (exit 0) ONLY when a gh-pr-create/merge
# command lacks a qualifying squad entry for HEAD; every other path is fail-open
# (no output). So "blocked" == stdout contains permissionDecision:deny.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/scripts/hooks/require-preship-squad.sh"
cd "$REPO_ROOT"

[[ -x "$HOOK" ]] || chmod +x "$HOOK"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

HEAD_SHA="$(git rev-parse --short HEAD)"
ANC_SHA="$(git rev-parse --short HEAD~1)"
BOGUS_SHA="0000000"

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  STALE_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"   # BSD/macOS
else
  STALE_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"  # GNU/Linux CI
fi

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

# make_reader <skill> <commit> <timestamp> -> path to an executable mock reader
# that emits a single review-log JSONL line then the ---CONFIG--- sentinel.
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

# empty_reader -> executable reader with no entries (just the sentinel)
empty_reader() {
  local f; f="$(mktemp "$TMPDIR_T/reader.XXXXXX")"
  printf '%s\n' '#!/usr/bin/env bash' 'echo ---CONFIG---' > "$f"
  chmod +x "$f"
  echo "$f"
}

# verdict <json-stdin> <reader-path> -> prints "deny" or "allow"
verdict() {
  local out
  out="$(printf '%s' "$1" | MMR_PRESHIP_REVIEW_READER="$2" bash "$HOOK" 2>/dev/null || true)"
  if printf '%s' "$out" | grep -q '"permissionDecision":"deny"'; then echo deny; else echo allow; fi
}

DUMMY="$(empty_reader)"  # reused where the reader must not be consulted

# Case 1: non-gh command => allow (reader never consulted)
[ "$(verdict '{"tool_input":{"command":"echo hi"}}' "$DUMMY")" = allow ] \
  || fail "non-gh command should be allowed"
pass "non-gh command allowed"

# Case 2: gh pr view (not create/merge) => allow
[ "$(verdict '{"tool_input":{"command":"gh pr view 123"}}' "$DUMMY")" = allow ] \
  || fail "gh pr view should be allowed"
pass "gh pr view allowed"

# Case 3: malformed JSON stdin => fail-open allow
[ "$(verdict 'not json {{{' "$DUMMY")" = allow ] \
  || fail "malformed JSON should fail open (allow)"
pass "malformed JSON fails open"

# Case 4: gh pr create with missing/non-exec reader => fail-open allow
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$TMPDIR_T/nonexistent")" = allow ] \
  || fail "missing reader should fail open (allow)"
pass "missing reader fails open"

# Case 5: gh pr create with fresh review entry for HEAD => allow
[ "$(verdict '{"tool_input":{"command":"gh pr create --fill"}}' "$(make_reader review "$HEAD_SHA" "$NOW_ISO")")" = allow ] \
  || fail "fresh review entry for HEAD should allow"
pass "fresh review@HEAD allowed"

# Case 6: adversarial-review entry for HEAD => allow
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader adversarial-review "$HEAD_SHA" "$NOW_ISO")")" = allow ] \
  || fail "adversarial-review entry for HEAD should allow"
pass "adversarial-review@HEAD allowed"

# Case 7: entry for an ANCESTOR commit (not HEAD) => allow via merge-base
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader review "$ANC_SHA" "$NOW_ISO")")" = allow ] \
  || fail "ancestor-commit entry should allow via merge-base"
pass "ancestor-commit entry allowed"

# Case 8: gh pr merge with matching entry => allow (covers the merge verb)
[ "$(verdict '{"tool_input":{"command":"gh pr merge 5 --squash"}}' "$(make_reader review "$HEAD_SHA" "$NOW_ISO")")" = allow ] \
  || fail "gh pr merge with matching entry should allow"
pass "gh pr merge with entry allowed"

# Case 9: gh pr create, no entries => DENY
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(empty_reader)")" = deny ] \
  || fail "gh pr create with no squad entry should deny"
pass "no entry denies"

# Case 10: gh pr create, entry is STALE (>2h) => DENY
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader review "$HEAD_SHA" "$STALE_ISO")")" = deny ] \
  || fail "stale (>2h) entry should deny"
pass "stale entry denies"

# Case 11: gh pr create, entry has wrong skill (qa) => DENY
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader qa "$HEAD_SHA" "$NOW_ISO")")" = deny ] \
  || fail "non-review skill should not satisfy the gate"
pass "wrong-skill entry denies"

# Case 12: gh pr create, entry for an unrelated/bogus commit => DENY
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader review "$BOGUS_SHA" "$NOW_ISO")")" = deny ] \
  || fail "bogus non-ancestor commit should deny"
pass "bogus-commit entry denies"

# Case 13: gh pr create, entry for HEAD but UNPARSEABLE timestamp => DENY
# A timestamp the guard cannot verify must fail toward "not authorized", never
# be blessed as fresh (regression guard for the es=0 fail-open-wrong-direction).
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader review "$HEAD_SHA" "not-a-timestamp")")" = deny ] \
  || fail "unparseable timestamp should deny (not be treated as fresh)"
pass "unparseable timestamp denies"

echo
echo "All 13 pre-ship squad gate cases passed."
