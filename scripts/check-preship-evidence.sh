#!/usr/bin/env bash
# check-preship-evidence.sh — verify the committed pre-ship review evidence artifact.
#
# Shared checker for the runtime-independent half of the pre-ship squad gate: the local
# Claude hook (require-preship-squad.sh) cannot fire in Codex (no hook layer exists
# there), so CI verifies proof/preship-review.json instead — written by
# scripts/emit-review-evidence.sh after the squad runs.
#
# Schema v1 (legacy): proof/preship-review.json — commit must be ancestor of target.
# Schema v2 (current): proof/preship-review/pr-<n>.json — pr_head_sha must equal target;
#   commit must still be ancestor of pr_head_sha (reviewed state contained in head).
#
# NO freshness window here, deliberately: the local hook's ±2h window exists because it
# reads a mutable laptop ledger. A committed artifact is immutable at a commit; wall-clock
# age is meaningless for it, and a window would fail every PR reviewed more than two
# hours before opening.
#
# Usage: scripts/check-preship-evidence.sh [--allow-issues-found] [evidence-file] [target-head]
#   --allow-issues-found  accept status=issues_found (land-pr uses this only with opt-in)
#   evidence-file         default proof/preship-review.json
#   target-head           default HEAD (CI passes the PR head SHA)
set -euo pipefail

ALLOW_ISSUES_FOUND=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --allow-issues-found)
      ALLOW_ISSUES_FOUND=1
      shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "check-preship-evidence: unknown option: $1" >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

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
  "run the pre-ship review squad, then scripts/emit-review-evidence.sh, and commit the artifact"

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

printf '%s' "$commit" | grep -Eq '^[0-9a-fA-F]{7,40}$' \
  || fail "evidence commit '$commit' is not a commit SHA" \
    "the artifact must pin a hex object id; re-run scripts/emit-review-evidence.sh"

git rev-parse --verify --quiet "${commit}^{commit}" >/dev/null \
  || fail "commit $commit is not in this repository's history" \
    "CI needs full history (fetch-depth: 0); locally, fetch before checking"

schema_version="$(jq -r '.schema_version // "1.0.0"' "$FILE")"
target_full="$(git rev-parse "$TARGET")"

case "$schema_version" in
  2.0.0)
    pr_head="$(jq -r '.pr_head_sha // ""' "$FILE")"
    pr_number="$(jq -r '.pr_number // ""' "$FILE")"
    printf '%s' "$pr_head" | grep -Eq '^[0-9a-fA-F]{40}$' \
      || fail "pr_head_sha '$pr_head' is not a full commit SHA" \
        "re-run scripts/emit-review-evidence.sh on the PR branch"
    case "$pr_number" in
      '' | null | *[!0-9]*)
        fail "pr_number '$pr_number' is not a positive integer" \
          "re-run scripts/emit-review-evidence.sh with an open PR"
        ;;
    esac
    [ "$pr_head" = "$target_full" ] \
      || fail "pr_head_sha $(git rev-parse --short "$pr_head") does not match target $(git rev-parse --short "$target_full")" \
        "re-run the squad on this PR head and re-emit evidence"
    git merge-base --is-ancestor "$commit" "$pr_head" \
      || fail "reviewed commit $commit is not an ancestor of pinned pr_head_sha" \
        "re-run the squad on this branch and re-emit"
    ;;
  *)
    git merge-base --is-ancestor "$commit" "$TARGET" \
      || fail "reviewed commit $commit is not an ancestor of $(git rev-parse --short "$TARGET")" \
        "the evidence is from another branch or a superseded state — re-run the squad on this branch and re-emit"
    ;;
esac

review_status="$(jq -r '.status // ""' "$FILE")"
case "$review_status" in
  issues_open)
    fail "evidence status is issues_open" \
      "resolve open review findings, re-run the squad, and re-emit before landing"
    ;;
  issues_found)
    [ "$ALLOW_ISSUES_FOUND" -eq 1 ] || fail "evidence status is issues_found" \
      "re-run the squad until clean, or land with explicit operator acknowledgment (MMR_LAND_ALLOW_ISSUES_FOUND=1)"
    ;;
  clean | "")
    ;;
  *)
    fail "evidence status '$review_status' is not recognized" \
      "re-run scripts/emit-review-evidence.sh after a squad review"
    ;;
esac

echo "check-preship-evidence: OK — $skill @ $commit for $(git rev-parse --short "$target_full") (schema $schema_version, status=${review_status:-clean})"
