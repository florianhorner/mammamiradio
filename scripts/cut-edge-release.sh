#!/usr/bin/env bash
# Cut a manual edge release.
#
# Sets the edge add-on's version: to the current origin/main short SHA and opens a
# PR you merge via /ship. The version string IS the Docker image tag the HA
# Supervisor pulls (ha-addon/mammamiradio-edge/config.yaml `version:` ->
# ghcr.io/<owner>/mammamiradio-addon-{arch}:<short-sha>), and "update available" is
# a version-string compare — so changing it surfaces an in-place Update on the Pi.
#
# No CI bot, no protected-main self-merge: YOU open the PR (so its required checks
# fire) and YOU merge it. Run AFTER `Build HA Addon` is green on the commit you are
# releasing, so the :<short-sha> image exists. Stable is never touched.
#
# Usage: make edge-release   (or: bash scripts/cut-edge-release.sh)
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree not clean — commit or stash first, then cut the edge release." >&2
  exit 1
fi

git fetch origin main --quiet
SHA="$(git rev-parse --short=7 origin/main)"
CURRENT="$(grep '^version:' "$EDGE_CONFIG" | awk '{print $2}' | tr -d '"')"

if [ "$CURRENT" = "$SHA" ]; then
  echo "Edge add-on already at $SHA (origin/main) — nothing to release."
  exit 0
fi

# The version string must resolve to an image tag that exists. The build pushes
# :<short-sha> on every main merge; verify it before advertising it, or the
# Supervisor would try to pull a non-existent tag and the update would fail.
OWNER="$(git remote get-url origin | sed 's|.*github.com[:/]||;s|/.*||')"
PKG="mammamiradio-addon-aarch64"
if TAGS="$(gh api "/users/$OWNER/packages/container/$PKG/versions" --jq '.[].metadata.container.tags[]' 2>/dev/null)"; then
  if ! printf '%s\n' "$TAGS" | grep -qx "$SHA"; then
    echo "ERROR: image ghcr.io/$OWNER/$PKG:$SHA not found." >&2
    echo "       Wait for 'Build HA Addon' to finish on origin/main ($SHA), then re-run." >&2
    exit 1
  fi
  echo "Verified image tag :$SHA exists."
else
  echo "WARNING: could not query GHCR to verify the :$SHA image (continuing)." >&2
  echo "         Ensure 'Build HA Addon' is green on $SHA before merging the PR." >&2
fi

BRANCH="edge-release/$SHA"
if EXISTING="$(gh pr list --head "$BRANCH" --state open --json url -q '.[0].url' 2>/dev/null)" && [ -n "$EXISTING" ]; then
  echo "An edge release PR for $SHA is already open: $EXISTING"
  echo "Merge it via /ship, or close it and re-run."
  exit 0
fi
# -B re-creates the branch from origin/main even if a stale local one exists, so a
# re-run after a failed attempt is idempotent. Errors are NOT swallowed.
git checkout -B "$BRANCH" origin/main
sed -i.bak "s/^version: .*/version: $SHA/" "$EDGE_CONFIG"
rm -f "$EDGE_CONFIG.bak"
git add "$EDGE_CONFIG"
git commit -q -m "chore(edge): cut edge release $SHA"
git push -u origin "$BRANCH" --force --quiet

PR_BODY="Cut the edge (dev) channel to \`$SHA\` (current \`main\`). The edge add-on's \`version:\`
is the image tag the Supervisor pulls (\`ghcr.io/$OWNER/mammamiradio-addon-{arch}:$SHA\`,
built by Build HA Addon on $SHA), so the soak Pi shows an in-place Update. Manual edge
release; stable is untouched.

## Proof

- [ ] build: n/a — metadata-only edge version bump; the multi-arch image was already built and pushed by Build HA Addon on $SHA
- [ ] tests: n/a — config-only, no code paths
- [ ] lint: n/a — single version-line change
- [ ] runtime: n/a — pulls the pre-built GHCR tag mammamiradio-addon-arch-$SHA already smoke-tested in CI on $SHA
- [ ] schema: n/a — edge option and schema unchanged, only the version field"

gh pr create --base main --head "$BRANCH" \
  --title "chore(edge): cut edge release $SHA" \
  --body "$PR_BODY"

echo "Opened edge release PR for $SHA — review + merge via /ship."
