#!/usr/bin/env bash
# Update Dependabot auto-merge from the advertised-version check.
# The workflow supplies GH_REPO and verified UPDATE_TYPE for PR-event arming.
# The sweep only disarms. Tests mock GitHub in
# tests/workflows/test_dependabot_automerge_gate.sh.
set -euo pipefail

HOLD_LABEL=cut-window-hold
usage() {
  echo "usage: $0 arm <pr> <metadata-head> <checked-main> | disarm <pr> | sweep <pass|fail|unknown>" >&2
  exit 2
}

# Read current PR state before changing auto-merge; list/event data can be stale.
load_pr() {
  local snapshot
  snapshot="$(gh pr view "$1" --json state,author,baseRefName,isDraft,headRefOid,autoMergeRequest,labels \
    --jq '[.state, .author.login, .baseRefName, .isDraft, .headRefOid, (.autoMergeRequest != null), ((.labels | map(.name) | index("cut-window-hold")) != null)] | @tsv')" || return 1
  IFS=$'\t' read -r pr_state pr_author pr_base pr_draft pr_head pr_armed pr_hold <<< "$snapshot"
  case "$pr_armed/$pr_hold" in
    true/true|true/false|false/true|false/false) ;;
    *) echo "::error::Invalid PR state for #$1"; return 1 ;;
  esac
}

eligible_pr() {
  [ "$pr_state" = OPEN ] && [ "$pr_author" = app/dependabot ] && [ "$pr_base" = main ]
}

ensure_label() {
  local exists
  if ! gh label create "$HOLD_LABEL" --color B60205 \
    --description "Auto-merge paused while main advertises an unpublished add-on version" >/dev/null 2>&1; then
    exists="$(gh label list --search "$HOLD_LABEL" --json name \
      --jq 'map(.name) | index("cut-window-hold") != null')" || return 1
    [ "$exists" = true ] || return 1
  fi
}

disarm_pr() {
  local num="$1"
  load_pr "$num" || { echo "::error::Could not read PR #$num"; return 1; }
  if ! eligible_pr || [ "$pr_armed" = false ]; then
    echo "::notice::#$num is not an armed open Dependabot PR targeting main; nothing to disarm."
    return 0
  fi
  if ! gh pr merge --disable-auto "$num"; then
    echo "::error::Could not disarm auto-merge on #$num"
    return 1
  fi
  # Explicit checks are required here: sweep calls this function in an OR list,
  # which disables Bash errexit inside the entire function.
  if ! ensure_label || ! gh pr edit "$num" --add-label "$HOLD_LABEL" >/dev/null; then
    echo "::error::#$num was disarmed, but recording its cut-window hold failed."
    return 1
  fi
  echo "::warning title=Cut window open::#$num is disarmed and labelled $HOLD_LABEL. After publication, use a fresh Dependabot PR event or the landing workflow."
}

arm_pr() {
  local num="$1" expected_head="$2" checked_main="$3" current_main
  case "${UPDATE_TYPE:-}" in
    version-update:semver-patch|version-update:semver-minor) ;;
    *) echo "::notice::No verified patch/minor metadata; #$num stays unchanged."; return 0 ;;
  esac
  load_pr "$num" || { echo "::error::Could not read PR #$num"; return 1; }
  if ! eligible_pr || [ "$pr_draft" != false ] || [ "$pr_head" != "$expected_head" ]; then
    echo "::notice::#$num no longer matches the eligible metadata event; skipping auto-merge."
    return 0
  fi
  current_main="$(gh api "repos/$GH_REPO/git/ref/heads/main" --jq .object.sha)" || return 1
  if [ -z "$checked_main" ] || [ "$current_main" != "$checked_main" ]; then
    echo "::notice::main changed since the registry probe; skipping auto-merge on #$num."
    return 0
  fi
  gh pr merge --squash --auto --match-head-commit "$expected_head" "$num" || return 1
  if [ "$pr_hold" = true ]; then
    gh pr edit "$num" --remove-label "$HOLD_LABEL" >/dev/null || return 1
  fi
}

sweep() {
  local verdict="$1" numbers status=0 num
  case "$verdict" in
    pass)
      echo "::notice::Cut window closed. The sweep never re-arms PRs. Resume held PRs via a fresh metadata-backed PR event or the landing workflow."
      return 0 ;;
    unknown)
      echo "::notice::Registry verdict unknown; auto-merge state left unchanged."
      return 0 ;;
    fail) ;;
    *) usage ;;
  esac
  # Read all pages so the sweep reaches PRs beyond the first 100.
  numbers="$(gh api --paginate "repos/$GH_REPO/pulls?state=open&base=main&per_page=100" \
    --jq '.[] | select(.user.login == "dependabot[bot]") | .number')" || return 1
  while IFS= read -r num; do
    [ -n "$num" ] || continue
    disarm_pr "$num" || status=1
  done <<< "$numbers"
  return "$status"
}

[ "${GH_REPO:-}" = florianhorner/mammamiradio ] || {
  echo "::error::GH_REPO must name the owned florianhorner/mammamiradio repository."
  exit 2
}
case "${1:-}" in
  arm) [ "$#" -eq 4 ] || usage; arm_pr "$2" "$3" "$4" ;;
  disarm) [ "$#" -eq 2 ] || usage; disarm_pr "$2" ;;
  sweep) [ "$#" -eq 2 ] || usage; sweep "$2" ;;
  *) usage ;;
esac
