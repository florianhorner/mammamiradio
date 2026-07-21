"""Tests for shadow queue synchronisation logic in streamer.py.

The shadow queue (StationState.queued_segments) is a UI-facing list that
mirrors the real asyncio.Queue of pre-rendered audio segments.  Drift between
the two produces misleading up-next displays.  These tests cover:

1. _sync_runtime_state — trim-on-excess, no-op-on-equal, no-op-when-no-queue
2. _runtime_health_snapshot — field correctness and edge cases
3. _public_status_payload — upcoming / upcoming_mode selection logic
4. source-switch (_apply_loaded_source equivalent via /api/playlist/load) —
   shadow is cleared atomically with the real queue purge
5. readyz endpoint — health contract built on shadow + real queue agreement
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mammamiradio.core.config import load_config
from mammamiradio.core.listener_session import ListenerSession, ListenerSessionCueState
from mammamiradio.core.models import (
    GenerationWasteReason,
    PlaylistSource,
    Segment,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.scheduling.producer import _adjacent_music_source, _enqueue_with_egress
from mammamiradio.web.streamer import (
    _CONTINUITY_CACHE_SCAN_LIMIT,
    _FALLBACK_REASON_LABELS,
    BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS,
    BRIDGE_HEALTH_THRESHOLD,
    BRIDGE_HEALTH_WINDOW_SECONDS,
    GENERATION_WASTE_DEGRADED_COUNT,
    GENERATION_WASTE_DEGRADED_SECONDS,
    GENERATION_WASTE_WINDOW_SECONDS,
    LiveStreamHub,
    _apply_ban,
    _apply_loaded_source,
    _bridge_health_snapshot,
    _claim_continuity_slot,
    _continuity_reservation_segments,
    _discard_unplayable_queue_prefix,
    _estimate_api_cost,
    _generation_waste_snapshot,
    _playable_runway_available,
    _purge_queue_and_shadow,
    _reserve_continuity_runway,
    _runtime_health_snapshot,
    _runtime_status_snapshot,
    _segment_is_immediately_playable,
    _sync_runtime_state,
    router,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(title: str = "Track A") -> dict:
    return {"type": "music", "label": title, "metadata": {"title": title}}


def _queue_segment(title: str = "Track A", *, duration_sec: float = 0.0) -> Segment:
    """A minimal Segment whose path.unlink() is a no-op (non-existent file)."""
    return Segment(
        type=SegmentType.MUSIC,
        path=Path(f"/tmp/test_seg_{title.replace(' ', '_')}.mp3"),
        duration_sec=duration_sec,
        metadata={"title": title},
    )


def _make_app(
    *,
    shadow: list[dict] | None = None,
    queue_items: int = 0,
    admin_password: str = "",
    admin_token: str = "",
) -> FastAPI:
    """Build a minimal test app with pre-populated shadow and real queue."""
    app = FastAPI()
    app.include_router(router)

    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = admin_token

    state = StationState(
        playlist=[Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="s1")],
    )
    if shadow is not None:
        state.queued_segments = list(shadow)

    q: asyncio.Queue = asyncio.Queue()
    for _ in range(queue_items):
        q.put_nowait(_queue_segment())

    app.state.queue = q
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


def _fake_request(app: FastAPI) -> Any:
    """Return a lightweight object shaped like a FastAPI Request."""
    req = MagicMock()
    req.app = app
    return req


def _provider_health(
    *,
    anthropic_degraded: bool = False,
    retry_after_s: int = 0,
    last_error: str = "",
) -> dict:
    return {
        "anthropic": {
            "degraded": anthropic_degraded,
            "retry_after_s": retry_after_s,
            "last_error": last_error,
        }
    }


# ---------------------------------------------------------------------------
# _sync_runtime_state unit tests
# ---------------------------------------------------------------------------


class TestSyncRuntimeState:
    def test_no_queue_attached_is_noop(self):
        """With no queue on app.state, shadow is left untouched."""
        app = FastAPI()
        state = StationState()
        state.queued_segments = [_seg(), _seg()]
        app.state.station_state = state
        req = _fake_request(app)

        _sync_runtime_state(req)

        assert len(state.queued_segments) == 2
        assert state.shadow_queue_corrections == 0

    def test_shadow_longer_than_queue_is_trimmed(self):
        """Shadow excess is trimmed to match real queue depth."""
        app = _make_app(shadow=[_seg("A"), _seg("B"), _seg("C")], queue_items=1)
        req = _fake_request(app)

        _sync_runtime_state(req)

        assert len(app.state.station_state.queued_segments) == 1
        assert app.state.station_state.shadow_queue_corrections == 1

    def test_shadow_shorter_than_queue_is_not_inflated(self):
        """When shadow is behind the real queue, no artificial inflation occurs."""
        app = _make_app(shadow=[_seg("A")], queue_items=3)
        req = _fake_request(app)

        _sync_runtime_state(req)

        # shadow stays at 1 — we never fabricate entries
        assert len(app.state.station_state.queued_segments) == 1
        assert app.state.station_state.shadow_queue_corrections == 0

    def test_shadow_equals_queue_no_correction(self):
        """Exact match: no trimming, no correction counter bump."""
        app = _make_app(shadow=[_seg("A"), _seg("B")], queue_items=2)
        req = _fake_request(app)

        _sync_runtime_state(req)

        assert len(app.state.station_state.queued_segments) == 2
        assert app.state.station_state.shadow_queue_corrections == 0

    def test_empty_shadow_and_empty_queue_is_noop(self):
        app = _make_app(shadow=[], queue_items=0)
        req = _fake_request(app)

        _sync_runtime_state(req)

        assert app.state.station_state.queued_segments == []
        assert app.state.station_state.shadow_queue_corrections == 0

    def test_runtime_sync_event_counter_increments(self):
        app = _make_app(shadow=[], queue_items=0)
        req = _fake_request(app)

        before = app.state.station_state.runtime_sync_events
        _sync_runtime_state(req)
        assert app.state.station_state.runtime_sync_events == before + 1

    def test_repeated_trims_accumulate_correction_count(self):
        """Each trim call increments the correction counter independently."""
        app = _make_app(shadow=[_seg("A"), _seg("B")], queue_items=1)
        req = _fake_request(app)

        _sync_runtime_state(req)  # shadow 2 → 1, corrections=1
        # Re-add to shadow to simulate drift recurring
        app.state.station_state.queued_segments.append(_seg("C"))
        _sync_runtime_state(req)  # shadow 2 → 1 again, corrections=2

        assert app.state.station_state.shadow_queue_corrections == 2

    def test_trim_preserves_oldest_entries(self):
        """Trim keeps the first N items (oldest = produced first)."""
        segs = [_seg(f"Track {i}") for i in range(5)]
        app = _make_app(shadow=segs, queue_items=2)
        req = _fake_request(app)

        _sync_runtime_state(req)

        remaining = app.state.station_state.queued_segments
        assert len(remaining) == 2
        assert remaining[0]["label"] == "Track 0"
        assert remaining[1]["label"] == "Track 1"


# ---------------------------------------------------------------------------
# _runtime_health_snapshot unit tests
# ---------------------------------------------------------------------------


class TestRuntimeHealthSnapshot:
    def test_queue_depth_matches_real_queue(self):
        app = _make_app(shadow=[_seg()], queue_items=3)
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["queue_depth"] == 3

    def test_shadow_queue_depth_matches_shadow(self):
        app = _make_app(shadow=[_seg(), _seg()], queue_items=3)
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["shadow_queue_depth"] == 2

    def test_in_sync_flag_true_when_equal(self):
        app = _make_app(shadow=[_seg()], queue_items=1)
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["shadow_queue_in_sync"] is True

    def test_in_sync_flag_false_when_drifted(self):
        app = _make_app(shadow=[_seg(), _seg()], queue_items=1)
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["shadow_queue_in_sync"] is False

    def test_queue_depth_minus_one_when_no_queue(self):
        app = FastAPI()
        state = StationState()
        app.state.station_state = state
        # deliberately no app.state.queue
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["queue_depth"] == -1

    def test_producer_task_alive_true_when_none(self):
        """No task attached → treated as alive (startup window)."""
        app = _make_app()
        # no producer_task / playback_task on state → defaults to None
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["producer_task_alive"] is True
        assert snap["playback_task_alive"] is True

    def test_producer_task_alive_false_when_done(self):
        app = _make_app()
        task = MagicMock()
        task.done.return_value = True
        app.state.producer_task = task
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["producer_task_alive"] is False

    def test_failover_active_false_for_normal_audio(self):
        app = _make_app()
        app.state.station_state.now_streaming = {"metadata": {"audio_source": "yt-dlp"}}
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["failover_active"] is False

    def test_failover_active_true_for_fallback_source(self):
        app = _make_app()
        app.state.station_state.now_streaming = {"metadata": {"audio_source": "fallback_tone"}}
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["failover_active"] is True

    def test_failover_active_true_for_canned_fallback_without_audio_source(self):
        app = _make_app()
        app.state.station_state.now_streaming = {"metadata": {"fallback": True, "canned": True}}
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "canned"
        assert snap["failover_active"] is True

    def test_failover_active_true_for_norm_cache_rescue(self):
        app = _make_app()
        app.state.station_state.now_streaming = {
            "metadata": {"audio_source": "norm_cache", "queue_drain_recovery": True}
        }
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "norm_cache"
        assert snap["failover_active"] is True

    def test_failover_active_true_for_emergency_tone(self):
        app = _make_app()
        app.state.station_state.now_streaming = {
            "metadata": {"audio_source": "emergency_tone", "queue_drain_recovery": True}
        }
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["failover_active"] is True

    def test_failover_active_true_for_silence_fallback(self):
        app = _make_app()
        app.state.station_state.now_streaming = {"metadata": {"silence_fallback": True}}
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["failover_active"] is True

    def test_download_audio_source_reports_playlist_source_not_failover(self):
        app = _make_app()
        app.state.station_state.playlist_source = PlaylistSource(kind="charts")
        app.state.station_state.now_streaming = {"metadata": {"audio_source": "download"}}
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "charts"
        assert snap["failover_active"] is False

    def test_shadow_corrections_reflected_in_snapshot(self):
        app = _make_app()
        app.state.station_state.shadow_queue_corrections = 7
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["shadow_queue_corrections"] == 7

    def test_audio_source_falls_back_to_playlist_source_when_prewarm(self):
        """audio_source 'prewarm' is replaced by playlist_source.kind in the snapshot."""
        from mammamiradio.core.models import PlaylistSource

        app = _make_app()
        app.state.station_state.now_streaming = {"metadata": {"audio_source": "prewarm"}}
        app.state.station_state.playlist_source = PlaylistSource(kind="demo")
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "demo"

    def test_audio_source_falls_back_to_playlist_source_when_empty(self):
        """Empty audio_source is replaced by playlist_source.kind in the snapshot."""
        from mammamiradio.core.models import PlaylistSource

        app = _make_app()
        app.state.station_state.now_streaming = {}
        app.state.station_state.playlist_source = PlaylistSource(kind="charts")
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "charts"

    def test_audio_source_playlist_source_none_returns_unknown(self):
        """When both now_streaming and playlist_source are unset, returns 'unknown'."""
        app = _make_app()
        app.state.station_state.now_streaming = {}
        app.state.station_state.playlist_source = None
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "unknown"

    def test_queue_empty_elapsed_and_silence_failure_are_exposed(self):
        app = _make_app()
        app.state.stream_hub.subscribe()
        app.state.station_state.queue_empty_since = time.monotonic() - 31
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["queue_empty_elapsed_s"] >= 30
        assert snap["silence_with_listeners"] is True


# ---------------------------------------------------------------------------
# _runtime_status_snapshot tests
# ---------------------------------------------------------------------------


class TestRuntimeStatusSnapshot:
    def test_initial_primary_audio_observation_does_not_emit_switch_event(self):
        app = _make_app()
        state = app.state.station_state
        state.playlist_source = PlaylistSource(kind="charts")

        state.on_stream_segment(
            Segment(
                type=SegmentType.MUSIC,
                path=Path("/tmp/primary.mp3"),
                metadata={"title": "Primary", "audio_source": "download"},
            )
        )

        assert state.runtime_provider_state["audio_source"]["current_provider"] == "charts"
        assert list(state.runtime_events) == []

    def test_regular_banter_does_not_overwrite_audio_provider(self):
        app = _make_app()
        state = app.state.station_state
        state.playlist_source = PlaylistSource(kind="charts")
        state.update_runtime_provider(
            "audio_source",
            current_provider="charts",
            primary_provider="charts",
            fallback_active=False,
            reason="Primary audio source is on air",
        )

        state.on_stream_segment(
            Segment(
                type=SegmentType.BANTER,
                path=Path("/tmp/banter.mp3"),
                metadata={"title": "Host talk"},
            )
        )

        assert state.runtime_provider_state["audio_source"]["current_provider"] == "charts"
        assert list(state.runtime_events) == []

    def test_ready_status_has_normalized_provider_contract(self):
        app = _make_app()
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        # The default cast spans ElevenLabs (hosts), Azure (sweeper) and OpenAI
        # (one ad voice); supply every key so the TTS provider is healthy and the
        # snapshot reports a clean, no-fallback contract.
        app.state.config.elevenlabs_api_key = "elevenlabs-key"
        app.state.config.azure_speech_key = "azure-key"
        app.state.config.azure_speech_region = "westeurope"
        app.state.station_state.now_streaming = {"metadata": {"audio_source": "charts"}}
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "ready"
        assert snap["fallback_active"] is False
        assert set(snap["providers"]) == {"audio_source", "script_provider", "tts_provider"}
        assert snap["providers"]["script_provider"]["current_provider"] == "anthropic"
        assert snap["no_failover_message"] == "No failover in current session."

    def test_station_on_air_true_when_tasks_alive_and_no_silence(self):
        app = _make_app()
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["station_on_air"] is True

    def test_station_on_air_true_even_when_script_fallback_active(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_exception",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "degraded"
        assert snap["station_on_air"] is True

    def test_station_on_air_false_when_silence_with_listeners(self):
        app = _make_app()
        app.state.stream_hub.subscribe()
        app.state.station_state.queue_empty_since = time.monotonic() - 31
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["station_on_air"] is False
        assert snap["health_state"] == "blocked"
        assert "playback is silent" in snap["health_explanation"]

    def test_station_on_air_false_when_producer_task_stopped(self):
        app = _make_app()
        task = MagicMock()
        task.done.return_value = True
        app.state.producer_task = task
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["station_on_air"] is False

    def test_station_on_air_false_when_session_stopped(self):
        app = _make_app()
        app.state.station_state.session_stopped = True
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["station_on_air"] is False
        assert snap["health_state"] == "ready"

    def test_session_stopped_stays_paused_even_with_silence_and_listener(self):
        # A deliberate operator pause must read as paused ("ready"), never the
        # red "blocked"/Error state, even after the silence window elapses with a
        # listener still connected — session_stopped is checked before silence.
        app = _make_app()
        app.state.station_state.session_stopped = True
        app.state.stream_hub.subscribe()
        app.state.station_state.queue_empty_since = time.monotonic() - 31
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "ready"
        assert "paused by the operator" in snap["health_explanation"]
        assert snap["health_explanation"] == "Station is paused by the operator."

    def test_degraded_status_surfaces_audio_failover_event(self):
        app = _make_app()
        state = app.state.station_state
        state.on_stream_segment(
            Segment(
                type=SegmentType.MUSIC,
                path=Path("/tmp/fallback.mp3"),
                metadata={
                    "title": "Fallback",
                    "audio_source": "fallback_demo_asset",
                    "fallback": True,
                    "fallback_reason": "Queue empty; demo asset rescue active",
                },
            )
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "degraded"
        assert snap["fallback_active"] is True
        assert snap["providers"]["audio_source"]["current_provider"] == "fallback_demo_asset"
        assert snap["failover_events"][0]["reason"] == "Queue empty; demo asset rescue active"
        assert snap["no_failover_message"] == ""

    def test_blocked_status_overrides_fallback_state(self):
        app = _make_app()
        task = MagicMock()
        task.done.return_value = True
        app.state.producer_task = task
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "blocked"
        assert snap["health_color"] == "red"

    def test_status_prefers_recorded_script_fallback_over_provider_health(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_exception",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "degraded"
        assert snap["providers"]["script_provider"]["current_provider"] == "openai"
        assert snap["providers"]["script_provider"]["fallback_active"] is True
        assert snap["providers"]["script_provider"]["switch_reason"] == "anthropic_exception"

    def test_status_uses_recorded_script_recovery_after_fallback(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        # The default cast spans ElevenLabs (hosts), Azure (sweeper) and OpenAI
        # (one ad voice); supply every key so the TTS provider is healthy and this
        # test isolates the script-provider recovery state.
        app.state.config.elevenlabs_api_key = "elevenlabs-key"
        app.state.config.azure_speech_key = "azure-key"
        app.state.config.azure_speech_region = "westeurope"
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_exception",
        )
        state.update_runtime_provider(
            "script_provider",
            current_provider="anthropic",
            primary_provider="anthropic",
            fallback_active=False,
            reason="Anthropic is the active script provider",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "ready"
        assert snap["providers"]["script_provider"]["current_provider"] == "anthropic"
        assert snap["providers"]["script_provider"]["fallback_active"] is False

    def test_norm_cache_rescue_is_detected_as_degraded(self):
        app = _make_app()
        state = app.state.station_state
        state.on_stream_segment(
            Segment(
                type=SegmentType.MUSIC,
                path=Path("/tmp/norm.mp3"),
                metadata={"audio_source": "norm_cache", "queue_drain_recovery": True},
            )
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "degraded"
        assert snap["providers"]["audio_source"]["fallback_active"] is True

    def test_emergency_tone_is_detected_as_degraded(self):
        app = _make_app()
        state = app.state.station_state
        state.on_stream_segment(
            Segment(
                type=SegmentType.MUSIC,
                path=Path("/tmp/tone.mp3"),
                metadata={"audio_source": "emergency_tone", "queue_drain_recovery": True},
            )
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req)

        assert snap["health_state"] == "degraded"
        assert snap["providers"]["audio_source"]["fallback_active"] is True


class TestScriptProviderStatusRecovery:
    def test_recovery_mode_transient_when_disabled_until_expired(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() - 1
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_exception",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req, provider_health=_provider_health())
        provider = snap["providers"]["script_provider"]

        assert provider["recovery_mode"] == "transient"
        assert provider["retry_in_seconds"] is None
        assert provider["action_guidance"] == "No action needed - will retry automatically"

    @pytest.mark.parametrize(
        "reason",
        [
            "anthropic_auth_failed",
            "anthropic_auth_blocked",
            "anthropic_usage_limit",
            "anthropic_usage_limit_blocked",
            "anthropic_nonretryable",
        ],
    )
    def test_expired_actionable_fallback_reasons_still_require_action(self, reason: str):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() - 1
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason=reason,
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req, provider_health=_provider_health())
        provider = snap["providers"]["script_provider"]

        assert provider["recovery_mode"] == "action_required"
        assert provider["retry_in_seconds"] is None
        assert provider["action_guidance"] == _FALLBACK_REASON_LABELS[reason]
        assert provider["action_guidance"] != "No action needed - will retry automatically"

    def test_recovery_mode_circuit_breaker_when_disabled_until_active(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() + 600
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_auth_failed",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(
            req,
            provider_health=_provider_health(anthropic_degraded=True, retry_after_s=300),
        )
        provider = snap["providers"]["script_provider"]

        assert provider["recovery_mode"] == "circuit_breaker"
        assert provider["retry_in_seconds"] == 300
        assert provider["action_guidance"] == _FALLBACK_REASON_LABELS["anthropic_auth_failed"]

    def test_retry_in_seconds_reads_from_provider_health(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() + 600
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_usage_limit",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(
            req,
            provider_health=_provider_health(anthropic_degraded=True, retry_after_s=17),
        )

        assert snap["providers"]["script_provider"]["retry_in_seconds"] == 17

    def test_action_guidance_populated_for_circuit_breaker(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() + 600
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_usage_limit_blocked",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(
            req,
            provider_health=_provider_health(anthropic_degraded=True, retry_after_s=180),
        )

        assert "usage limit" in snap["providers"]["script_provider"]["action_guidance"]

    def test_transient_reason_is_a_short_circuit_breaker_while_active(self):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() + 20
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason="anthropic_transient",
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(
            req,
            provider_health=_provider_health(anthropic_degraded=True, retry_after_s=20),
        )
        provider = snap["providers"]["script_provider"]

        assert provider["recovery_mode"] == "circuit_breaker"
        assert provider["retry_in_seconds"] == 20
        assert provider["action_guidance"] == _FALLBACK_REASON_LABELS["anthropic_transient"]
        assert provider["recovery_mode"] != "action_required"

    @pytest.mark.parametrize("reason", ["anthropic_transient", "anthropic_transient_blocked"])
    def test_expired_transient_reasons_auto_recover_without_operator_action(self, reason: str):
        app = _make_app()
        state = app.state.station_state
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        state.anthropic_disabled_until = time.time() - 1
        state.update_runtime_provider(
            "script_provider",
            current_provider="openai",
            primary_provider="anthropic",
            fallback_active=True,
            reason=reason,
        )
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req, provider_health=_provider_health())
        provider = snap["providers"]["script_provider"]

        assert provider["recovery_mode"] == "transient"
        assert provider["retry_in_seconds"] is None
        assert provider["action_guidance"] == "No action needed - will retry automatically"
        assert provider["recovery_mode"] != "action_required"

    def test_recovery_mode_none_when_no_fallback(self):
        app = _make_app()
        app.state.config.anthropic_api_key = "anthropic-key"
        app.state.config.openai_api_key = "openai-key"
        req = _fake_request(app)

        snap = _runtime_status_snapshot(req, provider_health=_provider_health())
        provider = snap["providers"]["script_provider"]

        assert provider["recovery_mode"] is None
        assert provider["retry_in_seconds"] is None
        assert provider["action_guidance"] == ""

    def test_fallback_reason_labels_covers_all_scriptwriter_fallback_reasons(self):
        scriptwriter = Path(__file__).resolve().parents[2] / "mammamiradio" / "hosts" / "scriptwriter.py"
        source = scriptwriter.read_text(encoding="utf-8")
        reasons = set(re.findall(r'(?:fallback_reason\s*=\s*|return\s+)"(anthropic_[^"]+)"', source))

        assert reasons == set(_FALLBACK_REASON_LABELS)


# ---------------------------------------------------------------------------
# public-status endpoint — upcoming / upcoming_mode selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_queued_when_shadow_has_items():
    """When shadow queue has entries, mode is 'queued' and source is rendered_queue."""
    app = _make_app(shadow=[_seg("Song A"), _seg("Song B")], queue_items=2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["upcoming_mode"] == "queued"
    assert all(item["source"] == "rendered_queue" for item in data["upcoming"])


@pytest.mark.asyncio
async def test_public_status_rendered_upcoming_is_capped_at_8_with_source_fields():
    """Rendered queue previews expose at most 8 listener-safe rows."""
    shadow = [_seg(f"Track {i}") for i in range(10)]
    app = _make_app(shadow=shadow, queue_items=10)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    upcoming = resp.json()["upcoming"]
    assert len(upcoming) == 8
    assert [item["label"] for item in upcoming] == [f"Track {i}" for i in range(8)]
    assert all(item["source"] == "rendered_queue" for item in upcoming)


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_building_when_shadow_empty():
    """With an empty rendered shadow, mode falls to 'building'."""
    app = _make_app(shadow=[], queue_items=0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["upcoming_mode"] == "building"
    assert data["upcoming"] == []


@pytest.mark.asyncio
async def test_public_status_hides_playlist_predictions_when_shadow_empty():
    """Empty rendered shadow stays empty even when the playlist has tracks."""
    app = _make_app(shadow=[], queue_items=0)
    # playlist already has one track from _make_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["upcoming_mode"] == "building"
    assert data["upcoming"] == []


@pytest.mark.asyncio
async def test_public_status_hides_force_next_predictions_when_shadow_empty():
    """A pinned next track is intent, not render-ready audio."""
    app = _make_app(shadow=[], queue_items=0)
    app.state.station_state.force_next = SegmentType.MUSIC
    app.state.station_state.pinned_track = app.state.station_state.playlist[0]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["upcoming_mode"] == "building"
    assert data["upcoming"] == []


@pytest.mark.asyncio
async def test_public_status_runtime_health_exposes_silence_budget():
    app = _make_app()
    app.state.stream_hub.subscribe()
    app.state.station_state.queue_empty_since = time.monotonic() - 31

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    runtime_health = resp.json()["runtime_health"]
    assert runtime_health["queue_empty_elapsed_s"] >= 30
    assert runtime_health["silence_with_listeners"] is True


@pytest.mark.asyncio
async def test_public_status_sync_increments_on_each_poll():
    """Every status poll triggers a _sync_runtime_state call (counter goes up)."""
    app = _make_app(shadow=[], queue_items=0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/public-status")
        await client.get("/public-status")

    assert app.state.station_state.runtime_sync_events == 2


# ---------------------------------------------------------------------------
# readyz endpoint — shadow/queue contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readyz_not_ready_when_queue_empty():
    app = _make_app(shadow=[], queue_items=0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    assert resp.json()["ready"] is False


@pytest.mark.asyncio
async def test_readyz_ready_when_queue_has_segments():
    """readyz returns 200 when queue_depth > 0 and tasks are alive."""
    app = _make_app(shadow=[_seg()], queue_items=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_readyz_ready_after_startup_window():
    """readyz returns 200 once uptime > 30s even with an empty queue."""
    app = _make_app(shadow=[], queue_items=0)
    app.state.start_time = time.time() - 31  # simulate 31s of uptime

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_readyz_not_ready_when_producer_dead():
    app = _make_app(shadow=[_seg()], queue_items=1)
    dead_task = MagicMock()
    dead_task.done.return_value = True
    app.state.producer_task = dead_task

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    data = resp.json()
    assert data["ready"] is False
    assert data["runtime"]["producer_task_alive"] is False


# ---------------------------------------------------------------------------
# Source switch — shadow is cleared atomically
# ---------------------------------------------------------------------------


def test_switch_playlist_does_not_clear_shadow():
    """switch_playlist alone does NOT clear queued_segments — that is the
    caller's (_apply_loaded_source) responsibility.  This test documents the
    boundary so a future change to switch_playlist doesn't silently assume it
    clears shadow state."""
    state = StationState(
        playlist=[Track(title="Old", artist="A", duration_ms=1000, spotify_id="o1")],
    )
    state.queued_segments = [_seg("Old queued")]
    new_tracks = [Track(title="New", artist="B", duration_ms=1000, spotify_id="n1")]

    state.switch_playlist(new_tracks)

    # queued_segments is the caller's concern — switch_playlist must NOT touch it
    assert len(state.queued_segments) == 1


def test_apply_loaded_source_rebuilds_shadow_and_real_queue_with_continuity_audio():
    """A source swap atomically replaces stale rows with protected audible runway."""
    from mammamiradio.core.models import PlaylistSource

    app = _make_app(shadow=[_seg("Old A"), _seg("Old B")], queue_items=2)
    # Wire skip_event (already set by _make_app) and simulate a now_streaming value
    # so that the skip branch is exercised.
    app.state.station_state.now_streaming = {"type": "music", "label": "Old A"}

    resolved_source = PlaylistSource(kind="local", source_id="local", label="Local", track_count=1)
    new_tracks = [Track(title="New Song", artist="New Artist", duration_ms=180_000, spotify_id="n1")]

    req = _fake_request(app)
    _apply_loaded_source(req, new_tracks, resolved_source)

    assert app.state.queue.qsize() == len(app.state.station_state.queued_segments) == 1
    assert app.state.queue._queue[0].metadata["continuity_reservation"] is True
    assert app.state.station_state.queued_segments[0]["reason"] == "Protected continuity audio."


# ---------------------------------------------------------------------------
# Producer rescue-bridge health snapshot (#547 observability)
# ---------------------------------------------------------------------------


def test_bridge_health_snapshot_empty_is_healthy():
    state = StationState()
    bh = _bridge_health_snapshot(state)

    assert bh["session_count"] == 0
    assert bh["window_count"] == 0
    assert bh["unhealthy"] is False
    assert bh["last_fire"] is None
    assert bh["by_type"] == {"drain": 0, "resume": 0, "idle": 0}
    assert bh["threshold"] == BRIDGE_HEALTH_THRESHOLD
    assert bh["window_seconds"] == BRIDGE_HEALTH_WINDOW_SECONDS
    assert isinstance(bh["queue_empty_elapsed_s"], float)


def test_bridge_health_snapshot_unhealthy_at_threshold():
    now = 10_000.0
    state = StationState()
    # THRESHOLD fires, all inside the window.
    for i in range(BRIDGE_HEALTH_THRESHOLD):
        state.record_bridge_fire("drain", "norm_cache", timestamp=now - i)

    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        bh = _bridge_health_snapshot(state)

    assert bh["session_count"] == BRIDGE_HEALTH_THRESHOLD
    assert bh["window_count"] == BRIDGE_HEALTH_THRESHOLD
    assert bh["unhealthy"] is True
    assert bh["unhealthy_reasons"] == ["bridge_frequency"]
    assert bh["last_fire"]["bridge_type"] == "drain"
    assert bh["last_fire"]["source"] == "norm_cache"


def test_bridge_health_snapshot_below_threshold_is_healthy():
    now = 10_000.0
    state = StationState()
    for i in range(BRIDGE_HEALTH_THRESHOLD - 1):
        state.record_bridge_fire("resume", "canned", timestamp=now - i)

    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        bh = _bridge_health_snapshot(state)

    assert bh["window_count"] == BRIDGE_HEALTH_THRESHOLD - 1
    assert bh["unhealthy"] is False
    assert bh["unhealthy_reasons"] == []


def test_bridge_health_snapshot_unhealthy_when_queue_empty_threshold_passes():
    now = 20_000.0
    state = StationState()
    state.queue_empty_since = now - BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS

    with (
        patch("mammamiradio.web.streamer.time.time", return_value=now),
        patch("mammamiradio.web.streamer._runtime_monotonic", return_value=now),
    ):
        bh = _bridge_health_snapshot(state)

    assert bh["window_count"] == 0
    assert bh["queue_empty_elapsed_s"] == BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS
    assert bh["unhealthy"] is True
    assert bh["unhealthy_reasons"] == ["queue_empty"]


def test_bridge_health_snapshot_does_not_round_up_below_threshold():
    """Regression: the snapshot must compare the RAW elapsed against the threshold,
    not the rounded payload value. At raw 59.96s the displayed elapsed rounds up to
    60.0, but the station is NOT yet unhealthy — rounding before the compare tripped
    'queue_empty' early. 0.04 (not 0.05) keeps the raw value off the 59.95 banker's-
    rounding midpoint, so round(raw, 1) == 60.0 unambiguously regardless of float
    representation."""
    now = 30_000.0
    state = StationState()
    state.queue_empty_since = now - (BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS - 0.04)

    with (
        patch("mammamiradio.web.streamer.time.time", return_value=now),
        patch("mammamiradio.web.streamer._runtime_monotonic", return_value=now),
    ):
        bh = _bridge_health_snapshot(state)

    # Payload rounds to the threshold for display tidiness...
    assert bh["queue_empty_elapsed_s"] == BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS
    # ...but the raw 59.95s is below threshold, so the station is still healthy.
    assert bh["unhealthy"] is False
    assert bh["unhealthy_reasons"] == []


def test_bridge_health_snapshot_unhealthy_exactly_at_threshold():
    """The boundary itself (raw == 60.0) degrades — the fix must not push the trip
    point past the threshold."""
    now = 40_000.0
    state = StationState()
    state.queue_empty_since = now - BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS

    with (
        patch("mammamiradio.web.streamer.time.time", return_value=now),
        patch("mammamiradio.web.streamer._runtime_monotonic", return_value=now),
    ):
        bh = _bridge_health_snapshot(state)

    assert bh["unhealthy"] is True
    assert bh["unhealthy_reasons"] == ["queue_empty"]


def test_bridge_health_snapshot_ignores_events_outside_window():
    """Stale fires (older than the rolling window) drop out of window_count but
    still count toward the lifetime session_count."""
    now = 100_000.0
    state = StationState()
    # THRESHOLD stale fires, each just outside the window.
    for i in range(BRIDGE_HEALTH_THRESHOLD):
        state.record_bridge_fire("idle", "canned", timestamp=now - BRIDGE_HEALTH_WINDOW_SECONDS - 10 - i)
    # One fresh fire inside the window.
    state.record_bridge_fire("drain", "norm_cache", timestamp=now - 5)

    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        bh = _bridge_health_snapshot(state)

    assert bh["session_count"] == BRIDGE_HEALTH_THRESHOLD + 1  # lifetime, all fires
    assert bh["window_count"] == 1  # only the fresh one
    assert bh["unhealthy"] is False
    assert bh["by_type"] == {"drain": 1, "resume": 0, "idle": BRIDGE_HEALTH_THRESHOLD}


def test_runtime_status_snapshot_includes_bridge_health():
    """The /status runtime_status dict carries bridge_health so the admin card
    can render the Queue rescue row."""
    app = _make_app()
    state = app.state.station_state
    state.record_bridge_fire("drain", "canned", timestamp=1.0)

    req = _fake_request(app)
    rs = _runtime_status_snapshot(req)

    assert "bridge_health" in rs
    assert rs["bridge_health"]["session_count"] == 1


def test_runtime_status_snapshot_bridge_health_degrades_without_marking_off_air():
    app = _make_app()
    state = app.state.station_state
    now = 10_000.0
    for i in range(BRIDGE_HEALTH_THRESHOLD):
        state.record_bridge_fire("drain", "norm_cache", timestamp=now - i)

    req = _fake_request(app)
    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        rs = _runtime_status_snapshot(req)

    assert rs["health_state"] == "degraded"
    assert rs["station_on_air"] is True
    assert rs["bridge_health"]["unhealthy"] is True


def test_runtime_status_snapshot_includes_producer_headroom(tmp_path):
    app = _make_app()
    app.state.config.pacing.lookahead_segments = 4
    app.state.queue = asyncio.Queue(maxsize=6)
    for title in ("A", "B"):
        segment = _queue_segment(title, duration_sec=180.0)
        segment.path = tmp_path / f"{title}.mp3"
        segment.path.write_bytes(b"ready")
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"type": "music"},
        {"type": "music"},
    ]

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        req = _fake_request(app)
        runtime_health = _runtime_health_snapshot(req)
        rs = _runtime_status_snapshot(req, runtime_health=runtime_health)

    headroom = rs["producer_headroom"]
    assert headroom["queue_capacity"] == 6
    assert headroom["lookahead_target"] == 4
    assert headroom["buffered_audio_sec"] == 360.0
    assert headroom["runway_floor_sec"] > 0
    assert headroom["headroom_ok"] is True
    assert headroom["reason"] == "ready runway"


def test_runtime_status_snapshot_producer_headroom_ready_runway(tmp_path):
    app = _make_app()
    app.state.config.pacing.lookahead_segments = 4
    app.state.queue = asyncio.Queue(maxsize=6)
    for idx in range(4):
        segment = _queue_segment(f"Track {idx}", duration_sec=180.0)
        segment.path = tmp_path / f"track-{idx}.mp3"
        segment.path.write_bytes(b"ready")
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"type": "music"},
        {"type": "music"},
        {"type": "music"},
        {"type": "music"},
    ]

    req = _fake_request(app)
    runtime_health = _runtime_health_snapshot(req)
    rs = _runtime_status_snapshot(req, runtime_health=runtime_health)

    headroom = rs["producer_headroom"]
    assert headroom["queue_depth"] == 4
    assert headroom["buffered_audio_sec"] == 720.0
    assert headroom["headroom_ok"] is True
    assert headroom["reason"] == "ready runway"


@pytest.mark.parametrize("rejection", ["missing", "zero-byte", "blocklisted", "stale-cue"])
def test_producer_headroom_never_calls_rejected_runway_ready(tmp_path, rejection):
    """Admin headroom and Skip use the same last-mile playability fence."""
    app = _make_app()
    state = app.state.station_state
    app.state.queue = asyncio.Queue(maxsize=6)
    path = tmp_path / f"{rejection}.mp3"
    if rejection == "zero-byte":
        path.touch()
    elif rejection != "missing":
        path.write_bytes(b"ready")
    metadata = {"artist": "Artist", "title_only": "Song"}
    segment_type = SegmentType.MUSIC
    if rejection == "blocklisted":
        state.blocklist = {("artist", "song"): {"display": "Artist - Song"}}
    elif rejection == "stale-cue":
        segment_type = SegmentType.BANTER
        metadata = {
            "listener_session_cue": "companionship",
            "listener_session_epoch": state.listener_session.epoch + 1,
        }
    segment = Segment(
        type=segment_type,
        path=path,
        duration_sec=300.0,
        metadata=metadata,
        ephemeral=False,
    )
    app.state.queue.put_nowait(segment)

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        req = _fake_request(app)
        runtime_health = _runtime_health_snapshot(req)
        rs = _runtime_status_snapshot(req, runtime_health=runtime_health)

    headroom = rs["producer_headroom"]
    assert _playable_runway_available(app.state.queue, state) is False
    assert headroom["buffered_audio_sec"] == 0.0
    assert headroom["headroom_ok"] is False
    assert headroom["reason"] == "building runway"


def test_continuity_reservation_evicts_only_the_ordinary_tail():
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=3)
    originals = [_queue_segment(f"Track {index}", duration_sec=4.0) for index in range(3)]
    for segment in originals:
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"id": f"row-{index}", "label": segment.metadata["title"]} for index, segment in enumerate(originals)
    ]

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    queued = list(app.state.queue._queue)
    assert evicted == 1
    assert queued[:2] == originals[:2]
    assert queued[-1].metadata["continuity_reservation"] is True
    assert len(app.state.station_state.queued_segments) == app.state.queue.qsize()
    assert app.state.station_state.continuity_epoch == 1


def test_continuity_reservation_eviction_abandons_queued_companionship_cue(tmp_path):
    """Even a default-reason live control settles every cue it removes."""
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=1)
    session = ListenerSession(monotonic=lambda: 1_800.0)
    session.observe_active_count(1, now=0.0)
    claim = session.claim_companionship(now=1_800.0)
    assert claim is not None
    assert session.mark_companionship_queued(claim.epoch) is True
    app.state.station_state.listener_session = session

    cue = Segment(
        type=SegmentType.BANTER,
        path=tmp_path / "companionship.mp3",
        duration_sec=5.0,
        metadata={
            "queue_id": "cue",
            "listener_session_epoch": claim.epoch,
            "listener_session_cue": "companionship",
        },
        ephemeral=False,
    )
    replacement = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "replacement.mp3",
        duration_sec=180.0,
        metadata={"continuity_reservation": True, "queue_id": "replacement"},
        ephemeral=False,
    )
    cue.path.write_bytes(b"ready companionship")
    replacement.path.write_bytes(b"ready replacement")
    app.state.queue.put_nowait(cue)
    app.state.station_state.queued_segments = [{"id": "cue", "type": "banter"}]

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch("mammamiradio.web.streamer._continuity_reservation_segments", return_value=[replacement]),
    ):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    assert evicted == 1
    assert list(app.state.queue._queue) == [replacement]
    assert session.companionship_cue_state is ListenerSessionCueState.ABANDONED
    assert app.state.station_state.discard_by_reason == {GenerationWasteReason.OPERATOR_PURGE: 1}
    assert app.state.station_state.queued_segments[0]["id"] == "replacement"
    assert app.state.queue.get_nowait() is replacement
    app.state.queue.task_done()
    assert app.state.queue._unfinished_tasks == 0


def test_continuity_reservation_never_trades_long_ready_audio_for_short_clip(tmp_path):
    """Count pressure cannot make listener runway shorter than it was before the guard."""
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=3)
    originals = [_queue_segment(f"Long {index}", duration_sec=70.0) for index in range(3)]
    for index, segment in enumerate(originals):
        segment.path = tmp_path / f"long-{index}.mp3"
        segment.path.write_bytes(b"ready long audio")
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"id": f"long-{index}", "label": segment.metadata["title"]} for index, segment in enumerate(originals)
    ]

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    assert evicted == 0
    assert list(app.state.queue._queue) == originals
    assert app.state.station_state.continuity_slot is not None
    assert sum(segment.duration_sec for segment in app.state.queue._queue) == 210.0
    assert app.state.station_state.continuity_epoch == 1


def test_continuity_reservation_uses_capacity_exempt_slot_for_ready_air_next():
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=1)
    air_next = _queue_segment("Operator break", duration_sec=1.0)
    air_next.metadata["air_next"] = True
    app.state.queue.put_nowait(air_next)
    app.state.station_state.queued_segments = [{"id": "operator", "label": "Operator break"}]

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    assert evicted == 0
    assert list(app.state.queue._queue) == [air_next]
    assert app.state.station_state.continuity_slot is not None
    assert app.state.station_state.continuity_slot.metadata["continuity_reservation"] is True


def test_repeated_continuity_guard_reuses_maximal_slot_without_epoch_churn(tmp_path):
    """A full air-next queue cannot admit more runway; keep its existing slot."""
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=1)
    air_next = _queue_segment("Operator break", duration_sec=1.0)
    air_next.metadata["air_next"] = True
    app.state.queue.put_nowait(air_next)
    app.state.station_state.queued_segments = [{"id": "operator", "label": "Operator break"}]
    slot = _queue_segment("Protected continuity", duration_sec=4.44)
    slot.path = tmp_path / "protected_slot.mp3"
    slot.path.write_bytes(b"protected")
    slot.metadata.update(
        {
            "continuity_reservation": True,
            "continuity_reservation_id": "existing-slot",
        }
    )
    app.state.station_state.continuity_slot = slot
    app.state.station_state.continuity_epoch = 7

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    assert evicted == 0
    assert list(app.state.queue._queue) == [air_next]
    assert app.state.station_state.continuity_slot is slot
    assert app.state.station_state.continuity_epoch == 7


def test_insufficient_protected_queue_is_upgraded_when_warm_cache_appears(tmp_path):
    """Only the capacity-exempt air-next slot is maximal; weak queued protection can improve."""
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=2)
    old = [_queue_segment(f"Old protection {index}", duration_sec=5.0) for index in range(2)]
    for segment in old:
        segment.metadata["continuity_reservation"] = True
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"id": f"old-{index}", "label": segment.metadata["title"]} for index, segment in enumerate(old)
    ]
    cached = tmp_path / "norm_new_runway_128k.mp3"
    cached.write_bytes(b"cached")
    app.state.station_state.immediate_audio_index = {cached: 240.0}

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch(
            "mammamiradio.web.streamer.load_track_metadata",
            return_value={"title": "New runway", "artist": "Cache Artist"},
        ),
    ):
        _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    queued = list(app.state.queue._queue)
    assert old[0] not in queued and old[1] not in queued
    assert cached in {segment.path for segment in queued}
    assert sum(segment.duration_sec for segment in queued) >= 240.0
    assert app.state.station_state.continuity_epoch == 1


def test_continuity_reservation_is_bounded_by_queue_capacity(tmp_path):
    """A large candidate set must never evict all real audio then collapse to one slot."""
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=3)
    originals = [_queue_segment(f"Short {index}", duration_sec=1.0) for index in range(3)]
    for segment in originals:
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"id": f"short-{index}", "label": segment.metadata["title"]} for index, segment in enumerate(originals)
    ]
    cached = []
    for index in range(3):
        path = tmp_path / f"norm_capacity_{index}_128k.mp3"
        path.write_bytes(b"cached")
        cached.append(path)
    app.state.station_state.immediate_audio_index = {path: 80.0 for path in cached}

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch("mammamiradio.web.streamer.load_track_metadata", return_value={}),
    ):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    queued = list(app.state.queue._queue)
    assert evicted == 3
    assert len(queued) == app.state.queue.maxsize == 3
    assert all(segment.metadata.get("continuity_reservation") is True for segment in queued)
    assert app.state.station_state.continuity_slot is None
    assert len(app.state.station_state.queued_segments) == len(queued)


def test_continuity_reservation_stops_cache_sidecar_reads_at_target(tmp_path):
    """A live-control hot path reads only the cache candidates it can actually reserve."""
    app = _make_app()
    cached = []
    for index in range(100):
        path = tmp_path / f"norm_hot_path_{index}_128k.mp3"
        path.write_bytes(b"cached")
        cached.append(path)
    app.state.station_state.immediate_audio_index = {path: 180.0 for path in cached}

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch("mammamiradio.web.streamer.load_track_metadata", return_value={}) as metadata_reads,
    ):
        _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    assert metadata_reads.call_count == 2
    assert app.state.queue.qsize() == 3  # packaged clip + the two candidates needed for 240s


def test_continuity_reservation_bounds_and_prunes_blocklisted_cache_scan(tmp_path):
    """A large durable-ban prefix cannot put unbounded sidecar I/O before a live control."""
    state = StationState(blocklist={("blocked artist", "blocked song"): {"display": "Blocked"}})
    cached = []
    for index in range(100):
        path = tmp_path / f"norm_blocked_hot_path_{index}_128k.mp3"
        path.write_bytes(b"cached")
        cached.append(path)
    state.immediate_audio_index = {path: 180.0 for path in cached}

    with patch(
        "mammamiradio.web.streamer.load_track_metadata",
        return_value={"artist": "Blocked Artist", "title": "Blocked Song"},
    ) as metadata_reads:
        selected = _continuity_reservation_segments(state, None, 240.0, max_segments=6)

    assert len(selected) == 1  # packaged continuity remains immediately available
    assert metadata_reads.call_count == _CONTINUITY_CACHE_SCAN_LIMIT
    assert len(state.immediate_audio_index) == 100 - _CONTINUITY_CACHE_SCAN_LIMIT


def test_continuity_reservation_bounds_and_prunes_stale_cache_prefix(tmp_path):
    """Deleted index debris is scanned in bounded batches and removed for the next control."""
    stale = [tmp_path / f"norm_stale_hot_path_{index}_128k.mp3" for index in range(100)]
    live = tmp_path / "norm_live_after_stale_prefix_128k.mp3"
    live.write_bytes(b"cached")
    state = StationState(immediate_audio_index={**{path: 180.0 for path in stale}, live: 180.0})

    with (
        patch(
            "mammamiradio.web.streamer._indexed_audio_path_is_file",
            side_effect=lambda path: path.is_file(),
        ) as existence_checks,
        patch("mammamiradio.web.streamer.load_track_metadata", return_value={}) as metadata_reads,
    ):
        selected = _continuity_reservation_segments(state, None, 240.0, max_segments=6)

    assert len(selected) == 1
    assert existence_checks.call_count == _CONTINUITY_CACHE_SCAN_LIMIT
    metadata_reads.assert_not_called()
    assert len(state.immediate_audio_index) == 101 - _CONTINUITY_CACHE_SCAN_LIMIT
    assert live in state.immediate_audio_index


def test_continuity_reservation_honors_excluded_track_keys_not_on_blocklist(tmp_path):
    """A just-removed song that was never banned still cannot re-enter the instant-audio path.

    `excluded_track_keys` is a distinct branch from the durable blocklist: it is
    how `dismiss_listener_request` (and the by-key ban path) keep a track that is
    *not* in `state.blocklist` out of the reservation. Nothing else guards it, so
    it needs its own coverage. A session exclusion is not a durable ban, so the
    index entry survives for a later control action once the key is no longer
    excluded.
    """
    state = StationState()
    dismissed = tmp_path / "norm_dismissed_128k.mp3"
    dismissed.write_bytes(b"cached")
    state.immediate_audio_index = {dismissed: 240.0}

    with patch(
        "mammamiradio.web.streamer.load_track_metadata",
        return_value={"artist": "Dismissed Artist", "title": "Dismissed Song"},
    ):
        selected = _continuity_reservation_segments(
            state,
            None,
            240.0,
            max_segments=6,
            excluded_track_keys={("dismissed artist", "dismissed song")},
        )

    assert all(segment.path != dismissed for segment in selected)
    assert dismissed in state.immediate_audio_index


def test_continuity_reservation_honors_excluded_paths(tmp_path):
    """A removed queue segment's own file cannot be reintroduced as continuity runway."""
    state = StationState()
    removed = tmp_path / "norm_removed_128k.mp3"
    removed.write_bytes(b"cached")
    state.immediate_audio_index = {removed: 240.0}

    with patch("mammamiradio.web.streamer.load_track_metadata", return_value={"artist": "A", "title": "T"}):
        selected = _continuity_reservation_segments(
            state,
            None,
            240.0,
            max_segments=6,
            excluded_paths={removed},
        )

    assert all(segment.path != removed for segment in selected)


