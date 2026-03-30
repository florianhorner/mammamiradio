"""Spotipy OAuth bootstrap for authenticated Spotify API access."""

from __future__ import annotations

from mammamiradio.config import StationConfig


def get_spotify_client(config: StationConfig):
    """Create an authenticated Spotify client via OAuth."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    auth = SpotifyOAuth(
        client_id=config.spotify_client_id,
        client_secret=config.spotify_client_secret,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope="user-modify-playback-state user-read-playback-state user-library-read playlist-read-private",
        cache_path=".spotify_token_cache",
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth)
