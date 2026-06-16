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
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mammamiradio.core.config import load_config
from mammamiradio.core.models import PlaylistSource, Segment, SegmentType, StationState, Track
from mammamiradio.web.streamer import (
    _FALLBACK_REASON_LABELS,
    BRIDGE_HEALTH_QUEUE_EMPTY_THRESHOLD_SECONDS,
    BRIDGE_HEALTH_THRESHOLD,
    BRIDGE_HEALTH_WINDOW_SECONDS,
    LiveStreamHub,
    _apply_loaded_source,
    _bridge_health_snapshot,
    _runtime_health_snapshot,
    _runtime_status_snapshot,
    _sync_runtime_state,
    router,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(title: str = "Track A") -> dict:
    return {"type": "music", "label": title, "metadata": {"title": title}}


def _queue_segment(title: str = "Track A") -> Segment:
    """A minimal Segment whose path.unlink() is a no-op (non-existent file)."""
    return Segment(
        type=SegmentType.MUSIC,
        path=Path(f"/tmp/test_seg_{title.replace(' ', '_')}.mp3"),
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
    """With empty shadow and no predictions, mode falls to 'building'."""
    app = _make_app(shadow=[], queue_items=0)

    # preview_upcoming always predicts non-music segments even with an empty
    # playlist, so we must mock it to exercise the truly-empty path.
    with patch("mammamiradio.web.streamer.preview_upcoming", return_value=[]):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/public-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["upcoming_mode"] == "building"
    assert data["upcoming"] == []


@pytest.mark.asyncio
async def test_public_status_predicted_source_when_shadow_empty_but_playlist_present():
    """Empty shadow but playlist present → predicted_from_playlist items."""
    app = _make_app(shadow=[], queue_items=0)
    # playlist already has one track from _make_app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/public-status")

    assert resp.status_code == 200
    data = resp.json()
    predicted = [i for i in data["upcoming"] if i["source"] == "predicted_from_playlist"]
    assert len(predicted) > 0


@pytest.mark.asyncio
async def test_public_status_predicted_upcoming_requests_8_with_source_fields():
    """Prediction previews request 8 items and annotate their source."""
    app = _make_app(shadow=[], queue_items=0)

    def _preview(*_args, count: int) -> list[dict]:
        return [{"type": "music", "label": f"Predicted {i}"} for i in range(count)]

    with patch("mammamiradio.web.streamer.preview_upcoming", side_effect=_preview) as preview:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/public-status")

    assert resp.status_code == 200
    preview.assert_called_once()
    assert preview.call_args.kwargs["count"] == 8
    upcoming = resp.json()["upcoming"]
    assert len(upcoming) == 8
    assert [item["label"] for item in upcoming] == [f"Predicted {i}" for i in range(8)]
    assert all(item["source"] == "predicted_from_playlist" for item in upcoming)


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


def test_apply_loaded_source_clears_shadow_and_real_queue():
    """_apply_loaded_source atomically clears both the shadow list and the real queue."""
    from mammamiradio.core.models import PlaylistSource

    app = _make_app(shadow=[_seg("Old A"), _seg("Old B")], queue_items=2)
    # Wire skip_event (already set by _make_app) and simulate a now_streaming value
    # so that the skip branch is exercised.
    app.state.station_state.now_streaming = {"type": "music", "label": "Old A"}

    resolved_source = PlaylistSource(kind="local", source_id="local", label="Local", track_count=1)
    new_tracks = [Track(title="New Song", artist="New Artist", duration_ms=180_000, spotify_id="n1")]

    req = _fake_request(app)
    _apply_loaded_source(req, new_tracks, resolved_source)

    assert app.state.station_state.queued_segments == []
    assert app.state.queue.empty()


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


def test_runtime_status_snapshot_includes_producer_headroom():
    app = _make_app()
    app.state.config.pacing.lookahead_segments = 4
    app.state.queue = asyncio.Queue(maxsize=6)
    app.state.station_state.queued_segments = [
        {"type": "music", "duration_sec": 180.0},
        {"type": "music", "duration_sec": 180.0},
    ]

    req = _fake_request(app)
    runtime_health = _runtime_health_snapshot(req)
    rs = _runtime_status_snapshot(req, runtime_health=runtime_health)

    headroom = rs["producer_headroom"]
    assert headroom["queue_capacity"] == 6
    assert headroom["lookahead_target"] == 4
    assert headroom["buffered_audio_sec"] == 360.0
    assert headroom["headroom_ok"] is False
