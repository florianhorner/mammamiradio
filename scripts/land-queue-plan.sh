#!/usr/bin/env bash
# land-queue-plan.sh — the land queue's decision engine, in SHADOW mode.
#
#   scripts/land-queue-plan.sh [--json | --json-out FILE]
#
# Computes what an auto-land controller WOULD do right now — which PR it would
# integrate or arm next, and why every other open PR is not that PR — and prints
# it. It does not push, comment, arm, merge, or move edge. Nothing here writes.
#
# This is phase 1 of .context/plans/2026-08-28-auto-land-edge-queue-plan.md: run
# the decision alongside the human landing seat long enough to trust it, then
# flip it live. The same staging the pre-ship evidence gate used
# (.github/workflows/preship-evidence.yml is report-only for the same reason).
#
# It reaches its verdict through the SAME predicates scripts/land-pr.sh arms on
# (scripts/land-gates.sh) and the SAME selection scripts/cut-edge-release.sh cuts
# on (scripts/edge-select.sh). A shadow that reasons from its own copy of the
# gates would prove nothing about the thing it shadows.
#
# Fail-closed everywhere (invariant I9): a gh error, a git error, or a state
# GitHub has not finished computing is reported as BLOCKED/CI_PENDING and the
# queue does not advance past it. There is no soft-pass into "would arm".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

say()  { printf '%s\n' "$*"; }
die()  { printf 'land-queue: %s\n' "$*" >&2; exit 1; }

JSON_ONLY=0
JSON_OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --json) JSON_ONLY=1; shift ;;
    --json-out)
      JSON_OUT="${2:-}"
      [ -n "$JSON_OUT" ] || die "--json-out needs a file path"
      shift 2 ;;
    -h|--help)
      say "Usage: $0 [--json | --json-out FILE]"
      say "  (no args)         human summary"
      say "  --json            machine-readable decision on stdout"
      say "  --json-out FILE   human summary on stdout AND the JSON to FILE,"
      say "                    from a single pass (the queue is queried once)"
      say "  Read-only; never writes to GitHub."
      exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Kill switch. Checked here rather than only in the workflow so that "off" means
# off from every invocation path — a local run, a second workflow, or the live
# controller this becomes. Absent file or LAND_QUEUE=0 stops it.
if [ ! -f "$ROOT/.github/land-queue.enabled" ] || [ "${LAND_QUEUE:-1}" = "0" ]; then
  say "land-queue: the shadow queue is switched off — nothing computed."
  say "            Restore .github/land-queue.enabled, or unset LAND_QUEUE=0, to turn it back on."
  exit 0
fi

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install GitHub CLI, then re-run."
command -v jq >/dev/null 2>&1 || die "jq not found. Install jq, then re-run."
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository."

LAND_GATES_LABEL="land-queue"
LAND_GATES_LIB="$SCRIPT_DIR/land-gates.sh"
[ -r "$LAND_GATES_LIB" ] || die "landing gate library not found at $LAND_GATES_LIB."
# shellcheck source=scripts/land-gates.sh
. "$LAND_GATES_LIB"

EDGE_SELECT_LIB="$SCRIPT_DIR/edge-select.sh"
[ -r "$EDGE_SELECT_LIB" ] || die "edge selection library not found at $EDGE_SELECT_LIB."
# shellcheck source=scripts/edge-select.sh
. "$EDGE_SELECT_LIB"

