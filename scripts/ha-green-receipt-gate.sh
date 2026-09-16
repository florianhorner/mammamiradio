#!/usr/bin/env bash
# Physical HA Green evidence section for pre-release-check.sh; source only.
# Callers provide ok(), fail(), and waive() reporters.
# shellcheck shell=bash

ha_green_receipt_gate_validate() {
    case "${MMR_REQUIRE_HA_RECEIPTS:-0}" in
        0 | 1) return 0 ;;
        *)
            echo "ERROR: MMR_REQUIRE_HA_RECEIPTS must be 0 or 1, got '${MMR_REQUIRE_HA_RECEIPTS}'." >&2
            return 2
            ;;
    esac
}

ha_green_receipt_gate() {
    local python="$1" validator="$2" release_version="$3"
    if [ "${MMR_REQUIRE_HA_RECEIPTS:-0}" != "1" ]; then
        waive "NOT CHECKED: this release ships WITHOUT physical Home Assistant Green cold-start evidence. Set MMR_REQUIRE_HA_RECEIPTS=1 to require it."
    elif "$python" "$validator" --release-version "$release_version"; then
        ok "at least 20 cold Home Assistant Green runs meet the <=2s p95 release contract"
    else
        fail "HA Green release evidence is incomplete — record 20 runs with scripts/ha-green-launch-smoke.py --record-release-receipt proof/media/ha-green-release-evidence, then commit only those receipt JSON files"
    fi
}
