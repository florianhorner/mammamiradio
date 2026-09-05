#!/usr/bin/env bash
# dependabot-window-hold.sh — keep Dependabot auto-merge out of the release cut window.
#
# Called by .github/workflows/dependabot-automerge.yml with GH_TOKEN and GH_REPO set.
#
#   dependabot-window-hold.sh disarm <pr-number>
#       Disable auto-merge on one PR and label it `cut-window-hold`. Exit 0 when it
#       was disarmed or was never armed; exit 1 (and ::error) when the API call
#       failed for any other reason, so the job goes red instead of pretending.
#
#   dependabot-window-hold.sh sweep <pass|fail|unknown>
#       fail    -> disarm every open Dependabot PR that is currently armed.
#       pass    -> re-arm only PRs carrying the hold label (the ones this tool
#                  disarmed) whose title classifies as a patch or minor update;
#                  a maintainer's manual disarm never carries the label and is
#                  never overridden. Held PRs that do not classify are left for
#                  their next Dependabot event.
#       unknown -> touch nothing (registry unreachable is not a verdict).
#
# Offline test: tests/workflows/test_dependabot_automerge_gate.sh runs both verbs
# against a mocked `gh`.
set -euo pipefail

HOLD_LABEL="${HOLD_LABEL:-cut-window-hold}"

usage() {
  echo "usage: $0 disarm <pr-number> | sweep <pass|fail|unknown>" >&2
  exit 2
}

ensure_label() {
  gh label create "$HOLD_LABEL" \
    --description "auto-merge paused by dependabot-automerge.yml while main advertises an unpublished add-on version" \
    --color B60205 >/dev/null 2>&1 || true
}

# disarm_pr <number> -> 0 disarmed or not armed; 1 API failure (reported as ::error).
# Reads the armed state first: gh's --disable-auto calls the mutation
# unconditionally and errors on an unarmed PR, so the state read is what tells
# "nothing to do" apart from "could not disarm".
disarm_pr() {
  local num="$1" armed err rc
  armed="$(gh pr view "$num" --json autoMergeRequest --jq '.autoMergeRequest != null')" || {
    echo "::error title=Could not read auto-merge state::#$num"
    return 1
  }
  if [ "$armed" != "true" ]; then
    echo "::notice::#$num has no auto-merge armed; nothing to disarm."
    return 0
  fi
  set +e
  err="$(gh pr merge --disable-auto "$num" 2>&1)"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "::error title=Could not disarm auto-merge::#$num: $err"
    return 1
  fi
  ensure_label
  gh pr edit "$num" --add-label "$HOLD_LABEL" >/dev/null
  echo "::warning title=Cut window open::main advertises an unpublished add-on version; auto-merge on #$num is disarmed and labelled $HOLD_LABEL until the window closes."
}

# classify <pr-title> -> patch | minor | major | unknown
# Dependabot single-package titles read "bump X from A to B". Group titles carry
# no versions and classify as unknown, which the sweep leaves alone.
classify() {
  local title="$1" from to fM fm tM tm
  from="$(printf '%s' "$title" | sed -nE 's/.* from v?([0-9]+\.[0-9]+(\.[0-9]+)?)[^ ]* to v?([0-9]+\.[0-9]+(\.[0-9]+)?).*/\1/p')"
  to="$(printf '%s' "$title" | sed -nE 's/.* from v?([0-9]+\.[0-9]+(\.[0-9]+)?)[^ ]* to v?([0-9]+\.[0-9]+(\.[0-9]+)?).*/\3/p')"
  if [ -z "$from" ] || [ -z "$to" ]; then
    echo unknown
    return 0
  fi
  IFS=. read -r fM fm _ <<<"$from"
  IFS=. read -r tM tm _ <<<"$to"
  if [ "$fM" != "$tM" ]; then echo major
  elif [ "${fm:-0}" != "${tm:-0}" ]; then echo minor
  else echo patch
  fi
}

sweep() {
  local verdict="$1" listing status=0 num state hold title kind
  case "$verdict" in
    pass|fail) ;;
    unknown)
      echo "::notice::advertised-version check returned unknown (registry unreachable); auto-merge state left as is."
      return 0 ;;
    *) echo "::error::unknown verdict '$verdict'"; return 2 ;;
  esac
  listing="$(mktemp)"
  trap 'rm -f "$listing"' RETURN
  gh pr list --author app/dependabot --state open --limit 100 \
    --json number,title,labels,autoMergeRequest \
    --jq '.[] | [ .number,
                  (if .autoMergeRequest then "armed" else "off" end),
                  (if ((.labels // []) | map(.name) | index("'"$HOLD_LABEL"'")) == null then "nohold" else "hold" end),
                  .title ] | @tsv' > "$listing"
  while IFS=$'\t' read -r num state hold title; do
    [ -n "$num" ] || continue
    if [ "$verdict" = "fail" ]; then
      [ "$state" = "armed" ] || continue
      disarm_pr "$num" || status=1
      continue
    fi
    # pass: re-arm only what this tool paused, and only patch/minor
    [ "$hold" = "hold" ] || continue
    kind="$(classify "$title")"
    case "$kind" in
      patch|minor)
        if gh pr merge --squash --auto "$num" && gh pr edit "$num" --remove-label "$HOLD_LABEL" >/dev/null; then
          echo "re-armed #$num ($kind): $title"
        else
          echo "::error title=Could not re-arm auto-merge::#$num"
          status=1
        fi ;;
      *)
        echo "::notice::#$num is held but its title does not classify as patch or minor ($kind); left for its next Dependabot event." ;;
    esac
  done < "$listing"
  return "$status"
}

case "${1:-}" in
  disarm)
    [ -n "${2:-}" ] || usage
    disarm_pr "$2" ;;
  sweep)
    [ -n "${2:-}" ] || usage
    sweep "$2" ;;
  classify)
    [ -n "${2:-}" ] || usage
    classify "$2" ;;
  *) usage ;;
esac