def test_continuity_reservation_keeps_maximal_prefix_beside_air_next(tmp_path):
    """Mixed capacity keeps every feasible protected member instead of collapsing to one slot."""
    app = _make_app()
    app.state.queue = asyncio.Queue(maxsize=3)
    air_next = _queue_segment("Operator break", duration_sec=1.0)
    air_next.metadata["air_next"] = True
    ordinary = [_queue_segment(f"Ordinary {index}", duration_sec=1.0) for index in range(2)]
    for segment in [air_next, *ordinary]:
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"id": f"row-{index}", "label": segment.metadata["title"]}
        for index, segment in enumerate([air_next, *ordinary])
    ]
    cached = []
    for index in range(3):
        path = tmp_path / f"norm_mixed_{index}_128k.mp3"
        path.write_bytes(b"cached")
        cached.append(path)
    app.state.station_state.immediate_audio_index = {path: 80.0 for path in cached}

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch("mammamiradio.web.streamer.load_track_metadata", return_value={}),
    ):
        evicted = _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)
        first_epoch = app.state.station_state.continuity_epoch
        _reserve_continuity_runway(app.state, app.state.station_state, app.state.config)

    queued = list(app.state.queue._queue)
    assert evicted == 2
    assert queued[0] is air_next
    assert len(queued) == app.state.queue.maxsize
    assert all(segment.metadata.get("continuity_reservation") for segment in queued[1:])
    assert app.state.station_state.continuity_slot is None
    assert app.state.station_state.continuity_epoch == first_epoch == 1


