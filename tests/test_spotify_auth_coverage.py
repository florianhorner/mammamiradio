"""Extended tests for mammamiradio/spotify_auth.py — coverage sprint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


@dataclass
class _FakeConfig:
    spotify_client_id: str = "test-client-id"
    spotify_client_secret: str = "test-client-secret"
    is_addon: bool = False
    cache_dir: Path = Path("cache")
    tmp_dir: Path = Path("tmp")


# ---------------------------------------------------------------------------
# _token_cache_path
# ---------------------------------------------------------------------------


def test_token_cache_local():
    from mammamiradio.spotify_auth import _token_cache_path

    config = _FakeConfig(is_addon=False)
    assert _token_cache_path(config) == ".spotify_token_cache"


def test_token_cache_addon():
    from mammamiradio.spotify_auth import _token_cache_path

    config = _FakeConfig(is_addon=True)
    assert _token_cache_path(config) == "/data/.spotify_token_cache"


# ---------------------------------------------------------------------------
# _build_oauth_manager
# ---------------------------------------------------------------------------


@patch("spotipy.oauth2.SpotifyOAuth")
def test_build_oauth_manager(mock_oauth):
    from mammamiradio.spotify_auth import _build_oauth_manager

    config = _FakeConfig()
    _build_oauth_manager(config, "http://localhost/callback")

    mock_oauth.assert_called_once()
    kwargs = mock_oauth.call_args[1]
    assert kwargs["client_id"] == "test-client-id"
    assert kwargs["redirect_uri"] == "http://localhost/callback"


# ---------------------------------------------------------------------------
# build_auth_url
# ---------------------------------------------------------------------------


@patch("spotipy.oauth2.SpotifyOAuth")
def test_build_auth_url(mock_oauth):
    from mammamiradio.spotify_auth import build_auth_url

    mock_oauth.return_value.get_authorize_url.return_value = "https://accounts.spotify.com/authorize?..."
    config = _FakeConfig()
    url = build_auth_url(config, "http://localhost/callback", state="abc")

    assert "accounts.spotify.com" in url
    mock_oauth.return_value.get_authorize_url.assert_called_once_with(state="abc")


# ---------------------------------------------------------------------------
# exchange_code
# ---------------------------------------------------------------------------


@patch("spotipy.oauth2.SpotifyOAuth")
def test_exchange_code_success(mock_oauth):
    from mammamiradio.spotify_auth import exchange_code

    mock_oauth.return_value.get_access_token.return_value = {"access_token": "abc"}
    config = _FakeConfig()
    assert exchange_code(config, "auth-code", "http://localhost/callback") is True


@patch("spotipy.oauth2.SpotifyOAuth")
def test_exchange_code_no_token(mock_oauth):
    from mammamiradio.spotify_auth import exchange_code

    mock_oauth.return_value.get_access_token.return_value = None
    config = _FakeConfig()
    assert exchange_code(config, "auth-code", "http://localhost/callback") is False


@patch("spotipy.oauth2.SpotifyOAuth")
def test_exchange_code_exception(mock_oauth):
    from mammamiradio.spotify_auth import exchange_code

    mock_oauth.return_value.get_access_token.side_effect = Exception("API error")
    config = _FakeConfig()
    assert exchange_code(config, "auth-code", "http://localhost/callback") is False


# ---------------------------------------------------------------------------
# has_user_token
# ---------------------------------------------------------------------------


def test_has_user_token_no_file(tmp_path, monkeypatch):
    from mammamiradio.spotify_auth import has_user_token

    config = _FakeConfig(is_addon=False)
    monkeypatch.chdir(tmp_path)
    assert has_user_token(config) is False


@patch("spotipy.oauth2.SpotifyOAuth")
def test_has_user_token_with_token(mock_oauth, tmp_path, monkeypatch):
    from mammamiradio.spotify_auth import has_user_token

    monkeypatch.chdir(tmp_path)
    cache_file = tmp_path / ".spotify_token_cache"
    cache_file.write_text('{"access_token": "x", "refresh_token": "y"}')

    mock_oauth.return_value.cache_handler.get_cached_token.return_value = {
        "access_token": "x",
        "refresh_token": "y",
    }

    config = _FakeConfig(is_addon=False)
    assert has_user_token(config) is True


@patch("spotipy.oauth2.SpotifyOAuth")
def test_has_user_token_without_refresh(mock_oauth, tmp_path, monkeypatch):
    from mammamiradio.spotify_auth import has_user_token

    monkeypatch.chdir(tmp_path)
    cache_file = tmp_path / ".spotify_token_cache"
    cache_file.write_text('{"access_token": "x"}')

    mock_oauth.return_value.cache_handler.get_cached_token.return_value = {"access_token": "x"}

    config = _FakeConfig(is_addon=False)
    assert has_user_token(config) is False


# ---------------------------------------------------------------------------
# clear_user_token
# ---------------------------------------------------------------------------


def test_clear_user_token(tmp_path, monkeypatch):
    from mammamiradio.spotify_auth import clear_user_token

    monkeypatch.chdir(tmp_path)
    cache_file = tmp_path / ".spotify_token_cache"
    cache_file.write_text("token data")

    config = _FakeConfig(is_addon=False)
    clear_user_token(config)

    assert not cache_file.exists()


def test_clear_user_token_missing_file(tmp_path, monkeypatch):
    from mammamiradio.spotify_auth import clear_user_token

    monkeypatch.chdir(tmp_path)
    config = _FakeConfig(is_addon=False)
    # Should not raise
    clear_user_token(config)


# ---------------------------------------------------------------------------
# get_spotify_client — non-addon, no token, CC fails, falls back to full OAuth
# ---------------------------------------------------------------------------


@patch("spotipy.oauth2.SpotifyOAuth")
@patch("spotipy.oauth2.SpotifyClientCredentials")
@patch("spotipy.Spotify")
def test_get_client_non_addon_cc_fails_falls_back_to_oauth(mock_spotify, mock_cc, mock_oauth):
    from mammamiradio.spotify_auth import get_spotify_client

    mock_oauth.return_value.cache_handler.get_cached_token.return_value = None
    mock_cc.side_effect = Exception("CC failed")

    config = _FakeConfig(is_addon=False)
    get_spotify_client(config)

    # Falls back to full OAuth (Spotify called with SpotifyOAuth auth_manager)
    mock_spotify.assert_called_with(auth_manager=mock_oauth.return_value)
