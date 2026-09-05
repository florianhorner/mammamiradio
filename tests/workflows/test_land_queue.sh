#!/usr/bin/env bash
# Self-test for scripts/land-queue-plan.sh and scripts/edge-select.sh.
#
# Hermetic: PATH-shimmed `gh` (every subcommand the planner uses) and a `git`
# shim that forwards read-only verbs to real git and REFUSES every mutating one,
# so a future write in the shadow planner fails the test instead of touching the
# repo. Evidence verification is stubbed through MMR_LAND_EVIDENCE_CHECKER. No
# network. Exits non-zero on any mismatch.
#
# What these cases exist to hold:
#   - shadow mode never writes (the whole premise of phase 1)
#   - fail-closed classification: an unverifiable gate is BLOCKED, never READY
#   - single-flight: exactly one decision per tick, never two
#   - FIFO fairness: the key cannot drift to a field that a bounce mutates
#   - bot lanes stay exempt (a dependabot PR at the head must not stall the queue)
#   - edge eligibility: green / drift / no-build, and never a soft pass

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLAN="$REPO_ROOT/scripts/land-queue-plan.sh"
EDGE_LIB="$REPO_ROOT/scripts/edge-select.sh"
cd "$REPO_ROOT"

[[ -x "$PLAN" ]] || chmod +x "$PLAN"

PASS_COUNT=0
fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT
BIN="$TMPDIR_T/bin"
mkdir -p "$BIN"

HEAD_FULL="$(git rev-parse HEAD)"
ANC_FULL="$(git rev-parse HEAD~1 2>/dev/null)" \
  || fail "HEAD~1 unavailable (shallow clone?) — checkout with fetch-depth >= 2"
REAL_GIT="$(command -v git)"

# The edge cases below walk real history. A PR checkout may or may not carry an
# origin/main remote-tracking ref, so resolve one that exists rather than
# assuming; the eligibility logic is ref-agnostic.
if git rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  MAIN_REF="origin/main"
else
  MAIN_REF="HEAD"
fi

# review_threads_json is a TWO-phase reader: a thread list (which requires an
# `id` per node) and then a per-thread comments query. A fixture without `id`
# makes the reader bail, and thread_check then reports BLOCKED_BOT from its
# fail-closed branch -- so the severity filter is never reached and deleting
# REVIEW_THREADS_BLOCKING_JQ entirely would keep the test green.
EMPTY_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}}'
EMPTY_COMMENTS='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[]}}}}'
ONE_THREAD='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"id":"T1","isResolved":false,"isOutdated":false}]}}}}}'
MAJOR_COMMENTS='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"author":{"login":"coderabbitai"},"body":"Major: this drops the queue","url":"https://example.test/comment/1"}]}}}}'
MINOR_COMMENTS='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"author":{"login":"coderabbitai"},"body":"P2: a nit","url":"https://example.test/comment/2"}]}}}}'

# ---- mock gh ----------------------------------------------------------------
# Env: GH_MOCK_PRS (pr list JSON), GH_MOCK_THREADS (graphql body),
#      GH_MOCK_RUN_SHAS (run list output), GH_MOCK_PR_LIST_FAIL, GH_MOCK_RUN_FAIL.
# Every invocation is appended to GH_MOCK_LOG so mutating calls are provable.
cat > "$BIN/gh" <<'GHEOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_MOCK_LOG"
case "$1 $2" in
  "pr list")
    [ "${GH_MOCK_PR_LIST_FAIL:-0}" = "1" ] && exit 1
    printf '%s' "${GH_MOCK_PRS:-[]}"
    exit 0 ;;
  "repo view")
    printf '%s' "florianhorner/mammamiradio"
    exit 0 ;;
  "pr view")
    printf '%s' "${GH_MOCK_LAST_PUSH:-2026-01-01T00:00:00Z}"
    exit 0 ;;
  "run list")
    [ "${GH_MOCK_RUN_FAIL:-0}" = "1" ] && exit 1
    printf '%s' "${GH_MOCK_RUN_SHAS:-}"
    exit 0 ;;
  "api graphql")
    if [[ "$*" == *"PullRequestReviewThread"* ]]; then
      printf '%s' "${GH_MOCK_COMMENTS:-}"
    else
      printf '%s' "${GH_MOCK_THREADS:-}"
    fi
    exit 0 ;;
esac
echo "gh mock: unhandled invocation: $*" >&2
exit 64
GHEOF
chmod +x "$BIN/gh"

