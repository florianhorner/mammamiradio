#!/usr/bin/env bash
# land-pr.sh — the only legitimate human/feature merge path for this repo.
#
#   scripts/land-pr.sh [--wait] <pr-number> [<pr-number>...]
#
# Landing contract (CLAUDE.md "Quality gates" → "Landing contract"): human and feature PRs are
# opened by /ship and never armed for auto-merge. On the operator's explicit
# merge signal, this wrapper:
#
#   1. verifies a pre-ship squad entry that is still about THIS code
#      (code-state freshness: the entry's commit must be the PR head or an
#      ancestor of it, and no commits may have been pushed to the PR after
#      the entry — wall-clock age is irrelevant, a soak of days is fine);
#   2. verifies committed pre-ship evidence on the PR head (per-PR schema v2
#      or legacy path during migration) and blocks open review debt;
#   3. updates the branch from base if it is behind (user-auth gh, so CI
#      retriggers normally; a conflict stops here for a human);
#   4. re-verifies squad + evidence on the post-update head when the branch
#      moved (squad must match the new head exactly after an update);
#   5. arms GitHub auto-merge pinned to the exact head it verified:
#      gh pr merge --squash --auto --match-head-commit <sha>.
#
# GitHub then merges only when required checks pass on the integrated state
# AND the head is still the one verified here. If anything pushes to the
# branch afterwards, the merge does not fire — re-run this script.
set -euo pipefail

GRACE_SECONDS="${MMR_LAND_GRACE_SECONDS:-600}"
UPDATE_TIMEOUT_SECONDS="${MMR_LAND_UPDATE_TIMEOUT:-120}"
WAIT_TIMEOUT_SECONDS="${MMR_LAND_WAIT_TIMEOUT:-1800}"
READER="${MMR_LAND_REVIEW_READER:-$HOME/.claude/skills/gstack/bin/gstack-review-read}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
CHECK_EVIDENCE="$REPO_ROOT/scripts/check-preship-evidence.sh"
LAND_WAIT=0

say()  { printf '%s\n' "$*"; }
die()  { printf 'land-pr: %s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null 2>&1 || die "gh CLI not found. Install GitHub CLI, then re-run."
command -v jq >/dev/null 2>&1 || die "jq not found. Install jq, then re-run."
git rev-parse --git-dir >/dev/null 2>&1 || die "not inside a git repository."
gh pr merge --help 2>/dev/null | grep -q -- '--match-head-commit' \
  || die "this gh CLI does not support --match-head-commit (needs gh >= 2.49). Upgrade gh, then re-run."

while [ "$#" -gt 0 ]; do
  case "$1" in
    --wait) LAND_WAIT=1; shift ;;
    --) shift; break ;;
    -*) die "unknown option: $1 (usage: scripts/land-pr.sh [--wait] <pr-number> [<pr-number>...])" ;;
    *) break ;;
  esac
done

[ "$#" -ge 1 ] || die "usage: scripts/land-pr.sh [--wait] <pr-number> [<pr-number>...]"

iso_to_epoch() {
  local ts="$1"
  [ -n "$ts" ] || return 0
  date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "$ts" +%s 2>/dev/null \
    || date -u -d "$ts" +%s 2>/dev/null \
    || true
}

ensure_head_local() {
  local head="$1" pr="$2"
  if ! git cat-file -e "${head}^{commit}" 2>/dev/null; then
    git fetch -q origin "pull/${pr}/head" 2>/dev/null || true
  fi
  git cat-file -e "${head}^{commit}" 2>/dev/null
}

