"""Tests for the FastAPI app lifecycle in mammamiradio/main.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "mammamiradio.main"


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
    mock_config.tmp_dir = MagicMock()
    mock_config.cache_dir = MagicMock()

    demo_tracks = [Track(title="Song", artist="Art", duration_ms=1000, spotify_id="t1")]

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.fetch_playlist", return_value=demo_tracks),
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
    mock_config.tmp_dir = MagicMock()
    mock_config.cache_dir = MagicMock()

    with (
        patch(f"{MODULE}.load_config", return_value=mock_config),
        patch(f"{MODULE}.fetch_playlist", return_value=[Track(title="S", artist="A", duration_ms=1, spotify_id="x")]),
        patch(f"{MODULE}.SpotifyPlayer", side_effect=FileNotFoundError("go-librespot not found")),
        patch(f"{MODULE}.run_producer", new_callable=AsyncMock),
        patch(f"{MODULE}.run_playback_loop", new_callable=AsyncMock),
    ):
        from mammamiradio.main import startup

        # Should not raise even though SpotifyPlayer fails
        await startup()


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