def test_runtime_status_projects_and_counts_capacity_exempt_continuity(tmp_path):
    app = _make_app()
    slot = _queue_segment("Protected continuity", duration_sec=240.0)
    slot.path = tmp_path / "norm_protected_status_128k.mp3"
    slot.path.write_bytes(b"protected")
    slot.metadata.update(
        {
            "continuity_reservation": True,
            "continuity_reservation_id": "status-slot",
            "audio_source": "norm_cache",
        }
    )
    app.state.station_state.continuity_slot = slot

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        req = _fake_request(app)
        runtime_health = _runtime_health_snapshot(req)
        rs = _runtime_status_snapshot(req, runtime_health=runtime_health)

    assert rs["continuity_slot"] == {
        "label": "Protected continuity",
        "duration_sec": 240.0,
        "audio_source": "norm_cache",
        "reservation_id": "status-slot",
    }
    assert rs["producer_headroom"]["buffered_audio_sec"] == 240.0
    assert rs["producer_headroom"]["continuity_slot_sec"] == 240.0
    assert rs["producer_headroom"]["headroom_ok"] is True


def test_runtime_status_clears_missing_capacity_exempt_continuity(tmp_path):
    """A deleted cache slot is not advertised or counted as ready audio."""
    app = _make_app()
    app.state.station_state.continuity_slot = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "norm_missing_slot_128k.mp3",
        duration_sec=240.0,
        metadata={"continuity_reservation": True, "audio_source": "norm_cache"},
        ephemeral=False,
    )

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        req = _fake_request(app)
        runtime_health = _runtime_health_snapshot(req)
        rs = _runtime_status_snapshot(req, runtime_health=runtime_health)

    assert rs["continuity_slot"] is None
    assert rs["producer_headroom"]["continuity_slot_sec"] == 0.0
    assert rs["producer_headroom"]["headroom_ok"] is False
    assert app.state.station_state.continuity_slot is None


