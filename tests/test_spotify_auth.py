"""Tests for mammamiradio.spotify_auth — Spotipy OAuth bootstrap."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@dataclass
class _AudioSection:
    sample_rate: int = 48000
    channels: int = 2
    bitrate: int = 192


@dataclass
class _StationSection:
    name: str = "Test"
    slogan: str = ""
    url: str = ""
    frequency: str = ""


@dataclass
class _PlaylistSection:
    spotify_playlist_id: str = ""
    use_liked_songs: bool = False


@dataclass
class _PacingSection:
    songs_between_banter: int = 2
    songs_between_ads: int = 4
    ads_per_break: int = 2


@dataclass
class _AdsSection:
    brands: list = field(default_factory=list)
    voices: list = field(default_factory=list)


@dataclass
class _HASection:
    enabled: bool = False
    url: str = ""
    poll_interval: float = 60.0
    mention_probability: float = 0.3


@dataclass
class _FakeConfig:
    """Minimal stand-in for StationConfig with spotify fields."""

    station: _StationSection = field(default_factory=_StationSection)
    playlist: _PlaylistSection = field(default_factory=_PlaylistSection)
    pacing: _PacingSection = field(default_factory=_PacingSection)
    hosts: list = field(default_factory=list)
    ads: _AdsSection = field(default_factory=_AdsSection)
    audio: _AudioSection = field(default_factory=_AudioSection)
    homeassistant: _HASection = field(default_factory=_HASection)
    cache_dir: Path = Path("cache")
    tmp_dir: Path = Path("tmp")
    bind_host: str = "127.0.0.1"
    port: int = 8000
    admin_username: str = "admin"
    admin_password: str = ""
    admin_token: str = ""
    spotify_client_id: str = "test-client-id"
    spotify_client_secret: str = "test-client-secret"
    is_addon: bool = False


# ---------------------------------------------------------------------------
# Core OAuth tests (from main)
# ---------------------------------------------------------------------------


def test_get_spotify_client(monkeypatch):
    """Verify SpotifyOAuth is configured with the right credentials."""
    mock_spotipy = MagicMock()
    mock_oauth_cls = MagicMock()
    mock_spotipy.oauth2.SpotifyOAuth = mock_oauth_cls

    monkeypatch.setitem(sys.modules, "spotipy", mock_spotipy)
    monkeypatch.setitem(sys.modules, "spotipy.oauth2", mock_spotipy.oauth2)

    # Force re-import to pick up mocked spotipy
    if "mammamiradio.spotify_auth" in sys.modules:
        del sys.modules["mammamiradio.spotify_auth"]

    from mammamiradio.spotify_auth import get_spotify_client

    config = _FakeConfig()
    get_spotify_client(config)

    mock_oauth_cls.assert_called_once()
    call_kwargs = mock_oauth_cls.call_args
    assert call_kwargs.kwargs.get("client_id") or call_kwargs[1].get("client_id") == "test-client-id"

    mock_spotipy.Spotify.assert_called_once()


def test_get_spotify_client_passes_correct_scope(monkeypatch):
    """Verify the OAuth scope includes the expected permissions."""
    mock_spotipy = MagicMock()
    mock_oauth_cls = MagicMock()
    mock_spotipy.oauth2.SpotifyOAuth = mock_oauth_cls

    monkeypatch.setitem(sys.modules, "spotipy", mock_spotipy)
    monkeypatch.setitem(sys.modules, "spotipy.oauth2", mock_spotipy.oauth2)

    if "mammamiradio.spotify_auth" in sys.modules:
        del sys.modules["mammamiradio.spotify_auth"]

    from mammamiradio.spotify_auth import get_spotify_client

    config = _FakeConfig()
    get_spotify_client(config)

    call_kwargs = mock_oauth_cls.call_args
    scope = call_kwargs.kwargs.get("scope", "") or call_kwargs[1].get("scope", "")
    assert "user-library-read" in scope
    assert "playlist-read-private" in scope


# ---------------------------------------------------------------------------
# Addon-mode tests
# ---------------------------------------------------------------------------


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.Spotify")
def test_addon_mode_cache_path(mock_spotify, mock_oauth):
    """Addon mode should use /data/.spotify_token_cache."""
    mock_oauth.return_value.cache_handler.get_cached_token.return_value = {"access_token": "x"}

    from mammamiradio.spotify_auth import get_spotify_client

    config = _FakeConfig(is_addon=True)
    get_spotify_client(config)

    call_kwargs = mock_oauth.call_args[1]
    assert call_kwargs["cache_path"] == "/data/.spotify_token_cache"


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.Spotify")
def test_addon_mode_no_browser(mock_spotify, mock_oauth):
    """Addon mode should disable browser opening."""
    mock_oauth.return_value.cache_handler.get_cached_token.return_value = {"access_token": "x"}

    from mammamiradio.spotify_auth import get_spotify_client

    config = _FakeConfig(is_addon=True)
    get_spotify_client(config)

    call_kwargs = mock_oauth.call_args[1]
    assert call_kwargs["open_browser"] is False


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.oauth2.SpotifyClientCredentials")
@patch("spotipy.Spotify")
def test_addon_mode_fallback_client_credentials(mock_spotify, mock_cc, mock_oauth):
    """Addon mode with no cached token should fall back to client credentials."""
    mock_oauth.return_value.cache_handler.get_cached_token.return_value = None

    from mammamiradio.spotify_auth import get_spotify_client

    config = _FakeConfig(is_addon=True)
    get_spotify_client(config)

    mock_cc.assert_called_once_with(
        client_id="test-client-id",
        client_secret="test-client-secret",
    )
    mock_spotify.assert_called_with(auth_manager=mock_cc.return_value)


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.Spotify")
def test_non_addon_mode_opens_browser(mock_spotify, mock_oauth):
    """Non-addon mode should enable browser opening."""
    from mammamiradio.spotify_auth import get_spotify_client

    config = _FakeConfig(is_addon=False)
    get_spotify_client(config)

    call_kwargs = mock_oauth.call_args[1]
    assert call_kwargs["open_browser"] is True
    assert call_kwargs["cache_path"] == ".spotify_token_cache"
