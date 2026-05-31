"""Tests for LiveStreamHub, HTTP routes, and admin auth in streamer.py."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web.listener_requests import router as listener_requests_router
from mammamiradio.web.streamer import (
    _ASSET_VERSION,
    QUEUE_FALLBACK_WAIT_SECONDS,
    SILENCE_FAILURE_SECONDS,
    LiveStreamHub,
    _persist_completed_music,
    _record_provider_verdict,
    _run_provider_verdict,
    _select_norm_cache_rescue,
    router,
    run_playback_loop,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app(*, admin_password: str = "", admin_token: str = "") -> FastAPI:
    """Build a minimal FastAPI app with the streamer router and populated state."""
    app = FastAPI()
    app.include_router(router)
    app.include_router(listener_requests_router)

    config = load_config(TOML_PATH)
    # Override auth settings for test isolation
    config.admin_password = admin_password
    config.admin_token = admin_token

    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000, spotify_id="t1")],
    )

    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


# ---------------------------------------------------------------------------
# LiveStreamHub -- pure async unit tests
# ---------------------------------------------------------------------------


def test_ha_green_queue_fallback_budget_is_shorter_than_health_failure():
    assert QUEUE_FALLBACK_WAIT_SECONDS <= 5.0
    assert SILENCE_FAILURE_SECONDS >= 30.0
    assert QUEUE_FALLBACK_WAIT_SECONDS < SILENCE_FAILURE_SECONDS


def test_select_norm_cache_rescue_avoids_current_song_when_alternatives_exist(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "50 Cent – In Da Club",
        "metadata": {"title": "50 Cent – In Da Club", "artist": "50 Cent"},
    }

    current = tmp_path / "norm_youtube_dQw4w9WgXcQ_192k.mp3"
    current.write_bytes(b"x")
    (tmp_path / "norm_youtube_dQw4w9WgXcQ_192k.mp3.json").write_text('{"title": "In Da Club", "artist": "50 Cent"}')
    alternative = tmp_path / "norm_raffaella_carra_a_far_l_amore.mp3"
    alternative.write_bytes(b"x")
    (tmp_path / "norm_raffaella_carra_a_far_l_amore.mp3.json").write_text(
        '{"title": "A far l amore comincia tu", "artist": "Raffaella Carra"}'
    )

    with patch("mammamiradio.web.streamer._random.choice", side_effect=lambda items: items[0]) as choice:
        rescue = _select_norm_cache_rescue(tmp_path, state)

    assert rescue == alternative
    choice.assert_called_once_with([alternative])


@pytest.mark.asyncio
async def test_subscribe_returns_id_and_queue():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    assert isinstance(lid, int)
    assert isinstance(q, asyncio.Queue)
    assert hub.has_listener(lid)


@pytest.mark.asyncio
async def test_subscribe_increments_id():
    hub = LiveStreamHub()
    id1, _ = hub.subscribe()
    id2, _ = hub.subscribe()
    assert id2 == id1 + 1


@pytest.mark.asyncio
async def test_unsubscribe_removes_listener():
    hub = LiveStreamHub()
    lid, _ = hub.subscribe()
    hub.unsubscribe(lid)
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_has_listener_false_for_unknown():
    hub = LiveStreamHub()
    assert not hub.has_listener(999)


@pytest.mark.asyncio
async def test_broadcast_pushes_to_all():
    hub = LiveStreamHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    chunk = b"audio-data"
    await hub.broadcast(chunk)
    assert q1.get_nowait() == chunk
    assert q2.get_nowait() == chunk


@pytest.mark.asyncio
async def test_broadcast_drops_slow_listeners():
    hub = LiveStreamHub(listener_queue_size=1)
    lid, q = hub.subscribe()
    # Fill the queue so the listener is slow
    q.put_nowait(b"old")
    await hub.broadcast(b"new")
    # Slow listener should have been dropped
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_close_sends_none():
    hub = LiveStreamHub()
    _, q1 = hub.subscribe()
    _, q2 = hub.subscribe()
    hub.close()
    assert q1.get_nowait() is None
    assert q2.get_nowait() is None


@pytest.mark.asyncio
async def test_close_clears_listeners():
    hub = LiveStreamHub()
    lid, _ = hub.subscribe()
    hub.close()
    assert not hub.has_listener(lid)


# ---------------------------------------------------------------------------
# Route tests -- using httpx.AsyncClient with ASGITransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_listen_returns_html():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/listen")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_persist_completed_music_records_finished_track():
    app = _make_test_app()
    state = app.state.station_state
    persona_store = MagicMock()
    persona_store._session_id = "session-1"
    persona_store.record_motif = AsyncMock()
    persona_store.record_play = AsyncMock()
    state.persona_store = persona_store

    metadata = {
        "title": "Artist 9 – Song 9",
        "title_only": "Song 9",
        "artist": "Artist 9",
        "youtube_id": "yt_9",
        "spotify_id": "sp_9",
    }

    with patch("mammamiradio.playlist.song_cues.detect_anthem", new=AsyncMock()) as detect_anthem:
        await _persist_completed_music(state, app.state.config, metadata, listen_sec=123.4)

    persona_store.record_motif.assert_awaited_once_with("Artist 9", "Song 9")
    persona_store.record_play.assert_awaited_once_with(
        "yt_9",
        "session-1",
        skipped=False,
        listen_duration_s=123.4,
    )
    detect_anthem.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_playback_loop_persists_music_only_after_segment_finishes(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()

    audio_path = tmp_path / "segment.mp3"
    audio_path.write_bytes(b"x" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Done", "title_only": "Done", "artist": "Artist", "youtube_id": "yt_done"},
        )
    )

    with patch("mammamiradio.web.streamer._persist_completed_music", new=AsyncMock()) as persist_completed:
        task = asyncio.create_task(run_playback_loop(app))
        try:
            for _ in range(20):
                if persist_completed.await_count:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    persist_completed.assert_awaited_once()
    assert not audio_path.exists()


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_fallback_keeps_queue_bookkeeping_balanced(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]

    fallback_path = tmp_path / "fallback.mp3"
    fallback_path.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=fallback_path),
        patch.object(app.state.queue, "task_done") as mock_task_done,
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while not app.state.station_state.now_streaming:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not stream fallback segment")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.now_streaming["metadata"].get("fallback") is True
    assert app.state.station_state.queued_segments == [{"type": "music", "label": "Queued Song"}]
    mock_task_done.assert_not_called()


@pytest.mark.asyncio
async def test_run_playback_loop_resets_queue_empty_since_after_real_segment(tmp_path):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.start_time = time.time() - 31
    app.state.station_state.queue_empty_since = time.monotonic() - 40

    audio_path = tmp_path / "real-segment.mp3"
    audio_path.write_bytes(b"x" * 4096)
    app.state.queue.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=audio_path,
            metadata={"title": "Real Song", "title_only": "Real Song", "artist": "Artist"},
        )
    )

    task = asyncio.create_task(run_playback_loop(app))
    try:
        deadline = time.monotonic() + 3.0
        while not app.state.station_state.now_streaming:
            if time.monotonic() > deadline:
                raise AssertionError("playback loop did not stream queued segment")
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_fallback_resets_queue_empty_since_and_no_error(tmp_path, caplog):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.stream_hub.subscribe()
    app.state.station_state.queue_empty_since = time.monotonic() - 35
    caplog.set_level(logging.INFO)

    fallback_path = tmp_path / "fallback-canned.mp3"
    fallback_path.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=fallback_path),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while app.state.station_state.now_streaming.get("metadata", {}).get("fallback") is not True:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not stream canned fallback")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None
    assert not any(record.levelname == "ERROR" for record in caplog.records)


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_uses_norm_cache_after_short_fallback_wait(tmp_path, caplog):
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_rescue.mp3"
    rescue_path.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    wait_for = AsyncMock(side_effect=_forced_timeout)
    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=wait_for),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=[100.0, 100.0 + QUEUE_FALLBACK_WAIT_SECONDS + 0.1, 105.2, 105.3, 105.4, 105.5],
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None
    wait_for.assert_called()
    assert wait_for.call_args.kwargs["timeout"] == QUEUE_FALLBACK_WAIT_SECONDS
    assert any("rescuing with norm cache" in record.message for record in caplog.records)
    # Item 20: title must NEVER be the raw filename ("Recovered: norm_rescue.mp3").
    # Without a sidecar, humanize_norm_filename turns "norm_rescue.mp3" → "Rescue".
    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Rescue", (
        f"rescue path should humanize filename when no sidecar present; got {now_meta.get('title')!r}"
    )
    assert "Recovered:" not in (now_meta.get("title") or ""), (
        "'Recovered:' prefix must not leak to listener-facing title"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_rescue_reads_sidecar_metadata(tmp_path, caplog):
    """When a norm-cache file has a `.json` sidecar, the rescue path should use
    its title+artist instead of the humanized filename fallback (Item 20)."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_rescue.mp3"
    rescue_path.write_bytes(b"x" * 4096)
    # Write the sidecar the way producer.save_track_metadata would.
    import json

    (tmp_path / "norm_rescue.mp3.json").write_text(json.dumps({"title": "Esibizionista", "artist": "Annalisa"}))

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=[100.0, 130.5, 130.6, 130.7, 130.8, 130.9],
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Annalisa – Esibizionista", (
        f"sidecar metadata should yield 'Annalisa – Esibizionista'; got {now_meta.get('title')!r}"
    )
    assert now_meta.get("artist") == "Annalisa"


