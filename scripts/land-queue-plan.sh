#!/usr/bin/env bash
# land-queue-plan.sh — the land queue's decision engine, in SHADOW mode.
#
#   scripts/land-queue-plan.sh [--json]
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

say()  { printf '%s\n' "$*"; }
die()  { printf 'land-queue: %s\n' "$*" >&2; exit 1; }

EMIT_JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) EMIT_JSON=1; shift ;;
    -h|--help)
      say "Usage: $0 [--json]"
      say "  Prints the land queue's next decision. Read-only; never writes."
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

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

# Gate messages are diagnostics here, not operator instructions — the queue's own
# output explains what to do. Capture them per PR instead of interleaving them.
GATE_OUT=""
quiet_gate() {
  GATE_OUT="$("$@" 2>&1)"
}

# --- signals (plan section 5.4) ----------------------------------------------
# Auto-land is ON by default for human/feature PRs, per the plan's default. It is
# OFF for the lanes that deliberately keep their own merge path:
#   - Dependabot: .github/workflows/dependabot-automerge.yml owns it
#   - chore(edge): mechanical one-liners must not queue behind feature work
# and OFF for any PR the operator has held.
HOLD_LABELS="hold manual-land"
# A blocked queue head STALLS the queue rather than being reordered around, which
# is what keeps FIFO fair. `skip-queue` is the escape that makes stalling
# survivable: it drops one PR out of head contention (typically a conflict only
# its owner can fix) so a day-long block cannot hold every other fix hostage.
SKIP_LABEL="skip-queue"

has_label() {
  printf '%s' "$1" | jq -e --arg want "$2" 'any(.[]; .name == $want)' >/dev/null 2>&1
}

held_by_operator() {
  local labels="$1" name
  for name in $HOLD_LABELS; do
    has_label "$labels" "$name" && return 0
  done
  return 1
}

# --- classification -----------------------------------------------------------
# Prints "<state>\t<reason>". Ordering is deliberate: cheap local facts first,
# then the two network gates, then merge-state routing. Threads and evidence are
# checked BEFORE integrating so the queue never spends a reattest cycle on a PR
# that is blocked on a bot Major anyway (plan section 6.2).
classify_pr() {
  local pr="$1" head="$2" base="$3" merge_state="$4" is_draft="$5" labels="$6"

  if [ "$is_draft" = "true" ]; then
    printf 'OPEN\tdraft — not a landing candidate\n'; return
  fi
  if held_by_operator "$labels"; then
    printf 'BLOCKED_YOU\thold/manual-land label set by the operator\n'; return
  fi
  if has_label "$labels" "$SKIP_LABEL"; then
    printf 'SKIPPED\t%s label set — out of head contention, the queue moves past it\n' "$SKIP_LABEL"; return
  fi
  if [ "$merge_state" = "DIRTY" ]; then
    printf 'BLOCKED_CONFLICT\tmerge conflict with base — the owning workspace resolves it\n'; return
  fi
  if ! ensure_head_local "$pr" "$head"; then
    printf 'BLOCKED_HEAD\thead object could not be fetched — gates cannot be evaluated against it\n'; return
  fi
  if ! quiet_gate thread_check "$pr"; then
    printf 'BLOCKED_BOT\t%s\n' "$(printf '%s' "$GATE_OUT" | head -1)"; return
  fi
  if ! quiet_gate evidence_check "$head" "$base"; then
    printf 'BLOCKED_EVIDENCE\tno committed v2 receipt covers this head\n'; return
  fi

  # mergeStateStatus is GitHub's own answer about required checks (plan Q7:
  # UNSTABLE means non-required checks are failing and is landable; BLOCKED means
  # a required check or the thread-resolution rule is not satisfied yet).
  case "$merge_state" in
    BEHIND)   printf 'READY_BEHIND\tgates pass; base moved — needs integrate + reattest\n' ;;
    CLEAN)    printf 'READY\tgates pass and required checks are green\n' ;;
    UNSTABLE) printf 'READY\tgates pass; only non-required checks are red\n' ;;
    BLOCKED)  printf 'CI_PENDING\twaiting on required checks or thread resolution\n' ;;
    HAS_HOOKS) printf 'READY\tgates pass; merge would fire repository hooks\n' ;;
    *)        printf 'CI_PENDING\tGitHub reports merge state %s — not acting on it\n' "$merge_state" ;;
  esac
}