# ---- mock git ---------------------------------------------------------------
# Read-only verbs pass through to real git. Every mutating verb HARD-FAILS, so
# the shadow planner cannot quietly grow a write.
cat > "$BIN/git" <<GITEOF
#!/usr/bin/env bash
case "\$1" in
  rev-parse|rev-list|cat-file|show|merge-base|diff|status|log|for-each-ref|worktree|symbolic-ref|show-ref|ls-tree|config)
    exec "$REAL_GIT" "\$@" ;;
  fetch)
    # A shadow run may want fresh refs, but this test must stay offline.
    exit 0 ;;
esac
echo "git mock: refusing mutating verb in a read-only shadow: \$*" >&2
exit 65
GITEOF
chmod +x "$BIN/git"

# ---- mock evidence checker --------------------------------------------------
cat > "$TMPDIR_T/evidence-ok.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$TMPDIR_T/evidence-missing.sh" <<'EOF'
#!/usr/bin/env bash
echo "no v2 receipt binds this content"
exit 1
EOF
chmod +x "$TMPDIR_T/evidence-ok.sh" "$TMPDIR_T/evidence-missing.sh"

pr_row() { # number title head merge draft labels created author is_bot [branch]
  local number="$1" title="$2" head="$3" merge="$4" draft="$5" labels="$6" created="$7" author="$8" is_bot="$9"
  local branch="${10:-feature/pr-$1}"
  # committedDate feeds the ledger age check; default is deliberately old so a
  # test that cares about staleness sets it explicitly.
  jq -cn --argjson number "$number" --arg title "$title" --arg head "$head" \
    --arg merge "$merge" --argjson draft "$draft" --argjson labels "$labels" \
    --arg created "$created" --arg author "$author" --argjson is_bot "$is_bot" \
    --arg branch "$branch" --arg pushed "${11:-2026-01-01T00:00:00Z}" \
    '{number:$number,title:$title,headRefOid:$head,baseRefOid:"'"$ANC_FULL"'",
      mergeStateStatus:$merge,isDraft:$draft,labels:$labels,createdAt:$created,
      headRefName:$branch,commits:[{committedDate:$pushed}],
      url:("https://example.test/pull/" + ($number|tostring)),
      author:{login:$author,is_bot:$is_bot}}'
}

ALL_GH_LOG="$TMPDIR_T/gh-all.log"
: > "$ALL_GH_LOG"

run_plan() { # prs-json [extra env assignments handled by caller]
  GH_MOCK_LOG="$TMPDIR_T/gh.log"
  [ -f "$GH_MOCK_LOG" ] && cat "$GH_MOCK_LOG" >> "$ALL_GH_LOG"
  : > "$GH_MOCK_LOG"
  GH_MOCK_PRS="$1" \
  GH_MOCK_LOG="$GH_MOCK_LOG" \
  GH_MOCK_THREADS="${THREADS:-$EMPTY_THREADS}" \
  GH_MOCK_COMMENTS="${COMMENTS:-$EMPTY_COMMENTS}" \
  GH_MOCK_RUN_SHAS="${RUN_SHAS:-}" \
  GH_MOCK_RUN_FAIL="${RUN_FAIL:-0}" \
  GH_MOCK_PR_LIST_FAIL="${PR_LIST_FAIL:-0}" \
  MMR_LAND_EVIDENCE_CHECKER="${EVIDENCE:-$TMPDIR_T/evidence-ok.sh}" \
  MMR_LAND_REVIEW_READER="/nonexistent" \
  GH_REPO="florianhorner/mammamiradio" \
  PATH="$BIN:$PATH" \
    bash "$PLAN" --json
}

# =============================================================================
# Case 1: a clean feature PR at the head is the one decision, and it is ARM.
# =============================================================================
PRS="[$(pr_row 10 "fix: a thing" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "arm" ] || fail "clean queue head should arm"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "10" ] || fail "wrong PR chosen"
[ "$(jq -r '.mode' <<<"$OUT")" = "shadow" ] || fail "planner must report shadow mode"
pass "clean feature PR at the head is the single ARM decision"

# =============================================================================
# Case 2: shadow mode writes nothing — no merge, comment, edit, or REST mutation.
# =============================================================================
grep -Eq 'pr (merge|comment|edit|review|close)|api -X|--method (POST|PUT|PATCH|DELETE)' "$TMPDIR_T/gh.log" \
  && fail "shadow planner must not issue any mutating gh call"
pass "shadow run issues no mutating gh call"

# =============================================================================
# Case 3: single-flight — two READY PRs yield exactly ONE decision, the older.
# =============================================================================
PRS="[$(pr_row 20 "fix: older" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false),
      $(pr_row 21 "fix: newer" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "20" ] || fail "FIFO must pick the older PR"
