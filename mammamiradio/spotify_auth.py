"""Spotipy OAuth bootstrap for authenticated Spotify API access."""

from __future__ import annotations

from mammamiradio.config import StationConfig


def get_spotify_client(config: StationConfig):
    """Create an authenticated Spotify client via OAuth.

    In addon mode (headless), uses cached tokens with open_browser=False.
    Falls back to client credentials (public playlists only) if no user token.
    """
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

    cache_path = "/data/.spotify_token_cache" if config.is_addon else ".spotify_token_cache"

    auth = SpotifyOAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="user-modify-playback-state user-read-playback-state user-library-read playlist-read-private",
        cache_path=cache_path,
        open_browser=not config.is_addon,
    )

    # In addon mode, if no cached user token exists, fall back to client credentials
    # (works for public playlist fetching, no user auth needed)
    if config.is_addon:
        token_info = auth.cache_handler.get_cached_token()
        if not token_info:
            cc_auth = SpotifyClientCredentials(
                client_id=config.spotify_client_id,
                client_secret=config.spotify_client_secret,
            )
            return spotipy.Spotify(auth_manager=cc_auth)

    return spotipy.Spotify(auth_manager=auth)
