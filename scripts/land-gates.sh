#!/usr/bin/env bash
# land-gates.sh — the landing gate predicates, in ONE implementation.
#
# Sourced by scripts/land-pr.sh (which arms merges) and by
# scripts/land-queue-plan.sh (which only reports what it would arm). Both must
# reach the same verdict on the same PR head; a second copy of this logic is the
# likeliest place a soft-pass creeps back in, so there is deliberately only one.
#
# Contract for every predicate here:
#   - returns 0 (gate passes) or non-zero (gate blocks); NEVER exits
#   - explains a block on stdout, prefixed with "$LAND_GATES_LABEL: "
#   - fails CLOSED on unverifiable state (gh/API/git error) — never soft-passes
#
# Callers that want a hard abort wrap the call: `ensure_head_local ... || die ...`.
# Set LAND_GATES_LABEL before sourcing to change the message prefix.
#
# shellcheck shell=bash

LAND_GATES_LABEL="${LAND_GATES_LABEL:-land-gates}"
LAND_GATES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Freshness grace: /ship pushes mechanical commits (version bump, changelog)
# right after the squad logs its entry; commits within this window after the
# entry are treated as part of the reviewed push, not new work.
GRACE_SECONDS="${MMR_LAND_GRACE_SECONDS:-600}"
# Reader override exists for tests; default is the repo-local ledger dump.
READER="${MMR_LAND_REVIEW_READER:-$LAND_GATES_DIR/read-preship-ledger.sh}"
if [ ! -x "$READER" ] && [ -x "$HOME/.claude/skills/gstack/bin/gstack-review-read" ]; then
  READER="$HOME/.claude/skills/gstack/bin/gstack-review-read"
fi
EVIDENCE_CHECKER="${MMR_LAND_EVIDENCE_CHECKER:-$LAND_GATES_DIR/check-preship-evidence.sh}"

if ! declare -F say >/dev/null 2>&1; then
  say() { printf '%s\n' "$*"; }
fi

_gate_say()  { say "$LAND_GATES_LABEL: $*"; }
# Continuation lines align under the message, so the indent has to follow the
# label width rather than assume one caller's prefix.
_gate_cont() { say "$(printf '%*s' $(( ${#LAND_GATES_LABEL} + 2 )) '')$*"; }

# The repository slug is constant for the process. Resolving it per PR cost a
# network round trip each time, and in CI `gh` already exports GH_REPO.
_LAND_GATES_SLUG=""
_repo_slug() {
  local candidate
  if [ -z "$_LAND_GATES_SLUG" ]; then
    # gh accepts a host-qualified GH_REPO ([HOST/]OWNER/REPO); `gh repo view`
    # normalized that away. Strip the host and accept only OWNER/REPO, so a
    # documented env form cannot turn into owner="github.com" and a GraphQL
    # null that surfaces as a misleading "check gh auth" refusal.
    candidate="${GH_REPO:-}"
    case "$candidate" in
      */*/*) candidate="${candidate#*/}" ;;
    esac
    case "$candidate" in
      */*/*|*" "*|"") candidate="" ;;
      */*) ;;
      *) candidate="" ;;
    esac
    if [ -n "$candidate" ]; then
      _LAND_GATES_SLUG="$candidate"
    else
      _LAND_GATES_SLUG="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)" \
        || return 1
    fi
  fi
  printf '%s' "$_LAND_GATES_SLUG"
}

# Hard precondition, as it was before the extraction: without the reader the
# bot-thread gate cannot run, and a soft skip surfaces later as a misleading
# "could not query review threads — check gh auth".
if [ ! -r "$LAND_GATES_DIR/review-threads.sh" ]; then
  say "$LAND_GATES_LABEL: review-thread reader not found at $LAND_GATES_DIR/review-threads.sh."
  exit 1
fi
# shellcheck source=scripts/review-threads.sh
. "$LAND_GATES_DIR/review-threads.sh"

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
    _gate_say "no pre-ship squad entry covers the current PR head."
    _gate_cont "Either commits were pushed after the last review, or no squad ran."
    _gate_cont "Re-run the review squad (/ship or /review) on this branch, then land again."
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
    _gate_say "committed v2 pre-ship evidence does not cover PR head ${target:0:12}."
    _gate_cont "Run the review squad, emit v2 evidence, commit it on the branch, then land again."
    if [ -n "$out" ]; then
      printf '%s\n' "$out" | sed "s/^/$(printf '%*s' $(( ${#LAND_GATES_LABEL} + 2 )) '')/"
    fi
    return 1
  fi
  return 0
}

thread_check() {
  local pr="$1" slug owner repo response blocked line
  if [ "${MMR_LAND_SKIP_THREAD_CHECK:-0}" = "1" ]; then
    return 0
  fi

  slug="$(_repo_slug)" \
    || {
      _gate_say "could not resolve repository identity for bot-thread check."
      _gate_cont "Check gh auth, then re-run."
      return 1
    }
  owner="${slug%%/*}"
  repo="${slug##*/}"

  response="$(review_threads_json "$owner" "$repo" "$pr")" \
    || {
      _gate_say "could not query review threads for PR #$pr."
      _gate_cont "Check gh auth, then re-run. Use MMR_LAND_SKIP_THREAD_CHECK=1 only for hotfix escape."
      return 1
    }

  # Capture before iterating. A process substitution discards its exit status,
  # so a jq failure (missing binary, unexpected node shape) yielded zero lines,
  # left blocked at 0, and returned PASS — land-pr.sh would then arm on a PR
  # whose thread debt was never evaluated. The one path in this file that did
  # not honour the fail-closed contract in the header.
  local urls
  urls="$(printf '%s' "$response" | jq -r "$REVIEW_THREADS_BLOCKING_JQ | .url")" \
    || {
      _gate_say "could not evaluate the review threads for PR #$pr."
      _gate_cont "Check that jq is installed and the API response is intact, then re-run."
      return 1
    }

  blocked=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    blocked=$((blocked + 1))
    _gate_say "unresolved Major/Critical bot thread: $line"
  done <<<"$urls"

  if [ "$blocked" -gt 0 ]; then
    _gate_say "$blocked unresolved Major/Critical bot review thread(s) block landing."
    _gate_cont "Resolve each thread on GitHub or fix and push, re-review, then land again."
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
    _gate_say "committed evidence was skipped and no qualifying local ledger entry covers ${head:0:12}."
    _gate_cont "Re-run without MMR_LAND_SKIP_EVIDENCE_CHECK=1 or provide current ledger evidence."
    return 1
  elif [ "${MMR_LAND_REQUIRE_LEDGER_SQUAD:-0}" = "1" ]; then
    return 1
  else
    _gate_say "no local ledger squad entry; committed v2 evidence covers PR head ${head:0:12}."
  fi
  thread_check "$pr" || return 1
}

# ensure_head_local <pr> <head-sha> -> 0 when the head object is present locally.
# Returns 1 SILENTLY instead of exiting: land-pr.sh wraps it in `|| die` (hard
# abort, stderr), while a reporting caller records BLOCKED and keeps going. The
# two want different wording and different exit behavior, so the phrasing stays
# with the caller.
ensure_head_local() {
  local pr="$1" head="$2"
  if ! git cat-file -e "${head}^{commit}" 2>/dev/null; then
    git fetch -q origin "pull/${pr}/head" 2>/dev/null || true
  fi
  git cat-file -e "${head}^{commit}" 2>/dev/null
}
