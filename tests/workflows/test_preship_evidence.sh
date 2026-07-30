#!/usr/bin/env bash
# Self-test for scripts/check-preship-evidence.sh and scripts/emit-review-evidence.sh
#
# Builds a throwaway git repo (two commits, so ancestor cases are real) and a mock gstack
# ledger (via GSTACK_HOME), then asserts the checker rejects every bad-evidence shape and
# both tools agree on the happy path. No network, no gh CLI, no real ~/.gstack.
#
# The checker is exercised exactly as CI runs it: from the repo root, with an explicit
# target head. Exits non-zero on any mismatch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHECK="$REPO_ROOT/scripts/check-preship-evidence.sh"
EMIT="$REPO_ROOT/scripts/emit-review-evidence.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

command -v jq >/dev/null 2>&1 || fail "jq is required"
[ -f "$CHECK" ] || fail "checker not found at $CHECK"
[ -f "$EMIT" ] || fail "emitter not found at $EMIT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- fixture repo: two commits so HEAD~1 is a real ancestor -----------------------------
FIX="$TMP/repo"
mkdir -p "$FIX"
git -C "$FIX" init -q -b main
git -C "$FIX" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m one
git -C "$FIX" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m two
HEAD_SHA="$(git -C "$FIX" rev-parse --short=7 HEAD)"
ANC_SHA="$(git -C "$FIX" rev-parse --short=7 HEAD~1)"

evidence() { # <commit> [skill] — write a syntactically valid artifact into the fixture
  mkdir -p "$FIX/proof"
  jq -cn --arg c "$1" --arg s "${2:-review}" \
    '{schema_version:"1.0.0",skill:$s,commit:$c,timestamp:"2026-07-30T12:00:00Z",status:"clean",emitted_at:"2026-07-30T12:00:01Z"}' \
    > "$FIX/proof/preship-review.json"
}

run_check() { (cd "$FIX" && bash "$CHECK" proof/preship-review.json HEAD); }

# 1. Missing artifact fails with the emit instruction.
rm -rf "$FIX/proof"
if run_check 2>"$TMP/err"; then fail "missing artifact must fail"; fi
grep -q "no evidence artifact" "$TMP/err" || fail "missing-artifact message wrong"
pass "missing artifact rejected"

# 2. Malformed JSON fails.
mkdir -p "$FIX/proof"
printf 'not json{' > "$FIX/proof/preship-review.json"
if run_check 2>"$TMP/err"; then fail "malformed JSON must fail"; fi
grep -q "not valid JSON" "$TMP/err" || fail "malformed-JSON message wrong"
pass "malformed JSON rejected"

# 3. Wrong skill fails — a design-review-lite entry is not squad evidence.
evidence "$HEAD_SHA" "design-review-lite"
if run_check 2>"$TMP/err"; then fail "non-squad skill must fail"; fi
grep -q "not a squad review" "$TMP/err" || fail "wrong-skill message wrong"
pass "non-squad skill rejected"

# 4. Sentinel commit fails — the squad must run on committed work.
evidence "uncommitted"
if run_check 2>"$TMP/err"; then fail "sentinel commit must fail"; fi
grep -q "no usable commit" "$TMP/err" || fail "sentinel-commit message wrong"
pass "sentinel commit rejected"

# 5. Unknown commit fails (not in history).
evidence "0000000"
if run_check 2>"$TMP/err"; then fail "unknown commit must fail"; fi
grep -q "not in this repository" "$TMP/err" || fail "unknown-commit message wrong"
pass "unknown commit rejected"

# 6. Non-ancestor commit fails: a commit on a side branch that HEAD does not contain.
git -C "$FIX" checkout -q -b side "$ANC_SHA"
git -C "$FIX" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m side
SIDE_SHA="$(git -C "$FIX" rev-parse --short=7 HEAD)"
git -C "$FIX" checkout -q main
evidence "$SIDE_SHA"
if run_check 2>"$TMP/err"; then fail "non-ancestor commit must fail"; fi
grep -q "not an ancestor" "$TMP/err" || fail "non-ancestor message wrong"
pass "non-ancestor commit rejected"

# 7. Happy path: evidence at HEAD passes.
evidence "$HEAD_SHA"
run_check >/dev/null || fail "evidence at HEAD must pass"
pass "evidence at HEAD accepted"

# 8. Ancestor path: evidence from a prior commit on the branch passes (this is what
#    survives a rebase/amend — the reviewed state is contained in the new head).
evidence "$ANC_SHA"
run_check >/dev/null || fail "evidence at an ancestor must pass"
pass "evidence at an ancestor accepted"

# 8b. Symbolic rev in the commit field is rejected. `HEAD` resolves fresh on EVERY pr
#     head, so a symbolic artifact would pass forever — the forgery the hex guard kills.
evidence "HEAD"
if run_check 2>"$TMP/err"; then fail "symbolic rev commit must fail"; fi
grep -q "not a commit SHA" "$TMP/err" || fail "symbolic-rev message wrong"
pass "symbolic rev (HEAD) rejected — hex object ids only"

# 8c. adversarial-review is the OTHER accepted skill; prove acceptance, not just the
#     rejection of a bad skill.
evidence "$HEAD_SHA" "adversarial-review"
run_check >/dev/null || fail "adversarial-review evidence must pass"
pass "adversarial-review skill accepted"

# 8d. Empty target must fail loud, not silently mis-scope to HEAD.
evidence "$HEAD_SHA"
if (cd "$FIX" && bash "$CHECK" proof/preship-review.json "" 2>"$TMP/err"); then
  fail "empty target must fail"
fi
grep -q "empty target head" "$TMP/err" || fail "empty-target message wrong"
pass "empty target head rejected"

