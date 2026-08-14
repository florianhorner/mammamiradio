#!/usr/bin/env bash
# Pre-release sanity check. Run as part of the release cut — it validates the version
# files and changelog heads AFTER they are bumped, which is what quality.yml does on the
# `chore(release): cut X.Y.Z` PR. (Deliberately does NOT check whether the advertised
# image exists: that is scripts/check-advertised-version.sh, and asking it here would
# deadlock every cut PR, since the cut names a version whose image is not built yet.)
# Catches the class of bugs that have caused production silence incidents.
#
# Usage: scripts/pre-release-check.sh
#        make pre-release
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIA_PYTHON="python3"
if ! "$MEDIA_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    if [ -x .venv/bin/python ]; then
        MEDIA_PYTHON=".venv/bin/python"
    else
        MEDIA_PYTHON="python3.11"
    fi
fi

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/pre-release-check.sh

Pre-release sanity check. Run before bumping the version number.
Verifies version consistency across pyproject.toml + addon config.yaml,
CHANGELOG head matches the version, all release invariants, and the physical
20-run Home Assistant Green cold-launch receipt set.

Catches the class of bugs that have caused production silence incidents.

Options:
  -h, --help   Show this help and exit

Also runs via `make pre-release`.
EOF
    exit 0
    ;;
esac

PASS=0
FAIL=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

echo ""
echo "=== mammamiradio pre-release check ==="
echo ""

# ── 1. Version consistency ────────────────────────────────────────────────────
echo "1. Version consistency"

ADDON_VER=$(grep '^version:' ha-addon/mammamiradio/config.yaml | awk '{print $2}' | tr -d '"')
PYPROJECT_VER=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml | head -1)
# `.get('version','')` + `|| true` so malformed JSON or a missing key yields an empty
# string and a clean [FAIL] below, never a Python traceback that aborts the release gate.
MANIFEST_VER=$("$MEDIA_PYTHON" -c "import json; print(json.load(open('custom_components/mammamiradio/manifest.json')).get('version',''))" 2>/dev/null || true)

if [ "$ADDON_VER" = "$PYPROJECT_VER" ]; then
    ok "config.yaml ($ADDON_VER) matches pyproject.toml ($PYPROJECT_VER)"
else
    fail "Version mismatch: config.yaml=$ADDON_VER pyproject.toml=$PYPROJECT_VER"
fi

# The HACS integration ships from this same repo. HACS shows the git release tag as the
# integration's version while Home Assistant shows manifest.json's version; keeping
# manifest.json == the release number makes those two displays agree. This is purely a
# version-LABEL fix — it does NOT change HACS update behavior. See docs/release-process.md
# "The HACS integration shares the release number". manifest.json is only ever bumped
# alongside config.yaml + pyproject.toml.
if [ -n "$MANIFEST_VER" ] && [ "$MANIFEST_VER" = "$ADDON_VER" ]; then
    ok "custom_components/mammamiradio/manifest.json ($MANIFEST_VER) matches config.yaml ($ADDON_VER)"
else
    fail "manifest.json version is '${MANIFEST_VER:-unreadable}' but config.yaml is $ADDON_VER — bump custom_components/mammamiradio/manifest.json with the release (or fix malformed JSON)"
fi

# ── 2. Both CHANGELOGs cover the current version ─────────────────────────────
echo ""
echo "2. CHANGELOGs"

# Take the FIRST whitespace-delimited token of the header, then strip brackets, so a dated
# header ("## 2.14.1 - 2026-06-21") or a bracketed one ("## [2.14.1]") both reduce to the
# bare version. Comparing the whole header string falsely failed whenever it carried a date.
# The trailing-whitespace/CR trim matters: without it "## Unreleased " (one stray
# space, or a CRLF line ending) stops matching the skip and gets reported as the
# newest *versioned* heading, blocking the release with a nonsense message.
CHANGELOG_VER=$(awk '/^## / {version=$0; sub(/^##[[:space:]]+/, "", version); gsub(/[[:space:]\r]+$/, "", version); if (version != "Unreleased" && version != "[Unreleased]") {split(version, a, /[[:space:]]+/); v=a[1]; gsub(/^\[|\]$/, "", v); print v; exit}}' ha-addon/mammamiradio/CHANGELOG.md)

if [ "$CHANGELOG_VER" = "$ADDON_VER" ]; then
    ok "ha-addon CHANGELOG latest version (## $CHANGELOG_VER) matches config.yaml ($ADDON_VER)"