[ "$(jq '.decision | type' <<<"$OUT")" = '"object"' ] || fail "decision must be a single object"
[ "$(jq '[.prs[] | select(.state == "READY")] | length' <<<"$OUT")" = "2" ] \
  || fail "both PRs should classify READY even though only one is acted on"
pass "two READY PRs produce exactly one decision, the FIFO-older"

# =============================================================================
# Case 4: FIFO key is createdAt — a field a bounce cannot move. A PR that was
# updated most recently must NOT jump the queue (plan section 6.3 fairness).
# =============================================================================
jq -e '.prs[0].fifo_key == "2026-01-01T00:00:00Z"' <<<"$OUT" >/dev/null \
  || fail "fifo_key must be the PR creation time"
grep -q 'sort_by(.createdAt)' "$PLAN" || fail "FIFO must sort on createdAt, not a mutable field"
grep -q 'sort_by(.updatedAt)' "$PLAN" && fail "updatedAt would reorder the queue on every bounce"
pass "FIFO key is the immutable creation time, not updatedAt"

# =============================================================================
# Case 5: BEHIND head routes to INTEGRATE, not ARM.
# =============================================================================
PRS="[$(pr_row 30 "fix: behind" "$HEAD_FULL" BEHIND false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "integrate" ] || fail "BEHIND head must integrate, not arm"
jq -e '.decision.why | test("reattest")' <<<"$OUT" >/dev/null \
  || fail "integrate decision must name the reattest step"
pass "BEHIND head routes to integrate + reattest"

# =============================================================================
# Case 6: DIRTY is BLOCKED_CONFLICT and the queue STALLS (does not reorder).
# =============================================================================
PRS="[$(pr_row 40 "fix: conflicted" "$HEAD_FULL" DIRTY false '[]' "2026-01-01T00:00:00Z" florianhorner false),
      $(pr_row 41 "fix: fine" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_CONFLICT" ] || fail "DIRTY must be BLOCKED_CONFLICT"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "none" ] \
  || fail "a blocked head must stall the queue, not let the next PR jump it"
pass "conflicted head is BLOCKED_CONFLICT and stalls the queue"

# =============================================================================
# Case 7: skip-queue drops a PR out of head contention so a stall is survivable.
# =============================================================================
PRS="[$(pr_row 50 "fix: conflicted" "$HEAD_FULL" DIRTY false '[{"name":"skip-queue"}]' "2026-01-01T00:00:00Z" florianhorner false),
      $(pr_row 51 "fix: fine" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "SKIPPED" ] || fail "skip-queue label must yield SKIPPED"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "51" ] || fail "queue must move past a skip-queue PR"
pass "skip-queue label releases the stall without reordering the rest"

# =============================================================================
# Case 8: an unresolved Major bot thread blocks — never READY (invariant I6).
# =============================================================================
PRS="[$(pr_row 60 "fix: bot debt" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(THREADS="$ONE_THREAD" COMMENTS="$MAJOR_COMMENTS" run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_BOT" ] || fail "Major thread must block"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "none" ] || fail "must not arm over a Major thread"
# Assert the REASON, not just the state: BLOCKED_BOT is reachable from a
# thread-query failure too, and asserting the enum alone let a fixture that
# never reached the severity filter pass this case.
jq -e '.prs[0].reason | test("unresolved Major/Critical")' <<<"$OUT" >/dev/null \
  || fail "BLOCKED_BOT must come from the severity filter, not the fail-closed branch"
jq -e '.prs[0].reason | test("example.test/comment/1")' <<<"$OUT" >/dev/null \
  || fail "the blocking comment URL must reach the operator"
pass "unresolved Major bot thread blocks via the severity filter (not fail-closed)"

# The negative twin: low-severity debt must NOT block, or the filter is just
# "any unresolved thread" wearing a severity label.
OUT="$(THREADS="$ONE_THREAD" COMMENTS="$MINOR_COMMENTS" run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "READY" ] \
  || fail "a P2 comment on an unresolved thread must not block landing"
pass "low-severity bot debt does not block"

# =============================================================================
# Case 9: missing v2 evidence blocks — never READY (invariant I7).
# =============================================================================
PRS="[$(pr_row 70 "fix: no receipt" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(EVIDENCE="$TMPDIR_T/evidence-missing.sh" run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_EVIDENCE" ] || fail "missing evidence must block"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "none" ] || fail "must not arm without evidence"
pass "missing v2 evidence blocks and never reaches READY"

