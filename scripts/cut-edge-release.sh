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
# builds an image when a commit touches the IMAGE_PATHS below: add-on or application
# source, canonical project/model config, media-proof inputs, image validation/smoke
# scripts, or the build workflow itself. When the tip commits are outside that trigger
# set, no :<sha> image exists for them, so pinning HEAD would make the Supervisor pull
# a missing tag. This script picks the newest main commit with a successful build run
# (that success is the proof both per-arch images were pushed, proven, and smoked) and
# HARD-FAILS rather than advertise an unverified tag. It also refuses if any trigger
# path changed between that built commit and HEAD — the pinned image would not implement
# the newer edge metadata.
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
MEDIA_PYTHON="python3"
if ! "$MEDIA_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  if [ -x .venv/bin/python ]; then
    MEDIA_PYTHON=".venv/bin/python"
  else
    MEDIA_PYTHON="python3.11"
  fi
fi

# --target-sha <sha> pins edge to one EXACT commit instead of "newest built".
#
# Needed by the release cut in docs/release-process.md: to soak the exact commit
# you are about to tag, edge must point at that commit and no other. The default
# selection walks origin/main newest-first, so a merge landing mid-cut would
# silently pin past the commit under test and the soak would prove nothing.
REQUESTED_SHA=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target-sha)
      REQUESTED_SHA="${2:-}"
      if [ -z "$REQUESTED_SHA" ]; then
        echo "ERROR: --target-sha needs a commit-ish" >&2
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--target-sha <commit>]"
      echo "  (no args)          pin edge to the newest origin/main commit with a green build"
      echo "  --target-sha <sha> pin edge to that exact commit, refusing any other"
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# IMAGE_PATHS, the green-build queries, and the drift check live in
# scripts/edge-select.sh so this cut and the shadow land queue
# (scripts/land-queue-plan.sh) select the same commit from one implementation.
EDGE_SELECT_LIB="$ROOT/scripts/edge-select.sh"
if [ ! -r "$EDGE_SELECT_LIB" ]; then
  echo "ERROR: edge selection library not found at $EDGE_SELECT_LIB." >&2
  exit 1
fi
# shellcheck source=scripts/edge-select.sh
. "$EDGE_SELECT_LIB"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree not clean — commit or stash first, then cut the edge release." >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI not available — cannot verify which main commit has a built image." >&2
  exit 1
fi

git fetch origin main --quiet

# Candidate set: recent origin/main commits whose `Build HA Addon` run SUCCEEDED
# (see edge_green_shas in scripts/edge-select.sh for what that success proves and
# for the lookback window). The result is ordered by run-creation time, not commit
# topology, so it is only a candidate set; the topologically-newest one is picked
# below. Hard-fail (never soft-pass) if the query fails.
OK_SHAS="$(edge_green_shas)" || {
  echo "ERROR: could not query 'Build HA Addon' runs (gh run list failed)." >&2
  echo "       Refusing to cut an edge release without a verified built commit." >&2
  exit 1
}