else
    fail "ha-addon CHANGELOG latest version is ## ${CHANGELOG_VER:-missing} but config.yaml is $ADDON_VER — update ha-addon/mammamiradio/CHANGELOG.md"
fi

# The root CHANGELOG is checked here for the same reason, using the same extractor.
# addon-release.yml's tag pre-flight already validates it, but that fires INSIDE the
# open cut window: a typo in "## [X.Y.Z]" passes every pre-merge gate, merges, opens
# the window where main advertises an unpublished image, and only then fails. Catching
# it here moves the failure to the cut PR, where the fix is an edit instead of a revert.
ROOT_CHANGELOG_VER=$(awk '/^## / {version=$0; sub(/^##[[:space:]]+/, "", version); gsub(/[[:space:]\r]+$/, "", version); if (version != "Unreleased" && version != "[Unreleased]") {split(version, a, /[[:space:]]+/); v=a[1]; gsub(/^\[|\]$/, "", v); print v; exit}}' CHANGELOG.md)

if [ "$ROOT_CHANGELOG_VER" = "$ADDON_VER" ]; then
    ok "root CHANGELOG latest version (## $ROOT_CHANGELOG_VER) matches config.yaml ($ADDON_VER)"
else
    fail "root CHANGELOG latest version is ## ${ROOT_CHANGELOG_VER:-missing} but config.yaml is $ADDON_VER — fold the changelog in the cut commit, before tagging"
fi

# ── 3. Stable release beat target ─────────────────────────────────────────────
echo ""
echo "3. Release beat manifest"

if "$MEDIA_PYTHON" "$SCRIPT_DIR/validate-release-beat.py" --channel stable --semver "$ADDON_VER"; then
    ok "release beat manifest matches stable release target ($ADDON_VER), is disabled, or is absent"
else
    fail "release beat manifest validation failed for stable release target $ADDON_VER"
fi

# ── 4. FFmpeg music_eq filter chain has exactly 3 equalizers ─────────────────
echo ""
echo "4. FFmpeg music_eq filter chain (normalizer.py)"
# Count equalizer= lines inside the music_eq_chain assignment block.
# MUST stay at 2: adding a 3rd triggers FFmpeg 8.x SIGABRT (psymodel.c:576) on Pi aarch64.
EQ_COUNT=$(awk '/music_eq_chain = \(/,/^\s*\)/' mammamiradio/audio/normalizer.py | grep -c 'equalizer=' || true)

if [ "$EQ_COUNT" -eq 2 ]; then
    ok "music_eq_chain has $EQ_COUNT equalizer filters (de-mud 200Hz + presence 3kHz)"
elif [ "$EQ_COUNT" -gt 2 ]; then
    fail "music_eq_chain has $EQ_COUNT equalizer filters, expected 2 — Pi/FFmpeg 8.x SIGABRT risk with >2 equalizers + loudnorm"
else
    fail "music_eq_chain has $EQ_COUNT equalizer filters, expected 2 — audio quality regression"
fi

# ── 5. Packaged recovery audio ───────────────────────────────────────────────
echo ""
echo "5. Packaged recovery audio"

RECOVERY_DIR="mammamiradio/assets/demo/recovery"
REQUIRED_RECOVERY_ASSETS=(
    "continuity_1.mp3"
    "emergency_tone.mp3"
)

for asset_name in "${REQUIRED_RECOVERY_ASSETS[@]}"; do
    asset_path="$RECOVERY_DIR/$asset_name"
    if [ ! -f "$asset_path" ]; then
        fail "Required recovery asset is missing: $asset_path"
        continue
    fi

    asset_size=$(wc -c < "$asset_path" | tr -d '[:space:]')
    if [ "$asset_size" -gt 1024 ]; then
        ok "$asset_name is present and nontrivial ($asset_size bytes)"
    else
        fail "$asset_name is too small ($asset_size bytes; must be > 1024)"
    fi
done

if grep -q 'generate_silence' mammamiradio/scheduling/producer.py; then
    fail "producer.py must not call generate_silence in recovery paths — use recovery clip, norm cache, or emergency tone"
else
    ok "producer recovery paths do not call generate_silence"
fi

if python3 "$SCRIPT_DIR/validate-spoken-assets.py" \
    --assets-root "$PWD/mammamiradio/assets/demo"; then
    ok "packaged spoken assets are manifest/hash/transcript approved"
else
    fail "packaged spoken-asset manifest/hash/transcript validation failed"
