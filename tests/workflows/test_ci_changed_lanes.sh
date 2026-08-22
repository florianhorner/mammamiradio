#!/usr/bin/env bash
# Self-test for scripts/ci-changed-lanes.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LANES="$REPO_ROOT/scripts/ci-changed-lanes.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

run_lanes() {
  local out
  out="$(mktemp)"
  env -u GITHUB_OUTPUT "$@" GITHUB_OUTPUT="$out" bash "$LANES"
  cat "$out"
  rm -f "$out"
}

push_out="$(EVENT_NAME=push run_lanes)"
printf '%s\n' "$push_out" | grep -qx 'browser=true' || fail "push must enable browser"
printf '%s\n' "$push_out" | grep -qx 'audio=true' || fail "push must enable audio"
pass "push enables every lane"

docs_out="$(EVENT_NAME=pull_request CHANGED_FILES=$'README.md\ndocs/architecture.md' run_lanes)"
printf '%s\n' "$docs_out" | grep -qx 'browser=false' || fail "docs-only must skip browser"
printf '%s\n' "$docs_out" | grep -qx 'media=false' || fail "docs-only must skip media"
printf '%s\n' "$docs_out" | grep -qx 'workflows=false' || fail "docs-only must skip workflow self-tests"
printf '%s\n' "$docs_out" | grep -qx 'audio=false' || fail "docs-only must skip ARM smoke"
pass "docs-only skips expensive lanes"

web_out="$(EVENT_NAME=pull_request CHANGED_FILES=$'mammamiradio/web/static/listener.js' run_lanes)"
printf '%s\n' "$web_out" | grep -qx 'browser=true' || fail "listener.js must enable browser"
printf '%s\n' "$web_out" | grep -qx 'audio=false' || fail "listener.js must not enable ARM"
pass "web path enables browser only"

audio_out="$(EVENT_NAME=pull_request CHANGED_FILES=$'mammamiradio/audio/normalizer.py' run_lanes)"
printf '%s\n' "$audio_out" | grep -qx 'audio=true' || fail "normalizer.py must enable ARM"
printf '%s\n' "$audio_out" | grep -qx 'browser=false' || fail "normalizer.py must not enable browser"
pass "audio path enables ARM only"

wf_out="$(EVENT_NAME=pull_request CHANGED_FILES=$'.github/workflows/quality.yml' run_lanes)"
printf '%s\n' "$wf_out" | grep -qx 'browser=true' || fail "quality.yml must re-run browser"
printf '%s\n' "$wf_out" | grep -qx 'workflows=true' || fail "quality.yml must re-run workflow self-tests"
pass "quality.yml edits re-run browser and workflow self-tests"

echo "ci-changed-lanes: all cases passed."
