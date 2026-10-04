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
  local pr="$1"
  # Receipt/ledger helpers remain historical utilities, never admission gates.
  thread_check "$pr" || return 1
}

# refresh_landed_ref <base-sha> -> always 0. Need-driven `git fetch origin main`.
#
# The merge witness in verify_v2 trusts a base only if it is landed content in
# origin/main. ensure_head_local fetches the PR head's OBJECTS, which carries the
# base commit into the object store — but it never moves the origin/main REF. A
# landing seat that has not fetched since main advanced then refuses GitHub's
# real base as "not landed" and stalls the documented integrate-push-land flow
# until someone thinks to fetch by hand.
#
# The refresh is keyed on the exact predicate the gate will evaluate: fetch only
# when a resolvable local origin/main does NOT already contain the base. A seat
# with complete history therefore never touches the network (a documented
# invariant of its own), and a seat with no origin/main at all is left alone —
# the evidence gate then refuses with "does not resolve", which is the
# fail-closed outcome. Fetch failure is tolerated for the same reason: a stale
# ref makes the gate refuse; the refresh can never be the thing that accepts.
# MMR_LAND_SKIP_FETCH=1 keeps self-tests offline.
refresh_landed_ref() {
  local base="$1"
  if [ "${MMR_LAND_SKIP_FETCH:-0}" = "1" ]; then
    return 0
  fi
  git rev-parse --verify --quiet origin/main >/dev/null 2>&1 || return 0
  if git merge-base --is-ancestor "$base" origin/main 2>/dev/null; then
    return 0
  fi
  git fetch -q origin main 2>/dev/null || true
  return 0
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
