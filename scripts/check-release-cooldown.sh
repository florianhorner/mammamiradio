#!/usr/bin/env bash
# Blocks a release if the prior release tag is less than MIN_COOLDOWN_HOURS old.
#
# Rule: GIT_TIME(prior_release) + MIN_COOLDOWN_HOURS > NOW  =>  exit 1 (block)
#
# Usage:
#   check-release-cooldown.sh [prior_iso] [now_iso]
#
# Both args optional:
#   - prior_iso: prior release publishedAt (ISO-8601 UTC). Defaults to
#     `gh release list --limit 1 --json publishedAt`.
#   - now_iso:   current time (ISO-8601 UTC). Defaults to `date -u`.
#
# Env:
#   MIN_COOLDOWN_HOURS  default 24
#
# Exit:
#   0  release allowed (cooldown cleared, or no prior release)
#   1  release blocked (within cooldown window)
#   2  usage / lookup error

set -euo pipefail

MIN_COOLDOWN_HOURS="${MIN_COOLDOWN_HOURS:-24}"

prior_iso="${1:-}"
now_iso="${2:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

if [[ -z "$prior_iso" ]]; then
  if ! command -v gh >/dev/null 2>&1; then
    echo "check-release-cooldown: gh CLI not available and no prior_iso provided" >&2
    exit 2
  fi
  prior_iso=$(gh release list --limit 1 --json publishedAt --jq '.[0].publishedAt // ""' 2>/dev/null || echo "")
fi

if [[ -z "$prior_iso" ]]; then
  echo "check-release-cooldown: no prior release found. Cooldown does not apply."
  exit 0
fi

iso_to_epoch() {
  local iso="$1"
  # Try GNU date first (Linux / CI), then BSD date (macOS).
  date -u -d "$iso" +%s 2>/dev/null \
    || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$iso" +%s 2>/dev/null \
    || { echo "check-release-cooldown: unparseable ISO timestamp: $iso" >&2; exit 2; }
}

prior_epoch=$(iso_to_epoch "$prior_iso")
now_epoch=$(iso_to_epoch "$now_iso")

diff_seconds=$((now_epoch - prior_epoch))
cooldown_seconds=$((MIN_COOLDOWN_HOURS * 3600))

if (( diff_seconds < 0 )); then
  echo "check-release-cooldown: clock skew detected (now is before prior release). Allowing." >&2
  exit 0
fi

diff_hours=$((diff_seconds / 3600))
diff_minutes=$(((diff_seconds % 3600) / 60))

if (( diff_seconds < cooldown_seconds )); then
  remaining=$((cooldown_seconds - diff_seconds))
  rem_h=$((remaining / 3600))
  rem_m=$(((remaining % 3600) / 60))
  echo "BLOCKED: prior release at $prior_iso (${diff_hours}h${diff_minutes}m ago)."
  echo "cooldown: ${rem_h}h${rem_m}m remaining (MIN_COOLDOWN_HOURS=${MIN_COOLDOWN_HOURS})"
  exit 1
fi

echo "OK: prior release at $prior_iso (${diff_hours}h${diff_minutes}m ago). Cooldown cleared."
exit 0
