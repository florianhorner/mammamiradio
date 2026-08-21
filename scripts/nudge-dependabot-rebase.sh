#!/usr/bin/env bash
# nudge-dependabot-rebase.sh — keep the merge queue landing under strict
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
# Mergeability is asked for, not assumed. GitHub computes `mergeStateStatus`
# lazily: for a while after a push to the base branch it answers UNKNOWN for
# every open PR. This workflow runs `on: push` to main, so it asks in exactly
# that window — and a filter for BEHIND against an UNKNOWN answer matches
# nothing and reports "nothing to do" while the queue is in fact deadlocked.
# On 2026-08-20 that is what happened: run 32342094107 logged "no behind
# Dependabot PRs" 2s after the push while four were behind, two of which had
# already been parked since 2026-08-17. So: re-ask until GitHub commits to an
# answer, and treat a still-UNKNOWN reply as unknown rather than as "fine".
#
# ONE list call serves both audiences. Fetching per-author would pay the
# UNKNOWN wait twice and, worse, let a Dependabot PR appear in both the
# nudge set and the human-escalation set — nudged and escalated at once.
# Partitioning one reply on `.author.login` makes the two sets disjoint by
# construction.
#
# Human-authored PRs deadlock the same way but cannot be fixed from here: a
# `gh pr update-branch` under GITHUB_TOKEN moves the head WITHOUT retriggering
# CI, leaving the PR waiting on checks that will never report — worse than
# behind. They are surfaced for a human instead of silently skipped, because a
# silent skip is what let #990 sit green, armed and parked.
#
# Note `mergeStateStatus` is a single enum, so a PR that is behind AND has a
# failing or pending required check reports BLOCKED, not BEHIND, and is not
# reported here. That is deliberate: such a PR is not merely stale, and
# telling a human to update its branch would not land it either.
#
# Hostile-input hygiene: only PR numbers (numeric) and ISO timestamps from
# the GitHub API are consumed; no PR titles/branch names/labels ever reach
# the shell. Failures are non-fatal per PR — a broken nudge must never fail
# the main-branch workflow run loudly enough to look like a build problem.
# Every exit is 0 by design.
set -euo pipefail

NUDGE_BODY="@dependabot rebase"
BOT_LOGIN="app/dependabot"

# How hard to press GitHub for a computed mergeability answer. Overridable so
# the self-test does not sleep. Non-numeric or zero input falls back to the
# default rather than disabling the loop: `for ((try=1; try<=abc; ...))` would
# silently never run, turning a typo into a dead mechanism.
MERGE_STATE_TRIES="${NUDGE_MERGE_STATE_TRIES:-6}"
MERGE_STATE_DELAY="${NUDGE_MERGE_STATE_DELAY:-10}"
case "$MERGE_STATE_TRIES" in ''|*[!0-9]*) MERGE_STATE_TRIES=6 ;; esac
[ "$MERGE_STATE_TRIES" -ge 1 ] 2>/dev/null || MERGE_STATE_TRIES=1
case "$MERGE_STATE_DELAY" in ''|*[!0-9]*) MERGE_STATE_DELAY=10 ;; esac

# `gh pr list` defaults to 30. A silent truncation on a mechanism whose whole
# job is to stop being silent would be the same bug one level down.
LIST_LIMIT="${NUDGE_LIST_LIMIT:-100}"
case "$LIST_LIMIT" in ''|*[!0-9]*) LIST_LIMIT=100 ;; esac

command -v gh >/dev/null 2>&1 || { echo "nudge: gh CLI not found — skipping."; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "nudge: jq not found — skipping."; exit 0; }

# Empty input is rejected up front: GNU `date -d ""` silently returns
# midnight today instead of failing.
iso_to_epoch() {
  local ts="$1"
  [ -n "$ts" ] || return 0
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null \
    || date -u -d "$ts" +%s 2>/dev/null \
    || true
}

