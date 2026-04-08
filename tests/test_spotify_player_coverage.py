"""Extended tests for mammamiradio/spotify_player.py — coverage sprint."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mammamiradio.spotify_player import SpotifyPlayer, resolve_go_librespot_bin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(config_dir: Path, tmp_path: Path) -> MagicMock:
    config = MagicMock()
    config.audio.go_librespot_config_dir = str(config_dir)
    config.audio.fifo_path = str(tmp_path / "mammamiradio.pcm")
    config.audio.go_librespot_port = 3678
    config.audio.go_librespot_bin = "go-librespot"
    config.audio.sample_rate = 48000
    config.audio.channels = 2
    config.audio.bitrate = 192
    config.tmp_dir = tmp_path
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    return config


@pytest.fixture
def player(tmp_path):
    config_dir = tmp_path / "go-librespot"
    config_dir.mkdir()
    return SpotifyPlayer(_make_config(config_dir, tmp_path))


# ---------------------------------------------------------------------------
# resolve_go_librespot_bin
# ---------------------------------------------------------------------------


def test_resolve_absolute_executable(tmp_path):
    """Returns the path directly when it's an absolute executable."""
    fake_bin = tmp_path / "go-librespot"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    assert resolve_go_librespot_bin(str(fake_bin)) == str(fake_bin)


def test_resolve_via_which():
    """Falls back to shutil.which when not absolute."""
    with patch("mammamiradio.spotify_player.shutil.which", return_value="/usr/local/bin/go-librespot"):
        assert resolve_go_librespot_bin("go-librespot") == "/usr/local/bin/go-librespot"


def test_resolve_from_candidates(tmp_path):
    """Falls back to candidate paths when which fails."""
    with (
        patch("mammamiradio.spotify_player.shutil.which", return_value=None),
        patch("mammamiradio.spotify_player.os.access", side_effect=lambda p, _: p == "/usr/bin/go-librespot"),
    ):
        assert resolve_go_librespot_bin("go-librespot") == "/usr/bin/go-librespot"


def test_resolve_returns_none_when_missing():
    """Returns None when binary cannot be found anywhere."""
    with (
        patch("mammamiradio.spotify_player.shutil.which", return_value=None),
        patch("mammamiradio.spotify_player.os.access", return_value=False),
    ):
        assert resolve_go_librespot_bin("go-librespot") is None


# ---------------------------------------------------------------------------
# SpotifyPlayer.__init__ / properties
# ---------------------------------------------------------------------------


def test_device_name_default(player):
    """Default device name when no config.yml exists."""
    assert isinstance(player.device_name, str)


def test_spotify_auth_url_default(player):
    """Default auth URL is empty."""
    assert player.spotify_auth_url == ""


# ---------------------------------------------------------------------------
# _ensure_fifo
# ---------------------------------------------------------------------------


def test_ensure_fifo_creates(player, tmp_path):
    """Creates a FIFO when it doesn't exist."""
    fifo = tmp_path / "mammamiradio.pcm"
    fifo.unlink(missing_ok=True)
    player._ensure_fifo()
    assert fifo.exists()


def test_ensure_fifo_replaces_regular_file(player, tmp_path):
    """Replaces a regular file with a FIFO."""
    fifo = tmp_path / "mammamiradio.pcm"
    fifo.write_text("not a fifo")
    player._ensure_fifo()
    assert fifo.is_fifo()


# ---------------------------------------------------------------------------
# _needs_interactive_auth
# ---------------------------------------------------------------------------


def test_needs_interactive_auth_no_local_hostname(player):
    """Returns False when hostname doesn't end with .local."""
    with patch("mammamiradio.spotify_player.socket.gethostname", return_value="myhost"):
        assert player._needs_interactive_auth() is False


def test_needs_interactive_auth_with_credentials(player, tmp_path):
    """Returns False when credentials exist even with .local hostname."""
    config_dir = tmp_path / "go-librespot"
    config_dir.mkdir(exist_ok=True)
    creds = config_dir / "credentials.json"
    creds.write_text('{"username": "user"}')
    player._config_dir = config_dir

    with patch("mammamiradio.spotify_player.socket.gethostname", return_value="mac.local"):
        assert player._needs_interactive_auth() is False


def test_needs_interactive_auth_triggers(player, tmp_path):
    """Returns True when .local hostname and no credentials."""
    config_dir = tmp_path / "go-librespot"
    config_dir.mkdir(exist_ok=True)
    player._config_dir = config_dir

    with patch("mammamiradio.spotify_player.socket.gethostname", return_value="mac.local"):
        assert player._needs_interactive_auth() is True


