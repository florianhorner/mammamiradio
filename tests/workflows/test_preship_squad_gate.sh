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
# CI checks out full history (quality.yml), which keeps HEAD~1 available.
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

# make_evidence <exit-code> <stdout-line> -> path to an executable mock checker.
# Stands in for scripts/check-preship-evidence.sh so the ledger cases below stay
# about the ledger, and the receipt cases can drive each outcome deliberately.
make_evidence() {
  local f; f="$(mktemp "$TMPDIR_T/evidence.XXXXXX")"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf 'echo %q\n' "$2"
    printf 'exit %s\n' "$1"
  } > "$f"
  chmod +x "$f"
  echo "$f"
}

EVIDENCE_OK="$(make_evidence 0 'landing-evidence: OK — pr content has 1 matching v2 receipt(s)')"

# verdict <json-stdin> <reader-path> [evidence-checker-path] -> "deny" or "allow".
# The evidence checker defaults to a passing stub so existing ledger cases assert
# the ledger rule alone; receipt cases pass their own stub.
verdict() {
  local out evidence="${3:-$EVIDENCE_OK}"
  out="$(printf '%s' "$1" \
    | MMR_PRESHIP_REVIEW_READER="$2" MMR_PRESHIP_EVIDENCE_CHECKER="$evidence" \
      bash "$HOOK" 2>/dev/null || true)"
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

# --- Rule 1b: the committed v2 receipt, not just the local ledger entry --------
# The ledger lives only on this machine, so every case below holds the ledger
# satisfied (fresh review@HEAD) and varies only what the evidence checker says.

FRESH_READER="$(make_reader review "$HEAD_SHA" "$NOW_ISO")"

# Case 15: squad logged AND receipt covers HEAD => allow
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$FRESH_READER" "$EVIDENCE_OK")" = allow ] \
  || fail "logged squad with a covering receipt should allow"
pass "logged squad + covering receipt allowed"

# Case 16: squad logged but NO receipt covers HEAD => DENY.
# This is the #1126 shape: the ledger entry existed, the receipt never did, and
# nothing objected until the landing attempt.
EVIDENCE_MISSING="$(make_evidence 1 'landing-evidence: FAIL — PR adds no new v2 review receipt')"
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "logged squad without a covering receipt should deny"
pass "missing receipt denies"

# Case 17: receipt exists but pins content outside base..target => DENY
EVIDENCE_STALE="$(make_evidence 1 'landing-evidence: FAIL — new v2 receipt pins a reviewed commit outside base-to-target history')"
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$FRESH_READER" "$EVIDENCE_STALE")" = deny ] \
  || fail "receipt not covering the head content should deny"
pass "non-covering receipt denies"

# Case 18: checker cannot run (no verdict rendered) => fail-open allow.
# Non-zero alone must not block: an unusable Python or a missing dependency is a
# tooling failure, and this guard never converts one into a blocked PR.
EVIDENCE_BROKEN="$(make_evidence 1 'check-preship-evidence: v2 requires Python 3.11+ (set MAMMAMIRADIO_PYTHON)')"
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$FRESH_READER" "$EVIDENCE_BROKEN")" = allow ] \
  || fail "a checker that cannot run should fail open (allow)"
pass "unrunnable checker fails open"

# Case 19: checker missing entirely => fail-open allow
[ "$(verdict '{"tool_input":{"command":"gh pr create"}}' "$FRESH_READER" "$TMPDIR_T/no-such-checker")" = allow ] \
  || fail "missing evidence checker should fail open (allow)"
pass "missing evidence checker fails open"

# Case 20: a denied receipt check still leaves non-create commands alone
[ "$(verdict '{"tool_input":{"command":"gh pr view 1126"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = allow ] \
  || fail "gh pr view must stay unguarded regardless of receipt state"
pass "gh pr view unaffected by receipt rule"

# Case 21: the deny message names the remedy, not just the problem
DENY_OUT="$(printf '%s' '{"tool_input":{"command":"gh pr create"}}' \
  | MMR_PRESHIP_REVIEW_READER="$FRESH_READER" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_MISSING" \
    bash "$HOOK" 2>/dev/null || true)"