@pytest.mark.asyncio
async def test_run_playback_loop_rescue_handles_malformed_sidecar(tmp_path, caplog):
    """Malformed sidecar JSON must not crash; rescue falls back to humanize (Item 20)."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    rescue_path = tmp_path / "norm_busted.mp3"
    rescue_path.write_bytes(b"x" * 4096)
    (tmp_path / "norm_busted.mp3.json").write_text("{not valid json")

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=[100.0, 130.5, 130.6, 130.7, 130.8, 130.9],
        ),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_norm_cache"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from norm cache")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Busted", (
        f"malformed sidecar should fall back to humanize; got {now_meta.get('title')!r}"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_uses_demo_assets_after_30s(tmp_path, caplog):
    """Scenario 2 (empty fallback): no canned clips, no norm cache — demo assets must rescue."""
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.WARNING)

    demo_dir = tmp_path / "demo" / "music"
    demo_dir.mkdir(parents=True)
    rescue_mp3 = demo_dir / "Pino Daniele - Napule E.mp3"
    rescue_mp3.write_bytes(b"x" * 4096)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=[100.0, 130.5, 130.6, 130.7, 130.8, 130.9],
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", tmp_path),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while (
                app.state.station_state.now_streaming.get("metadata", {}).get("audio_source") != "fallback_demo_asset"
            ):
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not rescue from demo assets")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is None
    assert any("rescuing with demo asset" in record.message for record in caplog.records)

    now_meta = app.state.station_state.now_streaming.get("metadata", {})
    assert now_meta.get("title") == "Napule E", (
        f"demo-asset rescue must parse 'Artist - Title.mp3' stems; got title={now_meta.get('title')!r}"
    )
    assert now_meta.get("artist") == "Pino Daniele", (
        f"demo-asset rescue must parse 'Artist - Title.mp3' stems; got artist={now_meta.get('artist')!r}"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_fully_empty_container_forces_banter(tmp_path, caplog):
    """Scenario 2 (fully empty): no canned, no norm cache, no demo assets.

    Guards the only remaining escape hatch — forced banter — when the operator
    has stripped every bundled audio rescue from the container. Silence must
    never be terminal.
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 3200
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.ERROR)

    empty_pkg = tmp_path / "empty_pkg"
    empty_pkg.mkdir()

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.web.streamer._runtime_monotonic",
            side_effect=[200.0, 260.5, 260.6, 260.7, 260.8, 260.9],
        ),
        patch("mammamiradio.web.streamer._ASSETS_DIR", empty_pkg),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while app.state.station_state.force_next is None:
                if time.monotonic() > deadline:
                    raise AssertionError("empty-container run did not reach forced banter fallback")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.force_next == SegmentType.BANTER
    assert app.state.station_state.queue_empty_since is not None, (
        "queue_empty_since must stay set so /readyz keeps reporting 503 starting until real audio resumes"
    )
    assert not any("rescuing with demo asset" in record.message for record in caplog.records), (
        "demo-asset rescue fired despite empty _ASSETS_DIR"
    )