# =============================================================================
# Case 10: bot lanes are exempt. A dependabot PR at the FIFO head must not stall
# the feature queue — the `gh` CLI spells it "app/dependabot", not the webhook's
# "dependabot[bot]", and reading the wrong one stalled the real queue once.
# =============================================================================
PRS="[$(pr_row 80 "chore(deps): bump x" "$HEAD_FULL" DIRTY false '[]' "2026-01-01T00:00:00Z" "app/dependabot" true),
      $(pr_row 81 "fix: real work" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].lane' <<<"$OUT")" = "dependabot" ] || fail "app/dependabot must map to the dependabot lane"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "EXEMPT" ] || fail "dependabot PRs must be exempt"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "81" ] || fail "a dependabot PR must not stall the feature queue"
pass "dependabot lane is exempt and cannot stall the feature queue"

# =============================================================================
# Case 11: edge PRs never enter the feature FIFO (plan Q4/C).
# =============================================================================
PRS="[$(pr_row 90 "chore(edge): cut edge release abc1234" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false "edge-release/abc1234"),
      $(pr_row 91 "fix: real work" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].lane' <<<"$OUT")" = "edge" ] || fail "edge-release/* branches belong to the edge lane"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "91" ] || fail "edge PR must not occupy the feature queue head"
pass "edge PRs stay out of the feature FIFO"

# The lane must key on what the automation emits, not on free text a human can
# retype: an edited title must not drop an edge PR into the gated feature lane.
PRS="[$(pr_row 92 "cut edge (retitled by hand)" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false "edge-release/abc1234")]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].lane' <<<"$OUT")" = "edge" ] || fail "lane must follow the branch, not the PR title"
pass "edge lane survives a retitled PR (keyed on branch, not title)"

# =============================================================================
# Case 12: hold / manual-land is the operator's stop, and it stalls the head.
# =============================================================================
PRS="[$(pr_row 100 "fix: held" "$HEAD_FULL" CLEAN false '[{"name":"hold"}]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_YOU" ] || fail "hold label must yield BLOCKED_YOU"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "none" ] || fail "must not act on a held PR"
pass "hold label stops the PR and the queue"

# =============================================================================
# Case 13: merge-state routing (plan Q7). UNSTABLE is landable, BLOCKED is not,
# and an unrecognised state is never treated as landable.
# =============================================================================
for pair in "UNSTABLE:READY" "BLOCKED:CI_PENDING" "UNKNOWN:CI_PENDING" "HAS_HOOKS:READY"; do
  ms="${pair%%:*}"; want="${pair##*:}"
  PRS="[$(pr_row 110 "fix: state $ms" "$HEAD_FULL" "$ms" false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
  OUT="$(run_plan "$PRS")"
  got="$(jq -r '.prs[0].state' <<<"$OUT")"
  [ "$got" = "$want" ] || fail "mergeStateStatus $ms should classify $want, got $got"
done
pass "merge-state routing: UNSTABLE/HAS_HOOKS landable, BLOCKED and unknown are not"

# =============================================================================
# Case 14: drafts are never landing candidates and never stall the queue.
# =============================================================================
PRS="[$(pr_row 120 "wip" "$HEAD_FULL" CLEAN true '[]' "2026-01-01T00:00:00Z" florianhorner false),
      $(pr_row 121 "fix: ready" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "OPEN" ] || fail "draft must classify OPEN"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "121" ] || fail "a draft must not stall the queue"
pass "drafts are skipped, not stalled on"

# =============================================================================
# Case 15: a failed PR query is fatal — never an empty queue reported as calm.
# =============================================================================
set +e
OUT="$(PR_LIST_FAIL=1 run_plan "[]" 2>&1)"; rc=$?
set -e
[ "$rc" -ne 0 ] || fail "a failed gh pr list must fail closed, not report an empty queue"
printf '%s' "$OUT" | grep -q "could not list open PRs" || fail "failure should name the unreadable state"
pass "unreadable PR list fails closed (never a soft empty queue)"

# =============================================================================
# Edge selection library (plan section 8.1). Sourced directly so the eligibility
# function is tested as the auto-edge controller will call it.
# =============================================================================
edge_probe() { # RUN_SHAS -> prints "<rc>\t<stdout>"
  local out rc
  set +e
  out="$(
    GH_MOCK_LOG="$TMPDIR_T/gh.log" \
    GH_MOCK_RUN_SHAS="$1" \
    GH_MOCK_RUN_FAIL="${2:-0}" \
    PATH="$BIN:$PATH" \
      bash -c 'set -euo pipefail; . "'"$EDGE_LIB"'"; eligible_edge_sha "'"$MAIN_REF"'"' 2>/dev/null
  )"
  rc=$?
  set -e
  printf '%s\t%s' "$rc" "$out"
}

