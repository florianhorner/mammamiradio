#!/usr/bin/env bash
# land-pr.sh — the only legitimate human/feature merge path for this repo.
#
#   scripts/land-pr.sh <pr-number> [<pr-number>...]
#
# Landing contract (CLAUDE.md "Quality gates" → "Landing contract"): human and feature PRs are
# opened by /ship and never armed for auto-merge. On the operator's explicit
# merge signal, this wrapper:
#
#   1. refuses a behind branch without mutating it, directing the feature
#      workspace to integrate main and reattest the resulting content;
#   2. verifies committed v2 pre-ship evidence on the PR head (portable proof —
#      works from cloud agents and CI once the receipt is on the branch);
#   3. when a local gstack ledger is present, also accepts a squad entry that
#      is still about THIS code (code-state freshness: the entry's commit must
#      be the PR head or an ancestor, and nothing was pushed after the entry);
#      without a ledger, v2 evidence alone satisfies the review gate;
#   4. blocks unresolved Major/Critical bot review threads on the PR head;
#   5. arms GitHub auto-merge pinned to the exact head it verified:
#      gh pr merge --squash --auto --match-head-commit <sha>.
#
# GitHub then merges only when required checks pass on the integrated state
# AND the head is still the one verified here. If anything pushes to the
# branch afterwards, the merge does not fire — re-run this script.
#
# This is a guard for tired humans and parallel agents, not a security
# boundary: it relies on the local PreToolUse hook denying raw `gh pr merge`
# (scripts/hooks/require-preship-squad.sh) and is bypassable via the GitHub
# UI/API on purpose.
#
# Multiple PR numbers are processed sequentially. A behind PR is parked without
# changing its branch; later PRs in the same invocation are still inspected.
#
# For multi-PR/coordinator landing sessions, scripts/pr-queue-status.sh is an
# optional read-only preflight that summarizes open-PR/worktree state before
# you decide order here. It is advisory only, not a gate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Freshness grace: /ship pushes mechanical commits (version bump, changelog)
# right after the squad logs its entry; commits within this window after the
# entry are treated as part of the reviewed push, not new work.
GRACE_SECONDS="${MMR_LAND_GRACE_SECONDS:-600}"
# Reader override exists for tests; default is the repo-local ledger dump.
READER="${MMR_LAND_REVIEW_READER:-$SCRIPT_DIR/read-preship-ledger.sh}"
if [ ! -x "$READER" ] && [ -x "$HOME/.claude/skills/gstack/bin/gstack-review-read" ]; then
  READER="$HOME/.claude/skills/gstack/bin/gstack-review-read"
fi
EVIDENCE_CHECKER="${MMR_LAND_EVIDENCE_CHECKER:-$SCRIPT_DIR/check-preship-evidence.sh}"

say()  { printf '%s\n' "$*"; }
die()  { printf 'land-pr: %s\n' "$*" >&2; exit 1; }

REVIEW_THREADS_LIB="$SCRIPT_DIR/review-threads.sh"
[ -r "$REVIEW_THREADS_LIB" ] || die "review-thread reader not found at $REVIEW_THREADS_LIB."
# shellcheck source=scripts/review-threads.sh
. "$REVIEW_THREADS_LIB"

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install GitHub CLI, then re-run."
command -v jq >/dev/null 2>&1 || die "jq not found. Install jq, then re-run."
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository."
# The head pin is the core safety guarantee — refuse to run on a gh too old
# to support it rather than silently landing without the pin.
gh pr merge --help 2>/dev/null | grep -- '--match-head-commit' >/dev/null \
  || die "this gh CLI does not support --match-head-commit (needs gh >= 2.49). Upgrade gh, then re-run."

[ "$#" -ge 1 ] || die "usage: scripts/land-pr.sh <pr-number> [<pr-number>...]"

# iso_to_epoch <iso8601> -> epoch seconds, or empty on failure.
# Handles both Z-suffixed UTC (BSD and GNU date) like the squad hook does.
# Empty input is rejected up front: GNU `date -d ""` silently returns
# midnight today instead of failing, which would bless missing timestamps.
iso_to_epoch() {
  local ts="$1" normalized
  [ -n "$ts" ] || return 0

  # GitHub committedDate values may include fractional seconds, such as
  # 2026-08-23T23:14:31.300Z. Strip the fraction before calling date.
  normalized="$(printf '%s' "$ts" | sed -E 's/\.[0-9]+Z$/Z/')" || return 0
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$normalized" +%s 2>/dev/null \
    || date -u -d "$normalized" +%s 2>/dev/null \
    || true
}