def test_replace_continuity_reservation_supersedes_old_protected_runway_and_slot(tmp_path):
    """A second destructive control must not retain an earlier safety reservation."""
    app = _make_app()
    old_reservation = _queue_segment("Old protected runway", duration_sec=4.44)
    old_reservation.metadata["continuity_reservation"] = True
    old_reservation.metadata["continuity_reservation_id"] = "old-runway"
    app.state.queue.put_nowait(old_reservation)
    stale_slot = _queue_segment("Old capacity-exempt runway", duration_sec=4.44)
    stale_slot.path = tmp_path / "old_capacity_exempt_runway.mp3"
    stale_slot.path.write_bytes(b"ready")
    stale_slot.metadata["continuity_reservation"] = True
    app.state.station_state.continuity_slot = stale_slot
    app.state.station_state.last_enqueued_type = SegmentType.MUSIC

    dropped = _reserve_continuity_runway(
        app.state,
        app.state.station_state,
        app.state.config,
        replace_queue=True,
    )

    queued = list(app.state.queue._queue)
    assert dropped == 1
    assert queued and queued[0] is not old_reservation
    assert queued[0].metadata["continuity_reservation"] is True
    assert app.state.station_state.continuity_slot is None
    assert app.state.station_state.last_enqueued_type == SegmentType.BANTER


