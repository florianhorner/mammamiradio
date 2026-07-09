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
# Ancestor cases need HEAD~1 — a depth-1 shallow clone has no parent commit.
# CI checks out with fetch-depth: 2 (quality.yml) precisely for this.
ANC_SHA="$(git rev-parse --short HEAD~1 2>/dev/null)" \
  || fail "HEAD~1 unavailable (shallow clone?) — checkout with fetch-depth >= 2"
BOGUS_SHA="0000000"

NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if date -u -v-3H +%s >/dev/null 2>&1; then
  STALE_ISO="$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)"    # BSD/macOS
  FUTURE_ISO="$(date -u -v+3H +%Y-%m-%dT%H:%M:%SZ)"
else
  STALE_ISO="$(date -u -d '3 hours ago' +%Y-%m-%dT%H:%M:%SZ)"  # GNU/Linux CI
  FUTURE_ISO="$(date -u -d '3 hours' +%Y-%m-%dT%H:%M:%SZ)"
fi

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

MERGE_GRAPHQL_FILE="$TMPDIR_T/merge.graphql"
READ_GRAPHQL_FILE="$TMPDIR_T/read.graphql"
MERGE_GRAPHQL_JSON="$TMPDIR_T/merge-payload.json"
READ_GRAPHQL_JSON="$TMPDIR_T/read-payload.json"

cat > "$MERGE_GRAPHQL_FILE" <<'GRAPHQL'
mutation {
  mergePullRequest(input: {pullRequestId: "PR_kw"}) {
    pullRequest { id }
  }
}
GRAPHQL

cat > "$READ_GRAPHQL_FILE" <<'GRAPHQL'
query {
  viewer { login }
}
GRAPHQL

cat > "$MERGE_GRAPHQL_JSON" <<'JSON'
{"query":"mutation { enablePullRequestAutoMerge(input: {pullRequestId: \"PR_kw\"}) { pullRequest { id } } }"}
JSON

cat > "$READ_GRAPHQL_JSON" <<'JSON'
{"query":"query { viewer { login } }"}
JSON

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

payload() {
  jq -nc --arg cmd "$1" '{tool_input:{command:$cmd}}'
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

# Case 8: gh pr merge => DENY even with a fresh matching entry. Landing goes
# through scripts/land-pr.sh (landing contract, 2026-06-12); the wrapper does
# its own code-state squad check and the hook never sees its internal gh calls.
[ "$(verdict '{"tool_input":{"command":"gh pr merge 5 --squash"}}' "$(make_reader review "$HEAD_SHA" "$NOW_ISO")")" = deny ] \
  || fail "raw gh pr merge must deny even with a fresh squad entry"
pass "raw gh pr merge denies (use land-pr.sh)"

# Case 8b: gh pr merge --squash --auto => DENY too (arming bypasses the wrapper
# and skips the head pin) — the way-out message names the wrapper.
out_8b="$(printf '%s' '{"tool_input":{"command":"gh pr merge 5 --squash --auto"}}' \
  | MMR_PRESHIP_REVIEW_READER="$DUMMY" bash "$HOOK" 2>/dev/null || true)"
printf '%s' "$out_8b" | grep -q '"permissionDecision":"deny"' \
  || fail "gh pr merge --auto must deny (wrapper owns arming)"
printf '%s' "$out_8b" | grep -q 'land-pr.sh' \
  || fail "merge deny message must name scripts/land-pr.sh as the way out"
pass "gh pr merge --auto denies, message names land-pr.sh"

# Case 8c: gh pr merge --disable-auto => ALLOW (disarming is a cancel, not a landing)
[ "$(verdict '{"tool_input":{"command":"gh pr merge 5 --disable-auto"}}' "$DUMMY")" = allow ] \
  || fail "gh pr merge --disable-auto should be allowed"
pass "gh pr merge --disable-auto allowed"

# Case 8c2: --disable-auto AFTER a shell operator must NOT unlock the merge —
# `gh pr merge 5 --squash && echo "--disable-auto"` is a bypass attempt.
[ "$(verdict '{"tool_input":{"command":"gh pr merge 5 --squash && echo \"--disable-auto\""}}' "$DUMMY")" = deny ] \
  || fail "--disable-auto beyond a shell operator must not bypass the merge deny"
pass "--disable-auto past a shell operator still denies"

# Case 8d: scripts/land-pr.sh invocation => ALLOW (hook does not match it; the
# wrapper enforces the squad itself with code-state freshness)
[ "$(verdict '{"tool_input":{"command":"scripts/land-pr.sh 5"}}' "$DUMMY")" = allow ] \
  || fail "land-pr.sh invocation should pass the hook"
pass "land-pr.sh invocation allowed"

# Case 8e: read-only gh api calls => ALLOW
[ "$(verdict '{"tool_input":{"command":"gh api repos/florianhorner/mammamiradio/pulls/5"}}' "$DUMMY")" = allow ] \
  || fail "read-only gh api calls should be allowed"
pass "read-only gh api allowed"

# Case 8f: read-only gh api call to the merge-status endpoint => ALLOW
[ "$(verdict '{"tool_input":{"command":"gh api /repos/florianhorner/mammamiradio/pulls/5/merge"}}' "$DUMMY")" = allow ] \
  || fail "read-only gh api merge-status endpoint should be allowed"
