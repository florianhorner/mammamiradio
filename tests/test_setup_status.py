from __future__ import annotations

from unittest.mock import MagicMock, patch

from mammamiradio.config import load_config
from mammamiradio.models import StationState, Track
from mammamiradio.setup_status import (
    _playlist_is_demo,
    _probe_playlist_url,
    build_setup_status,
    classify_station_mode,
    detect_run_mode,
    resolve_go_librespot_bin,
)


def _demo_state() -> StationState:
    return StationState(
        playlist=[Track(title="Volare", artist="Demo", duration_ms=180_000, spotify_id="demo1")],
    )


def _real_state() -> StationState:
    return StationState(
        playlist=[Track(title="Real Song", artist="Artist", duration_ms=180_000, spotify_id="spotify123")],
        spotify_connected=True,
    )


def test_classify_station_mode_demo_without_spotify():
    config = load_config()
    config.spotify_client_id = ""
    config.spotify_client_secret = ""

    mode = classify_station_mode(config, _demo_state())

    assert mode["id"] == "demo"
    assert "built-in demo tracks" in mode["summary"]


def test_classify_station_mode_degraded_when_spotify_falls_back():
    config = load_config()
    config.spotify_client_id = "client"
    config.spotify_client_secret = "secret"

    with patch("mammamiradio.setup_status.resolve_go_librespot_bin", return_value="/usr/local/bin/go-librespot"):
        mode = classify_station_mode(config, _demo_state())

    assert mode["id"] == "degraded"
    assert "fell back to demo tracks" in mode["summary"]


def test_classify_station_mode_real_spotify():
    config = load_config()
    config.spotify_client_id = "client"
    config.spotify_client_secret = "secret"

    with patch("mammamiradio.setup_status.resolve_go_librespot_bin", return_value="/usr/local/bin/go-librespot"):
        mode = classify_station_mode(config, _real_state())

    assert mode["id"] == "real_spotify"


def test_build_setup_status_returns_expected_shape_for_addon():
    config = load_config()
    config.is_addon = True
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _demo_state()

    with (
        patch("mammamiradio.setup_status._probe_playlist_url", return_value=("missing", "Playlist missing")),
        patch("mammamiradio.setup_status.resolve_go_librespot_bin", return_value=None),
    ):
        payload = build_setup_status(config, state)

    assert payload["detected_mode"] == "ha_addon"
    assert payload["onboarding_required"] is True
    assert payload["station_mode"]["id"] == "demo"
    assert payload["essentials"][0]["key"] == "spotify"
    assert payload["preflight_checks"][0]["key"] == "ffmpeg"
    assert "playlist_spotify_url" in payload["addon_options_snippet"]
    assert payload["signature"]


# --- detect_run_mode ---


def test_detect_run_mode_addon():
    config = load_config()
    config.is_addon = True
    result = detect_run_mode(config)
    assert result["detected"] == "ha_addon"


def test_detect_run_mode_docker():
    config = load_config()
    config.is_addon = False
    with patch("mammamiradio.setup_status.Path.exists", return_value=True):
        result = detect_run_mode(config)
    assert result["detected"] == "docker"


def test_detect_run_mode_macos():
    config = load_config()
    config.is_addon = False
    with (
        patch("mammamiradio.setup_status.Path.exists", return_value=False),
        patch("mammamiradio.setup_status.platform.system", return_value="Darwin"),
    ):
        result = detect_run_mode(config)
    assert result["detected"] == "macos"


def test_detect_run_mode_local_fallback():
    config = load_config()
    config.is_addon = False
    with (
        patch("mammamiradio.setup_status.Path.exists", return_value=False),
        patch("mammamiradio.setup_status.platform.system", return_value="Linux"),
    ):
        result = detect_run_mode(config)
    assert result["detected"] == "local"


# --- _playlist_is_demo ---


def test_playlist_is_demo_empty():
    state = StationState(playlist=[])
    assert _playlist_is_demo(state) is True


def test_playlist_is_demo_none():
    state = StationState(playlist=None)
    assert _playlist_is_demo(state) is True


# --- _probe_playlist_url ---


def test_probe_playlist_url_no_creds():
    config = load_config()
    config.spotify_client_id = ""
    config.spotify_client_secret = ""
    status, detail = _probe_playlist_url(config)
    assert status == "missing"
    assert "credentials" in detail.lower()