# --- gather -------------------------------------------------------------------
PR_JSON="$(gh pr list --state open --limit 50 \
  --json number,title,headRefOid,baseRefOid,mergeStateStatus,isDraft,labels,createdAt,url,author \
  2>/dev/null)" \
  || die "could not list open PRs. Check gh auth and repository context."
printf '%s' "$PR_JSON" | jq -e 'type == "array"' >/dev/null 2>&1 \
  || die "gh returned invalid PR JSON — refusing to plan against unverifiable state."

# FIFO key. The plan's ready_at (first entry into READY) needs a persisted ledger,
# which shadow mode has no write path for; createdAt is the honest read-only
# stand-in. It carries the property section 6.3 actually asks for — a PR that
# bounces and returns keeps its original place — because it never changes.
ROWS="$(printf '%s' "$PR_JSON" | jq -c 'sort_by(.createdAt)[]')"

RESULTS=""
while IFS= read -r row; do
  [ -n "$row" ] || continue
  number="$(printf '%s' "$row" | jq -r '.number')"
  title="$(printf '%s' "$row" | jq -r '.title')"
  head="$(printf '%s' "$row" | jq -r '.headRefOid')"
  base="$(printf '%s' "$row" | jq -r '.baseRefOid')"
  merge_state="$(printf '%s' "$row" | jq -r '.mergeStateStatus')"
  is_draft="$(printf '%s' "$row" | jq -r '.isDraft')"
  labels="$(printf '%s' "$row" | jq -c '.labels')"
  created="$(printf '%s' "$row" | jq -r '.createdAt')"
  url="$(printf '%s' "$row" | jq -r '.url')"
  author="$(printf '%s' "$row" | jq -r '.author.login // ""')"
  is_bot="$(printf '%s' "$row" | jq -r '.author.is_bot // false')"

  # Bot lane. Any bot-authored PR is exempt: Dependabot has its own guarded
  # workflow and no other bot is a human/feature PR this queue should land.
  # Matching on is_bot rather than on a login spelling is deliberate — the `gh`
  # CLI reports "app/dependabot" while the webhook payload that
  # dependabot-automerge.yml matches on reports "dependabot[bot]", and a queue
  # that silently stops recognising bots would stall its head on one of them.
  lane="feature"
  if [ "$is_bot" = "true" ]; then
    lane="bot"
  fi
  case "$author" in
    dependabot|app/dependabot|"dependabot[bot]") lane="dependabot" ;;
  esac
  case "$title" in
    "chore(edge): cut edge release"*) lane="edge" ;;
  esac

  if [ "$lane" = "feature" ]; then
    IFS=$'\t' read -r state reason <<<"$(classify_pr "$number" "$head" "$base" "$merge_state" "$is_draft" "$labels")"
  else
    state="EXEMPT"
    reason="$lane lane — keeps its own merge path, never queues behind feature work"
  fi

  RESULTS="$RESULTS$(jq -cn \
    --argjson number "$number" --arg title "$title" --arg head "$head" \
    --arg state "$state" --arg reason "$reason" --arg lane "$lane" \
    --arg created "$created" --arg url "$url" --arg merge "$merge_state" \
    '{number:$number,title:$title,head:$head,lane:$lane,state:$state,reason:$reason,merge_state:$merge,fifo_key:$created,url:$url}')"$'\n'
done <<<"$ROWS"

# --- decide (single-flight, plan section 6.4) ---------------------------------
# At most ONE feature PR may be acted on per tick (invariant I8). The queue head
# is the oldest FIFO key that is actionable; a blocked head STALLS the queue
# rather than being skipped, which is what keeps the ordering fair (section 15,
# manual rehearsal 2). Everything behind it waits.
DECISION_ACTION="none"
DECISION_PR=""
DECISION_WHY="no feature PR is actionable"

