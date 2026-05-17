#!/usr/bin/env bash
# Monotonic guard for the edge add-on version bump (addon-build.yml bump-edge job).
#
# Usage: edge-version-newer.sh <current> <candidate>
#   Exit 0  -> <candidate> is strictly newer than <current>; the bump should proceed.
#   Exit 1  -> equal, older, or malformed; the bump must be skipped.
#
# Protects against a raced or rerun CI build writing a stale calendar version
# into ha-addon/mammamiradio-edge/config.yaml (which would make HA see a
# version downgrade).
set -euo pipefail

CURRENT="${1:-}"
CANDIDATE="${2:-}"

VER_RE='^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$'
if ! echo "$CURRENT" | grep -qE "$VER_RE" || ! echo "$CANDIDATE" | grep -qE "$VER_RE"; then
    echo "edge-version-newer: malformed version (current='$CURRENT' candidate='$CANDIDATE')" >&2
    exit 1
fi

# Equal -> nothing to do.
if [ "$CURRENT" = "$CANDIDATE" ]; then
    exit 1
fi

# sort -V orders dotted numerics as versions; the candidate must be the greatest.
GREATEST=$(printf '%s\n%s\n' "$CURRENT" "$CANDIDATE" | sort -V | tail -1)
[ "$GREATEST" = "$CANDIDATE" ]
