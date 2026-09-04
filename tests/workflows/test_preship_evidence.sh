#!/usr/bin/env bash
# Self-test for scripts/check-preship-evidence.sh and scripts/emit-review-evidence.sh
#
# Builds a throwaway git repo and asserts the retired legacy v1 form is a loud no-op,
# while every current verb (default emit, the --v2 compatibility alias, --reattest, and
# --v2 verification) dispatches into the trusted Python package. Deep receipt semantics
# live in tests/repo/test_preship_evidence_v2.py; this file guards the shell adapters
# exactly as CI and operators invoke them. No network, no gh CLI, no real ~/.gstack.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHECK="$REPO_ROOT/scripts/check-preship-evidence.sh"
EMIT="$REPO_ROOT/scripts/emit-review-evidence.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[ -f "$CHECK" ] || fail "checker not found at $CHECK"
[ -f "$EMIT" ] || fail "emitter not found at $EMIT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Structural guard: GSTACK_HOME is exported file-wide to a nonexistent decoy path, so
# any invocation that forgets a per-call override dies on the missing-ledger guard
# instead of silently walking the real ~/.gstack on a developer machine.
export GSTACK_HOME="$TMP/gstack-DECOY-must-not-be-read"

PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
export MAMMAMIRADIO_PYTHON="$PYTHON_BIN"
export PYTHONPATH="$REPO_ROOT"

# --- fixture repo: two commits so an ancestor base is real -----------------------------
FIX="$TMP/repo"
mkdir -p "$FIX"
git -C "$FIX" init -q -b main
git -C "$FIX" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m one
git -C "$FIX" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m two
git -C "$FIX" remote add origin https://github.com/florianhorner/mammamiradio.git
HEAD_SHA="$(git -C "$FIX" rev-parse HEAD)"
ANC_SHA="$(git -C "$FIX" rev-parse HEAD~1)"
# A resolvable origin/main so reattest's base-trust check (base must be landed
# content) is satisfied and the assertions below reach the code paths they target.
git -C "$FIX" update-ref refs/remotes/origin/main "$HEAD_SHA"

# 1. Legacy positional v1 invocation is retired: loud notice, exit 0, no artifact read.
#    Old branches' report-only workflows still call this form against the base checker,
#    so it must never fail and must say where the real evidence lives.
if ! out="$(cd "$FIX" && bash "$CHECK" proof/preship-review.json "$HEAD_SHA" 2>&1)"; then
  fail "legacy positional form must exit 0"
fi
printf '%s' "$out" | grep -q "v1 evidence is retired" || fail "legacy form must announce the retirement"
printf '%s' "$out" | grep -q "proof/preship-reviews/v2" || fail "legacy form must point at the v2 receipts"
pass "legacy positional v1 form is a loud, pointed no-op"

# 1b. The no-op holds with no arguments and with an artifact-shaped file present.
mkdir -p "$FIX/proof"
printf 'not json{' > "$FIX/proof/preship-review.json"
(cd "$FIX" && bash "$CHECK" >/dev/null 2>&1) || fail "legacy no-arg form must exit 0"
(cd "$FIX" && bash "$CHECK" proof/preship-review.json HEAD >/dev/null 2>&1) \
  || fail "legacy form must not parse or validate a leftover artifact"
rm -f "$FIX/proof/preship-review.json"
pass "legacy form ignores leftover artifacts instead of validating them"

# 2. Default emit dispatches to the Python v2 emitter (missing-ledger guard proves it).
if (cd "$FIX" && bash "$EMIT" 2>"$TMP/err"); then
  fail "default emit must refuse without a review ledger"
fi
grep -q "no exact review ledger directory" "$TMP/err" \
  || fail "default emit did not dispatch to the Python v2 emitter"
pass "default emit dispatches to the v2 emitter"

# 2b. The --v2 compatibility alias reaches the same emitter.
if (cd "$FIX" && bash "$EMIT" --v2 2>"$TMP/err"); then
  fail "--v2 emit must refuse without a review ledger"
fi
grep -q "no exact review ledger directory" "$TMP/err" \
  || fail "--v2 alias did not dispatch to the Python v2 emitter"
pass "--v2 compatibility alias still dispatches"

# 3. --reattest dispatches and fails closed when there is no receipt to derive from.
if (cd "$FIX" && bash "$EMIT" --reattest --base "$ANC_SHA" 2>"$TMP/err"); then
  fail "--reattest must refuse without an existing receipt"
fi
grep -q "no v2 receipt exists on this branch to derive from" "$TMP/err" \
  || fail "--reattest did not dispatch to the Python reattest path"