# ---------------------------------------------------------------------------
# _parse_auth_url_from_log
# ---------------------------------------------------------------------------


def test_parse_auth_url_found(player, tmp_path):
    """Extracts the auth URL from the go-librespot log."""
    log = tmp_path / "go-librespot.log"
    log.write_text("INFO starting\nhttps://accounts.spotify.com/authorize?client_id=abc&scope=streaming\nINFO done")
    assert "accounts.spotify.com/authorize" in player._parse_auth_url_from_log()


def test_parse_auth_url_missing(player, tmp_path):
    """Returns empty string when no auth URL in log."""
    log = tmp_path / "go-librespot.log"
    log.write_text("no url here")
    assert player._parse_auth_url_from_log() == ""


def test_parse_auth_url_no_file(player, tmp_path):
    """Returns empty string when log file doesn't exist."""
    assert player._parse_auth_url_from_log() == ""


# ---------------------------------------------------------------------------
# check_auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_auth_connected(player):
    """Returns True and sets _authenticated when go-librespot returns username."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"username": "testuser"}

    player._http = AsyncMock()
    player._http.get = AsyncMock(return_value=mock_response)

    assert await player.check_auth() is True
    assert player._authenticated is True


@pytest.mark.asyncio
async def test_check_auth_no_username(player):
    """Returns False when go-librespot returns no username."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}

    player._http = AsyncMock()
    player._http.get = AsyncMock(return_value=mock_response)

    assert await player.check_auth() is False
    assert player._authenticated is False


@pytest.mark.asyncio
async def test_check_auth_http_error(player):
    """Returns False on HTTP errors."""
    player._http = AsyncMock()
    player._http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    assert await player.check_auth() is False


@pytest.mark.asyncio
async def test_check_auth_triggers_auto_transfer(player):
    """Auto-transfer is attempted periodically when not authenticated."""
    player._http = AsyncMock()
    player._http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    player._transfer_counter = 0

    with patch.object(player, "_try_transfer_playback", new_callable=AsyncMock) as mock_transfer:
        await player.check_auth()
        mock_transfer.assert_called_once()


# ---------------------------------------------------------------------------
# _try_transfer_playback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_transfer_skips_without_client_id(player):
    """Silently skips when no spotify_client_id is configured."""
    player.config.spotify_client_id = ""
    # Should not raise
    await player._try_transfer_playback()


@pytest.mark.asyncio
async def test_try_transfer_device_not_found(player):
    """Logs info when our device is not in the Spotify devices list."""
    player.config.spotify_client_id = "test-id"
    mock_sp = MagicMock()
    mock_sp.devices.return_value = {"devices": [{"id": "other-1", "name": "OtherDevice"}]}

    with patch("mammamiradio.spotify_auth.get_spotify_client", return_value=mock_sp):
        await player._try_transfer_playback()

    mock_sp.transfer_playback.assert_not_called()


# ---------------------------------------------------------------------------
# get_current_track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_track_success(player):
    """Returns a Track when go-librespot has a playing track."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "track": {
            "uri": "spotify:track:abc123",
            "name": "Volare",
            "artist_names": ["Modugno"],
            "duration": 180000,
            "position": 5000,
            "album_cover_url": "https://i.scdn.co/image/abc",
        },
        "username": "user",
    }

    player._http = AsyncMock()
    player._http.get = AsyncMock(return_value=mock_response)

    track = await player.get_current_track()
    assert track is not None
    assert track.title == "Volare"
    assert track.artist == "Modugno"
    assert track.spotify_id == "abc123"
    assert track.position_ms == 5000


@pytest.mark.asyncio
async def test_get_current_track_no_track(player):
    """Returns None when nothing is playing."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"track": None}

    player._http = AsyncMock()
    player._http.get = AsyncMock(return_value=mock_response)

    assert await player.get_current_track() is None