printf '%s' "$DENY_OUT" | grep -q 'emit-review-evidence.sh' \
  || fail "deny message must name scripts/emit-review-evidence.sh as the fix"
printf '%s' "$DENY_OUT" | jq -e . >/dev/null 2>&1 \
  || fail "deny payload must stay valid JSON once the checker output is embedded"
pass "deny message names the remedy and stays valid JSON"

# Case 22: the hook must hand the checker the FORK POINT, not the base tip.
# Asserting "no ancestry complaint against the ambient checkout" proves nothing:
# in a PR merge checkout origin/main is already an ancestor of HEAD, so that
# assertion passes even if the hook regresses to the base tip. Capture the --base
# the hook actually passes and compare it to git merge-base directly.
BASE_CAPTURE="$TMPDIR_T/captured-base"
EVIDENCE_CAPTURE="$(mktemp "$TMPDIR_T/evidence.XXXXXX")"
cat > "$EVIDENCE_CAPTURE" <<CAPTURE
#!/usr/bin/env bash
while [ \$# -gt 0 ]; do
  [ "\$1" = "--base" ] && { printf '%s' "\$2" > "$BASE_CAPTURE"; break; }
  shift
done
echo 'landing-evidence: OK — captured'
exit 0
CAPTURE
chmod +x "$EVIDENCE_CAPTURE"

verdict '{"tool_input":{"command":"gh pr create --base main"}}' "$FRESH_READER" "$EVIDENCE_CAPTURE" >/dev/null
EXPECTED_BASE="$(git merge-base origin/main HEAD 2>/dev/null || git rev-parse origin/main)"
[ "$(cat "$BASE_CAPTURE" 2>/dev/null)" = "$EXPECTED_BASE" ] \
  || fail "hook must pass the fork point as --base (got '$(cat "$BASE_CAPTURE" 2>/dev/null)', want '$EXPECTED_BASE')"
pass "fork point passed as --base, not the base tip"

# Case 22b: a comment is not part of the gh pr create argument vector. The
# apparent --base HEAD must be ignored, leaving the default fork point in use.
: > "$BASE_CAPTURE"
verdict '{"tool_input":{"command":"gh pr create # --base HEAD"}}' "$FRESH_READER" "$EVIDENCE_CAPTURE" >/dev/null
[ "$(cat "$BASE_CAPTURE" 2>/dev/null)" = "$EXPECTED_BASE" ] \
  || fail "comment text must not change the default fork-point base (got '$(cat "$BASE_CAPTURE" 2>/dev/null)', want '$EXPECTED_BASE')"
pass "comment text is excluded from --base extraction"

# Case 22c: the other half of the same rule. Only the FIRST gh pr create owns the
# argument vector; a --base belonging to a later command segment must not be read
# as this one's. Both halves are covered because either can regress alone.
: > "$BASE_CAPTURE"
verdict '{"tool_input":{"command":"gh pr create --base main && gh pr create --base HEAD"}}' "$FRESH_READER" "$EVIDENCE_CAPTURE" >/dev/null
[ "$(cat "$BASE_CAPTURE" 2>/dev/null)" = "$EXPECTED_BASE" ] \
  || fail "a later command segment's --base must not be used (got '$(cat "$BASE_CAPTURE" 2>/dev/null)', want '$EXPECTED_BASE')"
pass "later command segment's --base is ignored"

# Case 23: --base=VALUE form is parsed
[ "$(verdict '{"tool_input":{"command":"gh pr create --base=main"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "--base=VALUE form should still reach the receipt rule"
pass "--base=VALUE parsed"

# Case 24: --base inside --body prose must not be read as the option, in EITHER
# order. Prose first is the dangerous one: word-splitting picked up the prose ref,
# it failed to resolve, and the hook fell open — the gate silently off on a PR with
# no evidence. This PR's own body contains the token `--base`, which is how it
# surfaced. Both orderings must still reach the receipt rule and deny.
[ "$(verdict '{"tool_input":{"command":"gh pr create --base main --body \"see --base nonexistent-ref\""}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "a --base inside body prose (after the real option) must not derail the base"
pass "body-prose --base after the option does not derail the base"

[ "$(verdict '{"tool_input":{"command":"gh pr create --body \"see --base nonexistent-ref\" --base main"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "a --base inside body prose BEFORE the real option must not disable the gate"
pass "body-prose --base before the option does not disable the gate"

# Case 24c: an unbalanced quote must not become an accidental bypass either.
[ "$(verdict '{"tool_input":{"command":"gh pr create --body \"unterminated --base nope"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "unparseable quoting must fall back to the default base, not skip the gate"
pass "unbalanced quoting still reaches the receipt rule"

# Case 24d: an unresolvable base must never skip the gate. Base extraction is a
# convenience, not a security boundary: any command line that yields a ref git
# cannot resolve falls back to the default branch instead of exiting allow. The
# unquoted form below passes two --base options, so the "real" one is ambiguous
# even to gh; ambiguity must not read as permission.
[ "$(verdict '{"tool_input":{"command":"gh pr create --base totally-nonexistent-ref"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "an unresolvable --base must fall back to the default, not skip the receipt rule"
pass "unresolvable base falls back instead of bypassing"

[ "$(verdict '{"tool_input":{"command":"gh pr create --body see --base nonexistent-ref --base main"}}' "$FRESH_READER" "$EVIDENCE_MISSING")" = deny ] \
  || fail "an ambiguous multi---base command line must not bypass the receipt rule"
pass "ambiguous multi---base command line still denies"

# Case 25: checker output containing a backslash must still yield valid JSON.
# An unparseable deny is silently dropped, which retires the rule without a trace.
EVIDENCE_BACKSLASH="$(make_evidence 1 'landing-evidence: FAIL — receipt proof\v2\r.json is "wrong"')"
BS_OUT="$(printf '%s' '{"tool_input":{"command":"gh pr create"}}' \
  | MMR_PRESHIP_REVIEW_READER="$FRESH_READER" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_BACKSLASH" \
    bash "$HOOK" 2>/dev/null || true)"
printf '%s' "$BS_OUT" | jq -e . >/dev/null 2>&1 \
  || fail "deny payload must stay valid JSON when checker output contains backslashes or quotes"
printf '%s' "$BS_OUT" | grep -q '"permissionDecision":"deny"' \
  || fail "backslash-bearing checker output must still deny"
pass "backslash/quote checker output stays valid JSON"

# Case 26: the remedy is conditional. Re-emitting fixes a missing receipt; it does
# nothing for evidence the checker considers present but wrong, and saying so anyway
# sends a contributor round a loop that cannot terminate.
EVIDENCE_WRONG="$(make_evidence 1 'landing-evidence: FAIL — v2 receipt modifies a base receipt')"
WRONG_OUT="$(printf '%s' '{"tool_input":{"command":"gh pr create"}}' \
  | MMR_PRESHIP_REVIEW_READER="$FRESH_READER" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_WRONG" \
    bash "$HOOK" 2>/dev/null || true)"
printf '%s' "$WRONG_OUT" | grep -q '"permissionDecision":"deny"' \
  || fail "a non-emitter-fixable evidence failure must still deny"
printf '%s' "$WRONG_OUT" | grep -q 'emit-review-evidence.sh' \
  && fail "must not prescribe re-emitting for a failure re-emitting cannot fix"
pass "remedy is conditional on the failure class"

# ---------------------------------------------------------------------------
# Rule 1a: the guard only judges PRs against THIS repository.
#
# It is registered for every Bash call in the session, and every question it asks
# is local to this checkout: the ledger is keyed by this repo's slug and branch,
# and the evidence checker reads receipts under this working tree. Opening a PR
# on another repo from a session rooted here asked those questions about the
# wrong repository and denied a PR whose squad had run and was logged in its own
# repo's ledger.
#
# The reader is EMPTY in every case below, so anything that reaches the ledger
# rule denies. An allow verdict therefore proves the scope check ran; it does
# not mean a review was found.
# ---------------------------------------------------------------------------

THIS_REPO="$(git remote get-url origin 2>/dev/null \
  | sed -E 's#^(https://|git@)[^/:]+[/:]##; s#\.git$##')"
[ -n "$THIS_REPO" ] || fail "cannot resolve this checkout's owner/repo from origin"

FOREIGN_CREATE='gh pr create --repo florianhorner/gh-workflows --base main --title x --body y'
[ "$(verdict "$(payload "$FOREIGN_CREATE")" "$DUMMY")" = allow ] \
  || fail "a PR against another repository must not be judged by this repo's guard"
pass "foreign --repo is out of scope"

FOREIGN_SHORT='gh pr create -R florianhorner/engineering-standards --fill'
[ "$(verdict "$(payload "$FOREIGN_SHORT")" "$DUMMY")" = allow ] \
  || fail "-R naming another repository must be out of scope too"
pass "foreign -R is out of scope"

FOREIGN_URL='gh pr create --repo https://github.com/florianhorner/gh-workflows --fill'
[ "$(verdict "$(payload "$FOREIGN_URL")" "$DUMMY")" = allow ] \
  || fail "a URL form of a foreign repo must be out of scope"
pass "foreign --repo URL is out of scope"

# The direction that matters. A scope check that let its own repo through would
# retire the whole guard, so the allow/deny pair is what makes it a check rather
# than an off switch.
[ "$(verdict "$(payload "gh pr create --repo $THIS_REPO --fill")" "$DUMMY")" = deny ] \
  || fail "--repo naming this repository must still be judged"
pass "--repo naming this repo is still judged"

# GitHub treats these names case-insensitively. Comparing raw would switch the
# guard off for its own repo on nothing but capitalisation.
UPPER_REPO="$(printf '%s' "$THIS_REPO" | tr '[:lower:]' '[:upper:]')"
[ "$(verdict "$(payload "gh pr create --repo $UPPER_REPO --fill")" "$DUMMY")" = deny ] \
  || fail "a case-different spelling of this repo must still be judged"
pass "--repo is compared case-insensitively"

# The flagless form resolves to this checkout, so it is unchanged.
[ "$(verdict "$(payload 'gh pr create --fill')" "$DUMMY")" = deny ] \
  || fail "a flagless create must still be judged"
pass "flagless create is still judged"

# Same class as the --base-in-the-body hole: the option reader must see the
# argument vector, not the body text.
PROSE_CREATE='gh pr create --fill --body "ports the rule from --repo florianhorner/gh-workflows"'
[ "$(verdict "$(payload "$PROSE_CREATE")" "$DUMMY")" = deny ] \
  || fail "a repo named inside --body must not be read as the target"
pass "--repo inside prose is not a target"

# A later chained command's --repo must not exempt this one. The same shape was
# fixed for --base; one shared reader means it cannot regress in only one.
CHAINED_CREATE='gh pr create --fill && gh pr create --repo florianhorner/gh-workflows --fill'
[ "$(verdict "$(payload "$CHAINED_CREATE")" "$DUMMY")" = deny ] \
  || fail "a later segment's --repo must not exempt the first create"
pass "a later segment's --repo does not exempt this one"

# --- Regressions for the three bypasses an adversarial pass found in the first
# --- draft of Rule 1a. Each one allowed a PR that really lands in THIS repo.

# The CLI is last-wins on a repeated flag; a first-match read called this foreign
# and stood aside while the PR landed here. Verified against the real CLI.
REPEATED_REPO="gh pr create --repo florianhorner/gh-workflows --repo $THIS_REPO --fill"
[ "$(verdict "$(payload "$REPEATED_REPO")" "$DUMMY")" = deny ] \
  || fail "a repeated --repo must be read last-wins, as the CLI does"
pass "repeated --repo is read last-wins"

REPEATED_MIXED="gh pr create --repo florianhorner/gh-workflows -R $THIS_REPO --fill"
[ "$(verdict "$(payload "$REPEATED_MIXED")" "$DUMMY")" = deny ] \
  || fail "-R after --repo must win, as the CLI does"
pass "-R overriding --repo is read last-wins"

# Spellings the CLI resolves to this repo that a naive https-only normalizer
# read as foreign. Each was a silent bypass.
for form in \
  "github.com/$THIS_REPO" \
  "http://github.com/$THIS_REPO" \
  "ssh://git@github.com/$THIS_REPO.git" \
  "https://github.com/$THIS_REPO.git" \
  "git@github.com:$THIS_REPO.git"
do
  [ "$(verdict "$(payload "gh pr create --repo $form --fill")" "$DUMMY")" = deny ] \
    || fail "the --repo spelling '$form' resolves to this repo and must be judged"
done
pass "every CLI-accepted spelling of this repo is still judged"

# An opening command is exempt only if EVERY opening command in the string is
# foreign. Putting a foreign one first exempted the local one behind it, which
# the mirror ordering (already covered above) did not catch.
FOREIGN_FIRST="gh pr create --repo florianhorner/gh-workflows --fill && gh pr create --fill"
[ "$(verdict "$(payload "$FOREIGN_FIRST")" "$DUMMY")" = deny ] \
  || fail "a foreign first command must not exempt a local one behind it"
pass "a foreign first command does not exempt a local one"

FOREIGN_FIRST_SEMI="gh pr create --repo florianhorner/gh-workflows --fill ; gh pr create --fill"
[ "$(verdict "$(payload "$FOREIGN_FIRST_SEMI")" "$DUMMY")" = deny ] \
  || fail "a foreign first command must not exempt a local one after a semicolon"
pass "semicolon chaining does not exempt either"

# Two genuinely foreign commands together are still out of scope: the rule is
# "all foreign", not "more than one means judge".
BOTH_FOREIGN='gh pr create --repo florianhorner/gh-workflows --fill && gh pr create -R florianhorner/engineering-standards --fill'
[ "$(verdict "$(payload "$BOTH_FOREIGN")" "$DUMMY")" = allow ] \
  || fail "several foreign commands together are still out of scope"
pass "all-foreign chains stay out of scope"

# The attached shorthand the CLI accepts.
[ "$(verdict "$(payload 'gh pr create -Rflorianhorner/gh-workflows --fill')" "$DUMMY")" = allow ] \
  || fail "-Rowner/repo attached shorthand must be read as a target"
pass "-Rowner/repo attached shorthand is read"

# Unbalanced quotes make the tokenizer give up. That must mean "judge it", not
# "wave it through" — the exemption is the only new way out of this guard.
UNREADABLE="gh pr create --repo florianhorner/gh-workflows --title \"unterminated --fill"
[ "$(verdict "$(payload "$UNREADABLE")" "$DUMMY")" = deny ] \
  || fail "a command line the tokenizer cannot read must still be judged"
pass "an unreadable command line is still judged"

# Rule ORDERING is load-bearing. The merge deny must run before the scope skip;
# with the order swapped, appending a foreign-repo opening command to a merge
# retires the landing contract's hard stop and every other case here still passes.
ORDER_BYPASS='gh pr create --repo florianhorner/gh-workflows --fill && gh pr merge 5 --squash'
[ "$(verdict "$(payload "$ORDER_BYPASS")" "$DUMMY")" = deny ] \
  || fail "a foreign-repo create must not exempt a merge in the same command"
pass "the scope skip does not retire the merge deny"

# --base=VALUE has its own case; --repo=VALUE goes through the same branch of the
# shared reader and needs one too, or the fix silently misses the equals form.
[ "$(verdict "$(payload 'gh pr create --repo=florianhorner/gh-workflows --fill')" "$DUMMY")" = allow ] \
  || fail "--repo=VALUE must be read as the target"
pass "--repo=VALUE form is parsed"

# An unreadable origin cannot prove a mismatch, so the guard stays ON. This is
# the one branch of Rule 1a that deliberately fails toward checking rather than
# open, and it is the branch a dropped emptiness check would silently delete.
NO_ORIGIN="$TMPDIR_T/no-origin"
mkdir -p "$NO_ORIGIN"
git -C "$NO_ORIGIN" init -q .
git -C "$NO_ORIGIN" -c core.hooksPath=/dev/null -c user.email=t@t.test -c user.name=t \
  commit -q --allow-empty -m "chore: base"
NO_ORIGIN_OUT="$( (cd "$NO_ORIGIN" \
  && payload 'gh pr create --repo florianhorner/gh-workflows --fill' \
  | MMR_PRESHIP_REVIEW_READER="$DUMMY" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_OK" \
    bash "$HOOK") 2>/dev/null || true)"
printf '%s' "$NO_ORIGIN_OUT" | grep -q '"permissionDecision":"deny"' \
  || fail "an unreadable origin must leave the guard on, not switch it off"
pass "unreadable origin keeps the guard on"

# The option-value skip list is what stops prose being scanned. Only --body was
# ever exercised; an unquoted --title value goes through the same list.
TITLE_PROSE='gh pr create --fill --title --repo florianhorner/gh-workflows'
[ "$(verdict "$(payload "$TITLE_PROSE")" "$DUMMY")" = deny ] \
  || fail "a value consumed by --title must not be read as the target"
pass "--title consumes its value like --body"

# KNOWN GAP, pinned so it stays a decision rather than drift: a flagless command
# run after cd-ing into ANOTHER repository is still judged against this checkout,
# because the hook is invoked with the session cwd and sees no explicit target.
# It fails in the safe direction (a false refusal, not a false pass), and R12
# denies the flagless form fleet-wide so it is largely unreachable. Closing it
# needs the cwd resolution permission-guard.py R13/R19 already had to build.
CD_OTHER_REPO='cd /Users/florianhorner/repos/gh-workflows && gh pr create --fill'
[ "$(verdict "$(payload "$CD_OTHER_REPO")" "$DUMMY")" = deny ] \
  || fail "known gap changed: a cd-ed flagless create is no longer judged here"
pass "known gap: a cd-ed flagless create is still judged against this checkout"

# --- Regressions for three bypasses three review bots found independently.
# --- Each let the guard stand aside for a PR that really lands in THIS repo.

# A newline is a command separator. The tokenizer folded it into whitespace and
# the argv scan stopped only on ;&| , so a local flagless command absorbed the
# NEXT command's foreign target and the whole call read as out of scope.
NEWLINE_BYPASS='gh pr create --fill
gh pr create --repo florianhorner/gh-workflows --fill'
[ "$(verdict "$(payload "$NEWLINE_BYPASS")" "$DUMMY")" = deny ] \
  || fail "a newline must separate commands; a local create must not absorb the next target"
pass "a newline separates commands"

# Reversed order, same shape.
NEWLINE_BYPASS_REV='gh pr create --repo florianhorner/gh-workflows --fill
gh pr create --fill'
[ "$(verdict "$(payload "$NEWLINE_BYPASS_REV")" "$DUMMY")" = deny ] \
  || fail "a local create on a later line must still be judged"
pass "a local create on a later line is judged"

# A backslash continues the line, as the shell does, so this really is ONE
# foreign command and stays out of scope.
CONTINUED='gh pr create --repo florianhorner/gh-workflows \
  --title x --body y'
[ "$(verdict "$(payload "$CONTINUED")" "$DUMMY")" = allow ] \
  || fail "a backslash-continued foreign command is still one command"
pass "a backslash continues the line"

# Two genuinely foreign commands on separate lines are still out of scope: the
# rule is "every command is foreign", and newline handling must not break that.
BOTH_FOREIGN_LINES='gh pr create --repo florianhorner/gh-workflows --fill
gh pr create -R florianhorner/engineering-standards --fill'
[ "$(verdict "$(payload "$BOTH_FOREIGN_LINES")" "$DUMMY")" = allow ] \
  || fail "all-foreign commands on separate lines are still out of scope"
pass "all-foreign separate lines stay out of scope"

# Every value-taking flag must consume its value. Otherwise the value itself is
# read as an option: the CLI treats "--repo=..." as the label, lands the PR
# here, and the guard saw a foreign target.
for consuming in --assignee -a --label -l --milestone -m --project -p \
                 --reviewer -r --template -T --head -H --recover --base -B
do
  CONSUMED="gh pr create --fill $consuming --repo=florianhorner/gh-workflows"
  [ "$(verdict "$(payload "$CONSUMED")" "$DUMMY")" = deny ] \
    || fail "$consuming must consume its value, not leak it as the target"
done
pass "every value-taking flag consumes its value"

# Boolean flags must NOT consume the next token, or a real target goes unread.
for boolean in --draft -d --fill-first --fill-verbose --web -w --dry-run --editor -e
do
  BOOLEAN_THEN_REPO="gh pr create $boolean --repo florianhorner/gh-workflows"
  [ "$(verdict "$(payload "$BOOLEAN_THEN_REPO")" "$DUMMY")" = allow ] \
    || fail "$boolean must not swallow the following --repo"
done
pass "boolean flags do not swallow the target"

# A local clone names no owner/repo, so the origin is UNKNOWN and the guard must
# stay on. Reducing it to its last two path segments made it mismatch its own
# --repo and switched the guard off for its own repo.
LOCAL_ORIGIN="$TMPDIR_T/local-origin"
mkdir -p "$LOCAL_ORIGIN"
git -C "$LOCAL_ORIGIN" init -q .
git -C "$LOCAL_ORIGIN" -c core.hooksPath=/dev/null -c user.email=t@t.test -c user.name=t \
  commit -q --allow-empty -m "chore: base"
for local_url in "file://$TMPDIR_T/mammamiradio" "$TMPDIR_T/mammamiradio" "../mammamiradio"
do
  git -C "$LOCAL_ORIGIN" remote remove origin 2>/dev/null || true
  git -C "$LOCAL_ORIGIN" remote add origin "$local_url"
  LOCAL_OUT="$( (cd "$LOCAL_ORIGIN" \
    && payload 'gh pr create --repo florianhorner/mammamiradio --fill' \
    | MMR_PRESHIP_REVIEW_READER="$DUMMY" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_OK" \
      bash "$HOOK") 2>/dev/null || true)"
  printf '%s' "$LOCAL_OUT" | grep -q '"permissionDecision":"deny"' \
    || fail "a local origin ($local_url) names no owner/repo and must keep the guard on"