def test_continuity_reservation_never_requeues_a_blocklisted_cached_track(tmp_path):
    """Direct queue rebuilds still honor the final music blocklist gate."""
    app = _make_app()
    banned = tmp_path / "norm_banned_128k.mp3"
    banned.write_bytes(b"cached")
    app.state.station_state.immediate_audio_index = {banned: 240.0}
    app.state.station_state.blocklist = {("blocked artist", "blocked song"): {"display": "Blocked track"}}

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch(
            "mammamiradio.web.streamer.load_track_metadata",
            return_value={"artist": "Blocked Artist", "title": "Blocked Song"},
        ),
    ):
        _reserve_continuity_runway(app.state, app.state.station_state, app.state.config, replace_queue=True)

    assert all(segment.path != banned for segment in app.state.queue._queue)


def test_replace_continuity_reservation_preserves_ready_head_and_slot_when_no_audio_is_available(tmp_path):
    """An assetless replacement keeps ready runway and frees only tail capacity."""
    app = _make_app()
    old_song = _queue_segment("Old song", duration_sec=180.0)
    old_song.path = tmp_path / "old_song.mp3"
    old_song.path.write_bytes(b"ready")
    tails = [_queue_segment(f"Tail {index}", duration_sec=180.0) for index in range(2)]
    for index, segment in enumerate(tails):
        segment.path = tmp_path / f"tail_{index}.mp3"
        segment.path.write_bytes(b"tail")
    app.state.queue.put_nowait(old_song)
    for segment in tails:
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [{"label": segment.metadata["title"]} for segment in [old_song, *tails]]
    slot = _queue_segment("Existing slot", duration_sec=4.44)
    slot.path = tmp_path / "existing_slot.mp3"
    slot.path.write_bytes(b"slot")
    slot.metadata["continuity_reservation"] = True
    app.state.station_state.continuity_slot = slot
    app.state.station_state.last_enqueued_type = SegmentType.MUSIC
    app.state.station_state.continuity_epoch = 7
    missing_demo_root = tmp_path / "missing-demo-assets"

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", missing_demo_root):
        dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )

    assert dropped == 2
    assert list(app.state.queue._queue) == [old_song]
    assert app.state.station_state.continuity_slot is slot
    assert app.state.station_state.last_enqueued_type is SegmentType.MUSIC
    assert app.state.station_state.continuity_epoch == 8
    assert len(app.state.station_state.queued_segments) == 1