squad_check() {
  local pr_head="$1" last_push="$2" strict="${3:-0}" line skill rc ts es
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
    git cat-file -e "${rc}^{commit}" 2>/dev/null || continue
    if [ "$strict" -eq 1 ]; then
      [ "$(git rev-parse "${rc}^{commit}" 2>/dev/null)" = "$pr_head" ] || continue
    else
      { [ "$(git rev-parse "${rc}^{commit}" 2>/dev/null)" = "$pr_head" ] \
          || git merge-base --is-ancestor "$rc" "$pr_head" 2>/dev/null; } || continue
    fi
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

evidence_check() {
  local pr="$1" head="$2" tmp v2_path allow_flag=()
  if [ "${MMR_LAND_SKIP_EVIDENCE_CHECK:-0}" = "1" ]; then
    return 0
  fi
  [ -f "$CHECK_EVIDENCE" ] || return 0
  tmp="$(mktemp)"
  v2_path="proof/preship-review/pr-${pr}.json"
  if [ "${MMR_LAND_ALLOW_ISSUES_FOUND:-0}" = "1" ]; then
    allow_flag=(--allow-issues-found)
  fi
  if git show "${head}:${v2_path}" > "$tmp" 2>/dev/null; then
    :
  elif git show "${head}:proof/preship-review.json" > "$tmp" 2>/dev/null; then
    say "land-pr: using legacy proof/preship-review.json on PR head (migrate to $v2_path)."
  else
    rm -f "$tmp"
    say "land-pr: no committed pre-ship evidence on PR head (expected $v2_path)."
    say "         Run the review squad, then scripts/emit-review-evidence.sh, commit, and push."
    return 1
  fi
  if ! bash "$CHECK_EVIDENCE" "${allow_flag[@]}" "$tmp" "$head"; then
    rm -f "$tmp"
    return 1
  fi
  rm -f "$tmp"
  return 0
}

thread_check() {
  local pr="$1" response hits
  if [ "${MMR_LAND_SKIP_THREAD_CHECK:-0}" = "1" ]; then
    return 0
  fi
  response="$(gh api graphql -f query='
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes {
              isResolved
              isOutdated
              comments(first: 1) {
                nodes { author { login } body url }
              }
            }
          }
        }
      }
    }' -f owner='{owner}' -f name='{repo}' -F number="$pr" 2>/dev/null)" \
    || {
      say "land-pr: could not read review threads for PR #$pr."
      say "         Check gh auth and network, or set MMR_LAND_SKIP_THREAD_CHECK=1 for a hotfix."
      return 1
    }
  hits="$(printf '%s' "$response" | jq -r '
    .data.repository.pullRequest.reviewThreads.nodes[]
    | select(.isResolved == false and .isOutdated == false)
    | .comments.nodes[0]
    | select(
        (.author.login == "coderabbitai")
        or (.author.login == "copilot-pull-request-reviewer")
        or (.author.login == "chatgpt-codex-connector")
      )
    | select((.body | test("Major|Critical|\\bP0\\b|\\bP1\\b")))
    | "\(.author.login): \(.url)"
  ')"
  if [ -n "$hits" ]; then
    say "land-pr: PR #$pr has unresolved Major/Critical bot review threads:"
    printf '%s\n' "$hits" | while IFS= read -r line; do say "         $line"; done
    say "         Resolve each thread or fix + push + re-review, then land again."
    return 1
  fi
  return 0
}

verify_head() {
  local pr="$1" head="$2" last_push="$3" strict="${4:-0}"
  ensure_head_local "$head" "$pr" \
    || die "PR #$pr head $head is not available locally and could not be fetched — cannot verify it."
  squad_check "$head" "$last_push" "$strict" || return 1
  evidence_check "$pr" "$head" || return 1
  thread_check "$pr" || return 1
}

wait_for_merge() {
  local pr="$1" expected="$2" waited=0 state
  while [ "$waited" -lt "$WAIT_TIMEOUT_SECONDS" ]; do
    state="$(gh pr view "$pr" --json state,headRefOid --jq '.state + " " + .headRefOid' 2>/dev/null || true)"
    case "$state" in
      MERGED*) say "land-pr: PR #$pr merged."; return 0 ;;
      CLOSED*) say "land-pr: PR #$pr closed without merging."; return 1 ;;
      OPEN\ "$expected") sleep 5; waited=$((waited + 5)) ;;
      OPEN\ *) say "land-pr: PR #$pr head changed while waiting — re-run this script."; return 1 ;;
      *) sleep 5; waited=$((waited + 5)) ;;
    esac
  done
  say "land-pr: timed out after ${WAIT_TIMEOUT_SECONDS}s waiting for PR #$pr to merge."
  return 1
}

land_one() {
  local pr="$1" view state head merge_state last_push new_head waited updated=0

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

  if [ "$merge_state" != "BEHIND" ]; then
    verify_head "$pr" "$head" "$last_push_epoch" 0 || return 1
  fi

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
    waited=0
    new_head=""
    while [ "$waited" -lt "$UPDATE_TIMEOUT_SECONDS" ]; do
      new_head="$(gh pr view "$pr" --json headRefOid --jq '.headRefOid' 2>/dev/null || true)"
      if [ -n "$new_head" ] && [ "$new_head" != "$head" ]; then
        last_push="$(gh pr view "$pr" --json commits --jq '[.commits[].committedDate] | max // empty' 2>/dev/null || true)"
        last_push_epoch="$(iso_to_epoch "$last_push")"
        [ -n "$last_push_epoch" ] || die "could not parse PR #$pr commit dates after branch update."
        break
      fi
      new_head=""
      sleep 3; waited=$((waited + 3))
    done
    if [ -z "$new_head" ]; then
      die "PR #$pr branch update did not surface a new head within ${UPDATE_TIMEOUT_SECONDS}s — check the PR on GitHub, then re-run."
    fi
    head="$new_head"
    updated=1
    verify_head "$pr" "$head" "$last_push_epoch" 1 || return 1
  fi

  gh pr merge "$pr" --squash --auto --match-head-commit "$head" \
    || die "arming auto-merge for PR #$pr failed — see gh output above, fix, and re-run."
  if [ "$updated" -eq 1 ]; then
    say "land-pr: PR #$pr armed on post-update head ${head:0:12} (squad + evidence re-verified)."
  else
    say "land-pr: PR #$pr armed — GitHub merges it once required checks pass on head ${head:0:12}."
  fi
  say "         If the head changes before then, the merge will not fire; re-run this script."
  if [ "$LAND_WAIT" -eq 1 ]; then
    wait_for_merge "$pr" "$head" || return 1
  fi
}

rc=0
for pr in "$@"; do
  land_one "$pr" || rc=1
done
exit "$rc"
