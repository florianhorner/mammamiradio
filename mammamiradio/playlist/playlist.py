"""Playlist loading from the attributed starter catalog and operator sources."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from itertools import islice
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import Heading, PlaylistSource, SourceReadinessEvidence, Track
from mammamiradio.core.models import normalized_track_key as _core_normalized_track_key
from mammamiradio.playlist.cover_art import upscale_itunes_artwork

_DEMO_ASSETS_RECOVERY_DIR = Path(__file__).resolve().parent.parent / "assets" / "demo" / "recovery"
_MAX_LOCAL_TRACKS = 200
_MAX_LOCAL_DIRECTORY_ENTRIES = 4096
_CLASSIC_ERA_QUERIES: dict[str, tuple[str, int]] = {
    "70s": ("cantautori italiani anni 70 lucio battisti fabrizio de andre", 1975),
    "80s": ("canzoni italiane anni 80 vasco rossi eros ramazzotti celentano", 1985),
    "90s": ("canzoni italiane anni 90 laura pausini ligabue zucchero", 1995),
}

logger = logging.getLogger(__name__)

# Compatibility import for older callers. The unlicensed metadata-only demo
# catalog is intentionally gone; runtime starter tracks derive solely from the
# canonical attributed manifest once its evidence gate is complete.
DEMO_TRACKS: list[Track] = []

PERSISTED_SOURCE_FILENAME = "playlist_source.json"
PERSISTED_HEADING_FILENAME = "heading.json"
_APPLE_MUSIC_IT_CHARTS_URL = "https://rss.applemarketingtools.com/api/v2/it/music/most-played/100/songs.json"


class ExplicitSourceError(RuntimeError):
    """Raised when an explicit user-selected source cannot be loaded."""


class LegacyJamendoSourceRetiredError(ExplicitSourceError):
    """Raised for the removed persistent/download-style Jamendo source."""


class ExternalMediaUnavailableError(ExplicitSourceError):
    """Raised when an explicit source requires the optional extractor."""


def _copy_tracks_with_source(
    tracks: list[Track], source: Literal["youtube", "jamendo", "local", "demo", "classic", "starter"]
) -> list[Track]:
    """Return copies with a consistent source label for playlist-loaded tracks."""
    return [replace(track, source=source) for track in tracks]


def _source_evidence_for_config(config: StationConfig) -> SourceReadinessEvidence:
    """Create bounded configuration evidence before source attempts begin."""
    evidence = SourceReadinessEvidence()
    evidence.configure("charts", config.allow_ytdlp)
    evidence.configure("jamendo", bool((config.playlist.jamendo_client_id or "").strip()))
    evidence.configure("local", config.music_dir.exists())
    evidence.configure("demo", True)
    recovery_bundled = _DEMO_ASSETS_RECOVERY_DIR.exists() and any(islice(_DEMO_ASSETS_RECOVERY_DIR.glob("*.mp3"), 1))
    evidence.configure("recovery", recovery_bundled, bundled=recovery_bundled)
    return evidence


def _attach_source_evidence(
    source: PlaylistSource,
    tracks: Sequence[Track],
    evidence: SourceReadinessEvidence,
) -> PlaylistSource:
    evidence.set_current_rotation(source.kind, source.label)
    evidence.observe_tracks(tracks)
    if evidence.advanced is not None:
        evidence.mark_advanced_candidates(len(tracks))
    source.readiness_evidence = evidence
    return source


def _shuffle_if_needed(config: StationConfig, tracks: list[Track]) -> list[Track]:
    if config.playlist.shuffle:
        random.shuffle(tracks)
    return tracks


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


def _classic_italian_source(era: str, track_count: int) -> PlaylistSource:
    return PlaylistSource(
        kind="classic",
        source_id=era,
        label=f"Classici italiani anni '{era[:2]}",
        track_count=track_count,
        selected_at=time.time(),
        url=f"classic://italian/{era}",
    )


def _parse_classic_artist_title(video_title: str) -> tuple[str, str] | None:
    """Parse common `Artist - Title` YouTube titles for classic-era searches."""
    title = video_title.strip()
    for separator in (" - ", " – "):
        if separator not in title:
            continue
        artist, song_title = title.split(separator, 1)
        artist = artist.strip()
        song_title = song_title.strip()
        if artist and song_title:
            return artist, song_title
    return None


def _classic_era_from_source(source: PlaylistSource) -> str:
    source_id = (source.source_id or "").strip()
    url_era = ""
    if source.url:
        parsed = urlparse(source.url)
        url_era = parsed.path.strip("/").split("/")[-1].strip()
    era = url_era or source_id or "80s"
    if era not in _CLASSIC_ERA_QUERIES:
        raise ExplicitSourceError(f"Unsupported classic Italian era: {era}")
    return era


def _load_classic_italian_tracks(era: str) -> list[Track]:
    """Load an era-themed Italian playlist through lightweight yt-dlp search."""
    from mammamiradio.playlist.downloader import _ytdlp_enabled, search_ytdlp_metadata

    if not _ytdlp_enabled():
        return []
    query, year_hint = _CLASSIC_ERA_QUERIES[era]
    results = search_ytdlp_metadata(query, max_results=20)
    tracks: list[Track] = []
    for item in results:
        video_title = str(item.get("title") or "").strip()
        parsed_title = _parse_classic_artist_title(video_title)
        artist = str(item.get("artist") or "").strip()
        title = video_title
        if parsed_title:
            artist, title = parsed_title
        if not title:
            continue
        tracks.append(
            Track(
                title=title,
                artist=artist or "Artista italiano",
                duration_ms=int(item.get("duration_ms") or 210000),
                spotify_id=f"classic_{era}_{item.get('youtube_id') or len(tracks) + 1}",
                youtube_id=str(item.get("youtube_id") or ""),
                album_art=str(item.get("album_art") or ""),
                year=year_hint,
                source="classic",
            )
        )
    return tracks


def _load_local_music_tracks(music_dir: Path) -> list[Track]:
    """Return Track objects built from MP3 files found in music_dir.

    File names are parsed as ``Artist - Title.mp3`` when a hyphen is present;
    otherwise the stem is used as the title with artist "Unknown".  Silently
    returns an empty list if the directory does not exist or contains no MP3s.
    """
    if not music_dir.exists():
        return []
    tracks: list[Track] = []
    # Bound raw enumeration before checking extensions. ``Path.glob("*.mp3")``
    # can still walk every entry when a mounted directory is huge but contains
    # few songs, turning first startup into an unbounded wait.
    sampled_mp3s: list[Path] = []
    directory_over_limit = False
    track_over_limit = False
    try:
        with os.scandir(music_dir) as directory_entries:
            for raw_index, entry in enumerate(islice(directory_entries, _MAX_LOCAL_DIRECTORY_ENTRIES + 1)):
                if raw_index == _MAX_LOCAL_DIRECTORY_ENTRIES:
                    directory_over_limit = True
                    break
                if not entry.name.endswith(".mp3"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                sampled_mp3s.append(Path(entry.path))
                if len(sampled_mp3s) > _MAX_LOCAL_TRACKS:
                    track_over_limit = True
                    break
    except OSError as exc:
        logger.warning("Could not inspect local music directory %s: %s", music_dir, exc)
        return []

    all_mp3s = sorted(sampled_mp3s[:_MAX_LOCAL_TRACKS])
    if directory_over_limit:
        logger.warning(
            "%s contains more than %d entries; inspected only a bounded subset for MP3s",
            music_dir,
            _MAX_LOCAL_DIRECTORY_ENTRIES,
        )
    if track_over_limit:
        logger.warning(
            "%s contains more than %d MP3s; using a bounded subset",
            music_dir,
            _MAX_LOCAL_TRACKS,
        )
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
    return _core_normalized_track_key(track)


# Public alias: the single canonical (artist, title) identity used for both
# playlist dedup AND the persistent operator blocklist (see playlist/blocklist.py
# and core/models.py StationState.blocklist). One key definition, reused everywhere.
normalized_track_key = _normalized_track_key


def filter_blocklisted(tracks: Sequence[Track], blocklist: Mapping[tuple[str, str], object] | None) -> list[Track]:
    """Drop tracks whose normalized ``(artist, title)`` is in the operator blocklist.

    The enforcement primitive applied at every ingest doorway (startup, source
    switch, mid-session chart refresh, external/listener download). Returns a fresh
    list; a falsy blocklist is a cheap passthrough.
    """
    if not blocklist:
        return list(tracks)
    return [track for track in tracks if _normalized_track_key(track) not in blocklist]


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
    local_tracks = _copy_tracks_with_source(_load_local_music_tracks(config.music_dir), "local")
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
        # The RSS feed already carries cover art — no extra network call needed.
        # Upscale the 100px thumbnail to a real cover for lock-screen / HA surfaces.
        album_art = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
        if album_art:
            album_art = upscale_itunes_artwork(album_art)
        tracks.append(
            Track(
                title=title,
                artist=artist,
                duration_ms=210000,
                spotify_id=f"chart_{item_id or len(tracks) + 1}",
                album_art=album_art,
                source="youtube",
            )
        )
    if rejected:
        logger.info("Chart ingest: filtered %d non-music entries", rejected)
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


def read_persisted_heading(cache_dir: Path) -> Heading | None:
    """Read the active heading overlay from cache, if present."""
    path = cache_dir / PERSISTED_HEADING_FILENAME
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        logger.warning("Persisted heading is unreadable: %s", path)
        return None

    if not isinstance(payload, dict):
        return None
    try:
        heading_id = str(payload.get("id", "")).strip()
        seed = str(payload.get("seed", "")).strip()
        label = str(payload.get("label", "")).strip()
        if not heading_id or not seed or not label:
            return None
        targets: list[dict[str, str]] = []
        raw_targets = payload.get("targets", [])
        if isinstance(raw_targets, list):
            for raw_target in raw_targets:
                if not isinstance(raw_target, dict):
                    continue
                artist = str(raw_target.get("artist", "")).strip()
                title = str(raw_target.get("title", "")).strip()
                if artist and title:
                    targets.append({"artist": artist, "title": title})
        phase = str(payload.get("phase", "")).strip()
        if phase not in {"hunting", "steering", "complete"}:
            phase = "hunting" if targets and int(payload.get("selection_budget", 0) or 0) <= 0 else "steering"
        return Heading(
            id=heading_id,
            seed=seed,
            label=label,
            set_at=float(payload.get("set_at", 0.0) or 0.0),
            set_by=str(payload.get("set_by", "")),
            announced=bool(payload.get("announced", False)),
            selection_budget=max(0, int(payload.get("selection_budget", 0) or 0)),
            selection_spent=max(0, int(payload.get("selection_spent", 0) or 0)),
            targets=targets,
            phase=phase,
            hunt_started_announced=bool(payload.get("hunt_started_announced", False)),
            first_found_at=max(0.0, float(payload.get("first_found_at", 0.0) or 0.0)),
            last_narrated_at=max(0.0, float(payload.get("last_narrated_at", 0.0) or 0.0)),
            narration_count=max(0, int(payload.get("narration_count", 0) or 0)),
        )
    except (TypeError, ValueError):
        logger.warning("Persisted heading has invalid fields: %s", path)
        return None


def write_persisted_heading(cache_dir: Path, heading: Heading) -> None:
    """Persist the active heading overlay to cache (atomic write)."""
    path = cache_dir / PERSISTED_HEADING_FILENAME
    payload = {
        "id": heading.id,
        "seed": heading.seed,
        "label": heading.label,
        "set_at": heading.set_at,
        "set_by": heading.set_by,
        "announced": heading.announced,
        "selection_budget": max(0, int(heading.selection_budget or 0)),
        "selection_spent": max(0, int(heading.selection_spent or 0)),
        "phase": heading.phase if heading.phase in {"hunting", "steering", "complete"} else "steering",
        "hunt_started_announced": bool(heading.hunt_started_announced),
        "first_found_at": max(0.0, float(heading.first_found_at or 0.0)),
        "last_narrated_at": max(0.0, float(heading.last_narrated_at or 0.0)),
        "narration_count": max(0, int(heading.narration_count or 0)),
        "targets": [
            {"artist": str(target.get("artist", "")).strip(), "title": str(target.get("title", "")).strip()}
            for target in heading.targets
            if str(target.get("artist", "")).strip() and str(target.get("title", "")).strip()
        ],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def load_explicit_source(
    config: StationConfig,
    source: PlaylistSource,
    *,
    readiness: SourceReadinessEvidence | None = None,
) -> tuple[list[Track], PlaylistSource]:
    """Load a user-chosen source without any silent fallback."""
    evidence = readiness or _source_evidence_for_config(config)
    if source.kind in {"demo", "starter"}:
        from mammamiradio.media.starter import (
            StarterCatalogError,
            load_starter_rotation_tracks,
            starter_source,
        )

        evidence.mark_attempted("demo")
        try:
            tracks = load_starter_rotation_tracks()
        except StarterCatalogError as exc:
            evidence.mark_failure("demo", "The bundled starter catalog is not ready to play")
            raise ExplicitSourceError(f"Starter catalog is not release-ready: {exc}") from exc
        evidence.configure("demo", True, bundled=bool(tracks))
        evidence.mark_candidates("demo", len(tracks))
        return tracks, _attach_source_evidence(starter_source(len(tracks)), tracks, evidence)

    is_jamendo_request = source.kind == "jamendo" or (
        source.kind == "url" and urlparse(source.url or "").scheme == "jamendo"
    )
    if is_jamendo_request:
        evidence.mark_attempted("jamendo")
        evidence.mark_failure("jamendo", "Saved Jamendo playlists were retired")
        raise LegacyJamendoSourceRetiredError(
            "Saved Jamendo playlists were retired. Enable the transient Jamendo source in Setup instead."
        )

    is_classic_request = source.kind == "classic" or (
        source.kind == "url" and urlparse(source.url or "").scheme == "classic"
    )
    if is_classic_request:
        # Record the attempt before loading so both success and failure keep
        # explicit evidence for this advanced source.
        evidence.set_current_rotation("classic", source.label or "Classic Italian")
        evidence.mark_attempted("classic")
        from mammamiradio.playlist.downloader import external_media_enabled

        if not external_media_enabled(config.allow_ytdlp):
            evidence.mark_failure("classic", "External media support is not installed")
            raise ExternalMediaUnavailableError(
                "External media is unavailable. Standalone installs can add the external-media extra."
            )
        era = _classic_era_from_source(source)
        tracks = _shuffle_if_needed(
            config,
            _copy_tracks_with_source(_load_classic_italian_tracks(era), "classic"),
        )
        if not tracks:
            evidence.mark_failure("classic", "Classic Italian returned no playable candidates")
            raise ExplicitSourceError("Classic Italian playlist temporarily unavailable (yt-dlp disabled?)")
        resolved = _classic_italian_source(era, len(tracks))
        return tracks, _attach_source_evidence(resolved, tracks, evidence)

    if source.kind in ("charts", "url"):
        # "url" kind comes from /api/playlist/load — treat as charts reload
        evidence.mark_attempted("charts")
        from mammamiradio.playlist.downloader import external_media_enabled

        if not external_media_enabled(config.allow_ytdlp):
            evidence.mark_failure("charts", "External media support is not installed")
            raise ExternalMediaUnavailableError(
                "External media is unavailable. Standalone installs can add the external-media extra."
            )
        # Existing explicit chart/URL operations retain their standalone
        # behavior behind the single effective capability gate.
        tracks = _load_chart_source_tracks(config)
        if not tracks:
            evidence.mark_failure("charts", "Live charts returned no candidates")
            raise ExplicitSourceError("Current Italian charts are temporarily unavailable")
        evidence.observe_tracks(tracks)
        resolved = _charts_source(len(tracks))
        return tracks, _attach_source_evidence(resolved, tracks, evidence)

    if source.kind == "local":
        # Symmetry: matches the auto-degrade `local` source kind in
        # fetch_startup_playlist. Currently no write path persists a local
        # source, so this branch is defensive — it ensures a future cache
        # file or admin-API change can restore the user's local selection
        # explicitly without falling through to ExplicitSourceError.
        evidence.mark_attempted("local")
        local_tracks = _copy_tracks_with_source(_load_local_music_tracks(config.music_dir), "local")
        if not local_tracks:
            evidence.mark_failure("local", "No MP3 files were found in the configured music directory")
            raise ExplicitSourceError("No MP3 files found in the configured music directory")
        tracks = _shuffle_if_needed(config, local_tracks)
        evidence.mark_candidates("local", len(tracks))
        return tracks, _attach_source_evidence(_local_source(len(tracks)), tracks, evidence)

    raise ExplicitSourceError(f"Unsupported source kind: {source.kind}")


def fetch_startup_playlist(
    config: StationConfig, persisted_source: PlaylistSource | None = None
) -> tuple[list[Track], PlaylistSource, str]:
    """Load an explicit base or the local/starter first-run rotation."""
    evidence = _source_evidence_for_config(config)
    migrate_legacy_jamendo = False
    if persisted_source:
        migrate_legacy_jamendo = persisted_source.kind == "jamendo" or (
            persisted_source.kind == "url" and urlparse(persisted_source.url or "").scheme == "jamendo"
        )
        if migrate_legacy_jamendo:
            evidence.mark_failure("jamendo", "Saved Jamendo playlists were retired")
            error = "Saved Jamendo playlist retired; selected the current base source."
        else:
            try:
                tracks, source = load_explicit_source(config, persisted_source, readiness=evidence)
                return tracks, source, ""
            except ExplicitSourceError as exc:
                logger.warning("Persisted source restore failed: %s", exc)
                failure_kind = "charts" if persisted_source.kind == "url" else persisted_source.kind
                evidence.mark_failure(failure_kind, "The saved source could not be restored")
                error = str(exc)
    else:
        error = ""

    # Operator-owned local files remain the base when present. They are never
    # blended with bundled files or assigned license claims by the application.
    evidence.mark_attempted("local")
    local_tracks = _copy_tracks_with_source(_load_local_music_tracks(config.music_dir), "local")
    if local_tracks:
        logger.info("Using local music files from %s (%d tracks)", config.music_dir, len(local_tracks))
        tracks = _shuffle_if_needed(config, local_tracks)
        evidence.mark_candidates("local", len(tracks))
        source = _local_source(len(tracks))
        if migrate_legacy_jamendo:
            try:
                write_persisted_source(config.cache_dir, source)
            except OSError:
                logger.warning("Could not rewrite retired Jamendo base source", exc_info=True)
        return tracks, _attach_source_evidence(source, tracks, evidence), error
    evidence.mark_failure("local", "No MP3 files were found in the configured music directory")

    from mammamiradio.media.starter import StarterCatalogError, load_starter_rotation_tracks, starter_source

    evidence.mark_attempted("demo")
    try:
        tracks = load_starter_rotation_tracks()
    except StarterCatalogError as exc:
        logger.error("Starter catalog is not release-ready: %s", exc)
        evidence.mark_failure("demo", "The bundled starter catalog is not ready to play")
        source = starter_source(0)
        if migrate_legacy_jamendo:
            try:
                write_persisted_source(config.cache_dir, source)
            except OSError:
                logger.warning("Could not rewrite retired Jamendo base source", exc_info=True)
        detail = f"Starter catalog is not release-ready: {exc}"
        return [], _attach_source_evidence(source, [], evidence), f"{error}; {detail}".strip("; ")

    evidence.configure("demo", True, bundled=bool(tracks))
    evidence.mark_candidates("demo", len(tracks))
    source = starter_source(len(tracks))
    if migrate_legacy_jamendo:
        try:
            write_persisted_source(config.cache_dir, source)
        except OSError:
            logger.warning("Could not rewrite retired Jamendo base source", exc_info=True)
    logger.info("Using attributed starter catalog (%d tracks)", len(tracks))
    return tracks, _attach_source_evidence(source, tracks, evidence), error


def fetch_chart_refresh(existing_ids: set[str]) -> list[Track]:
    """Fetch the latest Italian charts and return only tracks not already in the playlist.

    Used for mid-session playlist refreshes: merges new chart entries into a
    live session without restarting the producer or resetting play history.
    Returns an empty list if the fetch fails or produces no new tracks.
    """
    fresh = _fetch_current_italy_charts()
    return [t for t in fresh if t.spotify_id not in existing_ids]