pass "read-only gh api merge-status endpoint allowed"

# Case 8g: gh api REST PUT to /pulls/<n>/merge => DENY (raw API landing bypass)
[ "$(verdict '{"tool_input":{"command":"gh api repos/florianhorner/mammamiradio/pulls/5/merge -X PUT"}}' "$DUMMY")" = deny ] \
  || fail "gh api REST -X PUT pull merge must deny"
pass "gh api REST -X PUT pull merge denies"

# Case 8h: compact -XPUT form is the same raw REST merge bypass => DENY
[ "$(verdict '{"tool_input":{"command":"gh api -XPUT /repos/florianhorner/mammamiradio/pulls/5/merge"}}' "$DUMMY")" = deny ] \
  || fail "gh api REST compact -XPUT pull merge must deny"
pass "gh api REST compact -XPUT pull merge denies"

# Case 8i: compact -XGET remains read-only => ALLOW
[ "$(verdict '{"tool_input":{"command":"gh api -XGET /repos/florianhorner/mammamiradio/pulls/5/merge"}}' "$DUMMY")" = allow ] \
  || fail "gh api REST compact -XGET merge-status should be allowed"
pass "gh api REST compact -XGET merge-status allowed"

# Case 8j: --method PUT form is the same raw REST merge bypass => DENY
[ "$(verdict '{"tool_input":{"command":"gh api --method PUT /repos/florianhorner/mammamiradio/pulls/5/merge"}}' "$DUMMY")" = deny ] \
  || fail "gh api REST --method PUT pull merge must deny"
pass "gh api REST --method PUT pull merge denies"

# Case 8k: gh api graphql mergePullRequest mutation => DENY
[ "$(verdict '{"tool_input":{"command":"gh api graphql -f query=mutation{mergePullRequest(input:{pullRequestId:PR_kw}){pullRequest{id}}}"}}' "$DUMMY")" = deny ] \
  || fail "gh api graphql mergePullRequest must deny"
pass "gh api graphql mergePullRequest denies"

# Case 8l: gh api graphql enablePullRequestAutoMerge mutation => DENY
[ "$(verdict '{"tool_input":{"command":"gh api graphql -f query=mutation{enablePullRequestAutoMerge(input:{pullRequestId:PR_kw}){pullRequest{id}}}"}}' "$DUMMY")" = deny ] \
  || fail "gh api graphql enablePullRequestAutoMerge must deny"
pass "gh api graphql enablePullRequestAutoMerge denies"

# Case 8m: gh api graphql query loaded from file with merge mutation => DENY
[ "$(verdict "$(payload "gh api graphql -F query=@$MERGE_GRAPHQL_FILE")" "$DUMMY")" = deny ] \
  || fail "gh api graphql -F query=@file merge mutation must deny"
pass "gh api graphql -F query=@file merge mutation denies"

# Case 8n: gh api graphql JSON body loaded from file with merge mutation => DENY
[ "$(verdict "$(payload "gh api graphql --input $MERGE_GRAPHQL_JSON")" "$DUMMY")" = deny ] \
  || fail "gh api graphql --input merge payload must deny"
pass "gh api graphql --input merge payload denies"

# Case 8o: gh api graphql JSON body loaded from stdin is uninspectable => DENY
[ "$(verdict '{"tool_input":{"command":"gh api graphql --input -"}}' "$DUMMY")" = deny ] \
  || fail "gh api graphql --input - should deny because payload is uninspectable"
pass "gh api graphql --input - denies"

# Case 8p: gh api graphql query loaded from stdin is uninspectable => DENY
[ "$(verdict '{"tool_input":{"command":"gh api graphql -F query=@-"}}' "$DUMMY")" = deny ] \
  || fail "gh api graphql -F query=@- should deny because payload is uninspectable"
pass "gh api graphql -F query=@- denies"

# Case 8q: read-only gh api graphql query => ALLOW
[ "$(verdict '{"tool_input":{"command":"gh api graphql -f query=query{viewer{login}}"}}' "$DUMMY")" = allow ] \
  || fail "read-only gh api graphql query should be allowed"
pass "read-only gh api graphql allowed"

# Case 8r: read-only gh api graphql query loaded from file => ALLOW
[ "$(verdict "$(payload "gh api graphql -F query=@$READ_GRAPHQL_FILE")" "$DUMMY")" = allow ] \
  || fail "read-only gh api graphql -F query=@file should be allowed"
pass "read-only gh api graphql -F query=@file allowed"

# Case 8s: read-only gh api graphql JSON body loaded from file => ALLOW
[ "$(verdict "$(payload "gh api graphql --input $READ_GRAPHQL_JSON")" "$DUMMY")" = allow ] \
  || fail "read-only gh api graphql --input should be allowed"
pass "read-only gh api graphql --input allowed"

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

# Case 14: gh pr create, entry timestamped >2h in the FUTURE => DENY
# A far-future timestamp is outside the +/-2h window and must not read as fresh.
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$(make_reader review "$HEAD_SHA" "$FUTURE_ISO")")" = deny ] \
  || fail "far-future timestamp should deny (not be treated as fresh)"
pass "far-future timestamp denies"

echo
echo "All 33 pre-ship squad gate cases passed."
