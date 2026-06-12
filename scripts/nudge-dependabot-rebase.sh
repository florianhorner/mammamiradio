#!/usr/bin/env bash
# nudge-dependabot-rebase.sh — keep Dependabot batches landing under strict
# branch protection.
#
# Branch protection on main requires branches to be up to date before merging
# (strict status checks, 2026-06-12). Dependabot only rebases PRs that have
# CONFLICTS; a merely-behind PR is never updated, so its armed auto-merge
# (dependabot-automerge.yml) deadlocks: it cannot merge (not up to date) and
# will not rebase (no conflict). github/docs#42298.
#
# Fix: after every push to main, comment "@dependabot rebase" on open,
# behind, dependabot-authored PRs. Dependabot performs the rebase itself and
# ITS push retriggers CI normally — unlike a GITHUB_TOKEN push, which would
# not. Comment-only permissions; the weakest write scope that works.
#
# Idempotent: a PR is skipped when a nudge comment already exists that is
# newer than the newest commit on the PR (the previous nudge has not been
# acted on yet — re-commenting would only spam).
#
# Hostile-input hygiene: only PR numbers (numeric) and ISO timestamps from
# the GitHub API are consumed; no PR titles/branch names/labels ever reach
# the shell. Failures are non-fatal per PR — a broken nudge must never fail
# the main-branch workflow run loudly enough to look like a build problem.
set -euo pipefail

NUDGE_BODY="@dependabot rebase"

command -v gh >/dev/null 2>&1 || { echo "nudge: gh CLI not found — skipping."; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "nudge: jq not found — skipping."; exit 0; }

iso_to_epoch() {
  local ts="$1"
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null \
    || date -u -d "$ts" +%s 2>/dev/null \
    || true
}

prs="$(gh pr list --author 'app/dependabot' --state open \
        --json number,mergeStateStatus --jq '.[] | select(.mergeStateStatus == "BEHIND") | .number' 2>/dev/null)" || {
  echo "nudge: could not list Dependabot PRs — skipping."
  exit 0
}

[ -n "$prs" ] || { echo "nudge: no behind Dependabot PRs — nothing to do."; exit 0; }

nudged=0
while IFS= read -r pr; do
  case "$pr" in (*[!0-9]*|'') continue ;; esac

  detail="$(gh pr view "$pr" --json commits,comments 2>/dev/null)" || {
    echo "nudge: could not read PR #$pr — skipping it."
    continue
  }
  last_commit_ts="$(printf '%s' "$detail" | jq -r '[.commits[].committedDate] | max // empty')"
  last_nudge_ts="$(printf '%s' "$detail" | jq -r --arg body "$NUDGE_BODY" \
    '[.comments[] | select(.body == $body) | .createdAt] | max // empty')"

  if [ -n "$last_nudge_ts" ] && [ -n "$last_commit_ts" ]; then
    nudge_epoch="$(iso_to_epoch "$last_nudge_ts")"
    commit_epoch="$(iso_to_epoch "$last_commit_ts")"
    if [ -n "$nudge_epoch" ] && [ -n "$commit_epoch" ] && [ "$nudge_epoch" -gt "$commit_epoch" ]; then
      echo "nudge: PR #$pr already has an un-actioned nudge — skipping."
      continue
    fi
  fi

  if gh pr comment "$pr" --body "$NUDGE_BODY" >/dev/null 2>&1; then
    echo "nudge: asked Dependabot to rebase PR #$pr."
    nudged=$((nudged + 1))
  else
    echo "nudge: comment on PR #$pr failed — skipping it."
  fi
done <<< "$prs"

echo "nudge: done ($nudged PR(s) nudged)."
