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
#      workspace to integrate main, re-review changed code, and push;
#   2. blocks unresolved Major/Critical bot review threads on the PR head;
#   3. checks release-cut admission and arms GitHub auto-merge pinned to the exact head it verified:
#      gh pr merge --squash --auto --delete-branch --match-head-commit <sha>.
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

say()  { printf '%s\n' "$*"; }
die()  { printf 'land-pr: %s\n' "$*" >&2; exit 1; }

# The bot-thread predicate and shared Git helpers live in
# scripts/land-gates.sh so this wrapper and the shadow land queue
# (scripts/land-queue-plan.sh) reach the same verdict from one implementation.
LAND_GATES_LIB="$SCRIPT_DIR/land-gates.sh"
[ -r "$LAND_GATES_LIB" ] || die "landing gate library not found at $LAND_GATES_LIB."
LAND_GATES_LABEL="land-pr"
# shellcheck source=scripts/land-gates.sh
. "$LAND_GATES_LIB"

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install GitHub CLI, then re-run."
command -v jq >/dev/null 2>&1 || die "jq not found. Install jq, then re-run."
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository."
LAND_REPO="$(_repo_slug)" || die "could not resolve repository identity; check gh auth, then re-run."
export GH_REPO="$LAND_REPO"
# The head pin is the core safety guarantee — refuse to run on a gh too old
# to support it rather than silently landing without the pin.
# Both the probe and merge carry the safety guard's repository/cleanup flags.
merge_help="$(gh pr merge --help --delete-branch --repo "$LAND_REPO")" \
  || die "could not inspect gh merge options; check the error above, then re-run."
printf '%s\n' "$merge_help" | grep -- '--match-head-commit' >/dev/null \
  || die "this gh CLI does not support --match-head-commit (needs gh >= 2.49). Upgrade gh, then re-run."

[ "$#" -ge 1 ] || die "usage: scripts/land-pr.sh <pr-number> [<pr-number>...]"

land_one() {
  local pr="$1" view state head base merge_state

  case "$pr" in (*[!0-9]*|'') die "PR number must be numeric, got: $pr" ;; esac

  view="$(gh pr view "$pr" --json state,headRefOid,baseRefOid,mergeStateStatus 2>/dev/null)" \
    || die "could not read PR #$pr. Check the number and your gh auth, then re-run."
  state="$(printf '%s' "$view" | jq -r '.state')"
  head="$(printf '%s' "$view" | jq -r '.headRefOid')"
  base="$(printf '%s' "$view" | jq -r '.baseRefOid')"
  merge_state="$(printf '%s' "$view" | jq -r '.mergeStateStatus')"

  if [ "$state" != "OPEN" ]; then
    say "land-pr: PR #$pr is $state, not open — nothing to land."
    return 1
  fi

  [ -n "$base" ] && [ "$base" != "null" ] \
    || die "PR #$pr reports no base commit — refusing to land; check the PR on GitHub."
  if [ "$merge_state" = "DIRTY" ]; then
    say "land-pr: PR #$pr has a merge conflict with its base."
    say "         Resolve the conflict on the branch (merge origin/main into it), push, re-review, then land again."
    return 1
  fi

  if [ "$merge_state" = "BEHIND" ]; then
    say "land-pr: PR #$pr is behind its base — refusing to change the branch from the landing seat."
    say "         In the feature workspace: git merge origin/main && git push."
    say "         Wait for CI, then land again. Re-review conflicted or hand-edited changes."
    return 1
  fi

  # The base is only trusted if it is landed in origin/main; a stale local ref
  # would refuse GitHub's real base. Refresh only if the local ref does not
  # already cover it — a seat with complete history never fetches.
  refresh_landed_ref "$base"
  ensure_head_local "$pr" "$head" \
    || die "PR #$pr head $head is not available locally and could not be fetched — cannot verify landing gates against it."
  if ! git cat-file -e "${base}^{commit}" 2>/dev/null; then
    git fetch -q --no-tags origin "$base" 2>/dev/null \
      || die "PR #$pr base $base could not be fetched. Check origin and retry."
  fi
  git cat-file -e "${base}^{commit}" 2>/dev/null \
    || die "PR #$pr base $base is unavailable locally; cannot verify landing gates."
  verify_head "$pr" || return 1

  # A stable-version change must not race an earlier Dependabot arming run.
  # This is read-only admission; freeze/thaw remain explicit release actions.
  GH_REPO="$LAND_REPO" bash "$SCRIPT_DIR/dependabot-window-hold.sh" check-cut "$base" "$head" || return 1

  # Pin the merge to the exact head verified above. If anything pushes to the
  # branch after this, GitHub refuses the merge instead of landing unseen code.
  gh pr merge "$pr" --squash --auto --match-head-commit "$head" --delete-branch --repo "$LAND_REPO" \
    || die "arming auto-merge for PR #$pr failed — see gh output above, fix, and re-run."
  say "land-pr: PR #$pr armed — GitHub merges it once required checks pass on head ${head:0:12}."
  say "         If the head changes before then, the merge will not fire; re-run this script."
}

rc=0
for pr in "$@"; do
  land_one "$pr" || rc=1
done
exit "$rc"
