"""Tests for LiveStreamHub, HTTP routes, and admin auth in streamer.py."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import Segment, SegmentType, StationState, Track
from mammamiradio.streamer import (
    LiveStreamHub,
    _persist_completed_music,
    router,
    run_playback_loop,
)

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app(*, admin_password: str = "", admin_token: str = "") -> FastAPI:
    """Build a minimal FastAPI app with the streamer router and populated state."""
    app = FastAPI()
    app.include_router(router)

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

    with patch("mammamiradio.song_cues.detect_anthem", new=AsyncMock()) as detect_anthem:
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

    with patch("mammamiradio.streamer._persist_completed_music", new=AsyncMock()) as persist_completed:
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
        patch("mammamiradio.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.producer._pick_canned_clip", return_value=fallback_path),
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
        patch("mammamiradio.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.producer._pick_canned_clip", return_value=fallback_path),
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
async def test_run_playback_loop_timeout_uses_norm_cache_after_30s(tmp_path, caplog):
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

    with (
        patch("mammamiradio.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.producer._pick_canned_clip", return_value=None),
        patch(
            "mammamiradio.streamer._runtime_monotonic",
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

    assert app.state.station_state.queue_empty_since is None
    assert any("rescuing with norm cache" in record.message for record in caplog.records)


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
        patch("mammamiradio.streamer.asyncio.wait_for", new=AsyncMock(side_effect=_forced_timeout)),
        patch("mammamiradio.producer._pick_canned_clip", return_value=None),
        patch("mammamiradio.streamer._runtime_monotonic", side_effect=[200.0, 260.5, 260.6, 260.7]),
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
    from mammamiradio.streamer import _audio_generator

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
    with patch("mammamiradio.song_cues.detect_skip_bit", new=AsyncMock()) as detect_skip_bit:
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
    with patch("mammamiradio.song_cues.detect_skip_bit", new=AsyncMock(return_value=True)):
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
    assert "Mamma Mi Radio" in resp.text
    assert "We're lighting the sign." in resp.text
    assert "Start the station" in resp.text


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
        with patch("mammamiradio.streamer._save_dotenv") as save_dotenv:
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/api/setup/save-keys",
                    json={"ANTHROPIC_API_KEY": "ant-test", "OPENAI_API_KEY": "openai-test"},
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "ANTHROPIC_API_KEY" in body["saved"]
        assert "OPENAI_API_KEY" in body["saved"]
        assert app.state.config.anthropic_api_key == "ant-test"
        assert app.state.config.openai_api_key == "openai-test"
        save_dotenv.assert_called_once()
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
async def test_admin_status_private_network_trusted():
    """Private network (RFC1918) client should be trusted without credentials."""
    app = _make_test_app(admin_password="secret123")
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
    assert "tier" in body
    assert "trial" in body
    assert "canned_clips_streamed" in body["trial"]


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
    from mammamiradio.producer import SHAREWARE_CANNED_LIMIT

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
    from mammamiradio.streamer import _audio_generator

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
    from mammamiradio.streamer import _audio_generator

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
    from mammamiradio.streamer import _audio_generator

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
    assert "mammamiradio.scriptwriter" in body["reloaded_modules"]
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
async def test_hot_reload_importerror_returns_500_with_stream_status():
    """When importlib.reload raises, the endpoint returns 500 with stream_status=unaffected."""
    app = _make_test_app(admin_token="testtoken")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    with patch("mammamiradio.streamer.importlib.reload", side_effect=ImportError("syntax error in scriptwriter.py")):
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
