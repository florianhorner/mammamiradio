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
async def test_startup_prewarm_is_capped_to_two_on_addon(tmp_path: Path):
    """startup() prewarms exactly two segments even when running as HA addon."""
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
    mock_config.audio.bitrate = 192
    mock_config.is_addon = True
    mock_config.allow_ytdlp = False
    mock_config.homeassistant.enabled = False

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock) as mock_prewarm,
    ):
        from mammamiradio.main import app, startup

        await startup()
        await app.state.prewarm_task

    assert mock_prewarm.await_count == 2


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
@pytest.mark.parametrize(("flag_exists", "expected"), [(True, True), (False, False)])
async def test_startup_restores_stopped_session_flag(tmp_path: Path, flag_exists: bool, expected: bool):
    """startup() preserves session_stopped across restarts so operator stop survives crashes."""
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
    # Flag file is preserved when it existed (operator stop survives restart)
    assert flag_file.exists() == flag_exists


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

    class _BadBitrate:
        def __int__(self) -> int:
            raise TypeError("cannot convert")

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
    # Use a real __int__ failure; MagicMock coerces to 1 here and misses the fallback branch.
    mock_config.audio.bitrate = _BadBitrate()

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
    """shutdown() cancels all lifecycle tasks and clears app.state handles."""
    import mammamiradio.main as main_mod

    prewarm_task = AsyncMock()
    producer_task = AsyncMock()
    playback_task = AsyncMock()
    for task in (prewarm_task, producer_task, playback_task):
        task.cancel = MagicMock()

    main_mod._prewarm_task = prewarm_task
    main_mod._producer_task = producer_task
    main_mod._playback_task = playback_task
    main_mod.app.state.prewarm_task = prewarm_task
    main_mod.app.state.producer_task = producer_task
    main_mod.app.state.playback_task = playback_task
    main_mod.app.state.stream_hub = MagicMock()

    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await main_mod.shutdown()

    for task in (prewarm_task, producer_task, playback_task):
        task.cancel.assert_called_once()
    mock_gather.assert_called_once_with(prewarm_task, producer_task, playback_task, return_exceptions=True)
    assert main_mod.app.state.prewarm_task is None
    assert main_mod.app.state.producer_task is None
    assert main_mod.app.state.playback_task is None
    main_mod.app.state.stream_hub.close.assert_called_once()

    # Cleanup
    main_mod._prewarm_task = None
    main_mod._producer_task = None
    main_mod._playback_task = None


@pytest.mark.asyncio
async def test_startup_demo_fallback_on_fetch_exception(tmp_path: Path):
    """When fetch_startup_playlist raises, startup falls back to DEMO_TRACKS."""
    from mammamiradio.main import app, startup
    from mammamiradio.playlist import DEMO_TRACKS

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
    mock_config.audio.bitrate = 128

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", side_effect=RuntimeError("network down")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
    ):
        await startup()

    # State should contain the demo tracks, not an empty list
    assert app.state.station_state.playlist == list(DEMO_TRACKS)
    assert app.state.station_state.startup_source_error == "network down"
    # playlist_source should be demo kind
    assert app.state.station_state.playlist_source.kind == "demo"


@pytest.mark.asyncio
async def test_startup_clip_ring_buffer_fallback_to_240(tmp_path: Path):
    """Ring buffer maxlen falls back to 240 when config.audio.bitrate raises ValueError."""
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
    # Simulate a config value that causes ValueError when int() is called
    mock_config.audio.bitrate = MagicMock(side_effect=ValueError("not a number"))

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

    assert app.state.clip_ring_buffer.maxlen == 240


@pytest.mark.asyncio
async def test_startup_no_ffmpeg_warning_when_found(tmp_path: Path, caplog):
    """startup() skips the FFmpeg warning when shutil.which finds ffmpeg."""
    import logging

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
        patch(f"{MODULE}.shutil.which", return_value="/usr/bin/ffmpeg"),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert not any("FFmpeg not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_warns_when_ytdlp_missing_but_allowed(tmp_path: Path, caplog):
    """startup() warns when yt-dlp is allowed in config but the binary is not installed."""
    import logging

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
        patch(f"{MODULE}.prewarm_first_segment", new_callable=AsyncMock),
        patch(f"{MODULE}.shutil.which", return_value=None),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert any("yt-dlp" in r.message and "not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_startup_no_ytdlp_warning_when_blocked(tmp_path: Path, caplog):
    """startup() does not warn about missing yt-dlp when allow_ytdlp is False."""
    import logging

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
        patch(f"{MODULE}.shutil.which", return_value=None),
        caplog.at_level(logging.WARNING, logger="mammamiradio"),
    ):
        from mammamiradio.main import startup

        await startup()

    assert not any("yt-dlp" in r.message and "not found" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_lifespan_calls_startup_and_shutdown(tmp_path):
    """_lifespan context manager calls startup() then shutdown() around the yield."""

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

    tracks = [Track(title="S", artist="A", duration_ms=1, spotify_id="x")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(tracks, None, "")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import _lifespan, app

        async with _lifespan(app):
            assert hasattr(app.state, "station_state")


@pytest.mark.asyncio
async def test_startup_clip_ring_buffer_invalid_string_bitrate(tmp_path: Path):
    """Ring buffer maxlen falls back to 240 when config.audio.bitrate is an unparseable string."""
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
    # A plain string causes ValueError from int()
    mock_config.audio.bitrate = "not-a-number"

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

    assert app.state.clip_ring_buffer.maxlen == 240


@pytest.mark.asyncio
async def test_shutdown_with_no_tasks_set():
    """shutdown() handles the case where all module-level task refs are None."""
    import mammamiradio.main as main_mod

    main_mod._producer_task = None
    main_mod._playback_task = None
    main_mod._prewarm_task = None

    for attr in ("producer_task", "prewarm_task", "playback_task", "stream_hub"):
        if hasattr(main_mod.app.state, attr):
            delattr(main_mod.app.state, attr)

    # Should complete without calling asyncio.gather (no tasks to cancel)
    with patch("asyncio.gather", new_callable=AsyncMock) as mock_gather:
        await main_mod.shutdown()

    mock_gather.assert_not_called()