@pytest.mark.asyncio
async def test_run_playback_loop_timeout_force_resumes_after_60s(tmp_path, caplog):
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    app.state.stream_hub.subscribe()
    caplog.set_level(logging.ERROR)

    async def _forced_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        await asyncio.sleep(0)
        raise TimeoutError

    with (
        patch("mammamiradio.web.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.scheduling.producer._pick_canned_clip", return_value=None),
        patch("mammamiradio.web.streamer._runtime_monotonic", side_effect=[200.0, 260.5, 260.6, 260.7]),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            deadline = time.monotonic() + 3.0
            while app.state.station_state.force_next is None:
                if time.monotonic() > deadline:
                    raise AssertionError("playback loop did not force-resume after prolonged silence")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert app.state.station_state.queue_empty_since is not None
    assert app.state.station_state.force_next == SegmentType.BANTER
    assert app.state.skip_event.is_set() is False
    assert any("requesting forced banter from producer" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_readyz_returns_503_when_silent_with_active_listeners():
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 1
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["silence_with_listeners"] is True
    assert body["queue_empty_elapsed_s"] >= 30


@pytest.mark.asyncio
async def test_readyz_does_not_fail_silence_gate_without_listeners():
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 0
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["silence_with_listeners"] is False
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_readyz_returns_503_when_session_stopped():
    """readyz must return 503 when session_stopped=True — station is not ready for listeners."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31  # startup_complete=True
    app.state.queue.put_nowait(object())  # queue_depth > 0
    app.state.station_state.session_stopped = True

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False


@pytest.mark.asyncio
async def test_readyz_returns_200_when_session_resumed():
    """readyz must return 200 once session_stopped is cleared and queue has audio."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.queue.put_nowait(object())
    app.state.station_state.session_stopped = False

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True


@pytest.mark.asyncio
async def test_healthz_returns_503_when_silent_with_active_listeners():
    """HA Supervisor polls /healthz — it must 503 when silently failing so auto-restart fires."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 1
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "failing"
    assert body["silence_with_listeners"] is True
    assert body["queue_empty_elapsed_s"] >= 30


@pytest.mark.asyncio
async def test_healthz_returns_200_when_quiet_but_no_listeners():
    """No listeners + queue empty is not a failure — nobody is being stranded."""
    app = _make_test_app()
    app.state.start_time = time.time() - 31
    app.state.station_state.listeners_active = 0
    app.state.station_state.queue_empty_since = time.monotonic() - 35

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["silence_with_listeners"] is False


@pytest.mark.asyncio
async def test_audio_generator_clears_persisted_session_stopped_on_connect(tmp_path):
    """Scenario 3 (post-restart): a new listener connecting must clear session_stopped state.

    After a restart, session_stopped.flag may persist on disk and session_stopped=True
    on the StationState. If the clearing logic regresses, every new listener hits a
    stopped session and gets no audio. This test guards that invariant.
    """
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    app.state.config.cache_dir = tmp_path
    flag = tmp_path / "session_stopped.flag"
    flag.touch()
    app.state.station_state.session_stopped = True

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    gen = _audio_generator(mock_request)
    async for _ in gen:  # pragma: no cover - generator exits before yielding
        break

    assert app.state.station_state.session_stopped is False
    assert not flag.exists()


@pytest.mark.asyncio
async def test_skip_route_persists_music_skips_with_youtube_id():
    app = _make_test_app()
    persona_store = MagicMock()
    persona_store._session_id = "session-2"
    persona_store.record_play = AsyncMock()
    app.state.station_state.persona_store = persona_store
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Skipped Song",
        "started": time.time() - 8,
        "metadata": {"youtube_id": "yt_skip"},
    }

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.playlist.song_cues.detect_skip_bit", new=AsyncMock()) as detect_skip_bit:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/skip")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    persona_store.record_play.assert_awaited_once()
    detect_skip_bit.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_bit_sets_pending_directive():
    """When detect_skip_bit returns True, ha_pending_directive is set for reactive banter."""
    app = _make_test_app()
    persona_store = MagicMock()
    persona_store._session_id = "session-3"
    persona_store.record_play = AsyncMock()
    app.state.station_state.persona_store = persona_store
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Hated Song",
        "started": time.time() - 5,
        "metadata": {"youtube_id": "yt_hated", "title_only": "Brutta Canzone"},
    }

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.playlist.song_cues.detect_skip_bit", new=AsyncMock(return_value=True)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/skip")

    assert resp.status_code == 200
    directive = app.state.station_state.ha_pending_directive
    assert "Brutta Canzone" in directive
    assert "saltato" in directive or "skippa" in directive


@pytest.mark.asyncio
async def test_get_root_serves_listener_page():
    """Root serves the public listener page (no auth required)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    # Brand-engine PR-C: listener is now Jinja-templated. Assert on stable
    # structural elements (CTA, brand identity) — the tagline is now per-brand
    # via brand.tagline, so no longer a fixed string.
    assert "Mamma Mi Radio" in resp.text  # default brand from radio.toml
    # CTA copy is Super-Italian-Mode-aware. Default ON renders Italian.
    assert "Ascolta Ora" in resp.text
    assert "Manda al DJ" in resp.text  # dediche eyebrow stays Italian (decorative)
    assert 'data-cap="ha"' in resp.text  # capability-conditional rendering hooks present
    # Tail-anchored: tolerate non-strict-semver pyproject versions (rc/post/dev).
    assert re.search(r"-[a-f0-9]{8}$", _ASSET_VERSION)
    assert f"/static/listener.css?v={_ASSET_VERSION}" in resp.text


@pytest.mark.asyncio
async def test_get_root_renders_italian_when_super_italian_on():
    """Super Italian Mode ON: CTA + form button render in Italian."""
    app = _make_test_app()
    app.state.config.super_italian_mode = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Ascolta Ora" in resp.text  # CTA in Italian
    assert "Spedisci con un bacio" in resp.text  # form submit in Italian
    assert "Listen Now" not in resp.text  # English CTA must be absent


@pytest.mark.asyncio
async def test_public_status_returns_json():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert "station" in body
    assert "now_streaming" in body
    assert "upcoming" in body
    assert "upcoming_mode" in body
    assert "stream_log" in body
    # Item 19: listener.html relies on session_stopped being in the public
    # payload so it can freeze the launch-waveform when the operator pauses.
    assert "session_stopped" in body
    assert body["session_stopped"] is False  # default for fresh test app


@pytest.mark.asyncio
async def test_public_status_reflects_session_stopped_flag():
    app = _make_test_app()
    app.state.station_state.session_stopped = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    assert resp.json()["session_stopped"] is True


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_shows_predictions_when_queue_empty():
    app = _make_test_app()
    # Queue is empty -- predictions from playlist are shown instead
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["upcoming"]) > 0
    assert all(item["source"] == "predicted_from_playlist" for item in body["upcoming"])
    assert body["upcoming_mode"] == "queued"


@pytest.mark.asyncio
async def test_public_status_upcoming_mode_queued_with_shadow_queue():
    app = _make_test_app()
    app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=Path("/tmp/fake.mp3"), metadata={}))
    app.state.station_state.queued_segments = [{"type": "music", "label": "Queued Song"}]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["upcoming"] == [{"type": "music", "label": "Queued Song", "source": "rendered_queue"}]
    assert body["upcoming_mode"] == "queued"