def test_failed_replacement_with_no_playable_queue_is_a_noop(tmp_path):
    """An assetless replacement must not mutate an entirely unusable queue."""
    app = _make_app()
    invalid = [_queue_segment(f"Invalid {index}", duration_sec=180.0) for index in range(2)]
    for index, segment in enumerate(invalid):
        segment.path = tmp_path / f"invalid_{index}.mp3"
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [{"label": segment.metadata["title"]} for segment in invalid]
    app.state.station_state.last_enqueued_type = SegmentType.MUSIC
    app.state.station_state.continuity_epoch = 23
    before_shadow = copy.deepcopy(app.state.station_state.queued_segments)
    before_tail_type = app.state.station_state.last_enqueued_type
    before_discard_bookkeeping = (
        app.state.station_state.discarded_segments_total,
        app.state.station_state.discarded_duration_total_sec,
        app.state.station_state.discarded_unproduced_segments_total,
        dict(app.state.station_state.discard_by_reason),
        dict(app.state.station_state.discard_by_type),
        list(app.state.station_state.discard_events),
        app.state.station_state.shadow_queue_corrections,
        app.state.queue._unfinished_tasks,
    )

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )

    assert dropped == 0
    assert list(app.state.queue._queue) == invalid
    assert app.state.station_state.queued_segments == before_shadow
    assert app.state.station_state.continuity_slot is None
    assert app.state.station_state.continuity_epoch == 23
    assert app.state.station_state.last_enqueued_type is before_tail_type
    assert (
        app.state.station_state.discarded_segments_total,
        app.state.station_state.discarded_duration_total_sec,
        app.state.station_state.discarded_unproduced_segments_total,
        dict(app.state.station_state.discard_by_reason),
        dict(app.state.station_state.discard_by_type),
        list(app.state.station_state.discard_events),
        app.state.station_state.shadow_queue_corrections,
        app.state.queue._unfinished_tasks,
    ) == before_discard_bookkeeping


def test_failed_replacement_uses_valid_slot_as_only_runway_and_advances_epoch_only_for_queue_mutation(tmp_path):
    """A ready capacity-exempt slot survives while an invalid queue is removed once."""
    app = _make_app()
    invalid = [_queue_segment(f"Invalid tail {index}", duration_sec=180.0) for index in range(2)]
    for index, segment in enumerate(invalid):
        segment.path = tmp_path / f"missing_tail_{index}.mp3"
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [{"label": segment.metadata["title"]} for segment in invalid]
    slot = _queue_segment("Existing capacity-exempt slot", duration_sec=4.44)
    slot.path = tmp_path / "existing_capacity_exempt_slot.mp3"
    slot.path.write_bytes(b"slot")
    slot.metadata["continuity_reservation"] = True
    app.state.station_state.continuity_slot = slot
    app.state.station_state.continuity_epoch = 19

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        first_dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )
        epoch_after_mutation = app.state.station_state.continuity_epoch
        second_dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )

    assert first_dropped == 2
    assert second_dropped == 0
    assert app.state.queue.empty()
    assert app.state.station_state.queued_segments == []
    assert app.state.station_state.continuity_slot is slot
    assert epoch_after_mutation == 20
    assert app.state.station_state.continuity_epoch == epoch_after_mutation


def test_replacement_clears_missing_existing_continuity_slot_before_building_runway(tmp_path):
    """A disappeared out-of-band slot cannot block a fresh replacement reserve."""
    app = _make_app()
    missing_slot = _queue_segment("Missing existing slot", duration_sec=4.44)
    missing_slot.path = tmp_path / "missing_existing_slot.mp3"
    missing_slot.metadata["continuity_reservation"] = True
    app.state.station_state.continuity_slot = missing_slot

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )

    assert dropped == 0
    assert app.state.queue.empty()
    assert app.state.station_state.continuity_slot is None


def test_slot_only_assetless_replacement_clears_stale_music_adjacency(tmp_path):
    """A valid out-of-band slot is a continuity boundary, not a music queue tail."""
    app = _make_app()
    stale_song = tmp_path / "stale_song.mp3"
    stale_song.write_bytes(b"stale")
    app.state.station_state.last_music_file = stale_song
    app.state.station_state.last_enqueued_type = SegmentType.MUSIC

    slot = _queue_segment("Existing capacity-exempt slot", duration_sec=4.44)
    slot.path = tmp_path / "existing_capacity_exempt_slot.mp3"
    slot.path.write_bytes(b"slot")
    slot.metadata["continuity_reservation"] = True
    app.state.station_state.continuity_slot = slot

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )

    assert dropped == 0
    assert app.state.queue.empty()
    assert app.state.station_state.continuity_slot is slot
    assert app.state.station_state.last_enqueued_type is None
    assert _adjacent_music_source(app.state.station_state) is None


def test_replace_continuity_reservation_promotes_first_playable_segment_past_missing_head(tmp_path):
    """A broken head cannot hide ready audio later in the queue during fallback."""
    app = _make_app()
    missing_head = _queue_segment("Missing head", duration_sec=180.0)
    missing_head.path = tmp_path / "missing_head.mp3"
    playable = _queue_segment("First playable", duration_sec=180.0)
    playable.path = tmp_path / "first_playable.mp3"
    playable.path.write_bytes(b"ready")
    tail = _queue_segment("Later tail", duration_sec=180.0)
    tail.path = tmp_path / "later_tail.mp3"
    tail.path.write_bytes(b"tail")
    for segment in (missing_head, playable, tail):
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"label": segment.metadata["title"]} for segment in (missing_head, playable, tail)
    ]
    app.state.station_state.continuity_epoch = 11

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
        )

    assert dropped == 2
    assert list(app.state.queue._queue) == [playable]
    assert app.state.station_state.continuity_epoch == 12
    assert len(app.state.station_state.queued_segments) == 1


def test_companionship_cue_is_playable_runway_only_while_current_and_queued(tmp_path):
    """The cutover predicate must match the playback loop's cue fence."""
    app = _make_app()
    now = [0.0]
    session = ListenerSession(monotonic=lambda: now[0])
    session.observe_active_count(1, now=0.0)
    now[0] = 1_800.0
    claim = session.claim_companionship()
    assert claim is not None
    assert session.mark_companionship_queued(claim.epoch) is True
    app.state.station_state.listener_session = session

    cue_path = tmp_path / "companionship_runway.mp3"
    cue_path.write_bytes(b"cue")
    cue = Segment(
        type=SegmentType.BANTER,
        path=cue_path,
        duration_sec=5.0,
        metadata={
            "listener_session_cue": "companionship",
            "listener_session_epoch": claim.epoch,
        },
        ephemeral=False,
    )

    assert _segment_is_immediately_playable(app.state.station_state, cue) is True

    cue.metadata["listener_session_epoch"] = True
    assert _segment_is_immediately_playable(app.state.station_state, cue) is False
    cue.metadata["listener_session_epoch"] = claim.epoch
    assert _segment_is_immediately_playable(app.state.station_state, cue) is True

    assert session.mark_companionship_consumed(claim.epoch) is True
    assert _segment_is_immediately_playable(app.state.station_state, cue) is False

    session.observe_active_count(0, now=1_800.0)
    now[0] = 2_400.0
    session.observe_active_count(1, now=2_400.0)

    assert session.epoch == claim.epoch + 1
    assert _segment_is_immediately_playable(app.state.station_state, cue) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [GenerationWasteReason.OPERATOR_PURGE, GenerationWasteReason.OPERATOR_BAN],
)
async def test_discard_unplayable_non_cue_prefix_preserves_operator_reason(tmp_path, reason):
    """Ordinary rejected audio keeps the Skip/Ban reason while exposing safe runway."""
    app = _make_app()
    state = app.state.station_state
    blocked_path = tmp_path / "blocked-prefix.mp3"
    blocked_path.write_bytes(b"blocked")
    blocked = Segment(
        type=SegmentType.MUSIC,
        path=blocked_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "blocked-prefix",
            "artist": "Blocked Artist",
            "title_only": "Blocked Song",
        },
        ephemeral=False,
    )
    safe_path = tmp_path / "safe-survivor.mp3"
    safe_path.write_bytes(b"safe")
    safe = Segment(
        type=SegmentType.MUSIC,
        path=safe_path,
        duration_sec=180.0,
        metadata={
            "queue_id": "safe-survivor",
            "artist": "Safe Artist",
            "title_only": "Safe Song",
        },
        ephemeral=False,
    )
    for segment in (blocked, safe):
        app.state.queue.put_nowait(segment)
    blocked_shadow = {"id": "blocked-prefix", "type": "music", "label": "Blocked Song"}
    safe_shadow = {"id": "safe-survivor", "type": "music", "label": "Safe Song"}
    state.queued_segments = [blocked_shadow, safe_shadow]
    state.blocklist = {("blocked artist", "blocked song"): {"display": "Blocked Artist - Blocked Song"}}
    state.continuity_epoch = 7

    dropped = _discard_unplayable_queue_prefix(app.state.queue, state, reason=reason)

    assert dropped == 1
    assert list(app.state.queue._queue) == [safe]
    assert state.queued_segments == [safe_shadow]
    assert state.discard_by_reason[reason] == 1
    assert state.discard_by_reason.get(GenerationWasteReason.LISTENER_SESSION_STALE, 0) == 0
    assert state.continuity_epoch == 8
    assert app.state.queue._unfinished_tasks == 1
    assert app.state.queue.get_nowait() is safe
    app.state.queue.task_done()
    await asyncio.wait_for(app.state.queue.join(), timeout=1.0)