# --- emitter, against a mock ledger -----------------------------------------------------
# The fixture repo has no origin remote, so the emitter scopes by toplevel dirname:
# "$FIX" is .../repo, so qualifying ledger dirs are exactly `repo` and `*-repo`.
#
# Structural guard (test-audit finding): GSTACK_HOME is exported file-wide to a
# nonexistent decoy path, so any future emitter invocation that forgets the per-call
# override dies on "no gstack ledger found" instead of silently walking the real
# ~/.gstack on a developer machine.
export GSTACK_HOME="$TMP/gstack-DECOY-must-not-be-read"
LEDGER="$TMP/gstack/projects/owner-repo"
OTHER="$TMP/gstack/projects/some-other-project"
mkdir -p "$LEDGER" "$OTHER"

# 9. Emitter refuses when the ledger root is missing entirely (the most likely real-world
#    path: a machine or CI runner with no ~/.gstack at all).
if (cd "$FIX" && GSTACK_HOME="$TMP/nonexistent" bash "$EMIT" 2>"$TMP/err"); then
  fail "emitter must refuse when the ledger root is missing"
fi
grep -q "no gstack ledger found" "$TMP/err" || fail "missing-ledger-root message wrong"
pass "missing ledger root rejected with problem/cause/fix"

# 9b. Emitter refuses when no qualifying entry exists (wrong skill + sentinel commit only).
jq -cn '{skill:"ship",commit:"'"$HEAD_SHA"'"}' > "$LEDGER/b-reviews.jsonl"
jq -cn '{skill:"review",commit:"uncommitted"}' >> "$LEDGER/b-reviews.jsonl"
if (cd "$FIX" && GSTACK_HOME="$TMP/gstack" bash "$EMIT" 2>"$TMP/err"); then
  fail "emitter must refuse with no qualifying entry"
fi
grep -q "no review evidence in the ledger" "$TMP/err" || fail "emitter no-entry message wrong"
pass "emitter refuses without a qualifying ledger entry"

# 10. Cross-project scoping (adversarial P1): a qualifying entry in ANOTHER project's
#     ledger must NOT satisfy this repo's gate, even though its commit is a real
#     ancestor here. Before scoping, this exact shape produced false green evidence.
LONG_HEAD="$(git -C "$FIX" rev-parse HEAD)"
jq -cn --arg c "$LONG_HEAD" \
  '{skill:"review",commit:$c,timestamp:"2026-07-30T12:00:00Z",status:"clean"}' \
  > "$OTHER/b-reviews.jsonl"
rm -f "$FIX/proof/preship-review.json"
if (cd "$FIX" && GSTACK_HOME="$TMP/gstack" bash "$EMIT" 2>"$TMP/err"); then
  fail "another project's review must not satisfy this repo's gate"
fi
[ ! -f "$FIX/proof/preship-review.json" ] || fail "cross-project entry must not produce an artifact"
pass "cross-project ledger entries are ignored (scoped to *-repo)"

# 10b. Emitter picks the NEWEST qualifying entry (both are ancestors — sort order is the
#      only thing deciding), resolves to the full 40-char SHA, skips non-ancestors, and
#      the checker accepts its output end-to-end.
LONG_ANC="$(git -C "$FIX" rev-parse HEAD~1)"
{
  jq -cn --arg c "$LONG_HEAD" \
    '{skill:"review",commit:$c,timestamp:"2026-07-30T11:00:00Z",status:"clean"}'
  # older — must lose to the HEAD entry:
  jq -cn --arg c "$LONG_ANC" \
    '{skill:"review",commit:$c,timestamp:"2026-07-30T10:00:00Z",status:"clean"}'
  # newest but NOT an ancestor of main — must be skipped:
  jq -cn --arg c "$SIDE_SHA" \
    '{skill:"review",commit:$c,timestamp:"2026-07-30T13:00:00Z",status:"clean"}'
} >> "$LEDGER/b-reviews.jsonl"
rm -f "$FIX/proof/preship-review.json"
(cd "$FIX" && GSTACK_HOME="$TMP/gstack" bash "$EMIT" >/dev/null) || fail "emitter happy path failed"
got="$(jq -r '.commit' "$FIX/proof/preship-review.json")"
[ "$got" = "$LONG_HEAD" ] || fail "emitter must pick the newest ancestor and pin the FULL resolved SHA (got '$got', want '$LONG_HEAD')"
run_check >/dev/null || fail "checker must accept the emitter's own output"
pass "newest qualifying entry wins, full SHA pinned, non-ancestors skipped, checker round-trip"

# 11. Corrupt ledger lines — single-line torn JSON AND the legacy multi-line
#     pretty-printed shape (retro/docs/upstream-drafts/01) — are skipped, not fatal,
#     and do not change which commit is selected.
printf '{"torn": tru\n' >> "$LEDGER/b-reviews.jsonl"
printf '{\n  "skill": "review",\n  "commit": "%s",\n  "timestamp": "2026-07-30T23:00:00Z"\n}\n' \
  "$LONG_ANC" >> "$LEDGER/b-reviews.jsonl"
rm -f "$FIX/proof/preship-review.json"
(cd "$FIX" && GSTACK_HOME="$TMP/gstack" bash "$EMIT" >/dev/null) || fail "corrupt ledger lines must not be fatal"
got="$(jq -r '.commit' "$FIX/proof/preship-review.json")"
[ "$got" = "$LONG_HEAD" ] || fail "corrupt lines must not change the selected commit (got '$got')"
pass "torn and pretty-printed ledger lines skipped without changing selection"

echo "test_preship_evidence: all assertions passed"