@pytest.mark.asyncio
async def test_setup_status_returns_onboarding_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/setup/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "essentials" in body
    assert "preflight_checks" in body
    assert "launch" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_status_includes_station_mode():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "station_mode" in body
    assert "id" in body["station_mode"]
    assert "provider_health" in body


@pytest.mark.asyncio
async def test_setup_recheck_returns_onboarding_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/recheck")
    assert resp.status_code == 200
    body = resp.json()
    assert "detected_mode" in body
    assert "station_mode" in body
    assert "signature" in body


@pytest.mark.asyncio
async def test_setup_provider_check_returns_secret_safe_probe_payload():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "anthropic-secret"
    app.state.config.openai_api_key = "openai-secret"
    probe_payload = {
        "ok": True,
        "providers": {
            "anthropic": {
                "provider": "anthropic",
                "configured": True,
                "ok": False,
                "status_code": 401,
                "error_type": "authentication_error",
                "detail": "authentication_error invalid x-api-key",
            },
            "openai_chat": {
                "provider": "openai_chat",
                "configured": True,
                "ok": True,
                "status_code": 200,
                "error_type": "",
                "detail": "",
            },
            "openai_tts": {
                "provider": "openai_tts",
                "configured": True,
                "ok": True,
                "status_code": 200,
                "error_type": "",
                "detail": "",
            },
        },
    }
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=probe_payload)) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/setup/provider-check")

    assert resp.status_code == 200
    body = resp.json()
    assert body == probe_payload
    assert "anthropic-secret" not in resp.text
    assert "openai-secret" not in resp.text
    probe.assert_awaited_once_with(app.state.config)


@pytest.mark.asyncio
async def test_setup_provider_check_shares_in_flight_probe():
    app = _make_test_app()
    probe_payload = {"ok": True, "providers": {}}
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_probe(config):
        assert config is app.state.config
        started.set()
        await release.wait()
        return probe_payload

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(side_effect=slow_probe)) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = asyncio.create_task(client.post("/api/setup/provider-check"))
            await started.wait()
            second = asyncio.create_task(client.post("/api/setup/provider-check"))
            await asyncio.sleep(0)
            release.set()
            first_resp, second_resp = await asyncio.gather(first, second)

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert first_resp.json() == probe_payload
    assert second_resp.json() == probe_payload
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_setup_provider_check_returns_cached_result_within_debounce_window():
    """Second call within the 2 s debounce window returns cached result without re-probing."""
    app = _make_test_app()
    probe_payload = {"ok": True, "providers": {}}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=probe_payload)) as probe:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post("/api/setup/provider-check")
            second = await client.post("/api/setup/provider-check")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == probe_payload
    assert second.json() == probe_payload
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_setup_provider_check_clears_task_on_exception():
    """If check_provider_keys raises, the in-flight task reference is cleared so next call retries."""
    app = _make_test_app()
    # raise_app_exceptions=False: converts server errors to 500 responses rather
    # than propagating them, so we can inspect app state after the failure.
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345), raise_app_exceptions=False)
    with patch(
        "mammamiradio.web.streamer.check_provider_keys",
        new=AsyncMock(side_effect=RuntimeError("probe failed")),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/setup/provider-check")

    assert resp.status_code == 500
    assert app.state._provider_check_task is None


@pytest.mark.asyncio
async def test_setup_provider_check_clears_task_on_cancel():
    """Cancelling the in-flight provider-check task clears the task reference."""
    app = _make_test_app()
    barrier = asyncio.Event()

    async def slow_probe(_config):
        barrier.set()
        await asyncio.sleep(10)
        return {"anthropic": True}

    with patch("mammamiradio.web.streamer.check_provider_keys", new=slow_probe):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            check_coro = client.post("/api/setup/provider-check")
            check_task = asyncio.create_task(check_coro)
            await barrier.wait()
            # Cancel the in-flight probe at the app-state level, then let the
            # HTTP task observe the cancellation.
            probe_task = app.state._provider_check_task
            assert probe_task is not None
            probe_task.cancel()
            try:
                await check_task
            except (asyncio.CancelledError, httpx.RemoteProtocolError):
                pass

    assert getattr(app.state, "_provider_check_task", None) is None


@pytest.mark.asyncio
async def test_addon_snippet_returns_snippet():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/setup/addon-snippet")
    assert resp.status_code == 200
    body = resp.json()
    assert "snippet" in body


@pytest.mark.asyncio
async def test_setup_save_keys_updates_live_config_without_disk_write():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))

    import os as _os

    _prev_anthropic = _os.environ.get("ANTHROPIC_API_KEY")
    _prev_openai = _os.environ.get("OPENAI_API_KEY")
    try:
        with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/api/setup/save-keys",
                    json={"ANTHROPIC_API_KEY": "ant-test\nEVIL=1", "OPENAI_API_KEY": "openai-test\rEVIL=1"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "ANTHROPIC_API_KEY" in body["saved"]
        assert "OPENAI_API_KEY" in body["saved"]
        assert app.state.config.anthropic_api_key == "ant-testEVIL=1"
        assert app.state.config.openai_api_key == "openai-testEVIL=1"
        save_dotenv.assert_called_once()
        assert save_dotenv.call_args.args[0] == {
            "ANTHROPIC_API_KEY": "ant-testEVIL=1",
            "OPENAI_API_KEY": "openai-testEVIL=1",
        }
    finally:
        # Restore env to avoid polluting subsequent tests
        if _prev_anthropic is None:
            _os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _os.environ["ANTHROPIC_API_KEY"] = _prev_anthropic
        if _prev_openai is None:
            _os.environ.pop("OPENAI_API_KEY", None)
        else:
            _os.environ["OPENAI_API_KEY"] = _prev_openai


@pytest.mark.asyncio
async def test_setup_save_keys_rejects_empty_payload():
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/setup/save-keys", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "No keys provided" in body["error"]


@pytest.mark.asyncio
async def test_admin_status_without_auth_public_ip_rejected():
    """Public IP client without credentials should be rejected."""
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_status_private_network_rejected_when_password_set():
    """When admin_password is configured, a LAN client must still authenticate —
    private-network trust no longer bypasses configured credentials."""
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_status_private_network_trusted_without_creds():
    """With no admin creds configured, a LAN client is still trusted (CSRF-guarded)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_status_with_basic_auth():
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert "queue_depth" in body
    assert "segments_produced" in body
    assert "runtime_health" in body


@pytest.mark.asyncio
async def test_admin_status_with_token():
    app = _make_test_app(admin_token="tok-abc-123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", headers={"X-Radio-Admin-Token": "tok-abc-123"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_status_bad_credentials():
    app = _make_test_app(admin_password="secret123")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status", auth=("admin", "wrong"))
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_skip_with_admin_auth():
    app = _make_test_app(admin_password="secret123")
    # Put something in now_streaming so skip has something to act on
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/skip", auth=("admin", "secret123"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


@pytest.mark.asyncio
async def test_stop_and_resume_toggle_session_state():
    app = _make_test_app()
    app.state.station_state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        stop = await client.post("/api/stop")
        assert stop.status_code == 200
        assert app.state.station_state.session_stopped is True
        assert app.state.station_state.now_streaming["type"] == "stopped"

        resume = await client.post("/api/resume")
        assert resume.status_code == 200
        assert app.state.station_state.session_stopped is False


@pytest.mark.asyncio
async def test_panic_cut_while_streaming():
    """Panic while a segment is playing: purges queue, fires skip_event, forces next=music, leaves stream live."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "Test", "started": time.time()}
    # Pre-populate shadow queue so we can verify it is cleared
    state.queued_segments.append({"type": "banter"})  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/panic")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "purged" in data
    # skip_event must have been set (skip fires for the current segment)
    assert app.state.skip_event.is_set()
    # force_next must be MUSIC
    assert state.force_next == SegmentType.MUSIC
    # session_stopped must NOT be set — stream stays live
    assert state.session_stopped is False
    # shadow queue must be cleared
    assert len(state.queued_segments) == 0


@pytest.mark.asyncio
async def test_panic_cut_when_idle():
    """Panic while nothing is playing: skip_event stays unset, force_next still set to music."""
    from mammamiradio.core.models import SegmentType

    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = None  # nothing streaming
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/panic")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # No segment to skip — skip_event should not be fired
    assert not app.state.skip_event.is_set()
    assert state.force_next == SegmentType.MUSIC
    assert state.session_stopped is False


@pytest.mark.asyncio
async def test_panic_does_not_set_session_stopped():
    """Panic must never set session_stopped — that would drop all active listeners."""
    app = _make_test_app()
    state = app.state.station_state
    state.now_streaming = {"type": "banter", "label": "AI banter", "started": time.time()}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/api/panic")
    assert state.session_stopped is False


@pytest.mark.asyncio
async def test_loopback_bypasses_auth_when_no_password():
    """Loopback client with no admin_password/token configured gets through."""
    app = _make_test_app()  # no password, no token
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_public_ip_no_auth_configured_rejected():
    """Public IP with no auth configured gets 403."""
    app = _make_test_app()  # no password, no token
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/status")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PWA static asset routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sw_js_returns_javascript():
    """GET /sw.js should return the service worker with correct content-type."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "CACHE_NAME" in resp.text


@pytest.mark.asyncio
async def test_sw_js_keeps_css_and_js_network_first():
    """Visual assets must not stay cache-first after a UI bug ships."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/sw.js")

    assert resp.status_code == 200
    text = resp.text
    assert "radio-itali-v6" in text
    assert "const isFreshAsset" in text
    assert "path.endsWith('.css')" in text
    assert "path.endsWith('.js')" in text
    assert "const isStableInstallAsset" in text

    stable_cache_block = text.split("const isStableInstallAsset", maxsplit=1)[1]
    assert "path.endsWith('.css')" not in stable_cache_block
    assert "path.endsWith('.js')" not in stable_cache_block


