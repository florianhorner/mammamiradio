#!/usr/bin/env bash
# Self-test for scripts/check-issue-body-lint.sh
#
# Runs fixture bodies through the lint (no network, no gh CLI) and asserts the
# expected allow/block/usage-error behavior, including one case per pattern in
# the workspace-archaeology group. Exits non-zero on any mismatch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/check-issue-body-lint.sh"

if [[ ! -x "$SCRIPT" ]]; then
  chmod +x "$SCRIPT"
fi

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT
BODY="$TMPDIR_T/body.md"

expect_block() { # $1=case name, body already written
  if bash "$SCRIPT" "$BODY" >/dev/null 2>&1; then
    fail "$1: should be blocked, was allowed"
  fi
  pass "$1 blocked"
}

expect_clean() { # $1=case name, body already written
  if ! bash "$SCRIPT" "$BODY" >/dev/null 2>&1; then
    fail "$1: should be clean, was blocked"
  fi
  pass "$1 clean"
}

# Case 1: clean, user-facing issue body passes
cat > "$BODY" <<'EOF'
The station keeps playing the same three songs after a source switch.

Steps: switch from charts to local music while a song is airing.
Expected: fresh rotation from the new source.
EOF
expect_clean "clean user-facing body"

# Case 2: missing argument => usage error (exit 2)
set +e
bash "$SCRIPT" >/dev/null 2>&1
rc=$?
set -e
(( rc == 2 )) || fail "missing arg should exit 2, got ${rc}"
pass "missing arg exits 2"

# Case 3: missing body file => exit 2
set +e
bash "$SCRIPT" "$TMPDIR_T/does-not-exist.md" >/dev/null 2>&1
rc=$?
set -e
(( rc == 2 )) || fail "missing body file should exit 2, got ${rc}"
pass "missing body file exits 2"

# Cases 4-10: one per workspace-archaeology pattern
echo 'the halves live at stash@{0} on one machine' > "$BODY"
expect_block "git stash ref"

echo 'both designs are local-only and unpushed' > "$BODY"
expect_block "local-only"

echo "everything sits on Florian's machine right now" > "$BODY"
expect_block "maintainer machine reference"

echo 'this records an abandoned attempt at a safety layer' > "$BODY"
expect_block "abandoned-work narrative"

echo '31 were confirmed under adversarial verification' > "$BODY"
expect_block "review-squad lingo"

echo 'of the 87 findings raised, most were never checked' > "$BODY"
expect_block "findings tallies"

echo 'a workspace held 3,500 lines of untracked code' > "$BODY"
expect_block "untracked-code archaeology"

# Case 11: plural form of the principles vocabulary is caught (regression:
# the singular-only pattern let the plural through)
echo 'this violates leadership principles #1 and #2' > "$BODY"
expect_block "principles vocabulary (plural)"

# Case 12: a long-standing shared pattern still fires through this consumer
echo 'shipped in Phase 2 of the rollout' > "$BODY"
expect_block "shared sprint-label pattern"

# Case 13: violation on a later line of a multi-line body is still caught
cat > "$BODY" <<'EOF'
The dial sticks at 98.5 when seeking backwards.

Context: reproduced during a soak window on the bench unit.
EOF
expect_block "violation on later line"

echo
echo "All issue-body lint cases passed."
