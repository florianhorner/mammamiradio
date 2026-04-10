"""Tests for the FastAPI app lifecycle in mammamiradio/main.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    mock_config.playlist.spotify_url = ""
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(f"{MODULE}.fetch_startup_playlist", return_value=(demo_tracks, None, "")),
        patch(f"{MODULE}.SpotifyPlayer", side_effect=Exception("no go-librespot")),
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
async def test_startup_without_golibrespot():
    """startup() continues gracefully when go-librespot is unavailable."""
    from mammamiradio.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.playlist.spotify_url = ""
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value=None),
        patch(
            f"{MODULE}.fetch_startup_playlist",
            return_value=([Track(title="S", artist="A", duration_ms=1, spotify_id="x")], None, ""),
        ),
        patch(f"{MODULE}.SpotifyPlayer", side_effect=FileNotFoundError("go-librespot not found")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        # Should not raise even though SpotifyPlayer fails
        await startup()


@pytest.mark.asyncio
async def test_startup_starts_spotify_before_fetching_playlist():
    from mammamiradio.models import Track

    order: list[str] = []
    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.playlist.spotify_url = "spotify:test"
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    mock_player = MagicMock()
    mock_player.device_name = "mammamiradio"
    mock_player.start.side_effect = lambda: order.append("spotify")

    def _fetch_playlist(_config, _persisted):
        order.append("playlist")
        return [Track(title="S", artist="A", duration_ms=1, spotify_id="x")], None, ""

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.read_persisted_source", return_value="persisted"),
        patch(f"{MODULE}.fetch_startup_playlist", side_effect=_fetch_playlist),
        patch(f"{MODULE}.SpotifyPlayer", return_value=mock_player),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        await startup()

    assert order[:2] == ["spotify", "playlist"]


@pytest.mark.asyncio
async def test_startup_reads_persisted_source_before_fetching():
    from mammamiradio.models import PlaylistSource, Track

    order: list[str] = []
    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.playlist.spotify_url = ""
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
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
        patch(f"{MODULE}.SpotifyPlayer", side_effect=Exception("no go-librespot")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        await startup()

    assert order == ["read", "fetch"]


@pytest.mark.asyncio
async def test_startup_restores_stopped_session_flag():
    """startup() reads session_stopped.flag and passes it to StationState."""
    from mammamiradio.models import Track

    mock_config = MagicMock()
    mock_config.station.name = "TestRadio"
    mock_config.station.language = "it"
    mock_config.playlist.spotify_url = ""
    mock_config.bind_host = "127.0.0.1"
    mock_config.port = 8000
    mock_config.pacing.lookahead_segments = 3
    mock_config.tmp_dir = TEST_TMP
    mock_config.cache_dir = TEST_CACHE

    # Simulate flag file existing
    flag_file = TEST_CACHE / "session_stopped.flag"
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    flag_file.touch()

    try:
        with (
            patch(f"{MODULE}.load_config", return_value=mock_config),
            patch(f"{MODULE}.read_persisted_source", return_value=None),
            patch(
                f"{MODULE}.fetch_startup_playlist",
                return_value=([Track(title="S", artist="A", duration_ms=1, spotify_id="x")], None, ""),
            ),
            patch(f"{MODULE}.SpotifyPlayer", side_effect=Exception("no go-librespot")),
            patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
            patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
        ):
            from mammamiradio.main import app, startup

            await startup()

        assert app.state.station_state.session_stopped is True
    finally:
        flag_file.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_shutdown_cancels_tasks():
    """shutdown() cancels producer and playback tasks and awaits gather."""
    import mammamiradio.main as main_mod

    mock_task = AsyncMock()
    mock_task.cancel = MagicMock()

    main_mod._producer_task = mock_task
    main_mod._playback_task = mock_task
    main_mod._spotify_player = None

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