@pytest.mark.asyncio
async def test_static_manifest_returns_json():
    """GET /static/manifest.json should serve the PWA manifest."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/manifest.json")
    assert resp.status_code == 200
    assert "Radio" in resp.text


@pytest.mark.asyncio
async def test_static_nonexistent_returns_404():
    """GET /static/nonexistent.txt should return 404."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/nonexistent.txt")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_path_traversal_blocked():
    """Path traversal attempts in /static/ should return 404."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/static/../streamer.py")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_regia_route_removed():
    """GET /regia must return 404 — the obsolete prototype was removed; admin lives at /admin."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/regia")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /admin route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_panel_loopback_no_password_returns_html():
    """GET /admin on loopback with no credentials configured should return 200 HTML."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_admin_panel_public_ip_without_auth_rejected():
    """GET /admin from public IP without credentials should return 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_panel_with_basic_auth_returns_html():
    """GET /admin with valid basic auth should return 200 HTML."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# /live mobile host control room — same auth contract as /admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_panel_loopback_no_password_returns_html():
    """GET /live on loopback with no credentials configured should return 200 HTML."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/live")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_live_panel_public_ip_without_auth_rejected():
    """GET /live from public IP without credentials should return 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/live")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_live_panel_with_basic_auth_returns_html():
    """GET /live with valid basic auth should return 200 HTML."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/live", auth=("admin", "secret"))
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_admin_panel_csp_allows_inline_handlers():
    """GET /admin must return CSP with 'unsafe-inline' so onclick/oninput handlers work.

    admin.html has ~40 inline event handlers. A nonce-only CSP blocks them even when
    the <script> block loads — nonces cover <script> elements, not attribute handlers.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")

    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "script-src" in csp, f"Admin response must set script-src CSP: {csp!r}"
    assert "'unsafe-inline'" in csp, f"Admin CSP must include 'unsafe-inline' to allow inline event handlers: {csp!r}"


@pytest.mark.asyncio
async def test_admin_panel_data_fetches_use_ingress_base():
    """admin.html must derive `_base` from window.location.pathname and prefix every
    data fetch with it, so HA Ingress-served pages reach the addon's API.

    Regression guard: prior to this fix, admin.html issued bare `fetch('/status')` and
    `fetch('/api/...')` calls. Under HA Ingress those resolved against the HA host root
    (not the addon's ingress prefix), returned non-JSON, were swallowed by the catch
    handler, and the panel hung at "Waiting for signal…". The server-side rewriter
    intentionally does NOT rewrite JS string literals (see _inject_ingress_prefix
    docstring) — adopting the `_base` contract is the admin page's responsibility,
    matching listener.js.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/admin")

    assert resp.status_code == 200
    body = resp.text

    assert "const _base = (() =>" in body, (
        "admin.html must declare a `_base` constant derived from window.location.pathname "
        "so HA Ingress data fetches resolve to the addon, not the HA host root."
    )

    bare_offenders = re.findall(r"fetch\((['\"`])/(?:api/|status|public-)", body)
    assert not bare_offenders, (
        "admin.html must not issue bare path-absolute fetches like `fetch('/status')` or "
        f"`fetch('/api/...')`; every call must compose against `_base`. Found: {bare_offenders!r}"
    )

    assert "fetch(_base+p," in body or "fetch(_base + p," in body, (
        "The `api(m, p, b)` helper in admin.html must call `fetch(_base+p, ...)` so every "
        "method/state/save call routed through it honors the HA Ingress prefix."
    )
    assert "__MAMMAMIRADIO_SCRIPT_NONCE__" not in resp.text, "Stale nonce placeholder found in rendered HTML."


# ---------------------------------------------------------------------------
# /api/capabilities route tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capabilities_loopback_returns_flags():
    """GET /api/capabilities on loopback returns capability flags and tier."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    # Flags are nested under "capabilities"; top-level has tier, trial, etc.
    caps = body.get("capabilities", body)
    assert "llm" in caps
    assert "jamendo" in caps
    assert "charts_reload" in caps
    assert "tier" in body
    assert "trial" in body
    assert "canned_clips_streamed" in body["trial"]


@pytest.mark.asyncio
async def test_capabilities_exposes_jamendo_and_charts_reload_flags():
    app = _make_test_app()
    app.state.config.playlist.jamendo_client_id = "jamendo-client"
    app.state.config.allow_ytdlp = True
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    caps = resp.json()["capabilities"]
    assert caps["jamendo"] is True
    assert caps["charts_reload"] is True


@pytest.mark.asyncio
async def test_capabilities_public_ip_without_auth_rejected():
    """GET /api/capabilities from public IP without credentials returns 401."""
    app = _make_test_app(admin_password="secret")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_capabilities_openai_only_marks_ai_as_available():
    app = _make_test_app()
    app.state.config.openai_api_key = "openai-key"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    assert resp.json()["capabilities"]["llm"] is True
    assert resp.json()["next_step"]["key"] != "add_ai_key"


@pytest.mark.asyncio
async def test_capabilities_trial_exhausted_flag():
    """trial.exhausted is True when canned_clips_streamed >= limit."""
    from mammamiradio.scheduling.producer import SHAREWARE_CANNED_LIMIT

    app = _make_test_app()
    app.state.station_state.canned_clips_streamed = SHAREWARE_CANNED_LIMIT
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trial"]["exhausted"] is True
    assert body["trial"]["canned_clips_streamed"] == SHAREWARE_CANNED_LIMIT


@pytest.mark.asyncio
async def test_capabilities_exposes_anthropic_degraded_health():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "bad-key"
    app.state.config.openai_api_key = "openai-key"
    app.state.station_state.anthropic_disabled_until = time.time() + 90
    app.state.station_state.anthropic_last_error = "AuthenticationError: invalid x-api-key"
    app.state.station_state.anthropic_auth_failures = 2

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert body["capabilities"]["anthropic_degraded"] is True
    assert body["provider_health"]["anthropic"]["degraded"] is True
    assert body["provider_health"]["anthropic"]["retry_after_s"] > 0
    assert body["provider_health"]["anthropic"]["auth_failures"] == 2


# ---------------------------------------------------------------------------
# Auto-resume: listener connecting clears session_stopped (v2.10.2 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_generator_does_not_auto_resume_stopped_session(tmp_path):
    """_audio_generator must clear session_stopped when a listener connects.

    A stopped session is resumed automatically when a listener connects —
    the listener connecting is the clearest signal that someone wants music.
    This prevents silence after an HA watchdog restart following a deliberate stop.
    Only an explicit POST /api/stop re-enters the stopped state.
    """
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = True
    flag = tmp_path / "session_stopped.flag"
    flag.touch()
    app.state.config.cache_dir = tmp_path

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    async for _ in _audio_generator(mock_request):
        pass

    # session_stopped must be cleared — listener connecting auto-resumes
    assert state.session_stopped is False, (
        "session_stopped was not cleared by _audio_generator. "
        "A listener connecting must auto-resume so HA watchdog restarts don't serve silence."
    )
    # The flag file must be removed
    assert not flag.exists(), (
        "session_stopped.flag was not deleted by _audio_generator. "
        "The flag must be removed when auto-resuming so the stopped state doesn't persist."
    )


@pytest.mark.asyncio
async def test_audio_generator_removes_flag_on_auto_resume(tmp_path):
    """When a listener connects to a stopped session, _audio_generator removes the flag file.

    The auto-resume clears session_stopped and deletes session_stopped.flag so that
    an HA watchdog restart after the resume does not re-enter the stopped state.
    """
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    app.state.station_state.session_stopped = True
    flag = tmp_path / "session_stopped.flag"
    flag.touch()
    app.state.config.cache_dir = tmp_path

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    async for _ in _audio_generator(mock_request):
        pass

    assert not flag.exists(), (
        "session_stopped.flag must be removed by _audio_generator on auto-resume. "
        "If the flag survives, an HA restart after the listener disconnects re-enters stopped state."
    )


@pytest.mark.asyncio
async def test_audio_generator_active_session_is_unaffected(tmp_path):
    """When the session is not stopped, _audio_generator subscribes normally.

    Regression guard: the auto-resume removal must not break the normal
    (session_stopped=False) path — the generator should subscribe without error.
    """
    from mammamiradio.web.streamer import _audio_generator

    app = _make_test_app()
    state = app.state.station_state
    state.session_stopped = False

    mock_request = MagicMock()
    mock_request.app = app
    mock_request.is_disconnected = AsyncMock(return_value=True)

    # Generator should run and exit cleanly (listener immediately disconnects)
    async for _ in _audio_generator(mock_request):
        pass

    assert state.session_stopped is False


# ---------------------------------------------------------------------------
# POST /api/hot-reload tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hot_reload_authenticated_200():
    """POST /api/hot-reload with valid admin token returns 200 with expected fields."""
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/hot-reload",
            headers={"X-Radio-Admin-Token": "testtoken"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["reloaded_modules"] == [
        "mammamiradio.hosts.prompt_world",
        "mammamiradio.hosts.transitions",
        "mammamiradio.hosts.fallbacks",
        "mammamiradio.hosts.scriptwriter",
    ]
    assert body["stream_status"] == "unaffected"
    assert body["effective_on"] == "next_banter_generation"
    assert isinstance(body["duration_ms"], int)


@pytest.mark.asyncio
async def test_hot_reload_unauthenticated_rejected():
    """POST /api/hot-reload without auth credentials is rejected."""
    app = _make_test_app(admin_password="secret", admin_token="tok")
    transport = httpx.ASGITransport(app=app, client=("10.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/hot-reload")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_hot_reload_prompt_world_stage_failure_returns_500():
    """First reload stage (prompt_world) raises → 500 with stream_status=unaffected.

    Guards the failure contract for the leaves-first stage. With prompt_world reloaded
    first, a single raising reload exercises this stage.
    """
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.importlib.reload",
        side_effect=ImportError("syntax error in prompt_world.py"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/hot-reload",
                headers={"X-Radio-Admin-Token": "testtoken"},
            )
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["stream_status"] == "unaffected"
    assert body["error_code"] == "reload_failed"
    assert body["retryable"] is True
    assert "syntax error in prompt_world.py" in body["exception"]


@pytest.mark.asyncio
async def test_hot_reload_scriptwriter_stage_failure_returns_500():
    """Last reload stage (the scriptwriter facade) fails after the leaves succeed → 500.

    The data leaves (prompt_world, transitions, fallbacks) reload cleanly (first three
    side-effects return), then the scriptwriter facade raises (fourth side-effect).
    Without the sequenced side-effect this stage would go uncovered, since an earlier
    reload would short-circuit the failure.
    """
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch(
        "mammamiradio.web.streamer.importlib.reload",
        side_effect=[None, None, None, ImportError("syntax error in scriptwriter.py")],
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/hot-reload",
                headers={"X-Radio-Admin-Token": "testtoken"},
            )
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["stream_status"] == "unaffected"
    assert body["error_code"] == "reload_failed"
    assert body["retryable"] is True
    assert "syntax error in scriptwriter.py" in body["exception"]


@pytest.mark.asyncio
async def test_hot_reload_debounce_returns_429_on_rapid_calls():
    """A second hot-reload call within 5s returns 429 with retry_after_s."""
    app = _make_test_app(admin_token="testtoken")
    # Prime the debounce timestamp to now
    app.state._last_hot_reload_ts = time.monotonic()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/api/hot-reload",
            headers={"X-Radio-Admin-Token": "testtoken"},
        )
    assert resp.status_code == 429
    body = resp.json()
    assert body["ok"] is False
    assert body["error_code"] == "debounced"
    assert body["stream_status"] == "unaffected"
    assert body["retryable"] is True
    assert body["retry_after_s"] > 0


@pytest.mark.asyncio
async def test_hot_reload_reloads_prompt_world_before_scriptwriter():
    """Data submodules reload before the scriptwriter facade (leaves-first).

    The facade re-imports values via ``from .prompt_world / .transitions / .fallbacks
    import ...``. Reloading the facade alone would rebind those names to the stale
    submodules, so an operator's edit to any data leaf would silently not take effect.
    The reload set must list (and reload) every data submodule ahead of scriptwriter.
    """
    app = _make_test_app(admin_token="testtoken")
    reloaded: list[str] = []
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    # Record the ACTUAL importlib.reload call sequence. Asserting only the response
    # `reloaded_modules` list is too weak — it's a fixed literal and would pass even if
    # the implementation issued the reloads in the wrong order.
    with patch(
        "mammamiradio.web.streamer.importlib.reload",
        side_effect=lambda mod: reloaded.append(mod.__name__),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/api/hot-reload",
                headers={"X-Radio-Admin-Token": "testtoken"},
            )
    assert resp.status_code == 200
    assert reloaded == [
        "mammamiradio.hosts.prompt_world",
        "mammamiradio.hosts.transitions",
        "mammamiradio.hosts.fallbacks",
        "mammamiradio.hosts.scriptwriter",
    ], "data submodules must reload before scriptwriter (leaves-first)"


# ---------------------------------------------------------------------------
# Provider key-validation verdict (rejected/valid/unverified)
#
# A bogus key persisted at boot must read as "key not working" BEFORE any banter
# segment 401s. These cover the mapping helper, the non-blocking runner, and the
# startup/save/on-demand wiring that persists the verdict onto StationState.
# ---------------------------------------------------------------------------


def _probe_entry(provider: str, outcome: str | None) -> dict:
    """Build one check_provider_keys provider entry. outcome: 'ok'|'auth'|'quota'|None."""
    if outcome is None:
        return {
            "provider": provider,
            "configured": False,
            "ok": False,
            "status_code": None,
            "error_type": "not_configured",
            "detail": "",
        }
    if outcome == "ok":
        return {
            "provider": provider,
            "configured": True,
            "ok": True,
            "status_code": 200,
            "error_type": "",
            "detail": "",
        }
    mapping = {"auth": (401, "authentication_error"), "quota": (403, "insufficient_quota"), "rate": (429, "rate_limit")}
    status_code, error_type = mapping[outcome]
    return {
        "provider": provider,
        "configured": True,
        "ok": False,
        "status_code": status_code,
        "error_type": error_type,
        "detail": "",
    }


def _probe_payload(*, anthropic: str | None = None, openai_chat: str | None = None) -> dict:
    providers = {
        "anthropic": _probe_entry("anthropic", anthropic),
        "openai_chat": _probe_entry("openai_chat", openai_chat),
        "openai_tts": _probe_entry("openai_tts", openai_chat),
    }
    return {"ok": any(p["ok"] for p in providers.values()), "providers": providers}


def test_record_provider_verdict_maps_auth_to_rejected():
    state = StationState()
    _record_provider_verdict(state, _probe_payload(anthropic="auth"))
    assert state.anthropic_key_status == "rejected"
    assert state.anthropic_key_checked_at > 0


def test_record_provider_verdict_maps_ok_to_valid():
    state = StationState()
    _record_provider_verdict(state, _probe_payload(anthropic="ok"))
    assert state.anthropic_key_status == "valid"


def test_record_provider_verdict_leaves_inconclusive_unchanged():
    """Quota / rate-limit / network are NOT auth rejections — status must not flip to rejected."""
    state = StationState()
    state.anthropic_key_status = "valid"
    _record_provider_verdict(state, _probe_payload(anthropic="quota"))
    assert state.anthropic_key_status == "valid", "quota error must not be mislabeled rejected"
    _record_provider_verdict(state, _probe_payload(anthropic="rate"))
    assert state.anthropic_key_status == "valid"


def test_record_provider_verdict_openai_parity():
    state = StationState()
    _record_provider_verdict(state, _probe_payload(openai_chat="auth"))
    assert state.openai_key_status == "rejected"
    _record_provider_verdict(state, _probe_payload(openai_chat="ok"))
    assert state.openai_key_status == "valid"


@pytest.mark.asyncio
async def test_run_provider_verdict_no_keys_skips_probe():
    app = _make_test_app()
    app.state.config.anthropic_api_key = ""
    app.state.config.openai_api_key = ""
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock()) as probe:
        await _run_provider_verdict(app.state)
    probe.assert_not_awaited()
    assert app.state.station_state.anthropic_key_status == "unverified"


@pytest.mark.asyncio
async def test_run_provider_verdict_swallows_probe_exception():
    """A flaky network must never crash boot or a key-save — status stays unverified."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-x"
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(side_effect=RuntimeError("boom"))):
        await _run_provider_verdict(app.state)  # must not raise
    assert app.state.station_state.anthropic_key_status == "unverified"


