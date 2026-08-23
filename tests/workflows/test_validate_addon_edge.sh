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
# Store-listing cases mutate the tree (a removed README, drifted artwork) rather
# than a YAML field, so anything touched by path is snapshotted here and put back
# by the same restore points that handle the configs.
STAGED_FILES=()
stage_file() {
  local f="$1"
  cp "$f" "$BACKUP_DIR/$(printf '%s' "$f" | tr '/' '_')"
  STAGED_FILES+=("$f")
}
restore_files() {
  local f
  for f in "${STAGED_FILES[@]+"${STAGED_FILES[@]}"}"; do
    cp "$BACKUP_DIR/$(printf '%s' "$f" | tr '/' '_')" "$f"
  done
  STAGED_FILES=()
}
restore_configs() {
  cp "$STABLE_BACKUP" "$STABLE_CONFIG"
  cp "$EDGE_BACKUP" "$EDGE_CONFIG"
  restore_files
}
# Tolerant restore for the EXIT trap only: on an already-failing/aborting run
# the backups may be missing or already restored, so best-effort is correct
# here even though it isn't during normal test flow.
cleanup() {
  local f
  cp "$STABLE_BACKUP" "$STABLE_CONFIG" 2>/dev/null || true
  cp "$EDGE_BACKUP" "$EDGE_CONFIG" 2>/dev/null || true
  for f in "${STAGED_FILES[@]+"${STAGED_FILES[@]}"}"; do
    cp "$BACKUP_DIR/$(printf '%s' "$f" | tr '/' '_')" "$f" 2>/dev/null || true
  done
  rm -f "$STABLE_CONFIG.tmp" "$EDGE_CONFIG.tmp"
  rm -rf "$BACKUP_DIR"
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

# --- Store-listing files ---
# Supervisor reads the per-app README.md as the listing's long description and
# returns null when it is absent, so a deleted README is a silently blank store
# card. Stable icon/logo had no existence coverage at all before 2026-08-15.

stage_file "ha-addon/mammamiradio/README.md"
rm -f "ha-addon/mammamiradio/README.md"
assert_rejects "stable README.md missing" "Missing: ha-addon/mammamiradio/README.md"

stage_file "ha-addon/mammamiradio-edge/README.md"
rm -f "ha-addon/mammamiradio-edge/README.md"
assert_rejects "edge README.md missing" "Missing: ha-addon/mammamiradio-edge/README.md"

# A README that exists but says nothing renders the same blank listing as a
# missing one, so emptiness is rejected separately from absence.
stage_file "ha-addon/mammamiradio/README.md"
: > "ha-addon/mammamiradio/README.md"
assert_rejects "stable README.md empty" "ha-addon/mammamiradio/README.md is empty"

stage_file "ha-addon/mammamiradio-edge/README.md"
printf '   \n\n\t\n' > "ha-addon/mammamiradio-edge/README.md"
assert_rejects "edge README.md whitespace-only" "ha-addon/mammamiradio-edge/README.md is empty"

stage_file "ha-addon/mammamiradio/icon.png"
rm -f "ha-addon/mammamiradio/icon.png"
assert_rejects "stable icon.png missing" "Missing: ha-addon/mammamiradio/icon.png"

stage_file "ha-addon/mammamiradio/logo.png"
rm -f "ha-addon/mammamiradio/logo.png"
assert_rejects "stable logo.png missing" "Missing: ha-addon/mammamiradio/logo.png"

# Nothing copies artwork between the channels — cut-edge-release.sh only rewrites
# `version:` — so parity is the only thing keeping both catalog entries on the
# same brand after a stable-only icon refresh.
stage_file "ha-addon/mammamiradio-edge/icon.png"
cp "ha-addon/mammamiradio/logo.png" "ha-addon/mammamiradio-edge/icon.png"
assert_rejects "edge icon.png drift" "edge icon.png drifted from stable"

stage_file "ha-addon/mammamiradio-edge/logo.png"
cp "ha-addon/mammamiradio/icon.png" "ha-addon/mammamiradio-edge/logo.png"
assert_rejects "edge logo.png drift" "edge logo.png drifted from stable"

# The listings must stay equivalent below the shared marker; only the Edge
# warning above it may differ.
stage_file "ha-addon/mammamiradio-edge/README.md"
printf '%s\n' "$(sed 's/^- A continuous music stream.*/- Something else entirely/' ha-addon/mammamiradio-edge/README.md)" \
  > "ha-addon/mammamiradio-edge/README.md"
assert_rejects "edge listing body drift" "edge listing body drifted from stable"

# A missing marker must fail loudly rather than silently comparing empty to empty.
stage_file "ha-addon/mammamiradio-edge/README.md"
printf '%s\n' "$(grep -v 'shared-listing-body' ha-addon/mammamiradio-edge/README.md)" \
  > "ha-addon/mammamiradio-edge/README.md"
assert_rejects "edge listing marker removed" "shared-listing-body"

# Editing only the Edge warning, above the marker, stays valid.
stage_file "ha-addon/mammamiradio-edge/README.md"
printf '%s\n' "$(sed 's/^> \*\*The development channel.*/> **Testing channel.**/' ha-addon/mammamiradio-edge/README.md)" \
  > "ha-addon/mammamiradio-edge/README.md"
assert_accepts "edge warning reworded above the marker"

echo "All validate-addon edge-block scenarios passed."
