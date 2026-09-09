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
#   I3  edge never pins a built SHA when any IMAGE_CONTENT_PATHS file differs
#       between that SHA and origin/main — the edge branch takes its metadata from
#       main, so the pinned image would not implement the metadata being advertised.
#       Files that only re-trigger the build (a dev lockfile, a validator script, a
#       test) are in IMAGE_PATHS but not in IMAGE_CONTENT_PATHS: they never enter
#       the image. Only the edge config's valid top-level version line is exempt.
#       A newer main commit with an attempted build but no successful run blocks
#       the pin: unchanged image content does not excuse failed proof.
#
# Every function fails CLOSED: an unverifiable state (gh error, git error) is a
# refusal, never a soft pass.
#
# shellcheck shell=bash

# Paths that trigger Build HA Addon — must mirror addon-build.yml `on.push.paths`.
# tests/workflows/test_cut_edge_release.sh asserts this parity on every run.
# Consumed by scripts/cut-edge-release.sh (sourced) and the parity tests, not here.
# shellcheck disable=SC2034
IMAGE_PATHS="ha-addon mammamiradio proof/media pyproject.toml requirements.txt requirements-dev.txt radio.toml model_registry.toml scripts/media-proof.py scripts/starter-catalog.py scripts/validate-addon.sh scripts/validate-starter-media.py scripts/ha-green-launch-smoke.py scripts/ha-green-perf-smoke.py tests/media tests/playlist/test_jamendo_transient.py tests/playlist/test_legacy_media.py tests/scheduling/test_queue_mutations.py tests/web/test_streamer_routes_extended.py .github/workflows/addon-build.yml"

# Paths whose content enters the add-on image or its Supervisor-facing metadata:
# the sources addon-build.yml stages into the build context ("Copy source into
# addon build context": mammamiradio/, pyproject.toml, model_registry.toml) plus
# the COPY lines in ha-addon/mammamiradio/Dockerfile (radio.toml, rootfs/), the
# stable and edge add-on directories (config, translations, access policy), and
# the workflow that picks the base image and build args. The drift check (I3)
# exempts only a valid version-line change in the edge config.
# It must stay a subset of IMAGE_PATHS and cover every staged source;
# tests/workflows/test_cut_edge_release.sh asserts both directions.
IMAGE_CONTENT_PATHS="ha-addon/mammamiradio ha-addon/mammamiradio-edge mammamiradio pyproject.toml radio.toml model_registry.toml .github/workflows/addon-build.yml"

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
  runs="$(gh run list --workflow=addon-build.yml --branch main --commit "$target" \
    --status success --limit 1 --json conclusion \
    -q 'if type != "array" then error("invalid run list")
        elif length > 1 or any(.[]; type != "object" or .conclusion != "success") then error("invalid success run")
        else length end' 2>/dev/null)" || return 2
  case "$runs" in 1) return 0 ;; 0) return 1 ;; *) return 2 ;; esac
}

# Check every intervening main SHA directly; the candidate window cannot prove
# that an older failed run is absent. No run is allowed for content-identical
# commits, as are completed skipped runs (the workflow skips edge-version cuts).
# Otherwise require a success for that SHA, including retries. A full response
# page cannot prove all attempts were skipped, so it also requires success.
# Return 2 when proof cannot be read.
edge_newer_builds_verified() {
  local target="$1" ref="${2:-origin/main}" commits commit runs rc limit=100
  commits="$(git rev-list "$target..$ref" 2>/dev/null)" || return 2
  while IFS= read -r commit; do
    [ -n "$commit" ] || continue
    runs="$(gh run list --workflow=addon-build.yml --branch main --commit "$commit" \
      --limit "$limit" --json status,conclusion \
      -q "if type != \"array\" then error(\"invalid run list\")
          elif any(.[]; type != \"object\" or (.status | type) != \"string\" or
                        (.conclusion | type) != \"string\") then error(\"invalid run\")
          elif length >= $limit or any(.[]; .status != \"completed\" or .conclusion != \"skipped\")
          then 1 else 0 end" \
      2>/dev/null)" || return 2
    case "$runs" in
      0) continue ;;
      1)
        edge_commit_has_green_build "$commit" || {
          rc=$?
          echo "Build HA Addon has no verified successful run for newer main commit $commit." >&2
          return "$rc"
        } ;;
      *) return 2 ;;
    esac
  done <<< "$commits"
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

