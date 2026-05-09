#!/usr/bin/env bash
# Compatibility wrapper. scripts/validate-addon.sh is the canonical HA add-on validator.
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel 2>/dev/null || { cd "$(dirname "$0")/.." && pwd; })
exec "$ROOT/scripts/validate-addon.sh" "$@"
