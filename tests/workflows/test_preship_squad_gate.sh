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

# The total is computed, not typed: a hand-maintained count drifted to 47 against
# 44 real cases on the first pass, and a summary nobody can verify is decoration.
CASE_COUNT="$(grep -c '^pass ' "$0")"
echo
echo "All $CASE_COUNT pre-ship squad gate cases passed."
