#!/usr/bin/env bash
# Prevent the retired Dependabot comment loop from returning.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WORKFLOWS_DIR="$REPO_ROOT/.github/workflows"
SCRIPTS_DIR="$REPO_ROOT/scripts"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

contains_dependabot_command() {
  grep -R -E '@dependabot[[:space:]]+(rebase|recreate)' "$@" >/dev/null 2>&1
}

[ ! -e "$WORKFLOWS_DIR/dependabot-nudge.yml" ] \
  || fail "dependabot-nudge.yml must stay retired"
pass "Dependabot nudge workflow stays retired"

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT
mkdir -p "$TMPDIR_T/.github/workflows" "$TMPDIR_T/scripts"
printf '%s\n' 'run: bash scripts/renamed-helper.sh' \
  > "$TMPDIR_T/.github/workflows/renamed.yml"
# shellcheck disable=SC2016  # Fixture must preserve literal runtime variables.
printf '%s\n' 'NUDGE_BODY="@dependabot rebase"' \
  'gh pr comment "$pr" --body "$NUDGE_BODY"' \
  > "$TMPDIR_T/scripts/renamed-helper.sh"
contains_dependabot_command \
  "$TMPDIR_T/.github/workflows" "$TMPDIR_T/scripts" \
  || fail "guard must catch a renamed workflow with an indirect helper"
pass "guard catches renamed and indirect Dependabot command automation"

if contains_dependabot_command "$WORKFLOWS_DIR" "$SCRIPTS_DIR"; then
  fail "workflows and scripts must not automate Dependabot comment commands"
fi
pass "workflows and scripts do not automate Dependabot comment commands"