def test_apply_ban_clears_lone_blocked_tail_as_speech_bed(tmp_path):
    """Purging the final queued song must sever its stale bed eligibility."""
    app = _make_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    blocked_path = tmp_path / "blocked-tail.mp3"
    blocked_path.write_bytes(b"blocked")
    blocked = Segment(
        type=SegmentType.MUSIC,
        path=blocked_path,
        duration_sec=180.0,
        metadata={"artist": "Blocked Artist", "title_only": "Blocked Song"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(blocked)
    state.last_music_file = blocked_path
    state.last_enqueued_type = SegmentType.MUSIC

    result = _apply_ban(
        state,
        app.state.config,
        [Track(title="Blocked Song", artist="Blocked Artist", duration_ms=180_000)],
        queue=app.state.queue,
    )

    assert result["purged"] == 1
    assert app.state.queue.empty()
    assert state.last_enqueued_type is None
    assert _adjacent_music_source(state) is None


def test_apply_ban_reanchors_exposed_protected_music_tail(tmp_path):
    """An exposed cache-rescue survivor remains a safe, known-clean bed source."""
    app = _make_app()
    app.state.config.cache_dir = tmp_path
    state = app.state.station_state
    protected_path = tmp_path / "protected-survivor.mp3"
    protected_path.write_bytes(b"protected")
    protected = Segment(
        type=SegmentType.MUSIC,
        path=protected_path,
        duration_sec=180.0,
        metadata={
            "artist": "Safe Artist",
            "title_only": "Safe Song",
            "rescue": True,
            "continuity_reservation": True,
        },
        ephemeral=False,
    )
    blocked_path = tmp_path / "blocked-tail.mp3"
    blocked_path.write_bytes(b"blocked")
    blocked = Segment(
        type=SegmentType.MUSIC,
        path=blocked_path,
        duration_sec=180.0,
        metadata={"artist": "Blocked Artist", "title_only": "Blocked Song"},
        ephemeral=False,
    )
    app.state.queue.put_nowait(protected)
    app.state.queue.put_nowait(blocked)
    state.last_music_file = blocked_path
    state.last_enqueued_type = SegmentType.MUSIC

    with patch(
        "mammamiradio.scheduling.producer.load_track_metadata",
        return_value={"artist": "Safe Artist", "title": "Safe Song"},
    ):
        result = _apply_ban(
            state,
            app.state.config,
            [Track(title="Blocked Song", artist="Blocked Artist", duration_ms=180_000)],
            queue=app.state.queue,
        )

        assert result["purged"] == 1
        assert list(app.state.queue._queue) == [protected]
        assert state.last_enqueued_type is SegmentType.MUSIC
        assert state.last_music_file == protected_path
        assert _adjacent_music_source(state) == protected_path


@pytest.mark.parametrize("rejection", ["excluded", "blocklisted"])
def test_failed_replacement_rejects_unsafe_head_and_retains_later_safe_segment(tmp_path, rejection):
    """Exclusion policy and durable bans cannot hide the next safe queued segment."""
    app = _make_app()
    unsafe_head = _queue_segment("Unsafe head", duration_sec=180.0)
    unsafe_head.path = tmp_path / "unsafe_head.mp3"
    unsafe_head.path.write_bytes(b"unsafe")
    unsafe_head.metadata.update({"artist": "Unsafe Artist", "title_only": "Unsafe Song"})
    safe = _queue_segment("Safe segment", duration_sec=180.0)
    safe.path = tmp_path / "safe_segment.mp3"
    safe.path.write_bytes(b"safe")
    tail = _queue_segment("Later tail", duration_sec=180.0)
    tail.path = tmp_path / "later_safe_tail.mp3"
    tail.path.write_bytes(b"tail")
    for segment in (unsafe_head, safe, tail):
        app.state.queue.put_nowait(segment)
    app.state.station_state.queued_segments = [
        {"label": segment.metadata["title"]} for segment in (unsafe_head, safe, tail)
    ]
    excluded_paths = {unsafe_head.path} if rejection == "excluded" else None
    if rejection == "blocklisted":
        app.state.station_state.blocklist = {
            ("unsafe artist", "unsafe song"): {"display": "Unsafe Artist - Unsafe Song"}
        }

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"):
        dropped = _reserve_continuity_runway(
            app.state,
            app.state.station_state,
            app.state.config,
            replace_queue=True,
            excluded_paths=excluded_paths,
        )

    assert dropped == 2
    assert list(app.state.queue._queue) == [safe]
    assert app.state.station_state.queued_segments[0]["label"] == "Safe segment"


@pytest.mark.asyncio
async def test_failed_replacement_invalidates_held_producer_admission(tmp_path):
    """Freed fallback capacity cannot admit producer work from the prior epoch."""
    app = _make_app()
    state = app.state.station_state
    config = app.state.config
    state.continuity_epoch = 13
    state.begin_render_timing(SegmentType.MUSIC.value)

    ready = _queue_segment("Ready head", duration_sec=180.0)
    ready.path = tmp_path / "ready_head.mp3"
    ready.path.write_bytes(b"ready")
    tail = _queue_segment("Discarded tail", duration_sec=180.0)
    tail.path = tmp_path / "discarded_tail.mp3"
    tail.path.write_bytes(b"tail")
    candidate = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "stale_candidate.mp3",
        duration_sec=180.0,
        metadata={"title": "Stale candidate", "title_only": "Stale candidate", "artist": "Artist"},
        ephemeral=True,
    )
    candidate.path.write_bytes(b"candidate")
    put_started = asyncio.Event()

    class ObservedQueue(asyncio.Queue[Segment]):
        async def put(self, item: Segment) -> None:
            if item is candidate:
                put_started.set()
            await super().put(item)

    queue = ObservedQueue(maxsize=2)
    queue.put_nowait(ready)
    queue.put_nowait(tail)
    app.state.queue = queue
    state.queued_segments = [
        {"id": "ready", "type": "music", "label": "Ready head"},
        {"id": "tail", "type": "music", "label": "Discarded tail"},
    ]
    captured_epoch = state.continuity_epoch

    def _stale_reason() -> str | None:
        if state.continuity_epoch != captured_epoch:
            return GenerationWasteReason.STALE_CONTINUITY
        return None

    with (
        patch(
            "mammamiradio.scheduling.producer._apply_egress",
            new_callable=AsyncMock,
            return_value=candidate,
        ),
        patch("mammamiradio.scheduling.producer._schedule_restart_handoff_spool") as schedule_spool,
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"),
    ):
        enqueue_task = asyncio.create_task(
            _enqueue_with_egress(
                queue,
                state,
                config,
                candidate,
                shadow_entry={"id": "candidate", "type": "music", "label": "Stale candidate"},
                stale_check=_stale_reason,
            )
        )
        await asyncio.wait_for(put_started.wait(), timeout=1.0)
        dropped = _reserve_continuity_runway(
            app.state,
            state,
            config,
            replace_queue=True,
        )
        admitted = await asyncio.wait_for(enqueue_task, timeout=1.0)

    state.finish_render_timing("discarded", reason=GenerationWasteReason.STALE_CONTINUITY)
    assert dropped == 1
    assert state.continuity_epoch == captured_epoch + 1
    assert admitted is False
    assert list(queue._queue) == [ready]
    assert len(state.queued_segments) == 1
    assert state.queued_segments[0]["label"] == "Ready head"
    assert not candidate.path.exists()
    assert state.discard_by_reason[GenerationWasteReason.STALE_CONTINUITY] == 1
    schedule_spool.assert_not_called()

    assert queue.get_nowait() is ready
    queue.task_done()
    await asyncio.wait_for(queue.join(), timeout=1.0)


def test_continuity_slot_claim_rejects_track_blocklisted_after_reservation(tmp_path):
    """The final playback claim closes the reserve-then-ban race."""
    state = StationState()
    path = tmp_path / "norm_late_ban_128k.mp3"
    path.write_bytes(b"ready")
    slot = Segment(
        type=SegmentType.MUSIC,
        path=path,
        duration_sec=180.0,
        metadata={
            "artist": "Late Artist",
            "title_only": "Late Song",
            "continuity_reservation": True,
        },
        ephemeral=False,
    )
    state.continuity_slot = slot
    state.blocklist = {("late artist", "late song"): {"display": "Late Artist - Late Song"}}

    assert _claim_continuity_slot(state) is None
    assert state.continuity_slot is None


def test_playable_runway_available_ignores_slot_when_queue_head_is_unplayable(tmp_path):
    """A ready slot must not greenlight a cut the playback loop won't honor.

    ``run_playback_loop`` consumes the capacity-exempt slot only when the real
    queue is empty. With a present-but-unplayable head, the loop pulls that head
    (not the slot), so cutting the current segment would break the illusion. The
    gate must return False even though the slot itself is ready.
    """
    state = StationState()
    ready_slot_path = tmp_path / "ready_slot.mp3"
    ready_slot_path.write_bytes(b"slot")
    state.continuity_slot = Segment(
        type=SegmentType.BANTER,
        path=ready_slot_path,
        duration_sec=4.44,
        metadata={"title": "Protected continuity", "continuity_reservation": True},
        ephemeral=False,
    )

    queue = asyncio.Queue()
    unplayable_head = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "missing_head.mp3",  # never written to disk
        duration_sec=180.0,
        metadata={"title": "Missing head", "title_only": "Missing head", "artist": "Artist"},
        ephemeral=False,
    )
    queue.put_nowait(unplayable_head)

    assert _playable_runway_available(queue, state) is False

    # Draining the unplayable head lets the ready slot bridge the cut.
    queue.get_nowait()
    assert _playable_runway_available(queue, state) is True


def test_segment_is_immediately_playable_handles_pathless_segment():
    """A segment with no path is not playable — it must never crash the gate.

    ``_segment_is_immediately_playable`` runs on the no-await live-control hot
    path; a raised AttributeError there would surface as a 500 on panic-cut or
    source-switch instead of degrading to the recovery ladder.
    """
    state = StationState()
    pathless = Segment(
        type=SegmentType.MUSIC,
        path=None,  # type: ignore[arg-type]
        duration_sec=180.0,
        metadata={"title": "No path", "title_only": "No path", "artist": "Artist"},
        ephemeral=False,
    )

    assert _segment_is_immediately_playable(state, pathless) is False


def test_continuity_reservation_uses_distinct_indexed_cache_tracks_to_reach_target(tmp_path):
    app = _make_app()
    first = tmp_path / "norm_first_128k.mp3"
    second = tmp_path / "norm_second_128k.mp3"
    first.write_bytes(b"cached")
    second.write_bytes(b"cached")
    app.state.station_state.immediate_audio_index = {first: 120.0, second: 120.0}

    with patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240):
        _reserve_continuity_runway(app.state, app.state.station_state, app.state.config, replace_queue=True)

    queued = list(app.state.queue._queue)
    assert [segment.path for segment in queued][1:] == [first, second]
    assert sum(segment.duration_sec for segment in queued) >= 240
    assert app.state.station_state.last_enqueued_type == SegmentType.MUSIC
    assert app.state.station_state.last_music_file == second


