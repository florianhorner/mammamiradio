#!/usr/bin/env bash
# Update Dependabot auto-merge from the advertised-version check.
# The workflow supplies GH_REPO and verified UPDATE_TYPE for PR-event arming.
# The sweep only disarms. Tests mock GitHub in
# tests/workflows/test_dependabot_automerge_gate.sh.
set -euo pipefail

HOLD_LABEL=cut-window-hold
WORKFLOW=dependabot-automerge.yml
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage() {
  echo "usage: $0 arm <pr> <metadata-head> <checked-main> | disarm <pr> | sweep <pass|fail|unknown> | freeze [timeout-seconds] | check-freeze | check-cut <base> <head> | thaw <release-run-id>" >&2
  exit 2
}

# Read current PR state before changing auto-merge; list/event data can be stale.
load_pr() {
  local snapshot field
  snapshot="$(gh pr view "$1" --json state,author,baseRefName,isDraft,headRefOid,autoMergeRequest,labels \
    --jq '[.state, .author.login, .baseRefName, .isDraft, .headRefOid, (.autoMergeRequest != null), ((.labels | map(.name) | index("cut-window-hold")) != null)] | @tsv')" || return 1
  IFS=$'\t' read -r pr_state pr_author pr_base pr_draft pr_head pr_armed pr_hold <<< "$snapshot"
  case "$pr_state/$pr_draft" in
    OPEN/true|OPEN/false|CLOSED/true|CLOSED/false|MERGED/true|MERGED/false) ;;
    *) echo "::error::Invalid PR status for #$1"; return 1 ;;
  esac
  for field in "$pr_author" "$pr_base" "$pr_head"; do
    [ -n "$field" ] && [ "$field" != null ] || return 1
  done
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
  # This read stops queued events during the pause. The cut-side drain is what
  # closes the race with a run that already passed this check.
  local workflow
  workflow="$(workflow_state)" || return 1
  if [ "$workflow" != active ]; then
    echo "::notice::Dependabot workflow is paused; #$num stays unchanged."
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

workflow_state() {
  local state
  state="$(gh api "repos/$GH_REPO/actions/workflows/$WORKFLOW" --jq .state)" || return 1
  case "$state" in
    active|disabled_manually|disabled_inactivity|disabled_fork|deleted) printf '%s\n' "$state" ;;
    *) echo "::error::Could not verify the Dependabot workflow state." >&2; return 1 ;;
  esac
}

require_disabled() {
  local state
  state="$(workflow_state)" || return 1
  [ "$state" = disabled_manually ] || {
    echo "::error::Freeze the Dependabot workflow before landing a stable-version change." >&2
    return 1
  }
}

active_run_count() {
  local pages
  # No status/branch/event filter: requested, waiting and pending runs also
  # carry future write authority. Read all pages, including old queued runs.
  pages="$(gh api --paginate --slurp "repos/$GH_REPO/actions/workflows/$WORKFLOW/runs?per_page=100")" || return 1
  jq -er '
    if type != "array" or length == 0 or
       (all(.[]; (.workflow_runs | type) == "array" and (.total_count | type) == "number") | not)
    then error("invalid workflow-run pages") else . end
    | .[0].total_count as $total | [.[].workflow_runs[]]
    | if length != $total or
         (all(.[]; (.id | type) == "number" and (.status | type) == "string" and .status != "") | not)
      then error("incomplete workflow-run listing") else . end
    | map(select(.status != "completed")) | length
  ' <<< "$pages"
}

open_pr_numbers() {
  local pages
  pages="$(gh api --paginate --slurp "repos/$GH_REPO/pulls?state=open&base=main&per_page=100")" || return 1
  jq -r '
    if type != "array" or length == 0 or
       (all(.[]; type == "array" and all(.[]; (.number | type) == "number" and .number > 0)) | not)
    then error("invalid pull-request pages") else .[][] | .number end
  ' <<< "$pages"
}