# --- classification -----------------------------------------------------------
# Prints "<state>\t<reason>". Ordering is deliberate: every free local fact is
# settled before either network gate runs, and the two gates run BEFORE the
# integrate/arm routing so the queue never spends a reattest cycle on a PR that
# is blocked on a bot Major anyway (plan section 6.2).
#
# Deliberately NOT short-circuited on mergeStateStatus == BLOCKED, even though
# the outcome is CI_PENDING either way and the gates are the expensive part:
# main requires review-thread resolution, so an unresolved bot Major always
# forces BLOCKED. Settling that state early would make BLOCKED_BOT unreachable
# in practice and replace the one actionable reason ("2 unresolved Major
# threads, here is the first") with "waiting on something".
classify_pr() {
  local pr="$1" head="$2" base="$3" merge_state="$4" is_draft="$5" held="$6" skipped="$7"
  local gate_out

  if [ "$is_draft" = "true" ]; then
    printf 'OPEN\tdraft — not a landing candidate\n'; return
  fi
  if [ "$held" = "true" ]; then
    printf 'BLOCKED_YOU\thold/manual-land label set by the operator\n'; return
  fi
  if [ "$skipped" = "true" ]; then
    printf 'SKIPPED\tskip-queue label set — out of head contention, the queue moves past it\n'; return
  fi
  if [ "$merge_state" = "DIRTY" ]; then
    printf 'BLOCKED_CONFLICT\tmerge conflict with base — the owning workspace resolves it\n'; return
  fi
  if ! ensure_head_local "$pr" "$head"; then
    printf 'BLOCKED_HEAD\thead object could not be fetched — gates cannot be evaluated against it\n'; return
  fi
  if ! gate_out="$(thread_check "$pr" 2>&1)"; then
    printf 'BLOCKED_BOT\t%s\n' "$(printf '%s' "$gate_out" | head -1)"; return
  fi
  if ! evidence_check "$head" "$base" >/dev/null 2>&1; then
    printf 'BLOCKED_EVIDENCE\tno committed v2 receipt covers this head\n'; return
  fi

  # Plan Q7: UNSTABLE means only non-required checks are failing and is landable.
  case "$merge_state" in
    BEHIND)    printf 'READY_BEHIND\tgates pass; base moved — needs integrate + reattest\n' ;;
    CLEAN)     printf 'READY\tgates pass and required checks are green\n' ;;
    UNSTABLE)  printf 'READY\tgates pass; only non-required checks are red\n' ;;
    HAS_HOOKS) printf 'READY\tgates pass; merge would fire repository hooks\n' ;;
    *)         printf 'CI_PENDING\tGitHub reports merge state %s — not acting on it\n' "$merge_state" ;;
  esac
}

# --- gather -------------------------------------------------------------------
PR_JSON="$(gh pr list --state open --limit 50 \
  --json number,title,headRefName,headRefOid,baseRefOid,mergeStateStatus,isDraft,labels,createdAt,url,author \
  2>/dev/null)" \
  || die "could not list open PRs. Check gh auth and repository context."
printf '%s' "$PR_JSON" | jq -e 'type == "array"' >/dev/null 2>&1 \
  || die "gh returned invalid PR JSON — refusing to plan against unverifiable state."

# FIFO key. The plan's ready_at (first entry into READY) needs a persisted ledger,
# which shadow mode has no write path for; createdAt is the honest read-only
# stand-in, and the JSON labels it as a proxy so a month of shadow data stays
# interpretable after the swap. It carries the property section 6.3 actually asks
# for — a PR that bounces and returns keeps its place — because it never changes.
FIFO_KEY_SOURCE="createdAt (proxy for ready_at; shadow mode has no write path)"