@pytest.mark.asyncio
async def test_run_provider_verdict_success_writes_state():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bogus"
    with patch(
        "mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=_probe_payload(anthropic="auth"))
    ):
        await _run_provider_verdict(app.state)
    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_provider_check_route_persists_rejected_verdict():
    """POST /api/setup/provider-check records the verdict on state, not just the response."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "anthropic-secret"
    payload = _probe_payload(anthropic="auth")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=payload)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.post("/api/setup/provider-check")
    assert resp.status_code == 200
    assert resp.json() == payload  # response body unchanged (existing contract)
    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_save_keys_resets_status_and_revalidates():
    app = _make_test_app()
    app.state.station_state.anthropic_key_status = "rejected"  # stale prior verdict
    previous = os.environ.get("ANTHROPIC_API_KEY")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    try:
        with (
            patch("mammamiradio.web.streamer._save_dotenv"),
            patch(
                "mammamiradio.web.streamer.check_provider_keys",
                new=AsyncMock(return_value=_probe_payload(anthropic="ok")),
            ),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post("/api/setup/save-keys", json={"ANTHROPIC_API_KEY": "sk-ant-new"})
            assert resp.status_code == 200
            # _apply_live_credentials wiped the stale verdict synchronously.
            assert app.state.station_state.anthropic_key_status == "unverified"
            # The scheduled background re-probe then writes the fresh verdict.
            await app.state.provider_verdict_task
        assert app.state.station_state.anthropic_key_status == "valid"
    finally:
        if previous is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous


@pytest.mark.asyncio
async def test_capabilities_exposes_key_status_and_steers_next_step():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bogus"
    app.state.config.openai_api_key = ""
    app.state.station_state.anthropic_key_status = "rejected"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    caps = body["capabilities"]
    assert caps["anthropic_key_status"] == "rejected"
    assert "openai_key_status" in caps
    # A confirmed-rejected sole key steers next_step toward replacing it.
    assert body["next_step"]["key"] == "fix_llm_key"
    # provider_health carries the verdict for both providers.
    assert body["provider_health"]["anthropic"]["key_status"] == "rejected"
    assert "key_status" in body["provider_health"]["openai"]


@pytest.mark.asyncio
async def test_capabilities_valid_key_does_not_steer_next_step():
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-good"
    app.state.station_state.anthropic_key_status = "valid"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["next_step"]["key"] != "fix_llm_key"


@pytest.mark.asyncio
async def test_capabilities_rejected_anthropic_but_valid_openai_does_not_steer():
    """OpenAI is a working fallback — a rejected Anthropic key must NOT nag to fix it."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bad"
    app.state.config.openai_api_key = "sk-openai-good"
    app.state.station_state.anthropic_key_status = "rejected"
    app.state.station_state.openai_key_status = "valid"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["next_step"]["key"] != "fix_llm_key"


