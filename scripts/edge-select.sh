#!/usr/bin/env bash
# edge-select.sh — which main commit may edge point at, in ONE implementation.
#
# Sourced by scripts/cut-edge-release.sh (which cuts the PR) and by
# scripts/land-queue-plan.sh (which only reports what an edge controller would
# pin). Both must reach the same verdict; a second copy of this selection is
# exactly where the old GHCR soft-pass bug lived, so there is only one.
#
# The two invariants this file owns:
#
#   I2  edge `version:` is always a short SHA with a SUCCESSFUL `Build HA Addon`
#       run for that exact commit — the version string IS the image tag the
#       Supervisor pulls, so an unbuilt SHA is an uninstallable add-on.
#   I3  edge never pins a built SHA when any IMAGE_PATHS file differs between
#       that SHA and origin/main — the edge branch takes its metadata from main,
#       so the pinned image would not implement the metadata being advertised.
#
# Every function fails CLOSED: an unverifiable state (gh error, git error) is a
# refusal, never a soft pass.
#
# shellcheck shell=bash

# Paths that trigger Build HA Addon — must mirror addon-build.yml `on.push.paths`.
# tests/workflows/test_cut_edge_release.sh asserts this parity on every run.
IMAGE_PATHS="ha-addon mammamiradio proof/media pyproject.toml requirements.txt requirements-dev.txt radio.toml model_registry.toml scripts/media-proof.py scripts/starter-catalog.py scripts/validate-addon.sh scripts/validate-starter-media.py scripts/ha-green-launch-smoke.py scripts/ha-green-perf-smoke.py tests/media tests/playlist/test_jamendo_transient.py tests/playlist/test_legacy_media.py tests/scheduling/test_queue_mutations.py tests/web/test_streamer_routes_extended.py .github/workflows/addon-build.yml"

# The edge add-on config whose `version:` field IS the image tag the Supervisor
# pulls. cut-edge-release.sh sets this before sourcing; the default serves every
# other caller.
EDGE_CONFIG="${EDGE_CONFIG:-ha-addon/mammamiradio-edge/config.yaml}"

# How far back to look for green builds. `gh run list` orders by run-creation
# time, not commit topology, so this is a candidate window, not a ranking.
EDGE_RUN_LOOKBACK="${EDGE_RUN_LOOKBACK:-40}"

# edge_green_shas -> newline list of head SHAs with a successful Build HA Addon run.
# A successful run means validate -> build (both arches) -> push -> smoke all
# passed, so both :<short-sha> images were pushed AT BUILD TIME. (A later GHCR
# prune is not detected — acceptable: the add-on images are not pruned, and the
# drift guard still blocks the dangerous "pin an image that predates an add-on
# change" case.) Returns non-zero if the query itself fails.
edge_green_shas() {
  gh run list --workflow=addon-build.yml --branch main --limit "$EDGE_RUN_LOOKBACK" \
    --json headSha,status,conclusion \
    -q '[.[] | select(.status == "completed" and .conclusion == "success") | .headSha] | .[]' \
    2>/dev/null
}

# edge_commit_has_green_build <full-sha> -> 0 if that exact commit has a green build.
#
# `--status success` filters SERVER-side, so `--limit 1` is enough: we only need
# to know whether ANY successful run exists. Filtering client-side over a capped
# page would reintroduce the window bug one level down — enough newer failed
# reruns on the same commit would push the successful one out of the page.
# Returns 2 (distinct from "no build") when the query itself fails.
edge_commit_has_green_build() {
  local target="$1" runs
  runs="$(gh run list --workflow=addon-build.yml --commit "$target" \
    --status success --limit 1 --json conclusion -q 'length' 2>/dev/null)" || return 2
  [ "${runs:-0}" -ge 1 ]
}

# edge_newest_built_sha [<ref>] -> full SHA of the newest commit on <ref>
# (default origin/main) that has a green build. Empty output + non-zero when none.
#
# Selecting from `git rev-list --topo-order` makes the result inherently an
# ancestor of the ref and topology-correct (children before parents) even when a
# merged branch carries stale commit dates or an older commit was re-run after a
# newer one.
edge_newest_built_sha() {
  local ref="${1:-origin/main}" green match
  green="$(edge_green_shas)" || return 2
  [ -n "$green" ] || return 1
  # One grep over the whole walk rather than a fork per commit: when no candidate
  # is in the lookback window this walks the entire history, and a fork per commit
  # made the failure path the most expensive one.
  #
  # Both inputs are process substitutions rather than a pipe: `grep -m1` exits at
  # the first match, which would SIGPIPE a piped `git rev-list`, and `pipefail`
  # would then report the successful lookup as a failure.
  local commits
  commits="$(git rev-list --topo-order "$ref")" || return 2
  match="$(grep -m1 -xF -f <(printf '%s\n' "$green") <(printf '%s\n' "$commits"))" \
    || return 1
  printf '%s\n' "$match"
}

# edge_image_drift <sha> [<ref>] -> prints IMAGE_PATHS files that changed between
# <sha> and <ref> (default origin/main). Empty output + 0 means no drift.
# Returns 2 when the diff itself could not be computed — an unverifiable drift
# check is a refusal, never an assumed-clean pass.
edge_image_drift() {
  local target="$1" ref="${2:-origin/main}" changed
  # shellcheck disable=SC2086  # IMAGE_PATHS intentionally word-splits into pathspecs
  # No `|| true`: `git diff --name-only` already exits 0 for both changed and
  # unchanged, so a non-zero here is a real verification failure (bad object,
  # git error). Treat it like every other unverifiable state — hard-fail.
  changed="$(git diff --name-only "$target" "$ref" -- $IMAGE_PATHS 2>/dev/null)" || return 2
  printf '%s' "$changed"
  [ -z "$changed" ]
}

# edge_pinned_version [<ref>] -> the short SHA edge currently advertises on <ref>
# (default origin/main), or empty when the config cannot be read. Reads the ref,
# never the working tree: a stale local branch that already carries the new
# version must not read as "already released".
edge_pinned_version() {
  { git show "${1:-origin/main}:$EDGE_CONFIG" 2>/dev/null || true; } \
    | awk '/^version:/ { print $2; exit }' | tr -d '"'
}

# eligible_edge_sha [<ref>] -> short SHA edge may be pinned to, or non-zero with
# a plain-language reason on stderr. This is the whole of the auto-edge
# eligibility function: newest green-built ancestor of <ref>, with no image drift
# between it and <ref>.
eligible_edge_sha() {
  local ref="${1:-origin/main}" target rc drift
  target="$(edge_newest_built_sha "$ref")" || {
    rc=$?
    if [ "$rc" -eq 2 ]; then
      echo "could not query 'Build HA Addon' runs — refusing to name an edge target." >&2
    else
      echo "no successful 'Build HA Addon' run for any commit on $ref." >&2
    fi
    return "$rc"
  }
  drift="$(edge_image_drift "$target" "$ref")" || {
    rc=$?
    if [ "$rc" -eq 2 ]; then
      echo "could not verify whether add-on image files changed since $target." >&2
      return 2
    fi
    echo "add-on image files changed between $(git rev-parse --short=7 "$target") and $ref:" >&2
    printf '%s\n' "$drift" | sed 's/^/  /' >&2
    return 1
  }
  git rev-parse --short=7 "$target"
}
