#!/usr/bin/env bash
# Self-test for the edge add-on validation block (section 13) of
# scripts/validate-addon.sh.
#
# Mutates stable and edge config.yaml fields, asserts validate-addon.sh rejects
# each invalid state with the expected message, and restores both files. An EXIT
# trap restores both configs even if an assertion aborts the test. No network.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

VALIDATE="scripts/validate-addon.sh"
STABLE_CONFIG="ha-addon/mammamiradio/config.yaml"
EDGE_CONFIG="ha-addon/mammamiradio-edge/config.yaml"

fail() { echo "FAIL: $1" >&2; exit 1; }
pass() { echo "PASS: $1"; }

[ -f "$STABLE_CONFIG" ] || fail "stable config not found: $STABLE_CONFIG"
[ -f "$EDGE_CONFIG" ] || fail "edge config not found: $EDGE_CONFIG"

BACKUP_DIR="$(mktemp -d)"
STABLE_BACKUP="$BACKUP_DIR/stable-config.yaml"
EDGE_BACKUP="$BACKUP_DIR/edge-config.yaml"
cp "$STABLE_CONFIG" "$STABLE_BACKUP"
cp "$EDGE_CONFIG" "$EDGE_BACKUP"
# Strict restore for use between assertions: a failed copy must abort the
# test loudly rather than let a later mutation/assertion run against an
# already-corrupted config and produce a misleading pass/fail result.
restore_configs() {
  cp "$STABLE_BACKUP" "$STABLE_CONFIG"
  cp "$EDGE_BACKUP" "$EDGE_CONFIG"
}
# Tolerant restore for the EXIT trap only: on an already-failing/aborting run
# the backups may be missing or already restored, so best-effort is correct
# here even though it isn't during normal test flow.
cleanup() {
  cp "$STABLE_BACKUP" "$STABLE_CONFIG" 2>/dev/null || true
  cp "$EDGE_BACKUP" "$EDGE_CONFIG" 2>/dev/null || true
  rm -f "$STABLE_CONFIG.tmp" "$EDGE_CONFIG.tmp" "$STABLE_BACKUP" "$EDGE_BACKUP"
  rmdir "$BACKUP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

# Portable in-place sed (BSD/macOS + GNU/Linux): edit via temp file.
# A sed that matches nothing leaves the file unchanged, the validator then
# correctly passes, and the case fails with a misleading "exited 0" — so a
# no-op mutation aborts loudly naming the stale target instead.
mutate_file() {
  local file="$1"
  shift
  sed "$@" "$file" > "$file.tmp"
  if cmp -s "$file" "$file.tmp"; then
    rm -f "$file.tmp"
    fail "mutation left $file unchanged (stale sed target?): $*"
  fi
  mv "$file.tmp" "$file"
}
mutate_edge() { mutate_file "$EDGE_CONFIG" "$@"; }
mutate_both() {
  mutate_file "$STABLE_CONFIG" "$@"
  mutate_file "$EDGE_CONFIG" "$@"
}

# assert_rejects <description> <expected-message-substring> [...]
# Runs validate-addon.sh against the already-mutated configs, asserts non-zero
# exit and every expected message, then restores both configs.
assert_rejects() {
  local desc="$1" out rc msg
  shift
  set +e
  out="$(bash "$VALIDATE" 2>&1)"
  rc=$?
  set -e
  restore_configs
  [ "$rc" -ne 0 ] || fail "$desc: validate-addon.sh exited 0, expected failure"
  for msg in "$@"; do
    grep -qF "$msg" <<<"$out" || fail "$desc: expected message '$msg' not found"
  done
  pass "$desc rejected"
}

# assert_accepts <description>: the already-mutated config must PASS validation.
assert_accepts() {
  local desc="$1" out rc
  set +e
  out="$(bash "$VALIDATE" 2>&1)"
  rc=$?
  set -e
  restore_configs
  [ "$rc" -eq 0 ] || { echo "$out" | tail -5 >&2; fail "$desc: validate-addon.sh exited $rc, expected pass"; }
  pass "$desc accepted"
}

# Baseline: the committed stable/edge pair must pass before mutation coverage.
assert_accepts "baseline stable/edge manifests"

# Backup contract: common-mode changes must fail even when parity still holds.
mutate_both 's/^backup: hot$/backup: cold/'
assert_rejects "both backup modes cold" \
  "stable backup mode must be hot" \
  "edge backup mode must be hot"

mutate_both '/^  - "cache\/clips"$/d'
assert_rejects "same backup exclusion removed from both" \
  "stable backup exclusion contract drifted" \
  "edge backup exclusion contract drifted"

mutate_both \
  -e 's|^  - "tmp"$|  - "__backup_swap__"|' \
  -e 's|^  - "cache/\.ytdlp_tmp"$|  - "tmp"|' \
  -e 's|^  - "__backup_swap__"$|  - "cache/.ytdlp_tmp"|'
assert_rejects "backup exclusions reordered in both" \
  "stable backup exclusion contract drifted" \
  "edge backup exclusion contract drifted"

# Unilateral drift must call out stable/edge parity.
mutate_edge '/^  - "\*\.tmp"$/d'
assert_rejects "edge-only backup exclusion drift" \
  "edge backup exclusion contract drifted" \
  "edge backup contract drifted from stable"

# Existing edge metadata/schema contract cases.
# Case 1: wrong slug
mutate_edge 's/^slug: .*/slug: wrong-edge-slug/'
assert_rejects "wrong slug" "edge slug must be"

# Case 2: malformed version
mutate_edge 's/^version: .*/version: not-a-version/'
assert_rejects "malformed version" "edge version must be"

# Case 2b: a manual edge release version (main short SHA) is accepted.
mutate_edge 's/^version: .*/version: b1866c8/'
assert_accepts "short-SHA edge version (make edge-release)"

# Case 3: wrong image path
mutate_edge 's#^image: .*#image: ghcr.io/wrong/image#'
assert_rejects "wrong image" "edge image mismatch"

# Case 3b: edge must keep the visible experimental channel marker.
mutate_edge 's/^stage: .*/stage: stable/'
assert_rejects "wrong stage" "edge stage must stay experimental"

# Case 4: schema drift from stable (flip a schema value type edge-side only)
mutate_edge 's/admin_token: password?/admin_token: str?/'
assert_rejects "schema drift" "edge schema block drifted"

# Case 5: options drift from stable (flip the edge default away from stable to force drift)
mutate_edge 's/super_italian_mode: false/super_italian_mode: true/'
assert_rejects "options drift" "edge options block drifted"

echo "All validate-addon edge-block scenarios passed."