# squad_check <pr-head-sha> <last-push-epoch> -> 0 if a qualifying entry
# exists, else prints the reason and returns 1.
squad_check() {
  local pr_head="$1" last_push="$2" line skill rc ts es resolved
  if [ ! -x "$READER" ]; then
    return 1
  fi
  while IFS= read -r line; do
    case "$line" in ---CONFIG---*) break ;; esac
    skill="$(printf '%s' "$line" | jq -r '.skill // ""' 2>/dev/null)" || continue
    case "$skill" in review | adversarial-review) ;; *) continue ;; esac
    rc="$(printf '%s' "$line" | jq -r '.commit // ""' 2>/dev/null)"
    { [ -z "$rc" ] || [ "$rc" = "null" ]; } && continue
    ts="$(printf '%s' "$line" | jq -r '.timestamp // ""' 2>/dev/null)"
    es="$(iso_to_epoch "$ts")"
    [ -n "$es" ] || continue
    git cat-file -e "${rc}^{commit}" 2>/dev/null || continue
    resolved="$(git rev-parse "${rc}^{commit}" 2>/dev/null)" || continue
    { [ "$resolved" = "$pr_head" ] \
        || git merge-base --is-ancestor "$rc" "$pr_head" 2>/dev/null; } || continue
    # Nothing was pushed to the PR after the entry (+grace for /ship's own
    # mechanical commits). A later push means the review saw older code.
    if [ "$last_push" -gt $((es + GRACE_SECONDS)) ]; then
      continue
    fi
    return 0
  done < <("$READER" 2>/dev/null)
  if [ "${MMR_LAND_REQUIRE_LEDGER_SQUAD:-0}" = "1" ]; then
    say "land-pr: no pre-ship squad entry covers the current PR head."
    say "         Either commits were pushed after the last review, or no squad ran."
    say "         Re-run the review squad (/ship or /review) on this branch, then land again."
  fi
  return 1
}

evidence_check() {
  local target="$1" base="$2" out rc
  if [ "${MMR_LAND_SKIP_EVIDENCE_CHECK:-0}" = "1" ]; then
    return 0
  fi
  out="$(bash "$EVIDENCE_CHECKER" --v2 --target "$target" --base "$base" --mode pr 2>&1)" || rc=$?
  if [ "${rc:-0}" -ne 0 ]; then
    say "land-pr: committed v2 pre-ship evidence does not cover PR head ${target:0:12}."
    say "         Run the review squad, emit v2 evidence, commit it on the branch, then land again."
    if [ -n "$out" ]; then
      printf '%s\n' "$out" | sed 's/^/         /'
    fi
    return 1
  fi
  return 0
}

