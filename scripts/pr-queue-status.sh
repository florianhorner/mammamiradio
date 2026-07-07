#!/usr/bin/env bash
# pr-queue-status.sh - advisory dashboard for multi-PR landing sessions.
#
# This is read-only by design. It does not fetch, update branches, edit PRs,
# comment, merge, or touch worktrees. It summarizes queue state so the landing
# conductor can decide order before using scripts/land-pr.sh.
set -euo pipefail

say() { printf '%s\n' "$*"; }
die() { printf 'pr-queue-status: %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh CLI not found."
command -v jq >/dev/null 2>&1 || die "jq not found."
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository."

root="$(git rev-parse --show-toplevel)"

# Enumerate every worktree (path, local branch, upstream tracking branch)
# ONCE and reuse it for every PR lookup below, instead of re-running
# `git worktree list --porcelain` (and an upstream rev-parse per worktree)
# once per open PR. Worktree state can't change mid-run of a read-only
# script, so one pass is always correct. Fields are tab-separated.
build_worktree_index() {
  local path="" branch=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*)
        path="${line#worktree }"
        branch=""
        ;;
      "branch "*)
        branch="${line#branch }"
        ;;
      "")
        if [ -n "$path" ]; then
          local upstream=""
          upstream="$(git -C "$path" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
          printf '%s\t%s\t%s\n' "$path" "$branch" "$upstream"
        fi
        path=""
        branch=""
        ;;
    esac
  done < <(git -C "$root" worktree list --porcelain)
  # `git worktree list --porcelain` may or may not end with a trailing blank
  # line depending on git version; flush a pending record either way.
  if [ -n "$path" ]; then
    local upstream=""
    upstream="$(git -C "$path" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    printf '%s\t%s\t%s\n' "$path" "$branch" "$upstream"
  fi
}

worktree_index="$(build_worktree_index)"

worktree_for_branch() {
  local target="$1"
  local wanted="refs/heads/$target" wanted_upstream="origin/$target"
  local path branch upstream

  # Prefer an exact local branch match.
  while IFS=$'\t' read -r path branch upstream; do
    [ -n "$path" ] || continue
    if [ "$branch" = "$wanted" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done <<<"$worktree_index"

  # Some Conductor worktrees use a local suffix branch that tracks the PR
  # head under a different local name. First upstream match wins; if more
  # than one worktree somehow tracks the same branch, iteration order (as
  # returned by `git worktree list`) decides, and that's an accepted,
  # deliberately unhandled edge case for this advisory-only tool.
  while IFS=$'\t' read -r path branch upstream; do
    [ -n "$path" ] || continue
    if [ "$upstream" = "$wanted_upstream" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done <<<"$worktree_index"
}

# Prints "<status>\t<display text>" where status is the enum "clean"/"dirty".
# Keeping the enum in its own field (rather than having callers infer
# dirtiness from the free-text display string) means the wording below can
# change without silently breaking recommendation()'s control flow.
dirty_summary() {
  local path="$1" status count first
  status="$(git -C "$path" status --porcelain 2>/dev/null || true)"
  if [ -z "$status" ]; then
    printf 'clean\tclean\n'
    return 0
  fi

  count="$(printf '%s\n' "$status" | sed '/^$/d' | wc -l | tr -d ' ')"
  first="$(printf '%s\n' "$status" | sed -n '1,3p' | tr '\n' '; ' | sed 's/[; ]*$//')"
  printf 'dirty\tdirty (%s file(s): %s)\n' "$count" "$first"
}

local_base_summary() {
  local path="$1"
  if ! git -C "$path" show-ref --verify --quiet refs/remotes/origin/main; then
    say "local origin/main unavailable"
    return 0
  fi
  if git -C "$path" merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
    say "contains local origin/main"
  else
    say "does not contain local origin/main"
  fi
}

recommendation() {
  local is_draft="$1" merge_state="$2" worktree="$3" dirty_status="$4"
  if [ "$is_draft" = "true" ]; then
    say "draft"
  elif [ "$merge_state" = "DIRTY" ]; then
    say "conflict/manual"
  elif [ -n "$worktree" ] && [ "$dirty_status" = "dirty" ]; then
    say "commit dirty work"
  elif [ -z "$worktree" ]; then
    say "inspect/no local worktree"
  elif [ "$merge_state" = "BEHIND" ]; then
    say "update + test"
  elif [ "$merge_state" = "CLEAN" ]; then
    say "land now"
  elif [ "$merge_state" = "BLOCKED" ] || [ "$merge_state" = "UNSTABLE" ]; then
    say "wait/checks"
  else
    say "inspect ($merge_state)"
  fi
}

prs="$(gh pr list --state open --json number,title,headRefName,headRefOid,mergeStateStatus,isDraft,updatedAt,url 2>/dev/null)" \
  || die "could not list open PRs. Check gh auth and repository context."

count="$(printf '%s' "$prs" | jq 'length')"
say "pr-queue-status: open PRs: $count"
say "pr-queue-status: advisory only; scripts/land-pr.sh remains the landing gate."

if [ "$count" -eq 0 ]; then
  exit 0
fi

printf '%s' "$prs" | jq -e 'type == "array"' >/dev/null 2>&1 || die "gh returned invalid PR JSON."

printf '%s' "$prs" | jq -c 'sort_by(.number)[]' | while IFS= read -r pr; do
  number="$(printf '%s' "$pr" | jq -r '.number')"
  title="$(printf '%s' "$pr" | jq -r '.title')"
  branch="$(printf '%s' "$pr" | jq -r '.headRefName')"
  head="$(printf '%s' "$pr" | jq -r '.headRefOid')"
  merge_state="$(printf '%s' "$pr" | jq -r '.mergeStateStatus')"
  is_draft="$(printf '%s' "$pr" | jq -r '.isDraft')"
  url="$(printf '%s' "$pr" | jq -r '.url')"
  updated_at="$(printf '%s' "$pr" | jq -r '.updatedAt')"
  short_head="${head:0:12}"

  wt="$(worktree_for_branch "$branch" || true)"
  dirty_status="n/a"
  dirty="n/a"
  local_base="n/a"
  if [ -n "$wt" ]; then
    IFS=$'\t' read -r dirty_status dirty <<<"$(dirty_summary "$wt")"
    local_base="$(local_base_summary "$wt")"
  fi
  rec="$(recommendation "$is_draft" "$merge_state" "$wt" "$dirty_status")"

  say ""
  say "PR #$number: $title"
  say "  branch: $branch"
  say "  head: $short_head"
  say "  merge: $merge_state"
  say "  draft: $is_draft"
  say "  updated: $updated_at"
  say "  url: $url"
  if [ -n "$wt" ]; then
    say "  worktree: $wt"
    say "  local: $dirty; $local_base"
  else
    say "  worktree: not found"
  fi
  say "  recommendation: $rec"
done
