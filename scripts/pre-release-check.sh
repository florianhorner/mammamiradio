#!/usr/bin/env bash
# Pre-release sanity check. Run before bumping the version number.
# Catches the class of bugs that have caused production silence incidents.
#
# Usage: scripts/pre-release-check.sh
#        make pre-release
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/pre-release-check.sh

Pre-release sanity check. Run before bumping the version number.
Verifies version consistency across pyproject.toml + addon config.yaml,
CHANGELOG head matches the version, and all release invariants
(FFmpeg eq chain count, recovery audio, test mocks, post-restart guard).

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
MANIFEST_VER=$(python3 -c "import json; print(json.load(open('custom_components/mammamiradio/manifest.json')).get('version',''))" 2>/dev/null || true)

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

# ── 2. ha-addon CHANGELOG covers the current version ─────────────────────────
echo ""
echo "2. ha-addon CHANGELOG"

# Take the FIRST whitespace-delimited token of the header, then strip brackets, so a dated
# header ("## 2.14.1 - 2026-06-21") or a bracketed one ("## [2.14.1]") both reduce to the
# bare version. Comparing the whole header string falsely failed whenever it carried a date.
CHANGELOG_VER=$(awk '/^## / {version=$0; sub(/^##[[:space:]]+/, "", version); if (version != "Unreleased" && version != "[Unreleased]") {split(version, a, /[[:space:]]+/); v=a[1]; gsub(/^\[|\]$/, "", v); print v; exit}}' ha-addon/mammamiradio/CHANGELOG.md)

if [ "$CHANGELOG_VER" = "$ADDON_VER" ]; then
    ok "CHANGELOG latest version (## $CHANGELOG_VER) matches config.yaml ($ADDON_VER)"
else
    fail "CHANGELOG latest version is ## ${CHANGELOG_VER:-missing} but config.yaml is $ADDON_VER — update ha-addon/mammamiradio/CHANGELOG.md"
fi

# ── 3. Stable release beat target ─────────────────────────────────────────────
echo ""
echo "3. Release beat manifest"

if python3 "$SCRIPT_DIR/validate-release-beat.py" --channel stable --semver "$ADDON_VER"; then
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

if [ -d mammamiradio/assets/demo/recovery ]; then
    RECOVERY_MP3_COUNT=$(find mammamiradio/assets/demo/recovery -maxdepth 1 -type f -name '*.mp3' -size +1024c | wc -l | tr -d ' ')
else
    RECOVERY_MP3_COUNT=0
fi

if [ "$RECOVERY_MP3_COUNT" -gt 0 ]; then
    ok "packaged recovery clip is present ($RECOVERY_MP3_COUNT mp3 file(s))"
else
    fail "No packaged recovery MP3 under mammamiradio/assets/demo/recovery/ — image can fall through to technical fallback audio"
fi

if grep -q 'generate_silence' mammamiradio/scheduling/producer.py; then
    fail "producer.py must not call generate_silence in recovery paths — use recovery clip, norm cache, or emergency tone"
else
    ok "producer recovery paths do not call generate_silence"
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
if python3 - "$QUEUE_FALLBACK_WAIT" <<'PY'
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

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================="
echo "  Passed: $PASS  Failed: $FAIL"
echo "======================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "Fix the failures above before bumping the version."
    exit 1
else
    echo "All checks passed. Safe to bump the version."
    exit 0
fi