done
pass "a local or file:// origin keeps the guard on"

# --- Round 3. One bypass, one regression this branch introduced, three narrow
# --- unsafe-direction gaps. All verified against the real CLI where relevant.

# The shared option reader lives inside the hook as a single-quoted python
# program. Pull it out so `option` mode can be tested directly: its only
# consumer (base_ref) is consumed internally and is not observable from the
# hook's own output, which is exactly how the regression below went unnoticed.
extract_arg_reader() {
  awk "/python3 -c '\$/{f=1;next} f&&/^' 2>\/dev\/null\$/{exit} f" "$HOOK"
}

# pflag strips one "=" from an attached shorthand, so -R=owner/repo names that
# repo. Keeping the "=" made the value never match this checkout.
[ "$(verdict "$(payload "gh pr create -R=$THIS_REPO --fill")" "$DUMMY")" = deny ] \
  || fail "-R=<this repo> must still be judged"
pass "-R=VALUE attached shorthand is read"

[ "$(verdict "$(payload 'gh pr create -R=florianhorner/gh-workflows --fill')" "$DUMMY")" = allow ] \
  || fail "-R=<foreign> must be read as a foreign target"
pass "-R=VALUE foreign form is out of scope"

# REGRESSION this branch introduced: adding --base/-B to the skip list retired
# the --base extraction Rule 1b reads, because the skip ran before the name
# match. base_ref then fell back to main and a stacked PR was verified against
# an older fork point than its own -- a silently wider receipt window.
for spelling in "--base release/1.2" "-B release/1.2" "--base=release/1.2"
do
  got="$(printf '%s' "gh pr create $spelling --fill" \
    | ARG_MODE=option ARG_NAME=--base python3 -c "$(extract_arg_reader)" 2>/dev/null || true)"
  [ "$got" = "release/1.2" ] \
    || fail "option mode must return the --base value for '$spelling' (got '${got:-empty}')"