@pytest.mark.asyncio
async def test_get_current_track_non_track_uri(player):
    """Returns None for non-track URIs (e.g., podcast episodes)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "track": {
            "uri": "spotify:episode:xyz",
            "name": "Podcast",
        }
    }

    player._http = AsyncMock()
    player._http.get = AsyncMock(return_value=mock_response)

    assert await player.get_current_track() is None


@pytest.mark.asyncio
async def test_get_current_track_error(player):
    """Returns None on HTTP errors."""
    player._http = AsyncMock()
    player._http.get = AsyncMock(side_effect=Exception("timeout"))

    assert await player.get_current_track() is None


# ---------------------------------------------------------------------------
# play_track
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_play_track_success(player):
    """Sends play request to go-librespot."""
    from mammamiradio.models import Track

    mock_response = MagicMock()
    mock_response.status_code = 204

    player._http = AsyncMock()
    player._http.post = AsyncMock(return_value=mock_response)

    track = Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="abc")
    await player.play_track(track)

    player._http.post.assert_called_once()


@pytest.mark.asyncio
async def test_play_track_failure(player):
    """Raises RuntimeError on play failure."""
    from mammamiradio.models import Track

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "internal error"

    player._http = AsyncMock()
    player._http.post = AsyncMock(return_value=mock_response)

    track = Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="abc")
    with pytest.raises(RuntimeError, match="go-librespot play failed"):
        await player.play_track(track)


# ---------------------------------------------------------------------------
# pause
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause(player):
    """Sends pause request to go-librespot."""
    player._http = AsyncMock()
    player._http.post = AsyncMock(return_value=MagicMock(status_code=200))

    await player.pause()
    player._http.post.assert_called_once()


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_external(player):
    """Stop with external process starts fallback drain."""
    player._external = True
    player._drain_running = True

    with patch.object(player, "_start_fallback_drain") as mock_fallback:
        player.stop()
        mock_fallback.assert_called_once()

    assert player._drain_running is False


def test_stop_internal(player, tmp_path):
    """Stop with internally launched process terminates it."""
    player._external = False
    player._drain_running = True
    mock_proc = MagicMock()
    mock_proc.wait.return_value = 0
    player._process = mock_proc

    player.stop()

    mock_proc.terminate.assert_called_once()
    assert player._process is None


def test_stop_internal_timeout(player):
    """Stop kills process when terminate times out."""
    player._external = False
    player._drain_running = True
    mock_proc = MagicMock()
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="go-librespot", timeout=5)
    player._process = mock_proc

    player.stop()

    mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# _read_fallback_drain_pid / _find_fallback_drain_pids / _is_fallback_drain_pid
# ---------------------------------------------------------------------------


def test_read_fallback_drain_pid_missing(player):
    """Returns None when pid file doesn't exist."""
    assert player._read_fallback_drain_pid() is None


def test_read_fallback_drain_pid_exists(player, tmp_path):
    """Reads pid from file."""
    player._drain_pid_file = tmp_path / "fifo-drain.pid"
    player._drain_pid_file.write_text("12345")
    assert player._read_fallback_drain_pid() == 12345


def test_read_fallback_drain_pid_invalid(player, tmp_path):
    """Returns None for non-numeric pid file."""
    player._drain_pid_file = tmp_path / "fifo-drain.pid"
    player._drain_pid_file.write_text("not-a-pid")
    assert player._read_fallback_drain_pid() is None


def test_find_fallback_drain_pids_no_match(player):
    """Returns empty list when pgrep finds nothing."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert player._find_fallback_drain_pids() == []


def test_find_fallback_drain_pids_found(player):
    """Returns list of PIDs from pgrep output."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="100\n200\n")
        assert player._find_fallback_drain_pids() == [100, 200]


def test_is_fallback_drain_pid_true(player, tmp_path):
    """Returns True when ps shows a cat command with the FIFO path."""
    fifo = str(tmp_path / "mammamiradio.pcm")
    player._fifo_path = Path(fifo)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=f"cat {fifo}")
        assert player._is_fallback_drain_pid(100) is True


def test_is_fallback_drain_pid_false(player):
    """Returns False when ps shows a different command."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="python script.py")
        assert player._is_fallback_drain_pid(100) is False


def test_is_fallback_drain_pid_process_gone(player):
    """Returns False when process doesn't exist."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert player._is_fallback_drain_pid(100) is False


# ---------------------------------------------------------------------------
# wait_for_auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_auth_immediate_success(player):
    """Returns True immediately when already authenticated."""
    with patch.object(player, "check_auth", new_callable=AsyncMock, return_value=True):
        assert await player.wait_for_auth(timeout=1.0) is True


@pytest.mark.asyncio
async def test_wait_for_auth_timeout(player):
    """Returns False when timeout expires."""
    with patch.object(player, "check_auth", new_callable=AsyncMock, return_value=False):
        assert await player.wait_for_auth(timeout=0.1) is False