pass "--reattest dispatches and fails closed without a source receipt"

# 3b. --reattest demands an integrated base before deriving anything.
git -C "$FIX" checkout -q -b side "$ANC_SHA"
git -C "$FIX" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m side
SIDE_SHA="$(git -C "$FIX" rev-parse HEAD)"
git -C "$FIX" checkout -q main
if (cd "$FIX" && bash "$EMIT" --reattest --base "$SIDE_SHA" 2>"$TMP/err"); then
  fail "--reattest must refuse a base that is not an ancestor of HEAD"
fi
grep -q "integrate the base first" "$TMP/err" \
  || fail "--reattest non-ancestor base message wrong"
pass "--reattest refuses an unintegrated base"

# 4. --v2 verification dispatches into the trusted package (PR receipt gate proves it).
if (cd "$FIX" && bash "$CHECK" --v2 --target "$HEAD_SHA" --base "$HEAD_SHA" --mode pr 2>"$TMP/err"); then
  fail "v2 checker must reject a PR with no v2 receipt"
fi
grep -q "PR adds no new v2 review receipt" "$TMP/err" \
  || fail "--v2 checker did not dispatch to the Python receipt gate"
pass "--v2 verification dispatches to the Python receipt gate"

# 5. Full operator ceremony through the real wrappers on a scratch repo: review
#    ledger -> emit -> commit -> advance+integrate base -> reattest (documented
#    no --base form) -> commit swap -> verify pr-mode. Guards the happy path the
#    failure-only cases above never reach.
CEREMONY="$TMP/ceremony"
mkdir -p "$CEREMONY"
git -C "$CEREMONY" init -q -b main
git -C "$CEREMONY" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q --allow-empty -m seed
git -C "$CEREMONY" remote add origin https://github.com/florianhorner/mammamiradio.git
FORK="$(git -C "$CEREMONY" rev-parse HEAD)"
git -C "$CEREMONY" update-ref refs/remotes/origin/main "$FORK"
git -C "$CEREMONY" checkout -q -b feature
printf 'feature\n' > "$CEREMONY/feature.txt"
git -C "$CEREMONY" add feature.txt
git -C "$CEREMONY" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q -m "feature"
REVIEWED="$(git -C "$CEREMONY" rev-parse HEAD)"
CLEDGER="$TMP/ceremony-gstack/projects/florianhorner-mammamiradio"
mkdir -p "$CLEDGER"
printf '{"skill":"review","timestamp":"2026-08-27T00:00:00Z","status":"clean","findings":[],"commit":"%s"}\n' \
  "$REVIEWED" > "$CLEDGER/branch-reviews.jsonl"
( cd "$CEREMONY" && GSTACK_HOME="$TMP/ceremony-gstack" bash "$EMIT" >/dev/null ) \
  || fail "ceremony: emit must succeed for reviewed content"
git -C "$CEREMONY" add proof
git -C "$CEREMONY" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q -m "receipt"
# Advance real main and integrate it.
git -C "$CEREMONY" checkout -q main
printf 'mainwork\n' > "$CEREMONY/mainwork.txt"
git -C "$CEREMONY" add mainwork.txt
git -C "$CEREMONY" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q -m "main advances"
NEWBASE="$(git -C "$CEREMONY" rev-parse HEAD)"
git -C "$CEREMONY" update-ref refs/remotes/origin/main "$NEWBASE"
git -C "$CEREMONY" checkout -q feature
git -C "$CEREMONY" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null merge -q --no-edit "$NEWBASE"
# Reattest via the documented no-argument form (defaults --base origin/main).
( cd "$CEREMONY" && bash "$EMIT" --reattest > "$TMP/reattest.out" ) \
  || fail "ceremony: reattest must succeed after a clean base integration"
grep -q "wrote proof/preship-reviews/v2/" "$TMP/reattest.out" || fail "ceremony: reattest did not write a receipt"
grep -q "superseded proof/preship-reviews/v2/" "$TMP/reattest.out" || fail "ceremony: reattest did not retire the stale receipt"
git -C "$CEREMONY" add -A
git -C "$CEREMONY" -c user.name=t -c user.email=t@t -c core.hooksPath=/dev/null commit -q -m "reattested"
( cd "$CEREMONY" && bash "$CHECK" --v2 --target HEAD --base "$NEWBASE" --mode pr >/dev/null ) \
  || fail "ceremony: pr-mode verification must accept the reattested branch"
pass "full emit -> integrate -> reattest -> verify ceremony succeeds through the wrappers"

echo "test_preship_evidence: all assertions passed"
