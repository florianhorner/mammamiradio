"""Playlist loading from charts, local files, or bundled demo tracks."""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import PlaylistSource, Track

_DEMO_ASSETS_MUSIC_DIR = Path(__file__).resolve().parent.parent / "assets" / "demo" / "music"
_JAMENDO_API_BASE_URL = "https://api.jamendo.com/v3.0/tracks/"
_JAMENDO_REQUIRED_PARAMS = {
    "cc_commercial": "1",
    "cc_sharealike": "0",
    "audioformat": "mp32",
    "include": "musicinfo",
}

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


def _copy_tracks_with_source(
    tracks: list[Track], source: Literal["youtube", "jamendo", "local", "demo"]
) -> list[Track]:
    """Return copies with a consistent source label for playlist-loaded tracks."""
    return [replace(track, source=source) for track in tracks]


def _load_demo_asset_tracks() -> list[Track]:
    """Return Track objects for MP3s bundled in demo_assets/music/.

    Files are expected to be named ``Artist - Title.mp3`` or ``Title.mp3``.
    The downloader finds them via title/cache_key substring match.
    """
    if not _DEMO_ASSETS_MUSIC_DIR.exists():
        return []
    tracks: list[Track] = []
    for mp3 in sorted(_DEMO_ASSETS_MUSIC_DIR.glob("*.mp3")):
        stem = mp3.stem.strip()
        if " - " in stem:
            artist_part, title_part = stem.split(" - ", 1)
        else:
            artist_part, title_part = "Unknown", stem
        tracks.append(
            Track(
                title=title_part.strip(),
                artist=artist_part.strip(),
                duration_ms=210000,
                spotify_id=f"demo_asset_{mp3.stem.lower().replace(' ', '_')}",
                local_path=mp3,
                source="demo",
            )
        )
    return tracks


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


def _local_source(track_count: int) -> PlaylistSource:
    return PlaylistSource(
        kind="local",
        source_id="local_music_dir",
        label="Local music/ files",
        track_count=track_count,
        selected_at=time.time(),
        url="",
    )


def _charts_source(track_count: int) -> PlaylistSource:
    return PlaylistSource(
        kind="charts",
        source_id="apple_music_it_top_100",
        label="Current Italian charts",
        track_count=track_count,
        selected_at=time.time(),
        url=_APPLE_MUSIC_IT_CHARTS_URL,
    )


def _jamendo_request_url(*, tags: str, country: str = "", order: str = "") -> str:
    params: dict[str, str] = {"tags": tags}
    if country:
        params["country"] = country
    if order:
        params["order"] = order
    return f"jamendo://playlist?{urlencode(params)}"


