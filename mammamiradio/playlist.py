"""Playlist loading from Spotify or bundled demo tracks."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from mammamiradio.config import StationConfig
from mammamiradio.models import PlaylistSource, Track
from mammamiradio.spotify_auth import get_spotify_client

logger = logging.getLogger(__name__)

DEMO_TRACKS = [
    Track(title="OSSESSIONE", artist="Samurai Jay", duration_ms=210000, spotify_id="demo1"),
    Track(title="TU MI PIACI TANTO", artist="Sayf", duration_ms=210000, spotify_id="demo2"),
    Track(title="Che fastidio!", artist="ditonellapiaga", duration_ms=210000, spotify_id="demo3"),
    Track(title="DAVVERODAVVERO", artist="Artie 5ive", duration_ms=210000, spotify_id="demo4"),
    Track(title="Stupida sfortuna", artist="Fulminacci", duration_ms=210000, spotify_id="demo5"),
    Track(title="Labirinto", artist="Luche", duration_ms=210000, spotify_id="demo6"),
    Track(title="Per sempre si", artist="Sal da Vinci", duration_ms=210000, spotify_id="demo7"),
    Track(title="Poesie Clandestine", artist="LDA & Aka 7even", duration_ms=210000, spotify_id="demo8"),
    Track(title="AL MIO PAESE", artist="Serena Brancale, Levante & DELIA", duration_ms=210000, spotify_id="demo9"),
    Track(title="CANZONE D'AMORE", artist="Geolier", duration_ms=210000, spotify_id="demo10"),
]

PERSISTED_SOURCE_FILENAME = "playlist_source.json"
_APPLE_MUSIC_IT_CHARTS_URL = "https://rss.applemarketingtools.com/api/v2/it/music/most-played/50/songs.json"


class ExplicitSourceError(RuntimeError):
    """Raised when an explicit user-selected source cannot be loaded."""


def _get_spotify_oauth(config: StationConfig):
    """Deprecated: use spotify_auth.get_spotify_client directly."""
    return get_spotify_client(config)


def _extract_playlist_id(url: str) -> str | None:
    """Pull the Spotify playlist ID out of a share URL."""
    m = re.search(r"playlist/([a-zA-Z0-9]+)", url)
    return m.group(1) if m else None


def _track_from_spotify_item(item: dict) -> Track | None:
    if not isinstance(item, dict):
        return None

    # Spotify playlist items can arrive either as {"track": {...}} or
    # as {"item": {...}} depending on the endpoint/response shape.
    track = item.get("track") or item.get("item")
    if not track or not track.get("id"):
        return None
    artist = track["artists"][0]["name"] if track.get("artists") else "Unknown"
    album = track.get("album", {}).get("name", "") if isinstance(track.get("album"), dict) else ""
    return Track(
        title=track["name"],
        artist=artist,
        duration_ms=track["duration_ms"],
        spotify_id=track["id"],
        album=album,
        explicit=bool(track.get("explicit", False)),
        popularity=int(track.get("popularity", 0) or 0),
    )


def _shuffle_if_needed(config: StationConfig, tracks: list[Track]) -> list[Track]:
    if config.playlist.shuffle:
        random.shuffle(tracks)
    return tracks


def _demo_source() -> PlaylistSource:
    return PlaylistSource(
        kind="demo",
        source_id="demo",
        label="Built-in modern Italian demo mix",
        track_count=len(DEMO_TRACKS),
        selected_at=time.time(),
        url="",
    )


def _charts_source(track_count: int) -> PlaylistSource:
    return PlaylistSource(
        kind="charts",
        source_id="apple_music_it_top_50",
        label="Current Italian charts",
        track_count=track_count,
        selected_at=time.time(),
        url=_APPLE_MUSIC_IT_CHARTS_URL,
    )


def _fetch_current_italy_charts(limit: int = 20) -> list[Track]:
    """Fetch a live Top Songs Italy list from Apple Music charts RSS."""
    try:
        with urlopen(_APPLE_MUSIC_IT_CHARTS_URL, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Live charts fetch failed: %s", exc)
        return []

    results = payload.get("feed", {}).get("results", [])
    tracks: list[Track] = []
    for item in results[:limit]:
        title = str(item.get("name", "")).strip()
        artist = str(item.get("artistName", "")).strip()
        item_id = str(item.get("id", "")).strip()
        if not title or not artist:
            continue
        tracks.append(
            Track(
                title=title,
                artist=artist,
                duration_ms=210000,
                spotify_id=f"chart_{item_id or len(tracks) + 1}",
            )
        )
    return tracks


def _build_source(
    kind: str,
    *,
    source_id: str = "",
    url: str = "",
    label: str = "",
    track_count: int = 0,
) -> PlaylistSource:
    return PlaylistSource(
        kind=kind,
        source_id=source_id,
        url=url,
        label=label,
        track_count=track_count,
        selected_at=time.time(),
    )


def _resolve_loaded_source(
    config: StationConfig,
    *,
    kind: str,
    tracks: list[Track],
    empty_message: str,
    source_id: str = "",
    url: str = "",
    label: str = "",
) -> tuple[list[Track], PlaylistSource]:
    if not tracks:
        raise ExplicitSourceError(empty_message)
    resolved = _build_source(
        kind,
        source_id=source_id,
        url=url,
        label=label,
        track_count=len(tracks),
    )
    return _shuffle_if_needed(config, tracks), resolved


def _load_playlist_source(
    config: StationConfig,
    *,
    kind: str,
    playlist_id: str,
    url: str = "",
    label: str = "",
    error_message: str,
) -> tuple[list[Track], PlaylistSource]:
    sp = _get_spotify_oauth(config)
    try:
        tracks, resolved_label = _fetch_tracks_from_playlist_id(sp, playlist_id)
    except Exception as exc:
        raise ExplicitSourceError(f"{error_message}: {exc}") from exc
    return _resolve_loaded_source(
        config,
        kind=kind,
        tracks=tracks,
        empty_message="Selected playlist returned zero playable tracks",
        source_id=playlist_id,
        url=url,
        label=label or resolved_label,
    )


def _load_liked_songs_source(
    config: StationConfig,
    *,
    source_id: str = "liked_songs",
    label: str = "Liked Songs",
) -> tuple[list[Track], PlaylistSource]:
    sp = _get_spotify_oauth(config)
    try:
        tracks = _fetch_liked_tracks(sp)
    except Exception as exc:
        raise ExplicitSourceError(f"Failed to load liked songs: {exc}") from exc
    return _resolve_loaded_source(
        config,
        kind="liked_songs",
        tracks=tracks,
        empty_message="Liked Songs returned zero playable tracks",
        source_id=source_id,
        label=label,
    )


def read_persisted_source(cache_dir: Path) -> PlaylistSource | None:
    """Read the last selected playlist source from cache, if present."""
    path = cache_dir / PERSISTED_SOURCE_FILENAME
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Persisted playlist source is unreadable: %s", path)
        return None

    if not isinstance(payload, dict) or not payload.get("kind"):
        return None

    try:
        return PlaylistSource(
            kind=str(payload.get("kind", "")),
            source_id=str(payload.get("source_id", "")),
            url=str(payload.get("url", "")),
            label=str(payload.get("label", "")),
            track_count=int(payload.get("track_count", 0) or 0),
            selected_at=float(payload.get("selected_at", 0.0) or 0.0),
        )
    except (TypeError, ValueError):
        logger.warning("Persisted playlist source has invalid fields: %s", path)
        return None


def write_persisted_source(cache_dir: Path, source: PlaylistSource) -> None:
    """Persist the last selected playlist source to cache (atomic write)."""
    path = cache_dir / PERSISTED_SOURCE_FILENAME
    payload = {
        "kind": source.kind,
        "source_id": source.source_id,
        "url": source.url,
        "label": source.label,
        "track_count": source.track_count,
        "selected_at": source.selected_at,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def _fetch_tracks_from_playlist_id(sp, playlist_id: str) -> tuple[list[Track], str]:
    tracks: list[Track] = []
    playlist_meta = sp.playlist(playlist_id, fields="name")
    label = playlist_meta.get("name") or "Spotify playlist"
    results = sp.playlist_tracks(playlist_id)
    while results and len(tracks) < _MAX_PLAYLIST_TRACKS:
        for item in results.get("items", []):
            track = _track_from_spotify_item(item)
            if track:
                tracks.append(track)
        results = sp.next(results) if results.get("next") else None
    return tracks[:_MAX_PLAYLIST_TRACKS], label


def _fetch_liked_tracks(sp, max_tracks: int = 200) -> list[Track]:
    tracks: list[Track] = []
    offset = 0
    limit = 50
    while offset < max_tracks:
        results = sp.current_user_saved_tracks(limit=limit, offset=offset)
        if not results.get("items"):
            break
        for item in results["items"]:
            track = _track_from_spotify_item(item)
            if track:
                tracks.append(track)
        offset += limit
        if not results.get("next"):
            break
    return tracks


def supports_user_sources(config: StationConfig) -> bool:
    """Return True when the run mode supports Spotify source picker (local/macOS only)."""
    return not config.is_addon and not Path("/.dockerenv").exists()


_MAX_PLAYLISTS = 500
_MAX_PLAYLIST_TRACKS = 500


def list_user_playlists(config: StationConfig, limit: int = 50) -> list[dict]:
    """List available user playlists for explicit source selection (capped at 500)."""
    if not supports_user_sources(config):
        return []
    sp = _get_spotify_oauth(config)
    playlists: list[dict] = []
    results = sp.current_user_playlists(limit=limit)
    while results and len(playlists) < _MAX_PLAYLISTS:
        for item in results.get("items", []):
            if not item or not item.get("id"):
                continue
            track_total = item.get("tracks", {}).get("total") or item.get("items", {}).get("total") or 0
            playlists.append(
                {
                    "id": item["id"],
                    "label": item.get("name") or "Spotify playlist",
                    "track_count": track_total,
                }
            )
        results = sp.next(results) if results.get("next") else None
    return playlists[:_MAX_PLAYLISTS]


def load_explicit_source(config: StationConfig, source: PlaylistSource) -> tuple[list[Track], PlaylistSource]:
    """Load a user-chosen source without any silent fallback."""
    if source.kind == "url":
        playlist_id = _extract_playlist_id(source.url)
        if not playlist_id:
            raise ExplicitSourceError("Playlist URL is not a valid Spotify playlist link")
        return _load_playlist_source(
            config,
            kind="url",
            playlist_id=playlist_id,
            url=source.url,
            label=source.label,
            error_message="Failed to load playlist URL",
        )

    if source.kind == "playlist":
        if not source.source_id:
            raise ExplicitSourceError("Playlist source_id is required")
        return _load_playlist_source(
            config,
            kind="playlist",
            playlist_id=source.source_id,
            label=source.label,
            error_message="Failed to load selected playlist",
        )

    if source.kind == "liked_songs":
        return _load_liked_songs_source(
            config,
            source_id=source.source_id or "liked_songs",
            label=source.label or "Liked Songs",
        )

    if source.kind == "demo":
        tracks = _shuffle_if_needed(config, list(DEMO_TRACKS))
        resolved = _demo_source()
        resolved.track_count = len(tracks)
        return tracks, resolved

    if source.kind == "charts":
        tracks = _shuffle_if_needed(config, _fetch_current_italy_charts())
        if not tracks:
            raise ExplicitSourceError("Current Italian charts are temporarily unavailable")
        resolved = _charts_source(len(tracks))
        return tracks, resolved

    raise ExplicitSourceError(f"Unsupported source kind: {source.kind}")


def fetch_startup_playlist(
    config: StationConfig, persisted_source: PlaylistSource | None = None
) -> tuple[list[Track], PlaylistSource, str]:
    """Load the startup playlist, degrading to demo when necessary."""
    if persisted_source:
        try:
            tracks, source = load_explicit_source(config, persisted_source)
            return tracks, source, ""
        except ExplicitSourceError as exc:
            logger.warning("Persisted source restore failed: %s", exc)
            error = str(exc)
        else:
            error = ""
    else:
        error = ""

    charts_allowed = os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes")

    if not config.spotify_client_id or not config.spotify_client_secret:
        if charts_allowed:
            chart_tracks = _shuffle_if_needed(config, _fetch_current_italy_charts())
            if chart_tracks:
                logger.warning("No Spotify credentials — using live Italian charts fallback")
                return chart_tracks, _charts_source(len(chart_tracks)), error or "Spotify credentials are missing"
        logger.warning("No Spotify credentials — using built-in modern Italian demo mix")
        tracks = _shuffle_if_needed(config, list(DEMO_TRACKS))
        return tracks, _demo_source(), error or "Spotify credentials are missing"

    playlist_id = _extract_playlist_id(config.playlist.spotify_url) if config.playlist.spotify_url else None

    if playlist_id:
        try:
            tracks, source = _load_playlist_source(
                config,
                kind="url",
                playlist_id=playlist_id,
                url=config.playlist.spotify_url,
                error_message="Failed to load playlist URL",
            )
            return tracks, source, error
        except Exception as exc:
            logger.warning("Playlist fetch failed (%s) — falling back to liked songs", exc)
            error = str(exc)

    try:
        logger.info("Fetching liked songs from Spotify...")
        tracks, source = _load_liked_songs_source(config)
        return tracks, source, error
    except Exception as exc:
        logger.warning("Liked songs fetch failed (%s) — using demo playlist", exc)
        error = str(exc)

    if charts_allowed:
        chart_tracks = _shuffle_if_needed(config, _fetch_current_italy_charts())
        if chart_tracks:
            logger.warning("No Spotify tracks available — using live Italian charts fallback")
            return chart_tracks, _charts_source(len(chart_tracks)), error or "No Spotify tracks available"

    logger.warning("No Spotify tracks available — using demo playlist")
    tracks = _shuffle_if_needed(config, list(DEMO_TRACKS))
    return tracks, _demo_source(), error or "No Spotify tracks available"


def fetch_playlist(config: StationConfig) -> list[Track]:
    """Legacy wrapper that preserves URL -> liked songs -> demo fallback behavior."""
    tracks, _, _ = fetch_startup_playlist(config)
    return tracks
