"""Tests for the FastAPI app lifecycle in mammamiradio/main.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

MODULE = "mammamiradio.main"
TEST_TMP = Path("/tmp/mammamiradio-test-main-tmp")
TEST_CACHE = Path("/tmp/mammamiradio-test-main-cache")


@pytest.mark.asyncio
async def test_startup_creates_state_and_tasks():
    """startup() loads config, fetches playlist, sets app.state, creates tasks."""
    from mammamiradio.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

        # Verify app.state was populated
        assert hasattr(app.state, "queue")
        assert hasattr(app.state, "stream_hub")
        assert hasattr(app.state, "station_state")
        assert hasattr(app.state, "config")
        assert hasattr(app.state, "producer_task")
        assert hasattr(app.state, "playback_task")
        assert app.state.station_state.playlist == demo_tracks


@pytest.mark.asyncio
async def test_startup_reads_persisted_source_before_fetching():
    from mammamiradio.models import PlaylistSource, Track

    order: list[str] = []
    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE
    persisted = PlaylistSource(kind="playlist", source_id="abc", label="Roadtrip")

    def _read(_cache_dir):
        order.append("read")
        return persisted

    def _fetch(_config, received):
        order.append("fetch")
        assert received is persisted
        return [Track(title="S", artist="A", duration_ms=1, spotify_id="x")], persisted, ""

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", side_effect=_read),
        patch(f"{MODULE}.fetch_startup_playlist", side_effect=_fetch),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        await startup()

    assert order == ["read", "fetch"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("flag_exists", "expected"), [(True, False), (False, False)])
async def test_startup_restores_stopped_session_flag(tmp_path: Path, flag_exists: bool, expected: bool):
    """startup() always clears session_stopped on restart — a restart is an intent to play."""
    from mammamiradio.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"

    flag_file = mock_config.cache_dir / "session_stopped.flag"
    if flag_exists:
        flag_file.parent.mkdir(parents=True, exist_ok=True)
        flag_file.touch()

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(
            f"{MODULE}.fetch_startup_playlist",
            return_value=([Track(title="S", artist="A", duration_ms=1, spotify_id="x")], None, ""),
        ),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    assert app.state.station_state.session_stopped is expected
    # Flag file must be deleted so a subsequent restart also starts clean
    assert not flag_file.exists()


@pytest.mark.asyncio
async def test_startup_boot_summary_and_purge(tmp_path: Path):
    """startup() calls purge_suspect_cache_files and logs boot summary."""
    from mammamiradio.models import PlaylistSource, Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = True
    mock_config.audio.bitrate = 192

    ps = PlaylistSource(kind="charts", source_id="it", label="Italian charts")
    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, ps, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.purge_suspect_cache_files", return_value=3) as mock_purge,
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        await startup()
        mock_purge.assert_called_once()

        # Verify clip ring buffer was created
        from mammamiradio.main import app

        assert hasattr(app.state, "clip_ring_buffer")


@pytest.mark.asyncio
async def test_startup_clip_ring_buffer_type_error(tmp_path: Path):
    """Clip ring buffer init handles TypeError from config.audio.bitrate."""
    from mammamiradio.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    # Make audio.bitrate raise TypeError when int() is called
    mock_config.audio.bitrate = MagicMock(side_effect=TypeError("cannot convert"))

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()
        # Should have fallen back to maxlen=240
        assert app.state.clip_ring_buffer.maxlen == 240


@pytest.mark.asyncio
async def test_healthz_and_readyz_contract_after_startup(tmp_path: Path):
    """/healthz and /readyz honour their contracts after the real startup() lifecycle.

    The existing health-probe unit tests build a synthetic app with direct state
    injection.  This test goes through startup() so regressions in the lifespan
    path (e.g. a missing app.state field, a broken start_time assignment) are
    caught before they reach production.

    Expected behaviour:
    - /healthz always returns 200 once startup completes
    - /readyz returns 503 "starting" when the queue is empty (prewarm mocked away)
    """
    from mammamiradio.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.max_cache_size_mb = 500
    mock_config.tmp_dir = tmp_path / "tmp"
    mock_config.cache_dir = tmp_path / "cache"
    mock_config.homeassistant.enabled = False
    mock_config.allow_ytdlp = False
    mock_config.audio.bitrate = 192

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        from mammamiradio.main import app, startup

        await startup()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert "uptime_s" in body

    # prewarm was mocked away → queue is empty → station is still starting
    assert ready.status_code == 503
    assert ready.json()["status"] == "starting"


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks():
    """shutdown() cancels producer and playback tasks and awaits gather."""
    import mammamiradio.main as main_mod

    mock_task = AsyncMock()
    mock_task.cancel = MagicMock()

    main_mod._producer_task = mock_task
    main_mod._playback_task = mock_task

    from mammamiradio.main import shutdown

    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await shutdown()

    assert mock_task.cancel.call_count == 2
    mock_gather.assert_called_once()
    # Verify return_exceptions=True was passed
    _, kwargs = mock_gather.call_args
    assert kwargs.get("return_exceptions") is True

    # Cleanup
    main_mod._producer_task = None
    main_mod._playback_task = None
