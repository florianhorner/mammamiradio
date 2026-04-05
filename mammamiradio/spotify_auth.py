"""Spotipy OAuth bootstrap for authenticated Spotify API access."""

from __future__ import annotations

import logging
import os

from mammamiradio.config import StationConfig

logger = logging.getLogger(__name__)

_SCOPES = "user-modify-playback-state user-read-playback-state user-library-read playlist-read-private"


def _token_cache_path(config: StationConfig) -> str:
    """Return the Spotipy token cache file path."""
    return "/data/.spotify_token_cache" if config.is_addon else ".spotify_token_cache"


def _build_oauth_manager(config: StationConfig, redirect_uri: str):
    """Build a SpotifyOAuth manager with the given redirect URI."""
    from spotipy.oauth2 import SpotifyOAuth

    return SpotifyOAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri=redirect_uri,
        scope=_SCOPES,
        cache_path=_token_cache_path(config),
        open_browser=False,
    )


def get_spotify_client(config: StationConfig):
    """Create an authenticated Spotify client via OAuth.

    In addon mode (headless), uses cached tokens with open_browser=False.
    Falls back to client credentials (public playlists only) if no user token.
    """
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

    cache_path = _token_cache_path(config)

    auth = SpotifyOAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=_SCOPES,
        cache_path=cache_path,
        open_browser=not config.is_addon,
    )

    # If cached user token exists, use it (supports liked songs, private playlists)
    token_info = auth.cache_handler.get_cached_token()
    if token_info:
        return spotipy.Spotify(auth_manager=auth)

    # No user token: try client credentials first (public playlists, no browser popup)
    try:
        cc_auth = SpotifyClientCredentials(
            client_id=config.spotify_client_id,
            client_secret=config.spotify_client_secret,
        )
        return spotipy.Spotify(auth_manager=cc_auth)
    except Exception:
        pass

    # Last resort: full OAuth flow (will open browser on local dev)
    if not config.is_addon:
        return spotipy.Spotify(auth_manager=auth)

    cc_auth = SpotifyClientCredentials(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
    )
    return spotipy.Spotify(auth_manager=cc_auth)


def build_auth_url(config: StationConfig, redirect_uri: str, state: str | None = None) -> str:
    """Build the Spotify authorization URL for the OAuth flow."""
    return _build_oauth_manager(config, redirect_uri).get_authorize_url(state=state)


def exchange_code(config: StationConfig, code: str, redirect_uri: str) -> bool:
    """Exchange an authorization code for access/refresh tokens."""
    try:
        auth = _build_oauth_manager(config, redirect_uri)
        token_info = auth.get_access_token(code, as_dict=True)
        if token_info:
            logger.info("Spotify OAuth token obtained successfully")
            return True
        return False
    except Exception as e:
        logger.error("Spotify token exchange failed: %s", e)
        return False


def has_user_token(config: StationConfig) -> bool:
    """Check whether a cached Spotify user token exists (valid or refreshable)."""
    from spotipy.oauth2 import SpotifyOAuth

    cache_path = _token_cache_path(config)
    if not os.path.exists(cache_path):
        return False
    try:
        auth = SpotifyOAuth(
            client_id=config.spotify_client_id or "placeholder",
            client_secret=config.spotify_client_secret or "placeholder",
            redirect_uri="http://localhost/unused",
            cache_path=cache_path,
        )
        token_info = auth.cache_handler.get_cached_token()
        return bool(token_info and token_info.get("refresh_token"))
    except Exception:
        return False


def clear_user_token(config: StationConfig) -> None:
    """Delete the cached Spotify user token."""
    cache_path = _token_cache_path(config)
    try:
        os.unlink(cache_path)
        logger.info("Spotify user token cleared")
    except FileNotFoundError:
        pass