fi

if command -v ffprobe >/dev/null 2>&1; then
    for asset_name in "${REQUIRED_RECOVERY_ASSETS[@]}"; do
        asset_path="$RECOVERY_DIR/$asset_name"
        if probe_output=$(ffprobe \
            -v error \
            -select_streams a:0 \
            -show_entries stream=codec_type \
            -of csv=p=0 \
            "$asset_path" 2>&1) && grep -qi 'audio' <<<"$probe_output"; then
            ok "$asset_name contains an ffprobe-readable audio stream"
        else
            fail "$asset_name is not ffprobe-readable audio (${probe_output:-no audio stream})"
        fi
    done
else
    fail "ffprobe is required to validate every packaged recovery asset"
fi

# ── 6. Test: _pick_canned_clip returns None (missing packaged clip scenario) ──
echo ""
echo "6. Test coverage — missing packaged recovery scenario"

CANNED_NONE=$(grep -rl '_pick_canned_clip.*return_value=None\|return_value=None.*_pick_canned_clip' tests/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$CANNED_NONE" -gt 0 ]; then
    ok "_pick_canned_clip returning None is tested ($CANNED_NONE test file(s))"
else
    fail "No test mocks _pick_canned_clip to return None — missing packaged recovery source is untested"
fi

# ── 7. Test: post-restart session_stopped scenario ───────────────────────────
echo ""
echo "7. Test coverage — post-restart scenario"

RESTART_TEST=$(grep -rl 'session_stopped' tests/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$RESTART_TEST" -gt 0 ]; then
    ok "session_stopped scenario is tested ($RESTART_TEST test file(s))"
else
    fail "No test covers session_stopped — post-restart silence is untested"
fi

# ── 8. HA Green fallback performance gates ───────────────────────────────────
echo ""
echo "8. HA Green fallback performance gates"

QUEUE_FALLBACK_WAIT=$(awk -F= '/QUEUE_FALLBACK_WAIT_SECONDS/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' mammamiradio/web/streamer.py)
if "$MEDIA_PYTHON" - "$QUEUE_FALLBACK_WAIT" <<'PY'
import sys
value = float(sys.argv[1])
raise SystemExit(0 if value <= 5.0 else 1)
PY
then
    ok "queue fallback wait is ${QUEUE_FALLBACK_WAIT}s (<= 5s)"
else
    fail "QUEUE_FALLBACK_WAIT_SECONDS must stay <= 5s for HA Green no-content windows (got ${QUEUE_FALLBACK_WAIT:-missing})"
fi

if grep -q 'norm_files\[0\]' mammamiradio/web/streamer.py; then
    fail "norm-cache rescue must not use deterministic norm_files[0]"
else
    ok "norm-cache rescue avoids deterministic first-file selection"
fi

if [ -x scripts/ha-green-perf-smoke.py ] && grep -q '^perf-smoke:' Makefile; then
    ok "HA Green perf smoke script and Make target are present"
else
    fail "Missing executable scripts/ha-green-perf-smoke.py or Makefile perf-smoke target"
fi

if [ -x scripts/ha-green-launch-smoke.py ] && grep -q '^launch-smoke:' Makefile; then
    ok "HA Green cold-launch smoke script and Make target are present"
else
    fail "Missing executable scripts/ha-green-launch-smoke.py or Makefile launch-smoke target"
fi

# ── 9. Physical HA Green release evidence ────────────────────────────────────
echo ""
echo "9. Physical HA Green release evidence"

if "$MEDIA_PYTHON" scripts/validate-ha-green-release-evidence.py --release-version "$ADDON_VER"; then
    ok "at least 20 cold Home Assistant Green runs meet the <=2s p95 release contract"
else
    fail "HA Green release evidence is incomplete — record 20 runs with scripts/ha-green-launch-smoke.py --record-release-receipt proof/media/ha-green-release-evidence, then commit only those receipt JSON files"
fi

# ── 10. Strict media-rights gate ──────────────────────────────────────────────
echo ""
echo "10. Strict media-rights gate"

if "$MEDIA_PYTHON" scripts/media-proof.py --quick; then
    ok "starter catalog evidence, bytes, audio, and packaging are release-ready"
else
    fail "strict media proof failed — release/publish paths must remain blocked"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================="
echo "  Passed: $PASS  Failed: $FAIL"
echo "======================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "Fix the failures above before tagging this cut."
    exit 1
else
    echo "All checks passed."
    exit 0
fi
