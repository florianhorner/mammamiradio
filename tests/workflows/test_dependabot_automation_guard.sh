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
  # Catch both a contiguous command and shell composition such as
  # `body="@dependabot"; action=rebase; gh pr comment ... --body "$body $action"`.
  grep -R -E '@dependabot[[:space:]]+(rebase|recreate)' "$@" >/dev/null 2>&1 \
    && return 0

  while IFS= read -r -d '' file; do
    grep -Iq . "$file" || continue
    grep -q '@dependabot' "$file" || continue
    grep -Eq '(^|[^[:alnum:]_])(rebase|recreate)([^[:alnum:]_]|$)' "$file" \
      || continue
    grep -Eq 'gh[[:space:]]+pr[[:space:]]+comment|gh[[:space:]]+api' "$file" \
      || continue
    return 0
  done < <(find "$@" -type f -print0)

  return 1
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
printf '%s\n' 'body="@dependabot"' \
  'action=rebase' \
  'gh pr comment "$pr" --body "$body $action"' \
  > "$TMPDIR_T/scripts/renamed-helper.sh"
contains_dependabot_command \
  "$TMPDIR_T/.github/workflows" "$TMPDIR_T/scripts" \
  || fail "guard must catch a renamed workflow with an indirect helper"
pass "guard catches renamed and indirect Dependabot command automation"

if contains_dependabot_command "$WORKFLOWS_DIR" "$SCRIPTS_DIR"; then
  fail "workflows and scripts must not automate Dependabot comment commands"
fi
pass "workflows and scripts do not automate Dependabot comment commands"