def test_continuity_reservation_precommits_packaged_tone_when_clip_and_cache_are_unavailable(tmp_path):
    app = _make_app()
    demo_root = tmp_path / "assets" / "demo"
    tone = demo_root / "recovery" / "emergency_tone.mp3"
    tone.parent.mkdir(parents=True)
    payload = b"tone"
    tone.write_bytes(payload)
    (demo_root / "spoken_assets.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": [
                    {
                        "path": "recovery/emergency_tone.mp3",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "kind": "tone",
                        "language": "none",
                        "transcript": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with (
        patch("mammamiradio.scheduling.producer.RUNWAY_FLOOR_SECONDS", 240),
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", demo_root),
    ):
        _reserve_continuity_runway(app.state, app.state.station_state, app.state.config, replace_queue=True)

    segment = app.state.queue.get_nowait()
    assert segment.path == tone
    assert segment.metadata["audio_source"] == "emergency_tone"
    assert segment.metadata["continuity_reservation"] is True


def test_continuity_reservation_rejects_tampered_manifested_emergency_tone(tmp_path):
    demo_root = tmp_path / "assets" / "demo"
    tone = demo_root / "recovery" / "emergency_tone.mp3"
    tone.parent.mkdir(parents=True)
    reviewed = b"reviewed tone"
    tone.write_bytes(reviewed)
    (demo_root / "spoken_assets.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": [
                    {
                        "path": "recovery/emergency_tone.mp3",
                        "sha256": hashlib.sha256(reviewed).hexdigest(),
                        "kind": "tone",
                        "language": "none",
                        "transcript": "",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state = StationState()

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", demo_root):
        approved = _continuity_reservation_segments(state, None, 2.0)
    assert [segment.path for segment in approved] == [tone]

    tone.write_bytes(b"tampered tone")
    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", demo_root):
        tampered = _continuity_reservation_segments(state, None, 2.0)
    assert all(segment.path != tone for segment in tampered)


def test_continuity_reservation_rejects_unmanifested_or_tampered_spoken_recovery(tmp_path):
    demo_root = tmp_path / "assets" / "demo"
    recovery_dir = demo_root / "recovery"
    recovery_dir.mkdir(parents=True)
    clip = recovery_dir / "continuity_1.mp3"
    clip.write_bytes(b"unreviewed speech")
    state = StationState()

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", demo_root):
        unmanifested = _continuity_reservation_segments(state, None, 4.0)
    assert all(segment.path != clip for segment in unmanifested)

    reviewed = b"reviewed speech"
    clip.write_bytes(reviewed)
    (demo_root / "spoken_assets.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assets": [
                    {
                        "path": "recovery/continuity_1.mp3",
                        "sha256": hashlib.sha256(reviewed).hexdigest(),
                        "kind": "speech",
                        "language": "en",
                        "transcript": "The station stays on air.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    clip.write_bytes(b"tampered speech")

    with patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", demo_root):
        tampered = _continuity_reservation_segments(state, None, 4.0)
    assert all(segment.path != clip for segment in tampered)


def test_generation_waste_snapshot_empty_is_not_degraded():
    state = StationState()
    gw = _generation_waste_snapshot(state)

    assert gw["total_segments"] == 0
    assert gw["recent_segments"] == 0
    assert gw["estimated_waste_cost_usd"] == 0.0
    assert gw["degraded"] is False
    assert "cost_basis" in gw


def test_generation_waste_snapshot_degraded_at_count_threshold(tmp_path):
    state = StationState()
    now = 10_000.0
    segment = Segment(type=SegmentType.BANTER, path=tmp_path / "b.mp3", duration_sec=5.0)
    for i in range(GENERATION_WASTE_DEGRADED_COUNT):
        state.record_discard(segment, reason="operator_stop", timestamp=now - i)

    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        gw = _generation_waste_snapshot(state)

    assert gw["recent_segments"] == GENERATION_WASTE_DEGRADED_COUNT
    assert gw["degraded"] is True
    assert gw["recent_top_reason"] == "operator_stop"


def test_generation_waste_snapshot_compares_raw_duration_before_rounding(tmp_path):
    # A single discard just under the threshold whose duration would round UP to
    # the threshold must NOT trip degraded — the comparison uses the raw sum and
    # rounds only the displayed payload value (#397).
    state = StationState()
    now = 10_000.0
    just_under = GENERATION_WASTE_DEGRADED_SECONDS - 0.04  # rounds to the threshold
    segment = Segment(type=SegmentType.MUSIC, path=tmp_path / "m.mp3", duration_sec=just_under)
    state.record_discard(segment, reason="quality_gate_reject", timestamp=now)

    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        gw = _generation_waste_snapshot(state)

    assert gw["recent_duration_sec"] == round(just_under, 1)  # display rounds up
    assert gw["recent_duration_sec"] >= GENERATION_WASTE_DEGRADED_SECONDS
    assert gw["degraded"] is False  # but the raw comparison keeps it under


def test_purge_clears_queue_even_when_ephemeral_unlink_fails():
    # A non-missing OSError during a temp unlink must not abort the purge: the
    # queue drains, the shadow clears, discards are recorded, and the count is
    # returned (#397).
    q: asyncio.Queue = asyncio.Queue()
    good = Segment(type=SegmentType.MUSIC, path=Path("/tmp/purge_ok.mp3"), metadata={"title": "A"}, ephemeral=True)
    bad_path = MagicMock(spec=Path)
    bad_path.unlink.side_effect = OSError("permission denied")
    bad = Segment(type=SegmentType.MUSIC, path=bad_path, metadata={"title": "B"}, ephemeral=True)
    q.put_nowait(good)
    q.put_nowait(bad)

    state = StationState()
    state.queued_segments = [{"id": "1"}, {"id": "2"}]

    count = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_PURGE)

    assert count == 2
    assert q.empty()
    assert state.queued_segments == []
    assert state.discarded_segments_total == 2
    assert state.discard_by_reason.get("operator_purge") == 2
    bad_path.unlink.assert_called_once()


def test_purge_restores_queued_release_beat_attempt():
    q: asyncio.Queue = asyncio.Queue()
    release_segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/release_banter.mp3"),
        metadata={
            "release_beat_id": "beat-1",
            "release_beat_attempt_id": "attempt-1",
            "queue_id": "q1",
        },
    )
    q.put_nowait(release_segment)
    state = StationState()
    state.queued_segments = [{"id": "q1"}]
    state.release_campaign = MagicMock()
    state.release_campaign.record_queue_discard.return_value = True

    count = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_PURGE)

    assert count == 1
    state.release_campaign.record_queue_discard.assert_called_once_with(release_segment.metadata)
    state.release_campaign.save_if_dirty.assert_called_once()


def test_purge_demotes_carried_moment_receipt():
    """A queued banter carrying an elected ritual/gag receipt that gets purged
    (stop, panic, source-switch, chaos/festival-enable, /api/purge — every
    caller routes through this one drain) must have its row demoted. Without
    this, the admin Moments panel keeps showing "waiting for its break" for a
    segment that no longer exists in the real queue."""
    from mammamiradio.home.moment_receipts import MomentStore

    q: asyncio.Queue = asyncio.Queue()
    store = MomentStore()
    ritual_id = store.record(lane="directive", family="morning_launch", public_label="Morning launch")
    gag_id = store.record(lane="running_gag", family="fridge_freezer_raid", public_label="Kitchen ritual")
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/purge_receipt.mp3"),
        metadata={"title": "Banter", "ritual_moment_id": ritual_id, "gag_moment_id": gag_id},
        ephemeral=False,
    )
    q.put_nowait(segment)
    state = StationState()
    state.moment_store = store
    state.queued_segments = [{"id": "1"}]

    count = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_STOP)

    assert count == 1
    ritual_row, gag_row = store.rows
    assert ritual_row.status == "dropped"
    assert ritual_row.drop_reason == "operator_stop"
    assert gag_row.status == "dropped"
    assert gag_row.drop_reason == "operator_stop"


def test_purge_without_moment_store_is_a_noop():
    """Purging must not raise when no moment_store is attached (standalone/no-HA)."""
    q: asyncio.Queue = asyncio.Queue()
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/purge_no_store.mp3"),
        metadata={"title": "Banter", "ritual_moment_id": "some-id"},
        ephemeral=False,
    )
    q.put_nowait(segment)
    state = StationState()
    state.queued_segments = [{"id": "1"}]

    count = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_STOP)

    assert count == 1


def test_purge_clears_queue_even_when_unlink_raises_non_oserror():
    # The unlink guard is broad (except Exception), so even a NON-OSError — e.g. a
    # malformed segment whose path raises AttributeError on .unlink — must not abort
    # the purge mid-loop and strand the UI shadow behind a half-drained queue (#397).
    q: asyncio.Queue = asyncio.Queue()
    good = Segment(type=SegmentType.MUSIC, path=Path("/tmp/purge_ok2.mp3"), metadata={"title": "A"}, ephemeral=True)
    bad_path = MagicMock(spec=Path)
    bad_path.unlink.side_effect = AttributeError("segment path is not a real Path")
    bad = Segment(type=SegmentType.MUSIC, path=bad_path, metadata={"title": "B"}, ephemeral=True)
    q.put_nowait(good)
    q.put_nowait(bad)

    state = StationState()
    state.queued_segments = [{"id": "1"}, {"id": "2"}]

    count = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_PURGE)

    assert count == 2
    assert q.empty()
    assert state.queued_segments == []
    assert state.discarded_segments_total == 2


def test_generation_waste_snapshot_clamps_cost_to_session_spend():
    # Operator-honesty bound (#5, #397): the waste figure can never exceed total
    # session spend, even if a counter edge case (a burst of already-counted purges
    # against a lagging produced counter) pushes the raw count ratio above 1.0.
    state = StationState()
    state.segments_produced = 1
    state.api_input_tokens = 1_000_000
    state.api_output_tokens = 0
    state.api_tokens_by_model = {}
    segment = Segment(type=SegmentType.BANTER, path=Path("/tmp/b.mp3"), duration_sec=10.0)
    for i in range(5):  # 5 already-counted discards vs 1 produced -> raw ratio 5.0
        state.record_discard(segment, reason="operator_purge", timestamp=float(i), already_counted_in_produced=True)

    gw = _generation_waste_snapshot(state)

    assert gw["total_segments"] == 5
    session_cost, unpriced = _estimate_api_cost(state)
    # Raw proration would exceed the registry-priced session cost; the clamp
    # keeps waste at exactly that cost. No per-model data is not an unpriced model.
    assert unpriced is False
    assert gw["estimated_waste_cost_usd"] == session_cost


def test_generation_waste_snapshot_prorates_cost():
    state = StationState()
    state.segments_produced = 3
    state.api_input_tokens = 1_000_000
    state.api_output_tokens = 0
    state.api_tokens_by_model = {}
    segment = Segment(type=SegmentType.BANTER, path=Path("/tmp/b.mp3"), duration_sec=10.0)
    state.record_discard(segment, reason="source_switch", timestamp=1.0, already_counted_in_produced=True)
    state.record_discard(segment, reason="source_switch", timestamp=2.0, already_counted_in_produced=True)

    gw = _generation_waste_snapshot(state)

    assert gw["total_segments"] == 2
    assert gw["unproduced_segments"] == 0
    session_cost, unpriced = _estimate_api_cost(state)
    assert unpriced is False
    assert gw["estimated_waste_cost_usd"] == round(session_cost * 2 / 3, 4)
    assert "discarded" in gw["cost_basis"]
    assert "produced" in gw["cost_basis"]


def test_generation_waste_snapshot_adds_prequeue_discards_to_cost_denominator():
    state = StationState()
    state.segments_produced = 3
    state.api_input_tokens = 1_000_000
    state.api_output_tokens = 0
    state.api_tokens_by_model = {}
    segment = Segment(type=SegmentType.BANTER, path=Path("/tmp/b.mp3"), duration_sec=10.0)
    state.record_discard(segment, reason="stale_source", timestamp=1.0)
    state.record_discard(segment, reason="stale_source", timestamp=2.0)

    gw = _generation_waste_snapshot(state)

    assert gw["total_segments"] == 2
    assert gw["unproduced_segments"] == 2
    session_cost, unpriced = _estimate_api_cost(state)
    assert unpriced is False
    assert gw["estimated_waste_cost_usd"] == round(session_cost * 2 / 5, 4)


def test_generation_waste_snapshot_ignores_events_outside_window(tmp_path):
    state = StationState()
    now = 10_000.0
    segment = Segment(type=SegmentType.MUSIC, path=tmp_path / "m.mp3", duration_sec=60.0)
    state.record_discard(segment, reason="stale_source", timestamp=now - GENERATION_WASTE_WINDOW_SECONDS - 10)
    state.record_discard(segment, reason="operator_panic", timestamp=now - 5)

    with patch("mammamiradio.web.streamer.time.time", return_value=now):
        gw = _generation_waste_snapshot(state)

    assert gw["total_segments"] == 2
    assert gw["recent_segments"] == 1
    assert gw["recent_top_reason"] == "operator_panic"


def test_runtime_status_snapshot_includes_generation_waste():
    app = _make_app()
    state = app.state.station_state
    state.record_discard(
        Segment(type=SegmentType.BANTER, path=Path("/tmp/b.mp3"), duration_sec=5.0),
        reason="operator_stop",
        timestamp=1.0,
    )

    req = _fake_request(app)
    rs = _runtime_status_snapshot(req)

    assert "generation_waste" in rs
    assert rs["generation_waste"]["total_segments"] == 1
