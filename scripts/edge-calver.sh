#!/usr/bin/env bash
# Emits the edge add-on calendar version: <date>.<commit-count>
#
# Both segments are monotonic and clock-skew-immune:
#   - date: the CI runner's UTC date (NTP-synced) — cosmetic, human-readable.
#   - commit-count: `git rev-list --count HEAD` — strictly increases with every
#     commit on the branch, uses no timestamps, so it is the real ordering key.
#
# Why not the commit timestamp: a single commit with a skewed committer clock
# (e.g. dated 2099) would, under a timestamp-based scheme, sort newer than every
# real future build and freeze the edge monotonic guard permanently. Commit count
# cannot be skewed.
#
# Requires full git history — the caller's `actions/checkout` must set
# `fetch-depth: 0`, or `git rev-list --count` returns a shallow (wrong) count.
#
# Usage: edge-calver.sh        -> prints e.g. 2026.5.17.1843
set -euo pipefail

COUNT="$(git rev-list --count HEAD)"
DATE="$(date -u +%Y.%-m.%-d)"
echo "${DATE}.${COUNT}"