def _jamendo_source(track_count: int, *, tags: str, country: str = "", order: str = "") -> PlaylistSource:
    label_parts: list[str] = ["Jamendo CC Music"]
    if country:
        label_parts.append(country)
    label_parts.append(f"({tags})")
    if order:
        label_parts.append(f"[{order}]")
    return PlaylistSource(
        kind="jamendo",
        source_id=tags,
        label=" ".join(label_parts),
        track_count=track_count,
        selected_at=time.time(),
        url=_jamendo_request_url(tags=tags, country=country, order=order),
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
                local_path=mp3,
                source="local",
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
    """Load chart tracks and blend local music/ tracks, then shuffle if configured.

    Local MP3s are an enrichment of the charts source, not a fallback. If the
    charts API returns zero tracks (outage, blocked region, scheme mismatch),
    return an empty list — do NOT silently substitute local files under the
    "charts" label. Callers handle the empty result:
      - load_explicit_source() raises ExplicitSourceError (honoring its
        "no silent fallback" contract)
      - fetch_startup_playlist() falls through to Jamendo / local / demo
        tiers, which correctly label the source.
    """
    chart_tracks = list(_fetch_current_italy_charts())
    if not chart_tracks:
        return []
    local_tracks = _copy_tracks_with_source(_load_local_music_tracks(Path("music")), "local")
    if local_tracks:
        merged_count = _merge_local_music_tracks(chart_tracks, local_tracks)
        logger.info(
            "Merged %d/%d local music/ tracks into chart playlist",
            merged_count,
            len(local_tracks),
        )
    return _shuffle_if_needed(config, chart_tracks)


# Markers that reliably indicate a chart entry is NOT music (podcast, comedy,
# audiobook, interview, etc.). Conservative list — each marker must be something
# that would almost never appear in a legitimate song title or artist name.
_NON_MUSIC_MARKERS: tuple[str, ...] = (
    "podcast",
    "bbc comedy",
    "bbc studios",
    "audiobook",
    "audio book",
    "interview with",
    "interview -",
    "tutorial",
    "how to ",
    "how-to ",
    "lecture",
    "documentary",
    "radio drama",
    "audio drama",
    "sleep story",
    "meditation guided",
    "asmr ",
    "news briefing",
    "news roundup",
)


def _is_plausible_music_title(title: str, artist: str) -> bool:
    """Conservative heuristic to reject obvious non-music chart entries.

    Apple Music's Italian chart sometimes surfaces BBC comedy, podcasts, or
    audiobooks. Playing them breaks the radio illusion harder than anything
    else. Reject these at ingest so they never enter the queue.

    Filter is deliberately narrow — only rejects markers almost never found in
    real song titles so valid tracks are never dropped.
    """
    if not title or not artist:
        return False
    if len(title) > 150 or len(artist) > 100:
        return False
    haystack = f"{title}  {artist}".lower()
    return not any(marker in haystack for marker in _NON_MUSIC_MARKERS)


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
    rejected = 0
    for item in results:
        if len(tracks) >= limit:
            break
        title = str(item.get("name", "")).strip()
        artist = str(item.get("artistName", "")).strip()
        item_id = str(item.get("id", "")).strip()
        if not title or not artist:
            continue
        if not _is_plausible_music_title(title, artist):
            rejected += 1
            logger.info("Rejecting non-music chart entry: %s - %s", artist, title)
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
                source="youtube",
            )
        )
    if rejected:
        logger.info("Chart ingest: filtered %d non-music entries", rejected)
    return tracks


def _jamendo_tags(config: StationConfig, source: PlaylistSource | None = None) -> str:
    if source is not None:
        persisted_tags = source.source_id.strip()
        if persisted_tags:
            return persisted_tags
        parsed = urlparse(source.url or "")
        if parsed.scheme == "jamendo":
            tags = parse_qs(parsed.query).get("tags", [""])[0].strip()
            if tags:
                return tags
    return (config.playlist.jamendo_tags or "pop").strip() or "pop"


def _jamendo_country(config: StationConfig, source: PlaylistSource | None = None) -> str:
    if source is not None:
        parsed = urlparse(source.url or "")
        if parsed.scheme == "jamendo":
            country = parse_qs(parsed.query).get("country", [""])[0].strip()
            if country:
                return country
    return (config.playlist.jamendo_country or "").strip()


def _jamendo_order(config: StationConfig, source: PlaylistSource | None = None) -> str:
    if source is not None:
        parsed = urlparse(source.url or "")
        if parsed.scheme == "jamendo":
            order = parse_qs(parsed.query).get("order", [""])[0].strip()
            if order:
                return order
    return (config.playlist.jamendo_order or "").strip()


def _build_jamendo_url(
    client_id: str,
    *,
    tags: str,
    country: str = "",
    order: str = "",
    limit: int = 50,
) -> str:
    params: dict[str, str] = {
        "client_id": client_id,
        "format": "json",
        "limit": str(limit),
        "tags": tags,
        **_JAMENDO_REQUIRED_PARAMS,
    }
    if country:
        params["country"] = country
    if order:
        params["order"] = order
    return f"{_JAMENDO_API_BASE_URL}?{urlencode(params)}"