# edge_image_drift <sha> [<ref>] -> prints IMAGE_CONTENT_PATHS files that changed between
# <sha> and <ref> (default origin/main). Empty output + 0 means no drift.
# Returns 2 when the diff itself could not be computed — an unverifiable drift
# check is a refusal, never an assumed-clean pass.
edge_image_drift() (
  local target="$1" ref="${2:-origin/main}" changed top path before after rc drift=""
  # No `|| true`: `git diff --name-only` already exits 0 for both changed and
  # unchanged, so a non-zero here is a real verification failure (bad object,
  # git error). Treat it like every other unverifiable state — hard-fail.
  # Pathspecs are relative to the cwd; anchor at the repository root so a caller in
  # a subdirectory cannot get an empty diff and accept a stale image.
  top="$(git rev-parse --show-toplevel 2>/dev/null)" || return 2
  cd "$top" || return 2
  # shellcheck disable=SC2086  # IMAGE_CONTENT_PATHS intentionally word-splits into pathspecs
  changed="$(git diff --no-renames --name-only "$target" "$ref" -- $IMAGE_CONTENT_PATHS 2>/dev/null)" || return 2
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    if [ "$path" = "$EDGE_CONFIG" ]; then
      rc=0
      before="$(edge_config_without_version "$target")" || rc=$?
      [ "$rc" -ne 2 ] || return 2
      if [ "$rc" -eq 0 ]; then
        after="$(edge_config_without_version "$ref")" || rc=$?
        [ "$rc" -ne 2 ] || return 2
        if [ "$rc" -eq 0 ] && [ "$before" = "$after" ]; then continue; fi
      fi
    fi
    drift="${drift}${path}"$'\n'
  done <<< "$changed"
  printf '%s' "$drift"
  [ -z "$drift" ]
)

# Compare immutable regular-file snapshots, including modes and trailing lines.
# Malformed/duplicate version fields cannot take the version-only exception.
edge_config_without_version() (
  set -o pipefail
  local entry content blob nul_free
  entry="$(git ls-tree "$1" -- "$EDGE_CONFIG" 2>/dev/null)" || return 2
  case "$entry" in '100644 blob '*|'100755 blob '*) ;; *) return 1 ;; esac
  # Bash drops NUL bytes in command substitutions. Prove none are present before
  # storing the text; hash-object without -w does not write an object.
  blob="${entry#* blob }"; blob="${blob%%$'\t'*}"
  nul_free="$(git show "$1:$EDGE_CONFIG" 2>/dev/null | tr -d '\000' | git hash-object --stdin)" || return 2
  [ "$blob" = "$nul_free" ] || return 1
  content="$(git show "$1:$EDGE_CONFIG" 2>/dev/null && printf '.')" || return 2
  printf '%s\n' "$content" | awk -v mode="${entry%% *}" '
    BEGIN { print mode }
    /^version:/ {
      count++
      value = substr($0, 10)
      if (value ~ /^"[0-9a-f]+"$/ || value ~ /^\047[0-9a-f]+\047$/)
        value = substr(value, 2, length(value) - 2)
      if ($0 !~ /^version: / || value !~ /^[0-9a-f]+$/ || length(value) < 7 || length(value) > 40)
        invalid = 1
      next
    }
    { print }
    END { if (count != 1 || invalid) exit 1 }
  '
)

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
  edge_newer_builds_verified "$target" "$ref" || return $?
  git rev-parse --short=7 "$target"
}
