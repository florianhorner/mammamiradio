#!/usr/bin/env bash
# Cut a manual edge release.
#
# Sets the edge add-on's version: to the newest origin/main commit that has a
# green `Build HA Addon` image, and opens a PR you merge via /ship. The version
# string IS the Docker image tag the HA Supervisor pulls
# (ha-addon/mammamiradio-edge/config.yaml `version:` ->
# ghcr.io/<owner>/mammamiradio-addon-{arch}:<short-sha>), and "update available" is
# a version-string compare — so changing it surfaces an in-place Update on the Pi.
#
# Why "newest BUILT commit" and not blind origin/main HEAD: `Build HA Addon` only
# builds an image when a commit touches ha-addon/**, mammamiradio/**, pyproject.toml,
# or radio.toml. When the tip commits are tests/docs/CI-only, no :<sha> image exists
# for them, so pinning HEAD would make the Supervisor pull a missing tag. This script
# picks the newest main commit with a successful build run (that success is the proof
# both per-arch images were pushed) and HARD-FAILS rather than advertise an unverified
# tag. It also refuses if any add-on image file changed between that built commit and
# HEAD — the pinned image would not implement the newer edge metadata.
#
# Selection uses `gh run list` (needs only actions:read). The old GHCR packages-API
# check is gone: it needed the read:packages scope the maintainer token lacks and
# 403'd into a soft-pass that could advertise a missing tag.
#
# No CI bot, no protected-main self-merge: YOU open the PR (so its required checks
# fire) and YOU merge it. Stable is never touched.
#
# Usage: make edge-release   (or: bash scripts/cut-edge-release.sh)
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"

# Paths that trigger Build HA Addon — must mirror addon-build.yml `on.push.paths`.
IMAGE_PATHS="ha-addon mammamiradio pyproject.toml radio.toml"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree not clean — commit or stash first, then cut the edge release." >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI not available — cannot verify which main commit has a built image." >&2
  exit 1
fi

git fetch origin main --quiet

# Candidate set: recent origin/main commits whose `Build HA Addon` run SUCCEEDED.
# A successful run means validate -> build (both arches) -> push -> smoke all passed,
# so both :<short-sha> images were pushed AT BUILD TIME. (A later GHCR prune/delete is
# not detected — acceptable: the add-on images are not pruned, and the drift guard
# below still blocks the dangerous "pin an image that predates an add-on change" case.)
# `gh run list` orders by run-creation time, not commit topology, so this is only a
# candidate set; we pick the topologically-newest one below. --limit 40 is the lookback
# window (~weeks at this repo's velocity). Hard-fail (never soft-pass) if the query fails.
OK_SHAS="$(gh run list --workflow=addon-build.yml --branch main --limit 40 \
  --json headSha,status,conclusion \
  -q '[.[] | select(.status == "completed" and .conclusion == "success") | .headSha] | .[]' \
  2>/dev/null)" || {
  echo "ERROR: could not query 'Build HA Addon' runs (gh run list failed)." >&2
  echo "       Refusing to cut an edge release without a verified built commit." >&2
  exit 1
}

# Walk origin/main newest-first and take the first commit that has a green build.
# Selecting from `git rev-list --topo-order origin/main` makes the result inherently
# an ancestor of main and topology-correct (children before parents) even when a merged
# branch carries stale commit dates or an older commit was re-run after a newer one.
TARGET_FULL=""
while IFS= read -r _commit; do
  [ -n "$_commit" ] || continue
  if printf '%s\n' "$OK_SHAS" | grep -qxF "$_commit"; then
    TARGET_FULL="$_commit"
    break
  fi
done < <(git rev-list --topo-order origin/main)

if [ -z "$TARGET_FULL" ]; then
  echo "ERROR: no successful 'Build HA Addon' run found for any commit on origin/main." >&2
  echo "       Wait for a build to go green on a commit that touches the add-on image" >&2
  echo "       ($IMAGE_PATHS), then re-run." >&2
  exit 1
fi

SHA="$(git rev-parse --short=7 "$TARGET_FULL")"
HEAD_SHORT="$(git rev-parse --short=7 origin/main)"

# Refuse to pin a built image that predates an add-on image change. If any image
# file differs between the built commit and HEAD, the newest image-affecting commit
# has NOT gone green yet (still building, or its build failed) — pinning the older
# image would advertise edge metadata (options/schema, run.sh behaviour) the image
# does not implement.
# shellcheck disable=SC2086  # IMAGE_PATHS intentionally word-splits into pathspecs
# No `|| true`: `git diff --name-only` already exits 0 for both changed and unchanged,
# so a non-zero here is a real verification failure (bad object, git error). Treat it
# like every other unverifiable state — hard-fail, never soft-pass.
if ! CHANGED="$(git diff --name-only "$TARGET_FULL" origin/main -- $IMAGE_PATHS 2>/dev/null)"; then
  echo "ERROR: could not verify whether add-on image files changed since $SHA." >&2
  echo "       Refusing to cut an edge release without a verified drift check." >&2
  exit 1