MAIN_FULL="$(git rev-parse "$MAIN_REF")"
MAIN_SHORT="$(git rev-parse --short=7 "$MAIN_REF")"

# 16: newest built commit with no drift is eligible.
IFS=$'\t' read -r rc out <<<"$(edge_probe "$MAIN_FULL")"
[ "$rc" = "0" ] || fail "a green build on the main tip should be eligible"
[ "$out" = "$MAIN_SHORT" ] || fail "eligible sha should be the main tip short sha, got '$out'"
pass "edge: green build on the main tip is eligible"

# 17: no green build anywhere -> refusal, never a guessed sha (invariant I2).
IFS=$'\t' read -r rc out <<<"$(edge_probe "")"
[ "$rc" != "0" ] || fail "no green build must refuse"
[ -z "$out" ] || fail "a refusal must not print a sha"
pass "edge: no green build refuses instead of naming a tag"

# 18: an unverifiable build query is a refusal, never a soft pass (invariant I9).
IFS=$'\t' read -r rc out <<<"$(edge_probe "" 1)"
[ "$rc" != "0" ] || fail "a failed run query must refuse"
[ -z "$out" ] || fail "a failed query must not print a sha"
pass "edge: unverifiable build query refuses (soft-pass guard)"

# 19: a built commit with IMAGE_PATHS drift since main is refused (invariant I3).
# Walk back to a commit that actually differs from main under IMAGE_PATHS, so the
# fixture is real repo history rather than an asserted assumption.
DRIFTED=""
while IFS= read -r c; do
  # shellcheck disable=SC2086
  if [ -n "$(git diff --name-only "$c" "$MAIN_REF" -- $(bash -c '. "'"$EDGE_LIB"'"; echo $IMAGE_PATHS'))" ]; then
    DRIFTED="$c"; break
  fi
done < <(git rev-list --topo-order -n 40 "$MAIN_REF")
[ -n "$DRIFTED" ] || fail "no commit with image drift found in the last 40 — fixture assumption broken"
IFS=$'\t' read -r rc out <<<"$(edge_probe "$DRIFTED")"
[ "$rc" != "0" ] || fail "a built commit with image drift must be refused"
[ -z "$out" ] || fail "a drift refusal must not print a sha"
pass "edge: image-path drift since the built commit refuses the pin"

# =============================================================================
# Case 21: the shadow workflow must stay report-only and keep its kill switch.
# =============================================================================
WF=".github/workflows/land-queue.yml"
[ -f "$WF" ] || fail "shadow workflow missing"
[ -f ".github/land-queue.enabled" ] || fail "kill-switch file missing"
grep -q "pull-requests: read" "$WF" || fail "shadow workflow must request read-only PR access"
grep -q "pull-requests: write" "$WF" && fail "shadow workflow must not request write access"
grep -q "contents: write" "$WF" && fail "shadow workflow must not request write access"
grep -Eq 'gh pr (merge|comment|edit)' "$WF" && fail "shadow workflow must not mutate PRs"
grep -q "vars.LAND_QUEUE" "$WF" || fail "shadow workflow must honour the LAND_QUEUE variable"
# One pass, not two: running the planner twice doubled every round trip and let
# the summary and the artifact disagree about the same queue.
[ "$(grep -c 'bash scripts/land-queue-plan.sh' "$WF")" = "1" ] \
  || fail "the planner must be invoked exactly once per run"
pass "shadow workflow is report-only, read-only, killable, and single-pass"

# =============================================================================
# Case 21b: the kill switch is a property of the CONTROLLER, not of one caller.
# Off must mean off from a local run too, or the switch is not a switch.
# =============================================================================
# Human path: prose that names the switch and how to undo it.
OUT="$(LAND_QUEUE=0 GH_MOCK_LOG="$TMPDIR_T/gh.log" GH_REPO="florianhorner/mammamiradio" \
  PATH="$BIN:$PATH" bash "$PLAN" 2>&1)"
printf '%s' "$OUT" | grep -q "switched off" || fail "LAND_QUEUE=0 must stop the planner itself"
printf '%s' "$OUT" | grep -q "turn it back on" || fail "a stopped queue must say how to restart it"
# --json path: the payload, and ONLY the payload. Prose here is a parse error
# for every consumer, including the documented pr-queue-status.sh --json.
OFF="$(LAND_QUEUE=0 run_plan "[]" 2>/dev/null)"
printf '%s' "$OFF" | jq -e '.mode == "off"' >/dev/null \
  || fail "a switched-off queue must answer --json with valid JSON, not prose"
pass "kill switch stops the planner itself and answers in the caller's format"