done
pass "--base extraction survives the skip list in all three spellings"

# An unreducible target is UNKNOWN, not foreign. Only an ABSENT target reached
# the keep-the-guard-on branch, so a present-but-unreadable one claimed to be
# somebody else's repo.
for unreadable in '"$REPO"' '"-n"' 'notarepo'
do
  [ "$(verdict "$(payload "gh pr create --repo $unreadable --fill")" "$DUMMY")" = deny ] \
    || fail "an unreducible target ($unreadable) is unknown and must keep the guard on"
done
pass "an unreducible target keeps the guard on"

# A subshell defeated all three command greps, including the landing-contract
# hard stop. Pre-existing, and the one that mattered most.
[ "$(verdict "$(payload '(gh pr merge 5 --squash)')" "$DUMMY")" = deny ] \
  || fail "a merge inside a subshell must still be denied"
pass "a subshell does not defeat the merge deny"

[ "$(verdict "$(payload '(gh api -X PUT repos/a/b/pulls/5/merge)')" "$DUMMY")" = deny ] \
  || fail "a REST merge inside a subshell must still be denied"
pass "a subshell does not defeat the REST merge deny"

[ "$(verdict "$(payload 'X=$(gh pr create --fill)')" "$DUMMY")" = deny ] \
  || fail "a create inside a command substitution must still be judged"
