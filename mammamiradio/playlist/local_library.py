from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from mammamiradio.audio.admission import ffmpeg_slot
from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import StationState, Track, normalized_track_key, song_identity_key
from mammamiradio.core.song_identity import song_identity_key_is_blocklisted

logger = logging.getLogger(__name__)

LOCAL_LIBRARY_SCAN_INTERVAL_SECONDS = 60.0
MAX_LOCAL_LIBRARY_ENTRIES = 20_000
MAX_LOCAL_LIBRARY_TRACKS = 5_000
SUPPORTED_LOCAL_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav"})
LOCAL_METADATA_PROBE_TIMEOUT_SECONDS = 5
# Headroom over MAX_LOCAL_LIBRARY_TRACKS on purpose. Sized equal to it, a
# library one file over the track cap evicts its own first entry every pass and
# never registers a single cache hit again (see the cap-before-probe guard in
# scan_local_library, which stops the overflow probe in the first place).
LOCAL_METADATA_CACHE_MAX_ENTRIES = 6_000
# One untrusted file must not be able to hand us an unbounded string: ffprobe
# emits whatever a tag contains, and the value is kept in memory, cached, put on
# a Track, and serialized into every status payload downstream.
LOCAL_METADATA_MAX_TAG_CHARS = 300
LOCAL_METADATA_MAX_PROBE_BYTES = 64 * 1024
# Whole-scan probe budget. asyncio.to_thread() cannot be cancelled, so a shutdown
# or reload only stops awaiting this worker — it keeps running. Without a
# deadline a large or damaged library can hold the shared background ffmpeg slot
# long past the point anyone is listening for the answer.
LOCAL_METADATA_SCAN_BUDGET_SECONDS = 90.0

_LocalMetadata = tuple[str, str]
_LocalSignature = tuple[int, int]
# path_key -> (signature, metadata). Keyed by path so a changed file replaces its
# own entry in O(1); the previous shape needed a full scan per store, which made a
# cold pass over N files O(N^2) under the lock.
_local_metadata_cache: dict[str, tuple[_LocalSignature, _LocalMetadata]] = {}
# The periodic scanner holds an asyncio lock, but empty-rotation recovery and
# source loading call scan_local_library() from their own worker threads. Guard
# the miss/probe/store sequence so two overlapping scans cannot each probe the
# whole library, and so dict mutation stays single-threaded.
_local_metadata_lock = threading.Lock()
_local_metadata_inflight: dict[str, threading.Event] = {}


@dataclass
class LocalLibraryScanResult:
    roots: tuple[Path, ...]
    root_keys: tuple[str, ...] = ()
    has_available_root: bool = False
    tracks: list[Track] = field(default_factory=list)
    complete: bool = True
    entries_seen: int = 0
    supported_files: int = 0
    ignored: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def status_payload(self) -> dict[str, Any]:
        return {
            "in_progress": False,
            "complete": self.complete,
            "roots": [str(root) for root in self.roots],
            "files_found": self.supported_files,
        }


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _finish_scan(result: LocalLibraryScanResult, warning: str = "") -> LocalLibraryScanResult:
    if warning:
        result.complete = False
        result.warnings.append(warning)
    return result


def _clean_tag_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split()).strip()
    # A tag longer than any real song credit is malformed input, not metadata.
    # Drop it rather than truncate: half of a 2 MB string is not a better title
    # than the filename we already have.
    if len(cleaned) > LOCAL_METADATA_MAX_TAG_CHARS:
        return ""
    return cleaned


def _tag_value(tags: object, wanted: str) -> str:
    if not isinstance(tags, dict):
        return ""
    for key, value in tags.items():
        normalized_key = str(key).strip().casefold().removeprefix("tag:")
        if normalized_key == wanted:
            cleaned = _clean_tag_value(value)
            if cleaned:
                return cleaned
    return ""


def _is_audio_stream(stream: object) -> bool:
    """Only an audio stream may name the song.

    An MP3 with embedded cover art carries an attached-picture stream whose
    title tag is typically ``Cover (front)``; an MP4's video stream can carry
    its own title. Without this check an untagged file with artwork airs as
    "Cover (front)" — the exact wrong-title bug this metadata pass exists to fix.
    """
    return isinstance(stream, dict) and str(stream.get("codec_type") or "").strip().casefold() == "audio"