fi
if [ -n "$CHANGED" ]; then
  echo "ERROR: add-on image files changed since the latest built commit ($SHA):" >&2
  printf '%s\n' "$CHANGED" | sed 's/^/         /' >&2
  echo "       The newest add-on-affecting commit has no green 'Build HA Addon' image yet" >&2
  echo "       (still building, or its build failed). Pinning now would ship edge metadata" >&2
  echo "       the pinned image does not implement. Wait for that build (or fix it), then re-run." >&2
  exit 1
fi

# Read the current edge version from origin/main (what the cut actually rewrites),
# NOT the caller's checked-out tree — running from a stale local branch that already
# carries `version: $SHA` must not falsely report "already released" while origin/main
# still needs the bump. An unreadable config -> empty -> proceed to cut (the safe way).
CURRENT="$(git show "origin/main:$EDGE_CONFIG" 2>/dev/null | awk '/^version:/ { print $2; exit }' | tr -d '"')" || CURRENT=""
if [ "$CURRENT" = "$SHA" ]; then
  echo "Edge add-on already at $SHA (latest built main commit) — nothing to release."
  exit 0
fi

if [ "$SHA" != "$HEAD_SHORT" ]; then
  echo "Note: pinning to the latest BUILT main commit $SHA (origin/main HEAD is $HEAD_SHORT;" >&2
  echo "      the commits in between touch no add-on image files)." >&2
fi

# OWNER feeds the PR body and image-path string below. Derive it AFTER target
# selection (the old GHCR check that also used it is gone). Do NOT fold this into a
# deleted block — the PR body needs it, and `set -u` would abort after the push.
OWNER="$(git remote get-url origin | sed 's|.*github.com[:/]||;s|/.*||')"

BRANCH="edge-release/$SHA"
if EXISTING="$(gh pr list --head "$BRANCH" --state open --json url -q '.[0].url' 2>/dev/null)" && [ -n "$EXISTING" ]; then
  echo "An edge release PR for $SHA is already open: $EXISTING"
  echo "Merge it via /ship, or close it and re-run."
  exit 0
fi
# -B re-creates the branch from origin/main even if a stale local one exists, so a
# re-run after a failed attempt is idempotent. The branch carries the CURRENT main
# tree (so validate-addon.sh checks live edge schema/options on the PR); only the
# version: line points at the (possibly-behind) built SHA — do NOT cut from
# $TARGET_FULL, that would drop newer edge metadata from the PR. Errors NOT swallowed.
git checkout -B "$BRANCH" origin/main
python3 scripts/validate-release-beat.py --channel edge --target-sha "$SHA"
sed -i.bak "s/^version: .*/version: $SHA/" "$EDGE_CONFIG"
rm -f "$EDGE_CONFIG.bak"
git add "$EDGE_CONFIG"
git commit -q -m "chore(edge): cut edge release $SHA"
git push -u origin "$BRANCH" --force --quiet

PR_BODY="Cut the edge (dev) channel to \`$SHA\`. The edge add-on's \`version:\` is the image tag
the Supervisor pulls (\`ghcr.io/$OWNER/mammamiradio-addon-{arch}:$SHA\`), so the soak Pi
shows an in-place Update.

\`$SHA\` is the newest \`main\` commit with a green \`Build HA Addon\` image (that run is the
proof both per-arch images were pushed). It may trail \`origin/main\` HEAD ($HEAD_SHORT) when
the tip commits touch only files that do not rebuild the image (tests/docs/CI); no \`:<sha>\`
image exists for those, so pinning to the newest *built* commit is what guarantees the Update
can actually pull. Manual edge release; stable is untouched.

## Proof

- [ ] build: n/a — metadata-only edge version bump; the multi-arch image was already built and pushed by Build HA Addon on $SHA
- [ ] tests: n/a — config-only, no code paths
- [ ] lint: n/a — single version-line change
- [ ] runtime: n/a — pulls the pre-built GHCR tag mammamiradio-addon-{arch}:$SHA already smoke-tested by Build HA Addon on $SHA
- [ ] schema: n/a — edge option and schema unchanged, only the version field"

gh pr create --base main --head "$BRANCH" \
  --title "chore(edge): cut edge release $SHA" \
  --body "$PR_BODY"

echo "Opened edge release PR for $SHA — review + merge via /ship."