# =============================================================================
# Case 22: gate-set parity. land-pr.sh arms on verify_head; the queue re-composes
# the same gates to report WHICH one failed. Adding a gate to verify_head without
# teaching the queue would leave the shadow reporting READY without it — the
# exact drift the shared library exists to prevent, one level up.
# =============================================================================
gates_in() { sed -n "/^$2() {/,/^}/p" "$1" | grep -oE '\b(evidence_check|squad_check|thread_check|ensure_head_local)\b' | sort -u; }
VERIFY_GATES="$(gates_in "$REPO_ROOT/scripts/land-gates.sh" verify_head)"
QUEUE_GATES="$(gates_in "$PLAN" classify_pr)"
# Without this, renaming verify_head (or moving its closing brace off column 0)
# makes gates_in return nothing, comm finds nothing missing, and this case
# prints PASS forever while asserting nothing.
[ -n "$VERIFY_GATES" ] || fail "gate extraction found no gates in verify_head — the parity check would pass vacuously"
[ -n "$QUEUE_GATES" ] || fail "gate extraction found no gates in classify_pr — the parity check would pass vacuously"
# squad_check is deliberately absent from the queue: it reads a local gstack
# ledger that no runner has, and v2 evidence already covers the review gate.
MISSING="$(comm -23 <(printf '%s\n' "$VERIFY_GATES" | sed '/^$/d' | sort) \
                    <(printf '%s\nsquad_check\n' "$QUEUE_GATES" | sed '/^$/d' | sort))"
[ -z "$MISSING" ] || fail "verify_head gates the queue does not evaluate: $MISSING"
pass "queue evaluates every gate land-pr arms on (squad_check exempted by design)"

# =============================================================================
# Case 23: a PR whose author account was deleted (.author == null) must not
# shift every field after it. Tab is IFS whitespace, so a tab-separated gather
# collapsed the empty author field and slid the branch, title and url one place
# left — putting an edge-release PR into the gated feature lane, where it has no
# v2 receipt, and stalling the entire queue behind it.
# =============================================================================
GHOST="$(jq -cn --arg base "$ANC_FULL" --arg head "$HEAD_FULL" \
  '{number:130,title:"chore(edge): cut edge release abc1234",headRefOid:$head,
    baseRefOid:$base,mergeStateStatus:"CLEAN",isDraft:false,labels:[],
    createdAt:"2026-01-01T00:00:00Z",headRefName:"edge-release/abc1234",
    commits:[{committedDate:"2026-01-01T00:00:00Z"}],
    url:"https://example.test/pull/130",author:null}')"
OUT="$(run_plan "[$GHOST]")"
[ "$(jq -r '.prs[0].lane' <<<"$OUT")" = "edge" ] \
  || fail "a null author must not shift the branch field and mis-lane the PR"
[ "$(jq -r '.prs[0].branch' <<<"$OUT")" = "edge-release/abc1234" ] \
  || fail "branch field must survive an empty author field"
[ "$(jq -r '.prs[0].fifo_key' <<<"$OUT")" = "2026-01-01T00:00:00Z" ] \
  || fail "fifo_key must survive an empty author field"
pass "deleted-author PR keeps every field aligned (empty-field shift guard)"

# =============================================================================
# Case 24: the generic bot lane. is_bot=true grants a TOTAL gate exemption — no
# evidence, no thread check, dropped from head contention. Case 10 only ever
# exercised the dependabot branch, so this one was untested.
# =============================================================================
PRS="[$(pr_row 140 "chore: automated" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" "some-app[bot]" true),
      $(pr_row 141 "fix: real work" "$HEAD_FULL" CLEAN false '[]' "2026-02-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].lane' <<<"$OUT")" = "bot" ] || fail "a non-dependabot bot must land in the bot lane"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "EXEMPT" ] || fail "bot PRs are exempt"
[ "$(jq -r '.decision.pr' <<<"$OUT")" = "141" ] || fail "a bot PR must not hold the feature queue head"
pass "generic bot lane is exempt and cannot stall the queue"

# =============================================================================
# Case 25: BLOCKED_HEAD. An unfetchable head must not silently skip the two
# network gates — every other fixture uses a local SHA, so this never ran.
# =============================================================================
PRS="[$(pr_row 150 "fix: ghost head" "0000000000000000000000000000000000000000" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_HEAD" ] || fail "an unfetchable head must block"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "none" ] || fail "must not act on a head we cannot verify"
pass "unfetchable head blocks instead of skipping the gates"

# =============================================================================
# Case 26: invalid PR JSON fails closed rather than planning against it.
# =============================================================================
set +e
OUT="$(run_plan '{"not":"an array"}' 2>&1)"; rc=$?
set -e
[ "$rc" -ne 0 ] || fail "non-array PR JSON must fail closed"
printf '%s' "$OUT" | grep -q "invalid PR JSON" || fail "failure should name the unverifiable state"
pass "invalid PR JSON fails closed"