thread_check() {
  local pr="$1" slug owner repo response blocked
  if [ "${MMR_LAND_SKIP_THREAD_CHECK:-0}" = "1" ]; then
    return 0
  fi

  slug="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" \
    || {
      say "land-pr: could not resolve repository identity for bot-thread check."
      say "         Check gh auth, then re-run."
      return 1
    }
  owner="${slug%%/*}"
  repo="${slug##*/}"

  response="$(review_threads_json "$owner" "$repo" "$pr")" \
    || {
      say "land-pr: could not query review threads for PR #$pr."
      say "         Check gh auth, then re-run. Use MMR_LAND_SKIP_THREAD_CHECK=1 only for hotfix escape."
      return 1
    }

  blocked=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    blocked=$((blocked + 1))
    say "land-pr: unresolved Major/Critical bot thread: $line"
  done < <(
    printf '%s' "$response" | jq -r '
      .data.repository.pullRequest.reviewThreads.nodes[]
      | select(.isResolved == false and .isOutdated == false)
      | ([.comments.nodes[]?
          | select(.author.login == "coderabbitai"
              or .author.login == "copilot-pull-request-reviewer"
              or .author.login == "chatgpt-codex-connector")
          | select((.body // "") | test("(Major|Critical|P0|P1)"; "i"))
          | .url
        ][0] // empty)
    '
  )

  if [ "$blocked" -gt 0 ]; then
    say "land-pr: $blocked unresolved Major/Critical bot review thread(s) block landing."
    say "         Resolve each thread on GitHub or fix and push, re-review, then land again."
    return 1
  fi
  return 0
}

verify_head() {
  local pr="$1" head="$2" base="$3" last_push_epoch="$4"
  evidence_check "$head" "$base" || return 1
  if squad_check "$head" "$last_push_epoch"; then
    :
  elif [ "${MMR_LAND_SKIP_EVIDENCE_CHECK:-0}" = "1" ]; then
    say "land-pr: committed evidence was skipped and no qualifying local ledger entry covers ${head:0:12}."
    say "         Re-run without MMR_LAND_SKIP_EVIDENCE_CHECK=1 or provide current ledger evidence."
    return 1
  elif [ "${MMR_LAND_REQUIRE_LEDGER_SQUAD:-0}" = "1" ]; then
    return 1
  else
    say "land-pr: no local ledger squad entry; committed v2 evidence covers PR head ${head:0:12}."
  fi
  thread_check "$pr" || return 1
}

ensure_head_local() {
  local pr="$1" head="$2"
  if ! git cat-file -e "${head}^{commit}" 2>/dev/null; then
    git fetch -q origin "pull/${pr}/head" 2>/dev/null || true
  fi
  git cat-file -e "${head}^{commit}" 2>/dev/null \
    || die "PR #$pr head $head is not available locally and could not be fetched — cannot verify landing gates against it."
}

land_one() {
  local pr="$1" view state head base merge_state last_push

  case "$pr" in (*[!0-9]*|'') die "PR number must be numeric, got: $pr" ;; esac

  view="$(gh pr view "$pr" --json state,headRefOid,baseRefOid,mergeStateStatus,commits 2>/dev/null)" \
    || die "could not read PR #$pr. Check the number and your gh auth, then re-run."
  state="$(printf '%s' "$view" | jq -r '.state')"
  head="$(printf '%s' "$view" | jq -r '.headRefOid')"
  base="$(printf '%s' "$view" | jq -r '.baseRefOid')"
  merge_state="$(printf '%s' "$view" | jq -r '.mergeStateStatus')"
  last_push="$(printf '%s' "$view" | jq -r '[.commits[].committedDate] | max // empty')"

  if [ "$state" != "OPEN" ]; then
    say "land-pr: PR #$pr is $state, not open — nothing to land."
    return 1
  fi

  [ -n "$base" ] && [ "$base" != "null" ] \
    || die "PR #$pr reports no base commit — refusing to land; check the PR on GitHub."
  [ -n "$last_push" ] || die "PR #$pr reports no commits — refusing to land; check the PR on GitHub."
  local last_push_epoch
  last_push_epoch="$(iso_to_epoch "$last_push")"
  [ -n "$last_push_epoch" ] || die "could not parse the PR #$pr head commit date ($last_push)."

  if [ "$merge_state" = "DIRTY" ]; then
    say "land-pr: PR #$pr has a merge conflict with its base."
    say "         Resolve the conflict on the branch (merge origin/main into it), push, re-review, then land again."
    return 1
  fi

  if [ "$merge_state" = "BEHIND" ]; then
    say "land-pr: PR #$pr is behind its base — refusing to change the branch from the landing seat."
    say "         In the feature workspace, merge origin/main and run:"
    say "           scripts/emit-review-evidence.sh --reattest --base origin/main"
    say "         Commit and push the receipt swap, wait for CI, then land again."
    return 1
  fi

  ensure_head_local "$pr" "$head"
  verify_head "$pr" "$head" "$base" "$last_push_epoch" || return 1

  # Pin the merge to the exact head verified above. If anything pushes to the
  # branch after this, GitHub refuses the merge instead of landing unseen code.
  gh pr merge "$pr" --squash --auto --match-head-commit "$head" \
    || die "arming auto-merge for PR #$pr failed — see gh output above, fix, and re-run."
  say "land-pr: PR #$pr armed — GitHub merges it once required checks pass on head ${head:0:12}."
  say "         If the head changes before then, the merge will not fire; re-run this script."
}

rc=0
for pr in "$@"; do
  land_one "$pr" || rc=1
done
exit "$rc"
