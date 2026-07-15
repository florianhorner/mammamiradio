#!/usr/bin/env bash
# Audio delivery invariants — runs on every PR.
# Catches regressions that cause production silence incidents.
# Does NOT check version sync (that belongs in pre-release-check.sh / release PRs).
#
# Usage: bash scripts/check-release-invariants.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASS=0
FAIL=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }

echo ""
echo "=== mammamiradio release invariants ==="
echo ""

# ── 1. FFmpeg music_eq filter chain ──────────────────────────────────────────
echo "1. FFmpeg music_eq filter chain (normalizer.py)"
# MUST stay at 2: adding a 3rd triggers FFmpeg 8.x SIGABRT (psymodel.c:576) on Pi aarch64.
EQ_COUNT=$(awk '/music_eq_chain = \(/,/^\s*\)/' mammamiradio/audio/normalizer.py | grep -c 'equalizer=' || true)

if [ "$EQ_COUNT" -eq 2 ]; then
    ok "music_eq_chain has $EQ_COUNT equalizer filters (de-mud 200Hz + presence 3kHz)"
elif [ "$EQ_COUNT" -gt 2 ]; then
    fail "music_eq_chain has $EQ_COUNT equalizer filters, expected 2 — Pi/FFmpeg 8.x SIGABRT risk"
else
    fail "music_eq_chain has $EQ_COUNT equalizer filters, expected 2 — audio quality regression"
fi

# ── 2. Packaged recovery audio ───────────────────────────────────────────────
echo ""
echo "2. Packaged recovery audio"

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

if python3 "$SCRIPT_DIR/validate-spoken-assets.py"; then
    ok "packaged spoken assets are manifest-bound and listener-truth safe"
else
    fail "packaged spoken asset manifest validation failed"
fi

# ── 3. Test: _pick_canned_clip returns None (missing packaged clip scenario) ──
echo ""
echo "3. Test coverage — missing packaged recovery scenario"

CANNED_NONE=$(grep -rl '_pick_canned_clip.*return_value=None\|return_value=None.*_pick_canned_clip' tests/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$CANNED_NONE" -gt 0 ]; then
    ok "_pick_canned_clip returning None is tested ($CANNED_NONE test file(s))"
else
    fail "No test mocks _pick_canned_clip to return None — empty-container / missing packaged recovery source is untested"
fi

# ── 4. Test: post-restart session_stopped scenario ───────────────────────────
echo ""
echo "4. Test coverage — post-restart scenario"

RESTART_TEST=$(grep -rl 'session_stopped' tests/ 2>/dev/null | wc -l | tr -d ' ')

if [ "$RESTART_TEST" -gt 0 ]; then
    ok "session_stopped scenario is tested ($RESTART_TEST test file(s))"
else
    fail "No test covers session_stopped — post-restart silence is untested"
fi

# ── 5. HA Green fallback performance gates ───────────────────────────────────
echo ""
echo "5. HA Green fallback performance gates"

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

# ── 6. Release beat source manifest ──────────────────────────────────────────
echo ""
echo "6. Release beat manifest"

if python3 "$SCRIPT_DIR/validate-release-beat.py"; then
    ok "release beat manifest is absent, disabled, or schema-valid"
else
    fail "release beat manifest validation failed"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================="
echo "  Passed: $PASS  Failed: $FAIL"
echo "======================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "Fix the failures above before merging."
    exit 1
else
    echo "All invariants passed."
    exit 0
fi