def _probe_local_audio_metadata(path: Path) -> _LocalMetadata | None:
    """Read embedded artist/title tags without making metadata a scan requirement.

    ``("", "")`` means the file was read and genuinely carries no usable tags —
    a durable answer, safe to cache. ``None`` means the probe FAILED (ffprobe
    missing, timeout, unreadable, malformed output) and we simply do not know.

    The distinction matters because a track's ``(artist, title)`` is the key a
    durable operator ban is stored under. Caching a failure as "no tags" pins the
    file to its filename identity for the life of the process and flips it back
    on the next restart, which would silently lift a ban placed in between.
    """
    try:
        with ffmpeg_slot(background=True):
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_entries",
                    "format_tags=title,artist:stream=codec_type:stream_tags=title,artist",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=LOCAL_METADATA_PROBE_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return None

    if result.returncode != 0:
        return None
    raw_stdout = result.stdout or ""
    # Bound what we are willing to parse at all. Tag values are capped per field
    # below, but a file with hundreds of oversized tags would still make us build
    # a huge dict before any of those caps applied.
    if len(raw_stdout) > LOCAL_METADATA_MAX_PROBE_BYTES:
        logger.debug("Ignoring oversized metadata for %s (%d chars)", path.name, len(raw_stdout))
        return "", ""
    try:
        payload = json.loads(raw_stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    title = ""
    artist = ""
    format_section = payload.get("format")
    format_tags = format_section.get("tags") if isinstance(format_section, dict) else None
    for field_name in ("title", "artist"):
        value = _tag_value(format_tags, field_name)
        if field_name == "title":
            title = value
        else:
            artist = value

    streams = payload.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not _is_audio_stream(stream):
                continue
            tags = stream.get("tags") if isinstance(stream, dict) else None
            if not title:
                title = _tag_value(tags, "title")
            if not artist:
                artist = _tag_value(tags, "artist")
            if title and artist:
                break
    return artist, title


def _metadata_signature(stat_result: object) -> _LocalSignature:
    return (
        int(getattr(stat_result, "st_mtime_ns", 0)),
        int(getattr(stat_result, "st_size", 0)),
    )


def _store_local_audio_metadata(path_key: str, signature: _LocalSignature, metadata: _LocalMetadata) -> None:
    _local_metadata_cache.pop(path_key, None)
    _local_metadata_cache[path_key] = (signature, metadata)
    while len(_local_metadata_cache) > LOCAL_METADATA_CACHE_MAX_ENTRIES:
        _local_metadata_cache.pop(next(iter(_local_metadata_cache)))


def _cached_local_audio_metadata(path: Path, stat_result: object) -> _LocalMetadata | None:
    """Probe this file at most once across every concurrent scan.

    Overlapping scans (the periodic scanner, empty-rotation recovery, and source
    loading each run in their own worker thread) would otherwise all miss the
    same key and each spawn ffprobe for it, multiplying the cost of the one
    shared background ffmpeg slot by the number of scanners.

    Returns ``None`` when the answer is unknown — a failed probe, or a wait that
    timed out. Only a real result is cached, so an unknown is retried next pass
    instead of being frozen in as this file's identity.
    """
    path_key = _path_key(path)
    signature = _metadata_signature(stat_result)
    while True:
        with _local_metadata_lock:
            cached = _local_metadata_cache.get(path_key)
            if cached is not None and cached[0] == signature:
                return cached[1]
            inflight = _local_metadata_inflight.get(path_key)
            if inflight is None:
                done = threading.Event()
                _local_metadata_inflight[path_key] = done
                break
        # Another scan owns this probe. Wait for its result instead of running a
        # duplicate. The owner can block on the shared ffmpeg slot for longer than
        # this wait, so a timeout means "unknown", never a different answer: two
        # scans must not derive two different identities for the same file.
        if not inflight.wait(timeout=LOCAL_METADATA_PROBE_TIMEOUT_SECONDS + 1):
            return None

    try:
        metadata = _probe_local_audio_metadata(path)
    except BaseException:
        with _local_metadata_lock:
            _local_metadata_inflight.pop(path_key, None)
        done.set()
        raise
    with _local_metadata_lock:
        if metadata is not None:
            _store_local_audio_metadata(path_key, signature, metadata)
        _local_metadata_inflight.pop(path_key, None)
    done.set()
    return metadata


def _strip_track_number(stem: str) -> str:
    return re.sub(r"^\s*\d{1,3}\s*[-_.]\s*", "", stem).strip()


def _humanize_filename(stem: str) -> str:
    cleaned = _strip_track_number(stem)
    cleaned = re.sub(r"[-_]+", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    if cleaned.islower():
        cleaned = cleaned.title()
    return cleaned


def _filename_metadata(path: Path) -> _LocalMetadata:
    stem = path.stem.strip()
    parsed_stem = _strip_track_number(stem)
    if " - " in parsed_stem:
        artist, title = parsed_stem.split(" - ", 1)
        artist = _clean_tag_value(artist)
        title = _clean_tag_value(title)
        if artist and title:
            return artist, title
    return "", _humanize_filename(stem) or path.name


def _peek_local_audio_metadata(path: Path, stat_result: object) -> _LocalMetadata | None:
    """Read an already-probed result without spawning ffprobe."""
    with _local_metadata_lock:
        cached = _local_metadata_cache.get(_path_key(path))
    if cached is None or cached[0] != _metadata_signature(stat_result):
        return None
    return cached[1]


def _local_track_metadata(path: Path, stat_result: object, *, probe: bool = True) -> _LocalMetadata:
    # Past the scan budget we stop spawning ffprobe, but a value already in the
    # cache is free — using it keeps a warm library's labels stable instead of
    # churning every track back to its filename for one slow pass.
    tagged = _cached_local_audio_metadata(path, stat_result) if probe else _peek_local_audio_metadata(path, stat_result)
    tagged_artist, tagged_title = tagged if tagged is not None else ("", "")
    filename_artist, filename_title = _filename_metadata(path)
    return tagged_artist or filename_artist, tagged_title or filename_title


def legacy_local_identity_key(path: Path) -> tuple[str, str]:
    """The identity this file had before embedded tags were read.

    Reading tags (and humanizing a slug filename) changes a local file's
    ``(artist, title)``, and that pair is the key durable operator bans and song
    preferences are stored under. Nothing on disk records the old key, but it is
    fully derivable from the path — this reproduces the previous rule verbatim,
    so a ban placed before the upgrade still matches the same file after it.
    """
    stem = path.stem.strip()
    artist, title = stem.split(" - ", 1) if " - " in stem else ("Unknown", stem)
    return song_identity_key(artist.strip() or "Unknown", title.strip() or path.name)


def local_identity_aliases(track: Track) -> tuple[tuple[str, str], ...]:
    """Every identity this local file may have been banned or voted under.

    A local file's ``(artist, title)`` is not stable across releases or even
    across a failed probe, but the operator's ban is stored under whichever one
    was showing at the time. Three can occur:

    1. its identity now;
    2. the pre-tags identity, reproduced from the path (``legacy_local_identity_key``);
    3. the filename-derived identity under the CURRENT rule, which is what a file
       shows while its probe is failing or while a cold scan is past its budget.

    Checking all three is what makes "banned for good" mean it.
    """
    keys = [normalized_track_key(track)]
    path = track.local_path
    if path is not None:
        keys.append(legacy_local_identity_key(path))
        filename_artist, filename_title = _filename_metadata(path)
        keys.append(song_identity_key(filename_artist, filename_title or path.name))
    seen: list[tuple[str, str]] = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return tuple(seen)


def local_track_is_blocklisted(track: Track, blocklist: object) -> bool:
    """Ban check for a local file, honouring every identity it may carry."""
    return any(
        song_identity_key_is_blocklisted(key, blocklist)  # type: ignore[arg-type]
        for key in local_identity_aliases(track)
    )


def scan_local_library(source: StationConfig | Path) -> LocalLibraryScanResult:
    roots = (source,) if isinstance(source, Path) else (Path(source.music_dir),)
    result = LocalLibraryScanResult(roots=roots)
    seen_identities: set[tuple[str, str]] = set()
    scan_roots: dict[str, tuple[Path, Path]] = {}
    probe_deadline = time.monotonic() + LOCAL_METADATA_SCAN_BUDGET_SECONDS
    probe_metadata = True
    for root in roots:
        try:
            if root.is_symlink():
                result.complete = False
                result.warnings.append(f"Symlinked music folder skipped: {root}. Use its real path; tracks kept.")
                continue
            resolved_root = root.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved_root = Path(_path_key(root))
        scan_roots.setdefault(_path_key(resolved_root), (root, resolved_root))
    result.root_keys = tuple(scan_roots)

    for root, resolved_root in scan_roots.values():
        if not resolved_root.is_dir():
            result.complete = False
            result.warnings.append(f"Music folder is unavailable: {root}")
            continue
        result.has_available_root = True

        pending = [resolved_root]
        while pending:
            directory = pending.pop()
            remaining_entries = max(0, MAX_LOCAL_LIBRARY_ENTRIES - result.entries_seen)
            try:
                with os.scandir(directory) as entries:
                    bounded_entries = list(islice(entries, remaining_entries + 1))
            except OSError as exc:
                logger.warning("Could not read local music directory %s: %s", directory, exc)
                result.complete = False
                result.warnings.append(f"Could not read {directory}: {exc}")
                continue
            entry_limit_reached = len(bounded_entries) > remaining_entries
            ordered_entries = sorted(bounded_entries[:remaining_entries], key=lambda entry: entry.name.casefold())

            for entry in ordered_entries:
                result.entries_seen += 1
                try:
                    if entry.is_symlink():
                        result.ignored["symlink"] += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        result.ignored["not_a_file"] += 1
                        continue
                except OSError:
                    result.complete = False
                    result.ignored["unreadable"] += 1
                    continue

                path = Path(entry.path)
                if path.suffix.casefold() not in SUPPORTED_LOCAL_AUDIO_SUFFIXES:
                    result.ignored["unsupported_format"] += 1
                    continue
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                    if stat_result.st_size <= 0:
                        result.ignored["empty_file"] += 1
                        continue
                except OSError:
                    result.complete = False
                    result.ignored["unreadable"] += 1
                    continue

                result.supported_files += 1
                # Cap BEFORE probing. Probing the file that pushes us over the
                # track limit spends an ffprobe on audio that is then thrown
                # away, and its cache entry evicts a track we do keep.
                if len(result.tracks) >= MAX_LOCAL_LIBRARY_TRACKS:
                    return _finish_scan(result, f"More than {MAX_LOCAL_LIBRARY_TRACKS} songs found; scan stopped.")
                if probe_metadata and time.monotonic() >= probe_deadline:
                    probe_metadata = False
                    result.complete = False
                    logger.warning(
                        "Local music metadata scan exceeded %.0fs; labelling the rest from filenames this pass.",
                        LOCAL_METADATA_SCAN_BUDGET_SECONDS,
                    )
                artist, title = _local_track_metadata(path, stat_result, probe=probe_metadata)
                digest = hashlib.sha256(str(path).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
                track = Track(
                    title=title.strip() or path.name,
                    artist=artist.strip(),
                    duration_ms=210_000,
                    spotify_id=f"local_{digest}",
                    local_path=path,
                    source="local",
                )
                identity = normalized_track_key(track)
                if identity in seen_identities:
                    result.ignored["duplicate"] += 1
                    continue
                seen_identities.add(identity)
                result.tracks.append(track)
            if entry_limit_reached:
                return _finish_scan(result, f"More than {MAX_LOCAL_LIBRARY_ENTRIES} entries found; scan stopped.")

    return _finish_scan(result)


def _path_is_in_root_keys(path: Path | None, root_keys: tuple[str, ...]) -> bool:
    if path is None:
        return False
    candidate = Path(_path_key(path))
    return any(candidate.is_relative_to(Path(root_key)) for root_key in root_keys)


def _migrate_local_preferences(state: StationState, tracks: list[Track]) -> None:
    """Move a like/dislike across the identity change reading tags caused.

    Bans are CHECKED against every alias (``local_identity_aliases``) because a
    ban must hold even for a file that is not in rotation. A preference only
    matters for a track that IS in rotation, so it is moved once instead.

    The legacy row is REMOVED, not copied. Leaving it behind meant clearing the
    preference deleted only the new key and the next 60-second scan copied the
    old one straight back — a vote the operator could not take off.
    """
    preferences = getattr(state, "song_preferences", None)
    if not preferences:
        return
    migrated = 0
    for track in tracks:
        if track.local_path is None:
            continue
        key = normalized_track_key(track)
        for legacy in local_identity_aliases(track):
            if legacy == key:
                continue
            existing = preferences.pop(legacy, None)
            if existing is None:
                continue
            preferences.setdefault(key, existing)
            migrated += 1
    if migrated:
        state.song_preferences_revision += 1
        logger.info("Carried %d local song preference(s) onto their new metadata identity", migrated)


def reconcile_local_library(state: StationState, scan: LocalLibraryScanResult) -> dict[str, int]:
    root_keys = scan.root_keys or tuple(_path_key(root) for root in scan.roots)
    candidates = [track for track in scan.tracks if not local_track_is_blocklisted(track, state.blocklist)]
    banned = len(scan.tracks) - len(candidates)
    _migrate_local_preferences(state, candidates)
    scanned_by_path = {_path_key(track.local_path): track for track in candidates if track.local_path is not None}
    removed_tracks: list[Track] = []
    reconciled: list[Track] = []

    non_managed_identities = {
        normalized_track_key(track)
        for track in state.playlist
        if not _path_is_in_root_keys(track.local_path, root_keys)
    }
    for existing in state.playlist:
        existing_path = existing.local_path
        if not _path_is_in_root_keys(existing_path, root_keys):
            reconciled.append(existing)
            continue
        assert existing_path is not None
        path_key = _path_key(existing_path)
        replacement = scanned_by_path.pop(path_key, None)
        if replacement is None:
            if scan.complete:
                removed_tracks.append(existing)
            else:
                reconciled.append(existing)
            continue
        if normalized_track_key(replacement) in non_managed_identities:
            removed_tracks.append(existing)
            continue
        same_identity = normalized_track_key(existing) == normalized_track_key(replacement)
        reconciled.append(existing if same_identity else replacement)
        if not same_identity:
            removed_tracks.append(existing)

    active_identities = {normalized_track_key(track) for track in reconciled}
    added_tracks: list[Track] = []
    for track in scanned_by_path.values():
        identity = normalized_track_key(track)
        if identity in active_identities:
            continue
        reconciled.append(track)
        added_tracks.append(track)
        active_identities.add(identity)

    if [track.cache_key for track in reconciled] != [track.cache_key for track in state.playlist]:
        state.playlist = reconciled
        state.playlist_revision += 1
        state.music_admission_changed.set()

    local_tracks = [track for track in state.playlist if _path_is_in_root_keys(track.local_path, root_keys)]
    readiness = state.source_readiness.entries["local"]
    readiness.configured = scan.has_available_root
    readiness.attempted = True
    readiness.candidates = len(local_tracks)
    readiness.exhausted = not local_tracks and scan.complete
    if not local_tracks and scan.complete:
        readiness.playable = 0
        readiness.failure = "Add supported songs to the local music library, then select Scan now."
    elif not local_tracks and scan.warnings:
        readiness.failure = "Check the local music folders, then select Scan now. Existing tracks were kept."
    else:
        readiness.failure = ""
    if removed_tracks:
        state.source_readiness.reconcile_active_tracks(state.playlist, removed_tracks=removed_tracks)
    if state.playlist_source is not None:
        state.playlist_source.track_count = len(state.playlist)

    return {
        "added": len(added_tracks),
        "removed": len(removed_tracks),
        "active": len(local_tracks),
        "banned": banned,
        "playlist_revision": state.playlist_revision,
    }


def initial_local_library_status(config: StationConfig) -> dict[str, Any]:
    return {
        **LocalLibraryScanResult(roots=(Path(config.music_dir),), complete=False).status_payload(),
        "in_progress": True,
        "active": 0,
        "added": 0,
        "removed": 0,
        "banned": 0,
    }


async def scan_and_reconcile_local_library(app_state: Any) -> dict[str, Any]:
    lock = app_state.local_library_scan_lock
    if lock.locked():
        return {**app_state.local_library_status, "in_progress": True, "already_in_progress": True}
    async with lock:
        previous = dict(app_state.local_library_status)
        app_state.local_library_status = {**previous, "in_progress": True, "error": ""}
        try:
            scan = await asyncio.to_thread(scan_local_library, app_state.config)
            async with app_state.source_switch_lock:
                outcome = reconcile_local_library(app_state.station_state, scan)
            status = {**scan.status_payload(), **outcome}
        except Exception:  # pragma: no cover - fail-soft audio boundary
            logger.warning("Local music scan failed", exc_info=True)
            status = {**previous, "in_progress": False, "complete": False}
            status["error"] = "Check the local music folders, then select Scan now."
        app_state.local_library_status = status
        return dict(status)


async def run_local_library_scanner(app_state: Any) -> None:
    while True:
        await scan_and_reconcile_local_library(app_state)
        await asyncio.sleep(LOCAL_LIBRARY_SCAN_INTERVAL_SECONDS)