def test_probe_playlist_url_no_url_addon():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = ""
    config.is_addon = True
    status, detail = _probe_playlist_url(config)
    assert status == "missing"
    assert "Add-on" in detail


def test_probe_playlist_url_no_url_local():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = ""
    config.is_addon = False
    status, _detail = _probe_playlist_url(config)
    assert status == "degraded"


def test_probe_playlist_url_invalid_url():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = "not-a-spotify-url"
    status, detail = _probe_playlist_url(config)
    assert status == "invalid"
    assert "valid Spotify" in detail


def test_probe_playlist_url_success():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = "https://open.spotify.com/playlist/abc123"

    mock_sp = patch(
        "spotipy.Spotify.playlist_tracks",
        return_value={"items": [{"track": {"id": "t1"}}]},
    )
    mock_auth = patch("spotipy.oauth2.SpotifyClientCredentials")
    with mock_sp, mock_auth:
        status, _detail = _probe_playlist_url(config)
    assert status == "configured"


def test_probe_playlist_url_uses_cached_user_token_for_private_playlists():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = "https://open.spotify.com/playlist/private123"
    config.is_addon = False

    oauth = MagicMock()
    oauth.cache_handler.get_cached_token.return_value = {"access_token": "cached"}

    with (
        patch("spotipy.oauth2.SpotifyOAuth", return_value=oauth) as mock_oauth,
        patch("spotipy.oauth2.SpotifyClientCredentials") as mock_cc,
        patch("spotipy.Spotify.playlist_tracks", return_value={"items": [{"track": {"id": "t1"}}]}),
    ):
        status, _detail = _probe_playlist_url(config)

    assert status == "configured"
    mock_oauth.assert_called_once()
    mock_cc.assert_not_called()


def test_resolve_go_librespot_bin_checks_opt_homebrew_before_usr_local():
    with (
        patch("mammamiradio.setup_status.shutil.which", return_value=None),
        patch("mammamiradio.setup_status.os.path.isabs", return_value=False),
        patch(
            "mammamiradio.setup_status.os.access",
            side_effect=lambda path, mode: path == "/opt/homebrew/bin/go-librespot",
        ),
    ):
        assert resolve_go_librespot_bin("go-librespot") == "/opt/homebrew/bin/go-librespot"


def test_probe_playlist_url_empty_playlist():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = "https://open.spotify.com/playlist/abc123"

    mock_sp = patch(
        "spotipy.Spotify.playlist_tracks",
        return_value={"items": []},
    )
    mock_auth = patch("spotipy.oauth2.SpotifyClientCredentials")
    with mock_sp, mock_auth:
        status, detail = _probe_playlist_url(config)
    assert status == "invalid"
    assert "no playable" in detail.lower()


def test_probe_playlist_url_spotify_exception():
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = "https://open.spotify.com/playlist/abc123"

    mock_sp = patch(
        "spotipy.Spotify.playlist_tracks",
        side_effect=Exception("401 Unauthorized"),
    )
    mock_auth = patch("spotipy.oauth2.SpotifyClientCredentials")
    with mock_sp, mock_auth:
        status, detail = _probe_playlist_url(config)
    assert status == "invalid"
    assert "rejected" in detail.lower()


def test_probe_playlist_url_import_error():
    """When spotipy is not installed, should return missing, not invalid."""
    config = load_config()
    config.spotify_client_id = "id"
    config.spotify_client_secret = "secret"
    config.playlist.spotify_url = "https://open.spotify.com/playlist/abc123"

    with patch.dict("sys.modules", {"spotipy": None, "spotipy.oauth2": None}):
        status, detail = _probe_playlist_url(config)
    assert status == "missing"
    assert "spotipy" in detail.lower()


# --- classify_station_mode: degraded with real tracks but no connect ---


def test_classify_station_mode_degraded_real_tracks_no_connect():
    config = load_config()
    config.spotify_client_id = "client"
    config.spotify_client_secret = "secret"

    state = StationState(
        playlist=[Track(title="Real", artist="Artist", duration_ms=180_000, spotify_id="sp1")],
        spotify_connected=False,
    )

    with patch("mammamiradio.setup_status.resolve_go_librespot_bin", return_value="/usr/local/bin/go-librespot"):
        mode = classify_station_mode(config, state)

    assert mode["id"] == "degraded"
    assert "not fully ready" in mode["summary"]
