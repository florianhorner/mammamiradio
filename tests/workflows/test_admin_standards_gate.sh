#!/usr/bin/env bash
# Self-test for scripts/check-admin-standards.sh
#
# Drives the gate with fixture PR bodies and a temp git history that touches
# an admin HTML file, so the standards check is actually reached. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/check-admin-standards.sh"

if [[ ! -x "$SCRIPT" ]]; then
  chmod +x "$SCRIPT"
fi

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT
EVENT="$TMPDIR_T/event.json"
FIXTURE="$TMPDIR_T/repo"

mkdir -p "$FIXTURE/mammamiradio/web/templates"
git -C "$FIXTURE" init -q -b main
git -C "$FIXTURE" config user.email "test@example.com"
git -C "$FIXTURE" config user.name "test"
git -C "$FIXTURE" config commit.gpgsign false
printf 'v1\n' > "$FIXTURE/mammamiradio/web/templates/admin.html"
git -C "$FIXTURE" add mammamiradio/web/templates/admin.html
git -C "$FIXTURE" commit -q -m "init"
printf 'v2\n' > "$FIXTURE/mammamiradio/web/templates/admin.html"
git -C "$FIXTURE" add mammamiradio/web/templates/admin.html
git -C "$FIXTURE" commit -q -m "change admin"

write_event() {
  jq -n --arg body "$1" '{pull_request: {body: $body}}' > "$EVENT"
}

run_gate() {
  (
    cd "$FIXTURE"
    env -u GITHUB_BASE_REF GITHUB_EVENT_PATH="$EVENT" bash "$SCRIPT"
  )
}

expect_block() { # $1=case name
  set +e
  run_gate >/dev/null 2>&1
  rc=$?
  set -e
  (( rc == 1 )) || fail "$1: expected exit 1 (blocked), got ${rc}"
  pass "$1 blocked"
}

expect_clean() { # $1=case name
  if ! run_gate >/dev/null 2>&1; then
    fail "$1: should be clean, was blocked"
  fi
  pass "$1 clean"
}

# Case 1: Admin Panel Standards section with a checked item — pass
write_event "$(cat <<'EOF'
## Admin Panel Standards
- [x] Token cost counter still visible
- [ ] Play button uses blue
EOF
)"
expect_clean "checked item in Admin Panel Standards"

# Case 2: standards items all unchecked, a different section has [x] — fail
write_event "$(cat <<'EOF'
## Something Else
- [x] unrelated checked box

## Admin Panel Standards
- [ ] Token cost counter still visible
- [ ] Play button uses blue
EOF
)"
expect_block "checked box outside Admin Panel Standards does not count"

# Case 3: no Admin Panel Standards section, other checked boxes — fail
write_event "$(cat <<'EOF'
## QA Impact
- [x] tests added
EOF
)"
expect_block "missing Admin Panel Standards section"

# Case 4: empty body — pass (push-context skip)
write_event ""
expect_clean "empty body push-context skip"

echo
echo "All admin-standards gate cases passed."
