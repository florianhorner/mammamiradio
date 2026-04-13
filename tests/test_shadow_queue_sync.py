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
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mammamiradio.config import load_config
from mammamiradio.models import Segment, SegmentType, StationState, Track
from mammamiradio.streamer import (
    LiveStreamHub,
    _apply_loaded_source,
    _runtime_health_snapshot,
    _sync_runtime_state,
    router,
)

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


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

    def test_shadow_corrections_reflected_in_snapshot(self):
        app = _make_app()
        app.state.station_state.shadow_queue_corrections = 7
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["shadow_queue_corrections"] == 7

    def test_audio_source_falls_back_to_playlist_source_when_prewarm(self):
        """audio_source 'prewarm' is replaced by playlist_source.kind in the snapshot."""
        from mammamiradio.models import PlaylistSource

        app = _make_app()
        app.state.station_state.now_streaming = {"metadata": {"audio_source": "prewarm"}}
        app.state.station_state.playlist_source = PlaylistSource(kind="demo")
        req = _fake_request(app)

        snap = _runtime_health_snapshot(req)

        assert snap["audio_source"] == "demo"

    def test_audio_source_falls_back_to_playlist_source_when_empty(self):
        """Empty audio_source is replaced by playlist_source.kind in the snapshot."""
        from mammamiradio.models import PlaylistSource

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
async def test_public_status_upcoming_mode_building_when_shadow_empty():
    """With empty shadow and no predictions, mode falls to 'building'."""
    app = _make_app(shadow=[], queue_items=0)

    # preview_upcoming always predicts non-music segments even with an empty
    # playlist, so we must mock it to exercise the truly-empty path.
    with patch("mammamiradio.streamer.preview_upcoming", return_value=[]):
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
    from mammamiradio.models import PlaylistSource

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