def _fetch_jamendo_playlist(
    config: StationConfig,
    *,
    tags: str | None = None,
    country: str | None = None,
    order: str | None = None,
    limit: int = 50,
) -> list[Track]:
    """Fetch a Creative Commons playlist from Jamendo."""
    client_id = (config.playlist.jamendo_client_id or "").strip()
    if not client_id:
        return []

    requested_tags = (tags or config.playlist.jamendo_tags or "pop").strip() or "pop"
    requested_country = (country if country is not None else config.playlist.jamendo_country or "").strip()
    requested_order = (order if order is not None else config.playlist.jamendo_order or "").strip()
    url = _build_jamendo_url(
        client_id,
        tags=requested_tags,
        country=requested_country,
        order=requested_order,
        limit=limit,
    )
    try:
        with urlopen(url, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Jamendo playlist fetch failed: %s", exc)
        return []

    results = payload.get("results", [])
    tracks: list[Track] = []
    for item in results:
        track_id = str(item.get("id", "")).strip()
        title = str(item.get("name", "")).strip()
        artist = str(item.get("artist_name", "")).strip()
        # Use audiodownload only — this is the CC-licensed download URL.
        # The `audio` field is a streaming-only URL without download rights.
        direct_url = str(item.get("audiodownload") or "").strip()
        if not track_id or not title or not artist or not direct_url:
            continue
        if not direct_url.startswith("https://"):
            logger.warning("Jamendo track %s has non-https direct_url, skipping", track_id)
            continue
        try:
            duration_ms = int(float(item.get("duration", 0) or 0) * 1000)
        except (TypeError, ValueError):
            duration_ms = 0
        jamendo_id = f"jamendo_{track_id}"
        tracks.append(
            Track(
                title=title,
                artist=artist,
                duration_ms=duration_ms or 210000,
                spotify_id=jamendo_id,
                youtube_id="",
                direct_url=direct_url,
                album_art=str(item.get("image", "") or ""),
                album=str(item.get("album_name", "") or ""),
                source="jamendo",
            )
        )

    if not tracks:
        logger.info("Jamendo returned zero playable tracks for tags '%s'", requested_tags)
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
        kind = str(payload.get("kind", ""))
        source_id = str(payload.get("source_id", ""))
        # Transparent migration: the charts source_id had a numerically wrong
        # suffix ("_top_50") even though the URL fetches up to 100 tracks.
        # Old caches from before the rename are remapped on load so operators
        # never see a Jamendo/charts mismatch warning.
        if kind == "charts" and source_id == "apple_music_it_top_50":
            source_id = "apple_music_it_top_100"
        return PlaylistSource(
            kind=kind,
            source_id=source_id,
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
        demo_asset_tracks = _copy_tracks_with_source(_load_demo_asset_tracks(), "demo")
        if demo_asset_tracks:
            tracks = _shuffle_if_needed(config, demo_asset_tracks)
        else:
            tracks = _shuffle_if_needed(config, _copy_tracks_with_source(list(DEMO_TRACKS), "demo"))
        resolved = _demo_source()
        resolved.track_count = len(tracks)
        return tracks, resolved

    is_jamendo_request = source.kind == "jamendo" or (
        source.kind == "url" and urlparse(source.url or "").scheme == "jamendo"
    )
    if is_jamendo_request:
        client_id = (config.playlist.jamendo_client_id or "").strip()
        if not client_id:
            raise ExplicitSourceError("Jamendo source is not configured")
        tags = _jamendo_tags(config, source)
        country = _jamendo_country(config, source)
        order = _jamendo_order(config, source)
        tracks = _shuffle_if_needed(
            config,
            _copy_tracks_with_source(
                _fetch_jamendo_playlist(config, tags=tags, country=country, order=order),
                "jamendo",
            ),
        )
        if not tracks:
            raise ExplicitSourceError("Jamendo playlist is temporarily unavailable")
        resolved = _jamendo_source(len(tracks), tags=tags, country=country, order=order)
        return tracks, resolved

    if source.kind in ("charts", "url"):
        # "url" kind comes from /api/playlist/load — treat as charts reload
        tracks = _load_chart_source_tracks(config)
        if not tracks:
            raise ExplicitSourceError("Current Italian charts are temporarily unavailable")
        resolved = _charts_source(len(tracks))
        return tracks, resolved

    if source.kind == "local":
        # Symmetry: matches the auto-degrade `local` source kind in
        # fetch_startup_playlist. Currently no write path persists a local
        # source, so this branch is defensive — it ensures a future cache
        # file or admin-API change can restore the user's `music/` selection
        # explicitly without falling through to ExplicitSourceError.
        local_tracks = _copy_tracks_with_source(_load_local_music_tracks(Path("music")), "local")
        if not local_tracks:
            raise ExplicitSourceError("No MP3 files found in the music/ directory")
        tracks = _shuffle_if_needed(config, local_tracks)
        return tracks, _local_source(len(tracks))

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

    jamendo_client_id = (config.playlist.jamendo_client_id or "").strip()
    if jamendo_client_id:
        tags = _jamendo_tags(config)
        country = _jamendo_country(config)
        order = _jamendo_order(config)
        jamendo_tracks = _shuffle_if_needed(
            config,
            _copy_tracks_with_source(
                _fetch_jamendo_playlist(config, tags=tags, country=country, order=order),
                "jamendo",
            ),
        )
        if jamendo_tracks:
            logger.info(
                "Using Jamendo CC playlist (%d tracks, tags=%s, country=%s, order=%s)",
                len(jamendo_tracks),
                tags,
                country or "any",
                order or "default",
            )
            return (
                jamendo_tracks,
                _jamendo_source(len(jamendo_tracks), tags=tags, country=country, order=order),
                error,
            )

    # Local music/ files are a real source on their own — they don't need yt-dlp
    # (yt-dlp only matters for downloading chart tracks). When the operator has
    # dropped MP3s into music/, honor that intent even if MAMMAMIRADIO_ALLOW_YTDLP
    # is off and Jamendo isn't configured. This used to be a warn-and-skip,
    # which silently fell through to bundled demo assets and ignored the
    # operator's actual files.
    local_tracks = _copy_tracks_with_source(_load_local_music_tracks(Path("music")), "local")
    if local_tracks:
        logger.info("Using local music/ files (%d tracks)", len(local_tracks))
        shuffled = _shuffle_if_needed(config, local_tracks)
        return shuffled, _local_source(len(shuffled)), error

    # Prefer real bundled MP3s over metadata-only demo placeholders.
    # When demo_assets/music/ contains actual files, the queue fills with
    # real audio instead of generated silence.
    demo_asset_tracks = _copy_tracks_with_source(_load_demo_asset_tracks(), "demo")
    if demo_asset_tracks:
        logger.info("Using bundled demo assets (%d tracks)", len(demo_asset_tracks))
        tracks = _shuffle_if_needed(config, demo_asset_tracks)
        src = _demo_source()
        src.track_count = len(tracks)
        return tracks, src, error

    logger.info("Using built-in modern Italian demo mix")
    tracks = _shuffle_if_needed(config, _copy_tracks_with_source(list(DEMO_TRACKS), "demo"))
    return tracks, _demo_source(), error


def fetch_chart_refresh(existing_ids: set[str]) -> list[Track]:
    """Fetch the latest Italian charts and return only tracks not already in the playlist.

    Used for mid-session playlist refreshes: merges new chart entries into a
    live session without restarting the producer or resetting play history.
    Returns an empty list if the fetch fails or produces no new tracks.
    """
    fresh = _fetch_current_italy_charts()
    return [t for t in fresh if t.spotify_id not in existing_ids]
