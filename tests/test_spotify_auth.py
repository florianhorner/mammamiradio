"""Tests for Spotify auth addon-mode behavior."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch


@dataclass
class _MockConfig:
    """Minimal config for spotify_auth tests."""

    is_addon: bool = False
    spotify_client_id: str = "test_id"
    spotify_client_secret: str = "test_secret"


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.Spotify")
def test_addon_mode_cache_path(mock_spotify, mock_oauth):
    """Addon mode should use /data/.spotify_token_cache."""
    mock_oauth.return_value.cache_handler.get_cached_token.return_value = {"access_token": "x"}

    from fakeitaliradio.spotify_auth import get_spotify_client

    config = _MockConfig(is_addon=True)
    get_spotify_client(config)

    call_kwargs = mock_oauth.call_args[1]
    assert call_kwargs["cache_path"] == "/data/.spotify_token_cache"


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.Spotify")
def test_addon_mode_no_browser(mock_spotify, mock_oauth):
    """Addon mode should disable browser opening."""
    mock_oauth.return_value.cache_handler.get_cached_token.return_value = {"access_token": "x"}

    from fakeitaliradio.spotify_auth import get_spotify_client

    config = _MockConfig(is_addon=True)
    get_spotify_client(config)

    call_kwargs = mock_oauth.call_args[1]
    assert call_kwargs["open_browser"] is False


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.oauth2.SpotifyClientCredentials")
@patch("spotipy.Spotify")
def test_addon_mode_fallback_client_credentials(mock_spotify, mock_cc, mock_oauth):
    """Addon mode with no cached token should fall back to client credentials."""
    mock_oauth.return_value.cache_handler.get_cached_token.return_value = None

    from fakeitaliradio.spotify_auth import get_spotify_client

    config = _MockConfig(is_addon=True)
    get_spotify_client(config)

    # Should have created SpotifyClientCredentials
    mock_cc.assert_called_once_with(
        client_id="test_id",
        client_secret="test_secret",
    )
    # Should have created Spotify with cc auth
    mock_spotify.assert_called_with(auth_manager=mock_cc.return_value)


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.Spotify")
def test_non_addon_mode_opens_browser(mock_spotify, mock_oauth):
    """Non-addon mode should enable browser opening."""
    from fakeitaliradio.spotify_auth import get_spotify_client

    config = _MockConfig(is_addon=False)
    get_spotify_client(config)

    call_kwargs = mock_oauth.call_args[1]
    assert call_kwargs["open_browser"] is True
    assert call_kwargs["cache_path"] == ".spotify_token_cache"
