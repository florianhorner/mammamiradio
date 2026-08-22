#!/usr/bin/env bash
# check-preship-evidence.sh — verify the committed pre-ship review evidence artifact.
#
# Shared checker for the runtime-independent half of the pre-ship squad gate: the local
# Claude hook (require-preship-squad.sh) cannot fire in Codex (no hook layer exists
# there), so CI verifies proof/preship-review.json instead — written by
# scripts/emit-review-evidence.sh after the squad runs.
#
# Checks: file exists, parses, skill is review/adversarial-review, and the pinned commit
# is the target head or an ancestor of it (walking ancestry is what survives
# rebase/amend/squash — the reason the artifact has a fixed name and an internal commit
# field instead of a SHA-named filename).
#
# NO freshness window here, deliberately: the local hook's ±2h window exists because it
# reads a mutable laptop ledger. A committed artifact is immutable at a commit; wall-clock
# age is meaningless for it, and a window would fail every PR reviewed more than two
# hours before opening.
#
# Honest scope, same words as land-pr.sh: this is a guard for tired humans and parallel
# agents, not a security boundary. The agent that would skip the squad also writes the
# evidence. What this changes: skipping the squad from ANY runtime now requires
# fabricating a diffable, commit-pinned artifact instead of being silently invisible.
#
# Usage: scripts/check-preship-evidence.sh [evidence-file] [target-head]
#   evidence-file  default proof/preship-review.json
#   target-head    default HEAD (CI passes the PR head SHA)
set -euo pipefail

FILE="${1:-proof/preship-review.json}"
TARGET="${2-HEAD}"

fail() {
  echo "check-preship-evidence: FAIL — $1" >&2
  echo "  fix: $2" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || fail "jq not found" "install jq"

[ -n "$TARGET" ] || fail "empty target head" \
  "pass an explicit head SHA (an empty second argument would silently mis-scope to HEAD)"

[ -f "$FILE" ] || fail "no evidence artifact at $FILE" \
  "run the pre-ship review squad, then scripts/emit-review-evidence.sh, and commit the file"

jq empty "$FILE" 2>/dev/null || fail "$FILE is not valid JSON" \
  "re-run scripts/emit-review-evidence.sh — do not hand-edit the artifact"

document_count="$(jq -s 'length' "$FILE")"
[ "$document_count" -eq 1 ] || fail "$FILE contains $document_count JSON documents" \
  "re-run scripts/emit-review-evidence.sh so the artifact contains one top-level object"

skill="$(jq -r '.skill // ""' "$FILE")"
case "$skill" in
  review | adversarial-review) ;;
  *) fail "skill '$skill' is not a squad review" \
    "evidence must come from a review/adversarial-review run, not '$skill'" ;;
esac

commit="$(jq -r '.commit // ""' "$FILE")"
case "$commit" in
  "" | null | uncommitted | unknown | pending)
    fail "evidence carries no usable commit ('$commit')" \
      "the squad must run on committed work; commit first, review, then emit" ;;
esac

# Hex object ids only. Without this, a symbolic rev like HEAD, @, a branch or a tag in
# the artifact resolves fresh on EVERY pr head and passes forever — defeating the
# commit-pinned guarantee the whole design rests on (adversarial-review finding).
printf '%s' "$commit" | grep -Eq '^[0-9a-fA-F]{7,40}$' \
  || fail "evidence commit '$commit' is not a commit SHA" \
    "the artifact must pin a hex object id; re-run scripts/emit-review-evidence.sh"

git rev-parse --verify --quiet "${commit}^{commit}" >/dev/null \
  || fail "commit $commit is not in this repository's history" \
    "CI needs full history (fetch-depth: 0); locally, fetch before checking"

# --is-ancestor is true for equality too, so head == reviewed commit passes.
git merge-base --is-ancestor "$commit" "$TARGET" \
  || fail "reviewed commit $commit is not an ancestor of $(git rev-parse --short "$TARGET")" \
    "the evidence is from another branch or a superseded state — re-run the squad on this branch and re-emit"

echo "check-preship-evidence: OK — $skill @ $commit is an ancestor of $(git rev-parse --short "$TARGET")"
