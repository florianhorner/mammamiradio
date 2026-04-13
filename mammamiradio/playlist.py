"""Playlist loading from charts, local files, or bundled demo tracks."""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from mammamiradio.config import StationConfig
from mammamiradio.models import PlaylistSource, Track

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
_APPLE_MUSIC_IT_CHARTS_URL = "https://rss.applemarketingtools.com/api/v2/it/music/most-played/100/songs.json"


class ExplicitSourceError(RuntimeError):
    """Raised when an explicit user-selected source cannot be loaded."""


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


def _load_local_music_tracks(music_dir: Path) -> list[Track]:
    """Return Track objects built from MP3 files found in music_dir.

    File names are parsed as ``Artist - Title.mp3`` when a hyphen is present;
    otherwise the stem is used as the title with artist "Unknown".  Silently
    returns an empty list if the directory does not exist or contains no MP3s.
    """
    _max_local_tracks = 200
    if not music_dir.exists():
        return []
    tracks: list[Track] = []
    all_mp3s = sorted(music_dir.glob("*.mp3"))
    if len(all_mp3s) > _max_local_tracks:
        logger.warning(
            "music/ contains %d MP3s; capping at %d to avoid blocking the event loop",
            len(all_mp3s),
            _max_local_tracks,
        )
        all_mp3s = all_mp3s[:_max_local_tracks]
    for mp3 in all_mp3s:
        stem = mp3.stem.strip()
        if " - " in stem:
            artist_part, title_part = stem.split(" - ", 1)
        else:
            artist_part, title_part = "Unknown", stem
        track_id = f"local_{mp3.stem.lower().replace(' ', '_')}"
        tracks.append(
            Track(
                title=title_part.strip(),
                artist=artist_part.strip(),
                duration_ms=210000,
                spotify_id=track_id,
            )
        )
    return tracks


def _normalized_track_key(track: Track) -> tuple[str, str]:
    return (track.artist.strip().lower(), track.title.strip().lower())


def _merge_local_music_tracks(chart_tracks: list[Track], local_tracks: list[Track]) -> int:
    """Append non-duplicate local tracks to chart tracks and return merged count."""
    existing_keys = {_normalized_track_key(t) for t in chart_tracks}
    merged = 0
    for local_track in local_tracks:
        track_key = _normalized_track_key(local_track)
        if track_key in existing_keys:
            continue
        chart_tracks.append(local_track)
        existing_keys.add(track_key)
        merged += 1
    return merged


def _load_chart_source_tracks(config: StationConfig) -> list[Track]:
    """Load chart tracks and blend local music/ tracks, then shuffle if configured."""
    chart_tracks = list(_fetch_current_italy_charts())
    local_tracks = _load_local_music_tracks(Path("music"))
    if local_tracks:
        merged_count = _merge_local_music_tracks(chart_tracks, local_tracks)
        logger.info(
            "Merged %d/%d local music/ tracks into chart playlist",
            merged_count,
            len(local_tracks),
        )
    return _shuffle_if_needed(config, chart_tracks)


def _fetch_current_italy_charts(limit: int = 100, max_per_artist: int = 2) -> list[Track]:
    """Fetch a live Top Songs Italy list from Apple Music charts RSS."""
    try:
        with urlopen(_APPLE_MUSIC_IT_CHARTS_URL, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Live charts fetch failed: %s", exc)
        return []

    results = payload.get("feed", {}).get("results", [])
    tracks: list[Track] = []
    artist_counts: dict[str, int] = {}
    for item in results:
        if len(tracks) >= limit:
            break
        title = str(item.get("name", "")).strip()
        artist = str(item.get("artistName", "")).strip()
        item_id = str(item.get("id", "")).strip()
        if not title or not artist:
            continue
        # Cap tracks per artist to ensure variety across the playlist
        artist_key = artist.lower()
        if artist_counts.get(artist_key, 0) >= max_per_artist:
            continue
        artist_counts[artist_key] = artist_counts.get(artist_key, 0) + 1
        tracks.append(
            Track(
                title=title,
                artist=artist,
                duration_ms=210000,
                spotify_id=f"chart_{item_id or len(tracks) + 1}",
            )
        )
    return tracks


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


def load_explicit_source(config: StationConfig, source: PlaylistSource) -> tuple[list[Track], PlaylistSource]:
    """Load a user-chosen source without any silent fallback."""
    if source.kind == "demo":
        tracks = _shuffle_if_needed(config, list(DEMO_TRACKS))
        resolved = _demo_source()
        resolved.track_count = len(tracks)
        return tracks, resolved

    if source.kind in ("charts", "url"):
        # "url" kind comes from /api/playlist/load — treat as charts reload
        tracks = _load_chart_source_tracks(config)
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

    charts_allowed = config.allow_ytdlp

    if charts_allowed:
        chart_tracks = _load_chart_source_tracks(config)
        if chart_tracks:
            logger.info("Using live Italian charts (%d tracks total)", len(chart_tracks))
            return chart_tracks, _charts_source(len(chart_tracks)), error

    local_present = any(Path("music").glob("*.mp3"))
    if local_present:
        logger.warning(
            "Local music/ files found but MAMMAMIRADIO_ALLOW_YTDLP is not set — "
            "set it to 'true' to blend local tracks into the playlist"
        )
    logger.info("Using built-in modern Italian demo mix")
    tracks = _shuffle_if_needed(config, list(DEMO_TRACKS))
    return tracks, _demo_source(), error


def fetch_playlist(config: StationConfig) -> list[Track]:
    """Legacy wrapper that preserves charts -> demo fallback behavior."""
    tracks, _, _ = fetch_startup_playlist(config)
    return tracks


def fetch_chart_refresh(existing_ids: set[str]) -> list[Track]:
    """Fetch the latest Italian charts and return only tracks not already in the playlist.

    Used for mid-session playlist refreshes: merges new chart entries into a
    live session without restarting the producer or resetting play history.
    Returns an empty list if the fetch fails or produces no new tracks.
    """
    fresh = _fetch_current_italy_charts()
    return [t for t in fresh if t.spotify_id not in existing_ids]