# settle_open_prs -> sets ALL_JSON to one reply whose mergeStateStatus values
# are as computed as we are willing to wait for. Returns non-zero only when gh
# itself fails; a permanently UNKNOWN reply is returned as-is so the caller
# degrades to "nothing to do" instead of hanging or guessing.
ALL_JSON=""
settle_open_prs() {
  local try
  for ((try = 1; try <= MERGE_STATE_TRIES; try++)); do
    ALL_JSON="$(gh pr list --state open --limit "$LIST_LIMIT" \
      --json number,mergeStateStatus,autoMergeRequest,author 2>/dev/null)" || return 1
    printf '%s' "$ALL_JSON" \
      | jq -e 'any(.[]; .mergeStateStatus == "UNKNOWN")' >/dev/null 2>&1 || return 0
    if [ "$try" -lt "$MERGE_STATE_TRIES" ]; then
      sleep "$MERGE_STATE_DELAY"
    fi
  done
  echo "nudge: GitHub still reports UNKNOWN mergeability after $MERGE_STATE_TRIES tries." >&2
  return 0
}

# numbers_matching <jq-select-body> -> space-free newline list of PR numbers.
# `select(type == "number")` keeps a non-numeric `number` from ever reaching
# the shell, whatever the API returns.
numbers_matching() {
  printf '%s' "$ALL_JSON" | jq -r --arg bot "$BOT_LOGIN" "
    [.[] | select($1) | .number] | map(select(type == \"number\") | tostring) | .[]" 2>/dev/null || true
}

# A human-authored PR with auto-merge armed and a behind branch is deadlocked
# and only a human can clear it (see the header note on GITHUB_TOKEN). Name it.
# Everything here is best-effort: this runs at the very end of the happy path,
# and a reporting bug must never turn a successful nudge run into a red step.
report_stuck_human_prs() {
  local stuck="" n
  # `numbers_matching` already drops non-numeric ids, but this is the path that
  # interpolates them into operator-facing text — a warning line and a step
  # summary telling a human what to run. It does not inherit that guarantee on
  # trust; it re-checks with the same filter the nudge loop uses, so a single
  # odd id can never become three bogus "run land-pr.sh on this" rows.
  # shellcheck disable=SC2016  # $bot is a jq variable (--arg bot), not a shell one
  while IFS= read -r n; do
    case "$n" in (*[!0-9]*|'') continue ;; esac
    stuck="${stuck:+$stuck }$n"
  done <<< "$(numbers_matching '.author.login != $bot and .mergeStateStatus == "BEHIND" and .autoMergeRequest != null')"
  [ -n "$stuck" ] || return 0
  echo "nudge: armed but behind, needs a human landing run: $stuck"
  echo "::warning title=PRs stuck behind main::Auto-merge is armed on #${stuck// /, #} but the branch is behind and strict status checks block the merge. Land with scripts/land-pr.sh, which updates the branch after verifying pre-ship evidence."
  # Probe appendability rather than relying on `2>/dev/null` on the write: a
  # redirect that cannot be opened is reported by the shell as it sets the
  # redirect up, so the group's own stderr redirection never sees that message.
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ] && { : >> "$GITHUB_STEP_SUMMARY"; } 2>/dev/null; then
    {
      echo "### Armed but behind"
      echo
      echo "Auto-merge is armed, but GitHub never updates a behind branch and strict"
      echo "status checks require one. These will not land on their own:"
      echo
      for n in $stuck; do echo "- #$n — \`scripts/land-pr.sh $n\`"; done
    } >> "$GITHUB_STEP_SUMMARY" 2>/dev/null
  fi
}

settle_open_prs || {
  echo "nudge: could not list open PRs — skipping."
  exit 0
}

# shellcheck disable=SC2016  # $bot is a jq variable (--arg bot), not a shell one
prs="$(numbers_matching '.author.login == $bot and .mergeStateStatus == "BEHIND"')"

if [ -z "$prs" ]; then
  echo "nudge: no behind Dependabot PRs — nothing to do."
  report_stuck_human_prs || true
  exit 0
fi

nudged=0
while IFS= read -r pr; do
  case "$pr" in (*[!0-9]*|'') continue ;; esac

  detail="$(gh pr view "$pr" --json commits,comments 2>/dev/null)" || {
    echo "nudge: could not read PR #$pr — skipping it."
    continue
  }
  last_commit_ts="$(printf '%s' "$detail" | jq -r '[.commits[].committedDate] | max // empty' 2>/dev/null)" || last_commit_ts=""
  last_nudge_ts="$(printf '%s' "$detail" | jq -r --arg body "$NUDGE_BODY" \
    '[.comments[] | select(.body == $body) | .createdAt] | max // empty' 2>/dev/null)" || last_nudge_ts=""

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

report_stuck_human_prs || true

echo "nudge: done ($nudged PR(s) nudged)."