# One jq pass for the whole queue. Label membership is resolved to booleans here
# rather than by string-matching a joined list in shell, so a label whose name
# contains the separator cannot be misread.
GATHER="$(printf '%s' "$PR_JSON" | jq -r 'sort_by(.createdAt)[]
  | (.labels | map(.name)) as $l
  | [ .number, .headRefOid, .baseRefOid, .mergeStateStatus, .isDraft,
      (($l | index("hold")) != null or ($l | index("manual-land")) != null),
      (($l | index("skip-queue")) != null),
      (.author.login // ""), (.author.is_bot // false),
      .headRefName, .createdAt, .url, .title
    ] | @tsv')"

# --- decide (single-flight, plan section 6.4) ---------------------------------
# At most ONE feature PR may be acted on per tick (invariant I8). The queue head
# is the first feature-lane row in FIFO order that is in head contention; a
# blocked head STALLS the queue rather than being skipped, which is what keeps
# the ordering fair (plan section 15). Everything behind it waits — but is still
# classified, because the shadow phase exists to be compared against the human
# seat and a row with no reason is not comparable.
DECISION_ACTION="none"
DECISION_PR=""
DECISION_WHY="no feature PR is actionable"
HEAD_DECIDED=0
RESULTS=""

while IFS=$'\t' read -r number head base merge_state is_draft held skipped \
                        author is_bot branch created url title; do
  [ -n "$number" ] || continue

  # Lane. Both non-feature lanes are keyed on signals the automation itself
  # emits — the bot flag and the branch name cut-edge-release.sh pushes — never
  # on the PR title, which a human can retype and which would otherwise decide a
  # total gate exemption.
  lane="feature"
  if [ "$is_bot" = "true" ]; then
    lane="bot"
  fi
  case "$author" in
    dependabot|app/dependabot|"dependabot[bot]") lane="dependabot" ;;
  esac
  case "$branch" in
    edge-release/*) lane="edge" ;;
  esac

  if [ "$lane" = "feature" ]; then
    IFS=$'\t' read -r state reason \
      <<<"$(classify_pr "$number" "$head" "$base" "$merge_state" "$is_draft" "$held" "$skipped")"
  else
    state="EXEMPT"
    reason="$lane lane — keeps its own merge path, never queues behind feature work"
  fi

  if [ "$HEAD_DECIDED" -eq 0 ] && [ "$lane" = "feature" ]; then
    case "$state" in
      OPEN|SKIPPED) : ;;   # not in head contention; keep scanning
      READY)
        HEAD_DECIDED=1; DECISION_ACTION="arm"; DECISION_PR="$number"
        DECISION_WHY="queue head, gates pass — would arm --squash --auto --match-head-commit ${head:0:12}" ;;
      READY_BEHIND)
        HEAD_DECIDED=1; DECISION_ACTION="integrate"; DECISION_PR="$number"
        DECISION_WHY="queue head, gates pass but base moved — would merge origin/main, reattest, push" ;;
      *)
        HEAD_DECIDED=1; DECISION_PR="$number"
        DECISION_WHY="queue head is $state — the queue stalls here rather than reordering around it" ;;
    esac
  fi

  RESULTS="$RESULTS$(jq -cn \
    --argjson number "$number" --arg title "$title" --arg head "$head" \
    --arg state "$state" --arg reason "$reason" --arg lane "$lane" \
    --arg branch "$branch" --arg created "$created" --arg url "$url" --arg merge "$merge_state" \
    '{number:$number,title:$title,head:$head,branch:$branch,lane:$lane,state:$state,
      reason:$reason,merge_state:$merge,fifo_key:$created,url:$url}')"$'\n'
done <<<"$GATHER"

# --- edge lane (plan section 8) -----------------------------------------------
# Independent of the feature queue by design (Q4/C): edge cuts are mechanical
# one-liners and must not sit behind feature FIFO.
EDGE_TARGET=""
if ! git rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  EDGE_STATE="blocked"
  EDGE_WHY="origin/main is not resolvable locally — refusing to name an edge target"
elif ! EDGE_TARGET="$(eligible_edge_sha origin/main 2>/dev/null)"; then
  EDGE_STATE="blocked"
  EDGE_WHY="no eligible built commit right now — edge stays where it is"
else
  EDGE_CURRENT="$(edge_pinned_version origin/main)"
  if [ "$EDGE_CURRENT" = "$EDGE_TARGET" ]; then
    EDGE_STATE="noop"
    EDGE_WHY="edge already pinned to $EDGE_TARGET — the newest eligible built commit"
  else
    EDGE_STATE="advance"
    EDGE_WHY="would pin edge $EDGE_CURRENT -> $EDGE_TARGET (green build, no image drift vs main)"
  fi
fi

# --- report -------------------------------------------------------------------
DECISION_JSON="$(printf '%s' "$RESULTS" | jq -s \
  --arg action "$DECISION_ACTION" --arg pr "$DECISION_PR" --arg why "$DECISION_WHY" \
  --arg fifo_source "$FIFO_KEY_SOURCE" \
  --arg edge_state "$EDGE_STATE" --arg edge_target "$EDGE_TARGET" --arg edge_why "$EDGE_WHY" \
  '{mode:"shadow",
    fifo_key_source:$fifo_source,
    decision:{action:$action, pr:(if $pr == "" then null else ($pr|tonumber) end), why:$why},
    edge:{state:$edge_state, target:(if $edge_target == "" then null else $edge_target end), why:$edge_why},
    prs:.}')"

if [ "$JSON_ONLY" = "1" ]; then
  printf '%s\n' "$DECISION_JSON"
  exit 0
fi
[ -n "$JSON_OUT" ] && printf '%s\n' "$DECISION_JSON" > "$JSON_OUT"

say "land-queue: SHADOW mode — this run changed nothing."
say ""
case "$DECISION_ACTION" in
  arm)       say "Next action: ARM PR #$DECISION_PR" ;;
  integrate) say "Next action: INTEGRATE PR #$DECISION_PR" ;;
  *)         say "Next action: NOTHING" ;;
esac
say "  $DECISION_WHY"
say ""
say "Edge channel: $EDGE_STATE"
say "  $EDGE_WHY"
say ""
say "Open PRs (FIFO order, oldest first):"
printf '%s' "$DECISION_JSON" | jq -r '.prs[] | "  #\(.number)  \(.state)  \(.title)\n         \(.reason)"'
