#!/usr/bin/env bash
# Self-test for the edge add-on validation block (section 13) of
# scripts/validate-addon.sh.
#
# Mutates ha-addon/mammamiradio-edge/config.yaml one field at a time, asserts
# validate-addon.sh rejects each invalid state with the expected message, and
# restores the file. An EXIT trap restores the config even if an assertion
# aborts the test. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VALIDATE="scripts/validate-addon.sh"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[ -f "$EDGE_CONFIG" ] || fail "edge config not found: $EDGE_CONFIG"

BACKUP="$(mktemp)"
cp "$EDGE_CONFIG" "$BACKUP"
restore() { cp "$BACKUP" "$EDGE_CONFIG" 2>/dev/null || true; rm -f "$BACKUP" "$EDGE_CONFIG.tmp"; }
trap restore EXIT

# Portable in-place sed (BSD/macOS + GNU/Linux): edit via temp file.
mutate() { sed "$1" "$EDGE_CONFIG" > "$EDGE_CONFIG.tmp" && mv "$EDGE_CONFIG.tmp" "$EDGE_CONFIG"; }

# assert_rejects <description> <expected-message-substring>
# Runs validate-addon.sh against the already-mutated config, asserts non-zero
# exit and the expected edge message, then restores the config.
assert_rejects() {
  local desc="$1" msg="$2" out rc
  set +e
  out="$(bash "$VALIDATE" 2>&1)"
  rc=$?
  set -e
  cp "$BACKUP" "$EDGE_CONFIG"
  [ "$rc" -ne 0 ] || fail "$desc: validate-addon.sh exited 0, expected failure"
  echo "$out" | grep -qF "$msg" || fail "$desc: expected message '$msg' not found"
  pass "$desc rejected ('$msg')"
}

# assert_accepts <description>: the already-mutated config must PASS validation.
assert_accepts() {
  local desc="$1" out rc
  set +e
  out="$(bash "$VALIDATE" 2>&1)"
  rc=$?
  set -e
  cp "$BACKUP" "$EDGE_CONFIG"
  [ "$rc" -eq 0 ] || { echo "$out" | tail -5 >&2; fail "$desc: validate-addon.sh exited $rc, expected pass"; }
  pass "$desc accepted"
}

# Case 1: wrong slug
mutate 's/^slug: .*/slug: wrong-edge-slug/'
assert_rejects "wrong slug" "edge slug must be"

# Case 2: malformed version
mutate 's/^version: .*/version: not-a-version/'
assert_rejects "malformed version" "edge version must be"

# Case 2b: a manual edge release version (main short SHA) is accepted.
mutate 's/^version: .*/version: b1866c8/'
assert_accepts "short-SHA edge version (make edge-release)"

# Case 3: wrong image path
mutate 's#^image: .*#image: ghcr.io/wrong/image#'
assert_rejects "wrong image" "edge image mismatch"

# Case 4: schema drift from stable
mutate 's/anthropic_api_key: password?/anthropic_api_key: str?/'
assert_rejects "schema drift" "edge schema block drifted"

# Case 5: options drift from stable
mutate 's/super_italian_mode: true/super_italian_mode: false/'
assert_rejects "options drift" "edge options block drifted"

echo "All validate-addon edge-block scenarios passed."
