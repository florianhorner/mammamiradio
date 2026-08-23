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
  local status=0
  out="$(mktemp)"
  if env -u GITHUB_OUTPUT GITHUB_OUTPUT="$out" bash "$LANES"; then
    status=0
  else
    status=$?
  fi
  cat "$out"
  rm -f "$out"
  return "$status"
}

assert_path_enables() {
  local lane="$1"
  local path="$2"
  local out
  out="$(EVENT_NAME=pull_request CHANGED_FILES="$path" run_lanes)"
  printf '%s\n' "$out" | grep -qx "$lane=true" || fail "$path must enable $lane"
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

for browser_input in \
  .github/actions/setup-python-ci/action.yml \
  pyproject.toml \
  requirements.txt \
  requirements-dev.txt
do
  assert_path_enables browser "$browser_input"
done
pass "browser install inputs enable browser smoke"

for lane_name in browser media workflows audio
do
  assert_path_enables "$lane_name" scripts/ci-changed-lanes.sh
done
pass "classifier changes fail open every lane"

audio_out="$(EVENT_NAME=pull_request CHANGED_FILES=$'mammamiradio/audio/normalizer.py' run_lanes)"
printf '%s\n' "$audio_out" | grep -qx 'audio=true' || fail "normalizer.py must enable ARM"
printf '%s\n' "$audio_out" | grep -qx 'browser=false' || fail "normalizer.py must not enable browser"
pass "audio path enables ARM only"

wf_out="$(EVENT_NAME=pull_request CHANGED_FILES=$'.github/workflows/quality.yml' run_lanes)"
printf '%s\n' "$wf_out" | grep -qx 'browser=true' || fail "quality.yml must re-run browser"
printf '%s\n' "$wf_out" | grep -qx 'media=true' || fail "quality.yml must re-run media"
printf '%s\n' "$wf_out" | grep -qx 'workflows=true' || fail "quality.yml must re-run workflow self-tests"
pass "quality.yml edits re-run browser, media, and workflow self-tests"

for media_input in \
  mammamiradio/media/starter.py \
  ha-addon/mammamiradio/Dockerfile \
  .github/actions/setup-python-ci/action.yml \
  pyproject.toml \
  requirements.txt \
  requirements-dev.txt \
  scripts/validate-starter-media.py \
  scripts/starter-catalog.py
do
  assert_path_enables media "$media_input"
done
pass "media inputs enable media reporting"

assert_path_enables workflows ha-addon/mammamiradio-edge/config.yaml
pass "add-on changes enable workflow self-tests"

for audio_input in \
  mammamiradio/main.py \
  mammamiradio/core/config.py \
  .github/actions/setup-python-ci/action.yml \
  pyproject.toml \
  radio.toml \
  requirements.txt \
  requirements-dev.txt
do
  assert_path_enables audio "$audio_input"
done
pass "ARM install inputs enable ARM smoke"

audio_workflow_out="$(EVENT_NAME=pull_request CHANGED_FILES=.github/workflows/pi-smoke.yml run_lanes)"
printf '%s\n' "$audio_workflow_out" | grep -qx 'audio=true' || fail "pi-smoke.yml must fail open ARM smoke"
pass "pi-smoke workflow changes enable ARM smoke"

diff_fail_out="$(EVENT_NAME=pull_request BASE_SHA=not-a-real-base HEAD_SHA=HEAD run_lanes 2>/dev/null)"
for lane_name in browser media workflows audio
do
  printf '%s\n' "$diff_fail_out" | grep -qx "$lane_name=true" || fail "git diff failure must enable $lane_name"
done
pass "git diff failures fail open every lane"

missing_base_status=0
if EVENT_NAME=pull_request run_lanes >/dev/null 2>&1; then
  missing_base_status=0
else
  missing_base_status=$?
fi
[ "$missing_base_status" -ne 0 ] || fail "pull requests without BASE_SHA must fail the required changes lane"
pass "missing BASE_SHA fails the required changes lane"

for event_name in workflow_dispatch schedule merge_group
do
  non_pr_out="$(EVENT_NAME="$event_name" run_lanes 2>/dev/null)"
  for lane_name in browser media workflows audio
  do
    printf '%s\n' "$non_pr_out" | grep -qx "$lane_name=true" || fail "$event_name without BASE_SHA must enable $lane_name"
  done
done
pass "non-PR events without BASE_SHA fail open every lane"

echo "ci-changed-lanes: all cases passed."
