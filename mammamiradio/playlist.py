"""Playlist loading from charts, local files, or bundled demo tracks."""

from __future__ import annotations

import json
import logging
import os
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
_APPLE_MUSIC_IT_CHARTS_URL = "https://rss.applemarketingtools.com/api/v2/it/music/most-played/50/songs.json"


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

    charts_allowed = os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes")

    if charts_allowed:
        chart_tracks = _shuffle_if_needed(config, _fetch_current_italy_charts())
        if chart_tracks:
            logger.info("Using live Italian charts")
            return chart_tracks, _charts_source(len(chart_tracks)), error

    logger.info("Using built-in modern Italian demo mix")
    tracks = _shuffle_if_needed(config, list(DEMO_TRACKS))
    return tracks, _demo_source(), error


def fetch_playlist(config: StationConfig) -> list[Track]:
    """Legacy wrapper that preserves charts -> demo fallback behavior."""
    tracks, _, _ = fetch_startup_playlist(config)
    return tracks