@pytest.mark.asyncio
async def test_capabilities_rejected_anthropic_with_unverified_openai_does_not_steer_yet():
    """While the second provider's probe is still in flight, hold the fix nudge."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-bad"
    app.state.config.openai_api_key = "sk-openai-pending"
    app.state.station_state.anthropic_key_status = "rejected"
    app.state.station_state.openai_key_status = "unverified"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["next_step"]["key"] != "fix_llm_key"


@pytest.mark.asyncio
async def test_capabilities_openai_rejected_alone_steers_fix_llm_key():
    """OpenAI-only deployment with a rejected key: surface it end-to-end via /api/capabilities."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = ""
    app.state.config.openai_api_key = "sk-openai-bad"
    app.state.station_state.openai_key_status = "rejected"
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/capabilities")).json()
    assert body["capabilities"]["openai_key_status"] == "rejected"
    assert body["provider_health"]["openai"]["key_status"] == "rejected"
    assert body["next_step"]["key"] == "fix_llm_key"


@pytest.mark.asyncio
async def test_provider_check_cached_result_does_not_clear_verdict():
    """A debounced (cached) second /provider-check must not wipe the persisted verdict."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "anthropic-secret"
    payload = _probe_payload(anthropic="auth")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.web.streamer.check_provider_keys", new=AsyncMock(return_value=payload)):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await client.post("/api/setup/provider-check")
            assert app.state.station_state.anthropic_key_status == "rejected"
            # Second call inside the 2s debounce window returns the cached result.
            await client.post("/api/setup/provider-check")
    assert app.state.station_state.anthropic_key_status == "rejected"


@pytest.mark.asyncio
async def test_run_provider_verdict_discards_stale_result_when_key_changed():
    """A late-finishing probe must not clobber the verdict after the key was swapped."""
    app = _make_test_app()
    app.state.config.anthropic_api_key = "sk-ant-old-bad"
    app.state.station_state.anthropic_key_status = "valid"  # a fresh save already set this

    async def _slow_probe(config):
        # Simulate save_keys swapping the key while this stale probe is in flight.
        config.anthropic_api_key = "sk-ant-new-good"
        return _probe_payload(anthropic="auth")

    with patch("mammamiradio.web.streamer.check_provider_keys", new=_slow_probe):
        await _run_provider_verdict(app.state)
    # Stale "rejected" for the old key must be discarded; the fresh verdict stands.
    assert app.state.station_state.anthropic_key_status == "valid"