# =============================================================================
# Case 27: --json-out is the ONLY flag production uses (land-queue.yml), and the
# artifact upload is gated on the file existing — so a silently-empty write
# would vanish the artifact behind a green workflow.
# =============================================================================
PRS="[$(pr_row 160 "fix: a thing" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUTFILE="$TMPDIR_T/decision.json"
rm -f "$OUTFILE"
HUMAN="$(GH_MOCK_LOG="$TMPDIR_T/gh.log" GH_MOCK_PRS="$PRS" GH_MOCK_THREADS="$EMPTY_THREADS" \
  GH_MOCK_COMMENTS="$EMPTY_COMMENTS" GH_MOCK_RUN_SHAS="" \
  MMR_LAND_EVIDENCE_CHECKER="$TMPDIR_T/evidence-ok.sh" MMR_LAND_REVIEW_READER="/nonexistent" \
  GH_REPO="florianhorner/mammamiradio" PATH="$BIN:$PATH" \
  bash "$PLAN" --json-out "$OUTFILE")"
[ -s "$OUTFILE" ] || fail "--json-out must write the decision file"
jq -e '.decision.action == "arm"' "$OUTFILE" >/dev/null || fail "--json-out payload must carry the decision"
printf '%s' "$HUMAN" | grep -q "SHADOW mode" || fail "--json-out must still print the human summary to stdout"
printf '%s' "$HUMAN" | jq -e . >/dev/null 2>&1 && fail "stdout under --json-out must be prose, not JSON"
# The human renderer itself is otherwise dead to this suite — every other case
# runs --json — yet it is the code path the scheduled workflow prints.
printf '%s' "$HUMAN" | grep -q "Open PRs (FIFO order" || fail "human renderer must list the queue"
printf '%s' "$HUMAN" | grep -q "#160" || fail "human renderer must name each PR"
pass "--json-out writes JSON and still renders the human summary"

# =============================================================================
# Case 28: the FILE half of the kill switch — the half the runbook names first.
# Only the LAND_QUEUE half was covered, so inverting the -f test passed.
# =============================================================================
SWITCH_ROOT="$TMPDIR_T/noswitch"
mkdir -p "$SWITCH_ROOT/scripts" "$SWITCH_ROOT/.github"
cp "$PLAN" "$REPO_ROOT/scripts/land-gates.sh" "$REPO_ROOT/scripts/edge-select.sh" \
   "$REPO_ROOT/scripts/review-threads.sh" "$SWITCH_ROOT/scripts/"
OUT="$(cd "$SWITCH_ROOT" && GH_REPO="florianhorner/mammamiradio" PATH="$BIN:$PATH" \
  bash "$SWITCH_ROOT/scripts/land-queue-plan.sh" 2>&1)"
printf '%s' "$OUT" | grep -q "switched off" \
  || fail "a missing .github/land-queue.enabled must stop the planner"
# ...and it must still answer in the caller's format, or pr-queue-status --json
# gets prose where it expects JSON and dies on a jq parse error.
OFF_JSON="$(cd "$SWITCH_ROOT" && GH_REPO="florianhorner/mammamiradio" PATH="$BIN:$PATH" \
  bash "$SWITCH_ROOT/scripts/land-queue-plan.sh" --json 2>/dev/null)"
printf '%s' "$OFF_JSON" | jq -e '.mode == "off" and (.prs | length) == 0' >/dev/null \
  || fail "a switched-off queue must still emit valid JSON under --json"
pass "kill-switch FILE half stops the planner and still answers in JSON"

# =============================================================================
# Case 29: the planner's edge lane had zero assertions, and it discarded the one
# distinction edge-select.sh exists to make — rc 2 (could not check) reported
# identically to rc 1 (checked, nothing eligible).
# =============================================================================
PRS="[$(pr_row 170 "fix: a thing" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
OUT="$(RUN_SHAS="$(git rev-parse "$MAIN_REF")" run_plan "$PRS")"
jq -e '.edge.state == "advance" or .edge.state == "noop"' <<<"$OUT" >/dev/null \
  || fail "a green build on the main tip should give the edge lane a verdict"
[ "$(jq -r '.edge.target' <<<"$OUT")" = "$(git rev-parse --short=7 "$MAIN_REF")" ] \
  || fail "edge target should be the eligible short sha"
