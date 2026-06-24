#!/usr/bin/env bash
# Pre-release sanity check. Run before bumping the version number.
# Catches the class of bugs that have caused production silence incidents.
#
# Usage: scripts/pre-release-check.sh
#        make pre-release
set -euo pipefail

case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: scripts/pre-release-check.sh

Pre-release sanity check. Run before bumping the version number.
Verifies version consistency across pyproject.toml + addon config.yaml,
CHANGELOG head matches the version, and all release invariants
(FFmpeg eq chain count, test mocks, post-restart silence guard).

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

if [ "$ADDON_VER" = "$PYPROJECT_VER" ]; then
    ok "config.yaml ($ADDON_VER) matches pyproject.toml ($PYPROJECT_VER)"
else
    fail "Version mismatch: config.yaml=$ADDON_VER pyproject.toml=$PYPROJECT_VER"
fi

# ── 2. ha-addon CHANGELOG covers the current version ─────────────────────────
echo ""
echo "2. ha-addon CHANGELOG"

CHANGELOG_VER=$(awk '/^## / {version=$0; sub(/^##[[:space:]]+/, "", version); if (version != "Unreleased" && version != "[Unreleased]") {gsub(/^\[|\]$/, "", version); print version; exit}}' ha-addon/mammamiradio/CHANGELOG.md)

if [ "$CHANGELOG_VER" = "$ADDON_VER" ]; then
    ok "CHANGELOG latest version (## $CHANGELOG_VER) matches config.yaml ($ADDON_VER)"
else
    fail "CHANGELOG latest version is ## ${CHANGELOG_VER:-missing} but config.yaml is $ADDON_VER — update ha-addon/mammamiradio/CHANGELOG.md"
fi

# ── 3. FFmpeg music_eq filter chain has exactly 3 equalizers ─────────────────
echo ""
echo "3. FFmpeg music_eq filter chain (normalizer.py)"
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

# ── 4. Test: _pick_canned_clip returns None (empty container scenario) ────────
echo ""
echo "4. Test coverage — empty fallback scenario"

CANNED_NONE=$(grep -rl '_pick_canned_clip.*return_value=None\|return_value=None.*_pick_canned_clip' tests/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$CANNED_NONE" -gt 0 ]; then
    ok "_pick_canned_clip returning None is tested ($CANNED_NONE test file(s))"
else
    fail "No test mocks _pick_canned_clip to return None — empty container silence is untested"
fi

# ── 5. Test: post-restart session_stopped scenario ───────────────────────────
echo ""
echo "5. Test coverage — post-restart scenario"

RESTART_TEST=$(grep -rl 'session_stopped' tests/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$RESTART_TEST" -gt 0 ]; then
    ok "session_stopped scenario is tested ($RESTART_TEST test file(s))"
else
    fail "No test covers session_stopped — post-restart silence is untested"
fi

# ── 6. HA Green fallback performance gates ───────────────────────────────────
echo ""
echo "6. HA Green fallback performance gates"

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
