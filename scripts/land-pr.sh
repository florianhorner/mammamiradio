#!/usr/bin/env bash
# land-pr.sh — the only legitimate merge path for this repo.
#
#   scripts/land-pr.sh <pr-number> [<pr-number>...]
#
# Landing contract (CLAUDE.md "Quality gates" → "Landing contract"): PRs are
# opened by /ship and never armed for auto-merge. On the operator's explicit
# merge signal, this wrapper:
#
#   1. verifies a pre-ship squad entry that is still about THIS code
#      (code-state freshness: the entry's commit must be the PR head or an
#      ancestor of it, and no commits may have been pushed to the PR after
#      the entry — wall-clock age is irrelevant, a soak of days is fine);
#   2. updates the branch from base if it is behind (user-auth gh, so CI
#      retriggers normally; a conflict stops here for a human);
#   3. arms GitHub auto-merge pinned to the exact head it verified:
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
# Multiple PR numbers are processed sequentially: land #1, update #2, land #2.
#
# For multi-PR/coordinator landing sessions, scripts/pr-queue-status.sh is an
# optional read-only preflight that summarizes open-PR/worktree state before
# you decide order here. It is advisory only, not a gate.
set -euo pipefail

# Freshness grace: /ship pushes mechanical commits (version bump, changelog)
# right after the squad logs its entry; commits within this window after the
# entry are treated as part of the reviewed push, not new work.
GRACE_SECONDS="${MMR_LAND_GRACE_SECONDS:-600}"
UPDATE_TIMEOUT_SECONDS="${MMR_LAND_UPDATE_TIMEOUT:-120}"
# Reader override exists for tests only; defaults to the real gstack log.
READER="${MMR_LAND_REVIEW_READER:-$HOME/.claude/skills/gstack/bin/gstack-review-read}"

say()  { printf '%s\n' "$*"; }
die()  { printf 'land-pr: %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install GitHub CLI, then re-run."
command -v jq >/dev/null 2>&1 || die "jq not found. Install jq, then re-run."
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository."
# The head pin is the core safety guarantee — refuse to run on a gh too old
# to support it rather than silently landing without the pin.
gh pr merge --help 2>/dev/null | grep -q -- '--match-head-commit' \
  || die "this gh CLI does not support --match-head-commit (needs gh >= 2.49). Upgrade gh, then re-run."

[ "$#" -ge 1 ] || die "usage: scripts/land-pr.sh <pr-number> [<pr-number>...]"

# iso_to_epoch <iso8601> -> epoch seconds, or empty on failure.
# Handles both Z-suffixed UTC (BSD and GNU date) like the squad hook does.
# Empty input is rejected up front: GNU `date -d ""` silently returns
# midnight today instead of failing, which would bless missing timestamps.
iso_to_epoch() {
  local ts="$1"
  [ -n "$ts" ] || return 0
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null \
    || date -u -d "$ts" +%s 2>/dev/null \
    || true
}

# squad_check <pr-head-sha> <last-push-epoch> -> 0 if a qualifying entry
# exists, else prints the reason and returns 1.
squad_check() {
  local pr_head="$1" last_push="$2" line skill rc ts es
  if [ ! -x "$READER" ]; then
    say "land-pr: no review log reader at $READER — cannot verify the pre-ship squad."
    say "         Run /ship (it logs the squad), or fix the gstack install, then re-run."
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
    # The entry must be about this code: its commit is the PR head or an
    # ancestor of it...
    git cat-file -e "${rc}^{commit}" 2>/dev/null || continue
    { [ "$(git rev-parse "${rc}^{commit}" 2>/dev/null)" = "$pr_head" ] \
        || git merge-base --is-ancestor "$rc" "$pr_head" 2>/dev/null; } || continue
    # ...and nothing was pushed to the PR after the entry (+grace for /ship's
    # own mechanical commits). A later push means the review saw older code.
    if [ "$last_push" -gt $((es + GRACE_SECONDS)) ]; then
      continue
    fi
    return 0
  done < <("$READER" 2>/dev/null)
  say "land-pr: no pre-ship squad entry covers the current PR head."
  say "         Either commits were pushed after the last review, or no squad ran."
  say "         Re-run the review squad (/ship or /review) on this branch, then land again."
  return 1
}

land_one() {
  local pr="$1" view state head merge_state last_push new_head waited

  case "$pr" in (*[!0-9]*|'') die "PR number must be numeric, got: $pr" ;; esac

  view="$(gh pr view "$pr" --json state,headRefOid,mergeStateStatus,commits 2>/dev/null)" \
    || die "could not read PR #$pr. Check the number and your gh auth, then re-run."
  state="$(printf '%s' "$view" | jq -r '.state')"
  head="$(printf '%s' "$view" | jq -r '.headRefOid')"
  merge_state="$(printf '%s' "$view" | jq -r '.mergeStateStatus')"
  last_push="$(printf '%s' "$view" | jq -r '[.commits[].committedDate] | max // empty')"

  if [ "$state" != "OPEN" ]; then
    say "land-pr: PR #$pr is $state, not open — nothing to land."
    return 1
  fi

  [ -n "$last_push" ] || die "PR #$pr reports no commits — refusing to land; check the PR on GitHub."
  local last_push_epoch
  last_push_epoch="$(iso_to_epoch "$last_push")"
  [ -n "$last_push_epoch" ] || die "could not parse the PR #$pr head commit date ($last_push)."

  # Make sure the PR head commit exists locally for the ancestor check; in a
  # Conductor worktree the PR branch is usually checked out already.
  if ! git cat-file -e "${head}^{commit}" 2>/dev/null; then
    git fetch -q origin "pull/${pr}/head" 2>/dev/null || true
  fi
  git cat-file -e "${head}^{commit}" 2>/dev/null \
    || die "PR #$pr head $head is not available locally and could not be fetched — cannot verify the squad entry against it."

  squad_check "$head" "$last_push_epoch" || return 1

  if [ "$merge_state" = "DIRTY" ]; then
    say "land-pr: PR #$pr has a merge conflict with its base."
    say "         Resolve the conflict on the branch (merge origin/main into it), push, re-review, then land again."
    return 1
  fi

  if [ "$merge_state" = "BEHIND" ]; then
    say "land-pr: PR #$pr is behind its base — updating the branch (CI will re-run)..."
    if ! gh pr update-branch "$pr" 2>/dev/null; then
      say "land-pr: could not update PR #$pr from its base (likely a conflict)."
      say "         Resolve on the branch, push, re-review, then land again."
      return 1
    fi
    # Wait for the update commit to appear so the head we pin is the updated
    # one. The ONLY way past this block is a confirmed new head — falling
    # through on timeout would arm the merge pinned to the pre-update head,
    # which GitHub would then silently never fire.
    waited=0
    new_head=""
    while [ "$waited" -lt "$UPDATE_TIMEOUT_SECONDS" ]; do
      new_head="$(gh pr view "$pr" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
      if [ -n "$new_head" ] && [ "$new_head" != "$head" ]; then
        break
      fi
      new_head=""
      sleep 3; waited=$((waited + 3))
    done
    if [ -z "$new_head" ]; then
      die "PR #$pr branch update did not surface a new head within ${UPDATE_TIMEOUT_SECONDS}s — check the PR on GitHub, then re-run."
    fi
    head="$new_head"
  fi

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