pass "a command substitution does not hide a create"

# --disable-auto must still disarm, and must not be smuggled in from a subshell.
[ "$(verdict "$(payload 'gh pr merge 5 --disable-auto')" "$DUMMY")" = allow ] \
  || fail "disarming must still be allowed"
pass "disarming a queued merge is still allowed"

# A backslash-newline is deleted by the shell, not turned into a space, so a
# token split across lines rejoins into one target.
SPLIT_TOKEN="gh pr create --repo florianhorner/mammamira\\
dio --fill"
[ "$(verdict "$(payload "$SPLIT_TOKEN")" "$DUMMY")" = deny ] \
  || fail "a target split across a backslash-newline must rejoin to this repo"
pass "a backslash-newline rejoins a split token"

# An origin URL with a trailing slash after .git, or an uppercase .GIT, must
# still reduce to this repo -- the strip order used to leave one of them behind
# and switch the guard off for its own repo.
for odd_origin in "https://github.com/$THIS_REPO.git/" "https://github.com/$THIS_REPO.GIT" \
                  "https://GITHUB.com/$THIS_REPO"
do
  git -C "$LOCAL_ORIGIN" remote remove origin 2>/dev/null || true
  git -C "$LOCAL_ORIGIN" remote add origin "$odd_origin"
  ODD_OUT="$( (cd "$LOCAL_ORIGIN" \
    && payload "gh pr create --repo $THIS_REPO --fill" \
    | MMR_PRESHIP_REVIEW_READER="$DUMMY" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_OK" \
      bash "$HOOK") 2>/dev/null || true)"
  printf '%s' "$ODD_OUT" | grep -q '"permissionDecision":"deny"' \
    || fail "origin '$odd_origin' names this repo and must keep the guard on"
done
pass "odd but hosted origin spellings still name this repo"

# A bare relative path names no host, so it is not a repository identity.
git -C "$LOCAL_ORIGIN" remote remove origin 2>/dev/null || true
git -C "$LOCAL_ORIGIN" remote add origin "repos/mammamiradio"
REL_OUT="$( (cd "$LOCAL_ORIGIN" \
  && payload "gh pr create --repo $THIS_REPO --fill" \
  | MMR_PRESHIP_REVIEW_READER="$DUMMY" MMR_PRESHIP_EVIDENCE_CHECKER="$EVIDENCE_OK" \
    bash "$HOOK") 2>/dev/null || true)"
printf '%s' "$REL_OUT" | grep -q '"permissionDecision":"deny"' \
  || fail "a bare relative origin path names no repository and must keep the guard on"
pass "a bare relative origin keeps the guard on"

# The total is computed, not typed: a hand-maintained count drifted to 47 against
# 44 real cases on the first pass, and a summary nobody can verify is decoration.
CASE_COUNT="$(grep -c '^pass ' "$0")"
echo
echo "All $CASE_COUNT pre-ship squad gate cases passed."
