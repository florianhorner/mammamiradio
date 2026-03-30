"""Tests for mammamiradio.spotify_auth — Spotipy OAuth bootstrap."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock


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


def test_get_spotify_client(monkeypatch):
    """Verify SpotifyOAuth is configured with the right credentials."""
    mock_spotipy = MagicMock()
    mock_oauth_cls = MagicMock()
    mock_spotipy.oauth2.SpotifyOAuth = mock_oauth_cls

    # Inject mock into sys.modules so `import spotipy` inside the function resolves
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

    # Verify Spotify() was called with the auth_manager
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
    # scope is passed as a keyword arg
    scope = call_kwargs.kwargs.get("scope", "") or call_kwargs[1].get("scope", "")
    assert "user-library-read" in scope
    assert "playlist-read-private" in scope
