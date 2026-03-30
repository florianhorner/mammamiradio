"""Playlist loading from Spotify or bundled demo tracks."""

from __future__ import annotations

import logging
import random
import re

from mammamiradio.config import StationConfig
from mammamiradio.models import Track

logger = logging.getLogger(__name__)

DEMO_TRACKS = [
    Track(title="Con te partirò", artist="Andrea Bocelli", duration_ms=250000, spotify_id="demo1"),
    Track(title="Volare", artist="Domenico Modugno", duration_ms=210000, spotify_id="demo2"),
    Track(title="L'italiano", artist="Toto Cutugno", duration_ms=240000, spotify_id="demo3"),
    Track(title="Sapore di sale", artist="Gino Paoli", duration_ms=180000, spotify_id="demo4"),
    Track(title="Felicità", artist="Al Bano e Romina Power", duration_ms=230000, spotify_id="demo5"),
    Track(title="Gloria", artist="Umberto Tozzi", duration_ms=260000, spotify_id="demo6"),
    Track(title="Azzurro", artist="Adriano Celentano", duration_ms=200000, spotify_id="demo7"),
    Track(title="Nel blu dipinto di blu", artist="Domenico Modugno", duration_ms=195000, spotify_id="demo8"),
    Track(title="Ti amo", artist="Umberto Tozzi", duration_ms=220000, spotify_id="demo9"),
    Track(title="La solitudine", artist="Laura Pausini", duration_ms=275000, spotify_id="demo10"),
]


from mammamiradio.spotify_auth import get_spotify_client  # noqa: E402


def _get_spotify_oauth(config: StationConfig):
    """Deprecated: use spotify_auth.get_spotify_client directly."""
    return get_spotify_client(config)


def _extract_playlist_id(url: str) -> str | None:
    """Pull the Spotify playlist ID out of a share URL."""
    m = re.search(r"playlist/([a-zA-Z0-9]+)", url)
    return m.group(1) if m else None


def fetch_playlist(config: StationConfig) -> list[Track]:
    """Fetch tracks from the configured Spotify source with demo fallbacks."""
    if not config.spotify_client_id or not config.spotify_client_secret:
        logger.warning("No Spotify credentials — using demo Italian playlist")
        tracks = list(DEMO_TRACKS)
        if config.playlist.shuffle:
            random.shuffle(tracks)
        return tracks

    sp = _get_spotify_oauth(config)

    # Try playlist URL first
    playlist_id = _extract_playlist_id(config.playlist.spotify_url) if config.playlist.spotify_url else None

    tracks: list[Track] = []  # type: ignore[no-redef]

    if playlist_id:
        try:
            results = sp.playlist_tracks(playlist_id)
            while results:
                for item in results["items"]:
                    t = item.get("track")
                    if not t or not t.get("id"):
                        continue
                    artist = t["artists"][0]["name"] if t["artists"] else "Unknown"
                    tracks.append(
                        Track(
                            title=t["name"],
                            artist=artist,
                            duration_ms=t["duration_ms"],
                            spotify_id=t["id"],
                        )
                    )
                results = sp.next(results) if results.get("next") else None
            logger.info("Fetched %d tracks from playlist", len(tracks))
        except Exception as e:
            logger.warning("Playlist fetch failed (%s) — falling back to liked songs", e)

    # Fallback: use liked songs
    if not tracks:
        logger.info("Fetching liked songs from Spotify...")
        offset = 0
        limit = 50
        max_tracks = 200  # cap for speed
        while offset < max_tracks:
            results = sp.current_user_saved_tracks(limit=limit, offset=offset)
            if not results["items"]:
                break
            for item in results["items"]:
                t = item["track"]
                if not t or not t.get("id"):
                    continue
                artist = t["artists"][0]["name"] if t["artists"] else "Unknown"
                tracks.append(
                    Track(
                        title=t["name"],
                        artist=artist,
                        duration_ms=t["duration_ms"],
                        spotify_id=t["id"],
                    )
                )
            offset += limit
            if not results.get("next"):
                break
        logger.info("Fetched %d liked songs", len(tracks))

    if not tracks:
        logger.warning("No Spotify tracks available — using demo playlist")
        return list(DEMO_TRACKS)

    if config.playlist.shuffle:
        random.shuffle(tracks)

    return tracks