# Walk origin/main newest-first and take the first commit that has a green build.
# Selecting from `git rev-list --topo-order origin/main` makes the result inherently
# an ancestor of main and topology-correct (children before parents) even when a merged
# branch carries stale commit dates or an older commit was re-run after a newer one.
TARGET_FULL=""
if [ -n "$REQUESTED_SHA" ]; then
  # Exact-commit mode: resolve, then refuse anything that is not that commit.
  if ! TARGET_FULL="$(git rev-parse --verify "${REQUESTED_SHA}^{commit}" 2>/dev/null)"; then
    echo "ERROR: --target-sha '$REQUESTED_SHA' is not a commit in this repository." >&2
    exit 1
  fi
  if ! git merge-base --is-ancestor "$TARGET_FULL" origin/main; then
    echo "ERROR: --target-sha $REQUESTED_SHA is not an ancestor of origin/main." >&2
    echo "       Edge may only point at a commit that is actually on main." >&2
    exit 1
  fi
  # Query the runs for THIS commit rather than reusing OK_SHAS. OK_SHAS is the 40
  # most recent runs on main, so a target older than that window would be rejected
  # as "no green build" even though its image exists.
  #
  # edge_commit_has_green_build filters server-side (see the rationale there), and
  # returns 2 for a failed query so an unverifiable answer is distinguishable from
  # a genuine "no build". Hard-fail on both — never soft-pass.
  edge_commit_has_green_build "$TARGET_FULL" && TARGET_BUILD_RC=0 || TARGET_BUILD_RC=$?
  if [ "$TARGET_BUILD_RC" -eq 2 ]; then
    echo "ERROR: could not query 'Build HA Addon' runs for $REQUESTED_SHA." >&2
    echo "       Refusing to pin an unverified commit." >&2
    exit 1
  fi
  if [ "$TARGET_BUILD_RC" -ne 0 ]; then
    echo "ERROR: --target-sha $REQUESTED_SHA has no successful 'Build HA Addon' run." >&2
    echo "       No :<short-sha> image exists for it, so the Supervisor would pull a" >&2
    echo "       missing tag. Wait for its build to go green, then re-run." >&2
    exit 1
  fi
else
  while IFS= read -r _commit; do
    [ -n "$_commit" ] || continue
    if printf '%s\n' "$OK_SHAS" | grep -qxF "$_commit"; then
      TARGET_FULL="$_commit"
      break
    fi
  done < <(git rev-list --topo-order origin/main)
fi

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
# edge_image_drift returns 2 when the diff itself could not be computed — an
# unverifiable drift check is a refusal, never an assumed-clean pass.
CHANGED="$(edge_image_drift "$TARGET_FULL" origin/main)" && DRIFT_RC=0 || DRIFT_RC=$?
if [ "$DRIFT_RC" -eq 2 ]; then
  echo "ERROR: could not verify whether add-on image files changed since $SHA." >&2
  echo "       Refusing to cut an edge release without a verified drift check." >&2
  exit 1
fi
if [ -n "$CHANGED" ]; then
  echo "ERROR: add-on image files changed between $SHA and origin/main:" >&2
  printf '%s\n' "$CHANGED" | sed 's/^/         /' >&2
  echo "       The edge branch takes its metadata (options/schema, run.sh) from" >&2
  echo "       origin/main, so pinning $SHA would advertise metadata that image does" >&2
  echo "       not implement." >&2
  if [ -n "$REQUESTED_SHA" ]; then
    # Mode-aware: with an explicit target the problem is never "wait for a build".
    # A newer image-affecting commit has landed, so this commit can no longer be
    # soaked as edge — the metadata has moved on without it.
    echo "       You asked for --target-sha $REQUESTED_SHA, but a newer image-affecting" >&2
    echo "       commit has landed on main since, so that commit can no longer be soaked" >&2
    echo "       as edge. Either revert the newer commit if it was not meant to be in" >&2
    echo "       this release, or accept it and cut from current main instead." >&2
  else
    echo "       The newest add-on-affecting commit has no green 'Build HA Addon' image yet" >&2
    echo "       (still building, or its build failed). Wait for that build (or fix it)," >&2
    echo "       then re-run." >&2
  fi
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
# Report-only while the twelve starter tracks are absent by design: run the
# proof and print its verdict, but do not block the edge cut on the missing
# content. The hard gate stays in scripts/pre-release-check.sh (section 10),
# so stable-release paths remain blocked.
if "$MEDIA_PYTHON" scripts/media-proof.py --quick; then
  echo "media-proof: PASS"
else
  echo "NOTICE: media-proof reported missing content: the twelve starter-catalog tracks (normalized audio and human-audition evidence) have not landed yet."
  echo "NOTICE: the edge cut proceeds report-only; scripts/pre-release-check.sh still fails hard on this proof on the release path."
fi
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