OUT="$(RUN_FAIL=1 run_plan "$PRS")"
[ "$(jq -r '.edge.state' <<<"$OUT")" = "blocked" ] || fail "an unverifiable edge query must block"
jq -e '.edge.why | test("could not check")' <<<"$OUT" >/dev/null \
  || fail "an unverifiable edge query must not report the calm 'no eligible built commit'"
OUT="$(RUN_SHAS="" run_plan "$PRS")"
jq -e '.edge.why | test("no eligible built commit")' <<<"$OUT" >/dev/null \
  || fail "a genuinely empty build history should report no eligible commit"
pass "edge lane distinguishes unverifiable from genuinely idle"

# =============================================================================
# Case 30: shadow mode wrote nothing across EVERY run in this file, not just
# the first (run_plan truncates the per-run log).
# =============================================================================
cat "$TMPDIR_T/gh.log" >> "$ALL_GH_LOG"
grep -Eq 'pr (merge|comment|edit|review|close)|api -X|--method (POST|PUT|PATCH|DELETE)' "$ALL_GH_LOG" \
  && fail "shadow planner issued a mutating gh call in at least one run"
[ -s "$ALL_GH_LOG" ] || fail "cumulative gh log is empty — the assertion would be vacuous"
pass "no mutating gh call across every planner run in this suite"

# =============================================================================
# Case 31: a jq failure inside the blocking-thread scan must BLOCK, not report
# "no debt". The scan used a process substitution, which discards its exit
# status: jq failing yielded zero lines, left the counter at 0, and returned
# PASS — so land-pr.sh would arm on a PR whose thread debt was never evaluated.
# =============================================================================
PRS="[$(pr_row 180 "fix: jq breaks" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false)]"
# A response the blocking filter cannot walk: reviewThreads.nodes is not an array.
BROKEN_THREADS='{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":"not-an-array"}}}}}'
OUT="$(THREADS="$BROKEN_THREADS" run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_BOT" ] \
  || fail "an unevaluable thread response must block, never read as zero debt"
[ "$(jq -r '.decision.action' <<<"$OUT")" = "none" ] \
  || fail "must not arm when thread debt could not be evaluated"
pass "unevaluable thread response fails closed (jq soft-pass guard)"

# =============================================================================
# Case 32: the blocking filter matches the bot logins in both spellings. This
# GraphQL path returns them unsuffixed, but the same accounts appear as
# "<name>[bot]" elsewhere on GitHub; matching one spelling only would silently
# report zero debt if this surface ever changed.
# =============================================================================
for login in "coderabbitai" "coderabbitai[bot]" "copilot-pull-request-reviewer[bot]"; do
  COMMENTS_JSON="$(jq -cn --arg l "$login" \
    '{data:{node:{comments:{pageInfo:{hasNextPage:false,endCursor:null},
      nodes:[{author:{login:$l},body:"Major: blocking",url:"https://example.test/c/1"}]}}}}')"
  OUT="$(THREADS="$ONE_THREAD" COMMENTS="$COMMENTS_JSON" run_plan "$PRS")"
  [ "$(jq -r '.prs[0].state' <<<"$OUT")" = "BLOCKED_BOT" ] \
    || fail "a Major from '$login' must block"
done
# ...and a genuinely unrelated author still does not block.
COMMENTS_JSON='{"data":{"node":{"comments":{"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[{"author":{"login":"some-human"},"body":"Major: opinion","url":"https://example.test/c/2"}]}}}}'
OUT="$(THREADS="$ONE_THREAD" COMMENTS="$COMMENTS_JSON" run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "READY" ] \
  || fail "a human comment must not be treated as blocking bot debt"
pass "blocking filter matches both bot login spellings, and only bots"

# =============================================================================
# Case 33: the ledger age check uses the head's own last push, not wall clock.
# Passing `date +%s` made every entry look stale once it aged past the grace
# window, so the shadow blocked where land-pr.sh accepts.
# =============================================================================
grep -q 'squad_check "$head" "$(date' "$PLAN" \
  && fail "the ledger age check must use the head commit date, not wall clock"
grep -q 'gh pr view "$pr" --json commits' "$PLAN" \
  || fail "the ledger age check must read the head commit date from the PR"
PRS="[$(pr_row 190 "fix: aged" "$HEAD_FULL" CLEAN false '[]' "2026-01-01T00:00:00Z" florianhorner false "feature/pr-190" "2026-01-01T00:00:00Z")]"
OUT="$(run_plan "$PRS")"
[ "$(jq -r '.prs[0].state' <<<"$OUT")" = "READY" ] \
  || fail "an old head commit date must not block when evidence is present"
pass "ledger age check reads the head commit date, not the wall clock"

echo
echo "All $PASS_COUNT land-queue cases passed."
