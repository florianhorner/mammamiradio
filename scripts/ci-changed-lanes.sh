#!/usr/bin/env bash
# Classify which CI lanes a diff should run. Writes GITHUB_OUTPUT keys
# browser/media/workflows/audio as true|false.
#
# Push events enable every lane (main always runs the full set). Pull requests
# classify from BASE_SHA...HEAD_SHA, or from CHANGED_FILES (newline-separated)
# for tests. A failed git diff fail-opens every lane so a filter bug cannot
# skip a required check.
set -euo pipefail

EVENT_NAME="${EVENT_NAME:-}"
BASE_SHA="${BASE_SHA:-}"
HEAD_SHA="${HEAD_SHA:-HEAD}"
OUTPUT="${GITHUB_OUTPUT:-/dev/stdout}"

emit_all() {
  local value="$1"
  {
    echo "browser=$value"
    echo "media=$value"
    echo "workflows=$value"
    echo "audio=$value"
  } >> "$OUTPUT"
}

if [ "$EVENT_NAME" = "push" ]; then
  emit_all true
  exit 0
fi

changed=""
if [ -n "${CHANGED_FILES:-}" ]; then
  changed="$CHANGED_FILES"
else
  if [ -z "$BASE_SHA" ]; then
    if [ "$EVENT_NAME" = "pull_request" ]; then
      echo "ci-changed-lanes: BASE_SHA required for pull_request events" >&2
      exit 1
    fi
    echo "ci-changed-lanes: no BASE_SHA for $EVENT_NAME; enabling every lane" >&2
    emit_all true
    exit 0
  fi
  if ! changed="$(git diff --name-only "$BASE_SHA...$HEAD_SHA")"; then
    echo "ci-changed-lanes: git diff failed; enabling every lane" >&2
    emit_all true
    exit 0
  fi
fi

lane() {
  local pattern="$1"
  if printf '%s\n' "$changed" | grep -E -q "$pattern"; then
    echo true
  else
    echo false
  fi
}

# Optional lanes must include their own workflow plus every input whose changes
# can invalidate the lane-specific proof. The required aggregators accept a
# skipped optional job, so an omitted dependency would otherwise false-green.
browser="$(lane '^(mammamiradio/web/|tests/web/test_admin_browser_smoke\.py|tests/web/test_first_listen_browser_smoke\.py|tests/web/test_player_smoke_contract\.py|scripts/player-smoke\.|scripts/ci-changed-lanes\.sh$|\.playwright-cli-version$|pyproject\.toml$|requirements\.txt$|requirements-dev\.txt$|\.github/workflows/quality\.yml$|\.github/actions/setup-python-ci/)')"
media="$(lane '^(mammamiradio/assets/|mammamiradio/media/|proof/media/|pyproject\.toml$|requirements\.txt$|requirements-dev\.txt$|scripts/media-proof\.py$|scripts/starter-catalog\.py$|scripts/validate-starter-media\.py$|tests/media/|ha-addon/mammamiradio/Dockerfile$|scripts/ci-changed-lanes\.sh$|\.github/workflows/quality\.yml$|\.github/actions/setup-python-ci/)')"
workflows="$(lane '^(\.github/|ha-addon/|scripts/|tests/workflows/|tests/repo/test_preship_evidence_v2\.py$|[^/]+\.py$|(.*/)?conftest\.py$|(.*/)?(\.coveragerc(\.toml)?|\.?pytest\.(toml|ini)|pyproject\.toml|tox\.ini|setup\.cfg)$|requirements\.txt$|requirements-dev\.txt$)')"
audio="$(lane '^(mammamiradio/audio/|tests/audio/|scripts/ha-green-launch-smoke\.py|mammamiradio/scheduling/|mammamiradio/web/streamer\.py|mammamiradio/main\.py$|mammamiradio/core/|pyproject\.toml$|requirements\.txt$|requirements-dev\.txt$|radio\.toml$|model_registry\.toml$|scripts/ci-changed-lanes\.sh$|\.github/workflows/pi-smoke\.yml$|\.github/actions/setup-python-ci/)')"

{
  echo "browser=$browser"
  echo "media=$media"
  echo "workflows=$workflows"
  echo "audio=$audio"
} >> "$OUTPUT"