require_unarmed() {
  local all_prs="${1:-false}" numbers num
  numbers="$(open_pr_numbers)" || return 1
  while IFS= read -r num; do
    [ -n "$num" ] || continue
    load_pr "$num" || return 1
    if [ "$pr_state" = OPEN ] && [ "$pr_base" = main ] && [ "$pr_armed" = true ] &&
       { [ "$all_prs" = true ] || [ "$pr_author" = app/dependabot ]; }; then
      echo "::error::PR #$num still has auto-merge armed." >&2
      return 1
    fi
  done <<< "$numbers"
}

check_freeze() {
  local active
  require_disabled || return 1
  active="$(active_run_count)" || return 1
  [ "$active" = 0 ] || { echo "::error::$active Dependabot runs have not finished." >&2; return 1; }
  require_unarmed "${1:-false}" || return 1
  require_disabled || return 1
}

freeze() {
  local timeout="${1:-300}" deadline active
  [[ "$timeout" =~ ^[0-9]{1,3}$ ]] || usage
  timeout=$((10#$timeout))
  [ "$timeout" -le 600 ] || usage
  gh workflow disable "$WORKFLOW" --repo "$GH_REPO" || {
    echo "::error::Workflow disable failed; inspect its state and do not cut." >&2
    return 1
  }
  require_disabled || return 1
  deadline=$((SECONDS + timeout))
  while :; do
    active="$(active_run_count)" || return 1
    [ "$active" != 0 ] || break
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "::error::Timed out waiting for $active Dependabot runs. The workflow stays disabled; do not cut." >&2
      return 1
    fi
    echo "::notice::Waiting for $active Dependabot runs before disarming PRs."
    sleep 5 || return 1
    require_disabled || return 1
  done
  # A run that passed the arm check may finish while draining. Disarm AFTER it.
  sweep fail || return 1
  check_freeze || return 1
  echo "::notice::Dependabot is frozen: workflow disabled, no active runs, no armed PRs."
}

stable_version() {
  # Accept the repository's plain, double-quoted or single-quoted scalar;
  # duplicate, missing or malformed version fields must never bypass the gate.
  jq -Rers '
    split("\n") | map(select(startswith("version:")))
    | if length == 1 then .[0] else error("missing/duplicate stable version") end
    | sub("^version:[ \\t]*"; "") | sub("[ \\t]+#.*$"; "") | sub("[ \\t]+$"; "")
    | if startswith("\"") then fromjson
      elif startswith("\u0027") then
        if endswith("\u0027") then .[1:-1] else error("invalid version quote") end
      else . end
    | select(type == "string" and test("^[0-9]+\\.[0-9]+\\.[0-9]+$"))
  '
}

check_cut() {
  local base="$1" head="$2" before after
  before="$(git show "$base:ha-addon/mammamiradio/config.yaml" | stable_version)" || return 1
  after="$(git show "$head:ha-addon/mammamiradio/config.yaml" | stable_version)" || return 1
  [ "$before" != "$after" ] || return 0
  require_owned_repo || return 1
  check_freeze || return 1
}

thaw() {
  local run_id="$1" main_sha config version release workflow_id attempt jobs identity latest tag_sha output verdict
  [[ "$run_id" =~ ^[1-9][0-9]*$ ]] || usage
  # A cut can be armed but still waiting for CI. Do not accept the OLD release
  # while that cut (or any other human merge) is queued.
  check_freeze true || return 1
  main_sha="$(gh api "repos/$GH_REPO/git/ref/heads/main" --jq .object.sha)" || return 1
  [[ "$main_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
  config="$(gh api "repos/$GH_REPO/contents/ha-addon/mammamiradio/config.yaml?ref=$main_sha" --header 'Accept: application/vnd.github.raw+json')" || return 1
  version="$(stable_version <<< "$config")" || return 1
  workflow_id="$(gh api "repos/$GH_REPO/actions/workflows/addon-release.yml" --jq .id)" || return 1
  [[ "$workflow_id" =~ ^[1-9][0-9]*$ ]] || return 1
  release="$(gh api "repos/$GH_REPO/actions/runs/$run_id")" || return 1
  identity="$(jq -ec --arg tag "v$version" --argjson workflow "$workflow_id" --argjson id "$run_id" '
    select(.id == $id and .workflow_id == $workflow and .path == ".github/workflows/addon-release.yml"
      and (.event == "push" or .event == "workflow_dispatch") and .head_branch == $tag
      and .status == "completed" and .conclusion == "success"
      and (.run_attempt | type) == "number" and .run_attempt >= 1)
    | [.id, .workflow_id, .path, .event, .head_branch, .head_sha, .run_attempt, .status, .conclusion]
  ' <<< "$release")" || { echo "::error::Release run does not prove publication of v$version." >&2; return 1; }
  tag_sha="$(gh api "repos/$GH_REPO/commits/v$version" --jq .sha)" || return 1
  [ "$tag_sha" = "$(jq -r .head_sha <<< "$release")" ] || return 1
  [[ "$tag_sha" =~ ^[0-9a-f]{40}$ ]] || return 1
  attempt="$(jq -r .run_attempt <<< "$release")" || return 1
  jobs="$(gh api --paginate --slurp "repos/$GH_REPO/actions/runs/$run_id/attempts/$attempt/jobs?per_page=100")" || return 1
  jq -e '
    if type != "array" or length == 0 or (all(.[]; (.jobs | type) == "array") | not)
    then error("invalid release jobs") else [.[].jobs[]] end
    | [.[] | select(.name == "promote (amd64)" or .name == "promote (aarch64)")]
    | length == 2 and (map(.name) | unique | length) == 2
      and all(.[]; .status == "completed" and .conclusion == "success")
  ' <<< "$jobs" >/dev/null || { echo "::error::Both architecture promotions must succeed in the same release attempt." >&2; return 1; }
  output="$(printf '%s\n' "$config" | bash "$SCRIPT_DIR/check-advertised-version.sh" --config /dev/stdin --version "$version" 2>&1)" || {
    printf '%s\n' "$output" >&2; return 1;
  }
  verdict="$(sed -n 's/^VERDICT: //p' <<< "$output" | tail -1)" || return 1
  [ "$verdict" = pass ] || { echo "::error::Registry verdict is $verdict; the workflow stays disabled." >&2; return 1; }
  # Registry probing takes time. Refuse changed main, tag, attempt or pause state.
  latest="$(gh api "repos/$GH_REPO/actions/runs/$run_id" \
    --jq '[.id, .workflow_id, .path, .event, .head_branch, .head_sha, .run_attempt, .status, .conclusion]')" || return 1
  [ "$(jq -c . <<< "$latest")" = "$identity" ] || return 1
  [ "$(gh api "repos/$GH_REPO/git/ref/heads/main" --jq .object.sha)" = "$main_sha" ] || return 1
  [ "$(gh api "repos/$GH_REPO/commits/v$version" --jq .sha)" = "$tag_sha" ] || return 1
  check_freeze true || return 1
  gh workflow enable "$WORKFLOW" --repo "$GH_REPO" || {
    echo "::error::Enable request failed; inspect the workflow state before proceeding." >&2
    return 1
  }
  [ "$(workflow_state)" = active ] || {
    echo "::error::Could not confirm enable; the workflow may be active. Inspect its state before proceeding." >&2
    return 1
  }
  echo "::notice::v$version is published on both architectures; Dependabot workflow enabled."
}

require_owned_repo() {
  [ "${GH_REPO:-}" = florianhorner/mammamiradio ] || {
    echo "::error::GH_REPO must name the owned florianhorner/mammamiradio repository."
    return 2
  }
}
# An ordinary check-cut only reads local Git objects; no repository API needed.
[ "${1:-}" = check-cut ] || require_owned_repo || exit 2
case "${1:-}" in
  arm) [ "$#" -eq 4 ] || usage; arm_pr "$2" "$3" "$4" ;;
  disarm) [ "$#" -eq 2 ] || usage; disarm_pr "$2" ;;
  sweep) [ "$#" -eq 2 ] || usage; sweep "$2" ;;
  freeze) [ "$#" -le 2 ] || usage; freeze "${2:-300}" ;;
  check-freeze) [ "$#" -eq 1 ] || usage; check_freeze ;;
  check-cut) [ "$#" -eq 3 ] || usage; check_cut "$2" "$3" ;;
  thaw) [ "$#" -eq 2 ] || usage; thaw "$2" ;;
  *) usage ;;
esac