while IFS= read -r r; do
  [ -n "$r" ] || continue
  [ "$(printf '%s' "$r" | jq -r '.lane')" = "feature" ] || continue
  st="$(printf '%s' "$r" | jq -r '.state')"
  case "$st" in
    READY)
      DECISION_ACTION="arm"
      DECISION_PR="$(printf '%s' "$r" | jq -r '.number')"
      DECISION_WHY="queue head, gates pass — would arm --squash --auto --match-head-commit $(printf '%s' "$r" | jq -r '.head' | cut -c1-12)"
      break ;;
    READY_BEHIND)
      DECISION_ACTION="integrate"
      DECISION_PR="$(printf '%s' "$r" | jq -r '.number')"
      DECISION_WHY="queue head, gates pass but base moved — would merge origin/main, reattest, push"
      break ;;
    OPEN|SKIPPED)
      # Drafts and skip-queue PRs are not in head contention; keep scanning.
      continue ;;
    *)
      DECISION_ACTION="none"
      DECISION_PR="$(printf '%s' "$r" | jq -r '.number')"
      DECISION_WHY="queue head is $st — the queue stalls here rather than reordering around it"
      break ;;
  esac
done <<<"$RESULTS"

# --- edge lane (plan section 8) -----------------------------------------------
# Independent of the feature queue by design (Q4/C): edge cuts are mechanical
# one-liners and must not sit behind feature FIFO.
EDGE_STATE="noop"
EDGE_TARGET=""
EDGE_WHY=""
if git rev-parse --verify --quiet origin/main >/dev/null 2>&1; then
  if EDGE_TARGET="$(eligible_edge_sha origin/main 2>/dev/null)"; then
    EDGE_CURRENT="$(git show "origin/main:ha-addon/mammamiradio-edge/config.yaml" 2>/dev/null \
      | awk '/^version:/ { print $2; exit }' | tr -d '"')" || EDGE_CURRENT=""
    if [ "$EDGE_CURRENT" = "$EDGE_TARGET" ]; then
      EDGE_STATE="noop"
      EDGE_WHY="edge already pinned to $EDGE_TARGET — the newest eligible built commit"
    else
      EDGE_STATE="advance"
      EDGE_WHY="would pin edge $EDGE_CURRENT -> $EDGE_TARGET (green build, no image drift vs main)"
    fi
  else
    EDGE_STATE="blocked"
    EDGE_TARGET=""
    EDGE_WHY="no eligible built commit right now — edge stays where it is"
  fi
else
  EDGE_STATE="blocked"
  EDGE_WHY="origin/main is not resolvable locally — refusing to name an edge target"
fi

# --- report -------------------------------------------------------------------
if [ "$EMIT_JSON" = "1" ]; then
  printf '%s' "$RESULTS" | jq -s \
    --arg action "$DECISION_ACTION" --arg pr "$DECISION_PR" --arg why "$DECISION_WHY" \
    --arg edge_state "$EDGE_STATE" --arg edge_target "$EDGE_TARGET" --arg edge_why "$EDGE_WHY" \
    '{mode:"shadow",
      decision:{action:$action, pr:(if $pr == "" then null else ($pr|tonumber) end), why:$why},
      edge:{state:$edge_state, target:(if $edge_target == "" then null else $edge_target end), why:$edge_why},
      prs:.}'
  exit 0
fi

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
while IFS= read -r r; do
  [ -n "$r" ] || continue
  printf '  #%-5s %-16s %s\n' \
    "$(printf '%s' "$r" | jq -r '.number')" \
    "$(printf '%s' "$r" | jq -r '.state')" \
    "$(printf '%s' "$r" | jq -r '.title')"
  printf '         %s\n' "$(printf '%s' "$r" | jq -r '.reason')"
done <<<"$RESULTS"
