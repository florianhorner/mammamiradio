from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import StationState, Track, normalized_track_key
from mammamiradio.core.song_identity import song_identity_key_is_blocklisted

logger = logging.getLogger(__name__)

LOCAL_LIBRARY_SCAN_INTERVAL_SECONDS = 60.0
MAX_LOCAL_LIBRARY_ENTRIES = 20_000
MAX_LOCAL_LIBRARY_TRACKS = 5_000
DEFAULT_LOCAL_DURATION_MS = 210_000
# Bag kinds whose only job is to name what is in the crate. Every other kind
# also picks a backing provider, so the scanner must leave it alone.
_COMPOSITION_PROMOTABLE_KINDS = frozenset({"", "demo", "local", "starter"})
SUPPORTED_LOCAL_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav"})
_FFPROBE_TIMEOUT_SEC = 5.0
# Whole-scan ceiling on metadata probing. ffprobe runs once per file and the
# scanner wakes every 60s, so an unbounded pass over a 5,000-track library could
# hold the box for hours and starve the two-FFmpeg normalization ceiling. When
# the budget is spent the scan stops probing, keeps the filename fallback for
# the rest, and reports ``complete=False`` so reconcile preserves what it
# already knows and the next pass finishes the job (successful probes are
# cached, so each pass makes progress).
_SCAN_PROBE_BUDGET_SEC = 20.0
_PROBE_CACHE_MAX_ENTRIES = MAX_LOCAL_LIBRARY_TRACKS * 2
# path key -> (size, mtime_ns, artist, title, duration_ms).  Only successful
# probes are cached: a failure must be retried, never remembered.
_probe_cache: dict[str, tuple[int, int, str, str, int | None]] = {}


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
    # Path keys whose metadata came from a successful ffprobe on this pass (or
    # from the cache, which only holds successful probes). Everything else in
    # ``tracks`` carries a filename guess and must not overwrite what the live
    # playlist already knows about that file.
    probe_ok_paths: set[str] = field(default_factory=set)
    # Subset of the above whose probe also yielded a real duration.
    measured_duration_paths: set[str] = field(default_factory=set)

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


def _filename_artist_title(path: Path) -> tuple[str, str]:
    """Filename convention fallback: ``Artist - Title.ext``, else Unknown + stem."""
    stem = path.stem.strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return (artist.strip() or "Unknown", title.strip() or path.name)
    return ("Unknown", stem or path.name)


def _probe_local_metadata(path: Path) -> tuple[str, str, int | None, bool]:
    """Read title/artist/duration via ffprobe; fall back to the filename.

    Same ``ffprobe -show_entries`` path used by the normalizer, Jamendo prepare,
    and audio-quality checks. Tags win when present; empty or missing tags keep
    the ``Artist - Title`` filename convention (or Unknown + stem).

    Returns ``(artist, title, duration_ms, ok)``. ``ok`` is False when ffprobe
    did not run or its output could not be parsed, which makes the returned
    artist/title a filename guess rather than a fact. ``duration_ms`` is None
    whenever no real duration was measured. Callers must not let either value
    overwrite metadata a previous successful probe established — a timeout is
    not evidence that a tagged 4:05 song became an untagged 3:30 one.
    """
    artist, title = _filename_artist_title(path)
    duration_ms: int | None = None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:format_tags=artist,title,ARTIST,TITLE",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_FFPROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("ffprobe metadata skipped for %s: %s", path.name, exc)
        return artist, title, duration_ms, False
    if result.returncode != 0:
        logger.debug(
            "ffprobe metadata failed for %s: %s",
            path.name,
            (result.stderr or result.stdout or "").strip(),
        )
        return artist, title, duration_ms, False
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return artist, title, duration_ms, False
    fmt = payload.get("format") if isinstance(payload, dict) else None
    if not isinstance(fmt, dict):
        return artist, title, duration_ms, False
    tags = fmt.get("tags")
    if isinstance(tags, dict):
        tag_artist = tags.get("artist") or tags.get("ARTIST") or ""
        tag_title = tags.get("title") or tags.get("TITLE") or ""
        if isinstance(tag_artist, str) and tag_artist.strip():
            artist = tag_artist.strip()
        if isinstance(tag_title, str) and tag_title.strip():
            title = tag_title.strip()
    raw_duration = fmt.get("duration")
    try:
        seconds = float(raw_duration) if raw_duration is not None else 0.0
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        duration_ms = max(1, round(seconds * 1000))
    return artist, title, duration_ms, True


def _probe_with_cache(
    path: Path,
    path_key: str,
    size: int,
    mtime_ns: int,
    deadline: float | None,
) -> tuple[str, str, int | None, bool]:
    """``_probe_local_metadata`` memoized on (size, mtime_ns), budget-aware.

    An unchanged file is probed exactly once across restarts of the 60s scan
    loop; only successful probes are remembered, so a transient ffprobe failure
    is retried on the next pass instead of being cached as truth. Past
    ``deadline`` nothing new is probed and the caller degrades the scan to
    incomplete.
    """
    cached = _probe_cache.get(path_key)
    if cached is not None and cached[0] == size and cached[1] == mtime_ns:
        return cached[2], cached[3], cached[4], True
    if deadline is not None and time.monotonic() >= deadline:
        artist, title = _filename_artist_title(path)
        return artist, title, None, False
    artist, title, duration_ms, ok = _probe_local_metadata(path)
    if ok:
        if len(_probe_cache) >= _PROBE_CACHE_MAX_ENTRIES:
            _probe_cache.clear()
        _probe_cache[path_key] = (size, mtime_ns, artist, title, duration_ms)
    return artist, title, duration_ms, ok


def _prune_probe_cache(live_path_keys: set[str]) -> None:
    """Drop cache entries for files this scan no longer saw, bounding growth."""
    for stale in [key for key in _probe_cache if key not in live_path_keys]:
        del _probe_cache[stale]


def scan_local_library(source: StationConfig | Path) -> LocalLibraryScanResult:
    roots = (source,) if isinstance(source, Path) else (Path(source.music_dir),)
    result = LocalLibraryScanResult(roots=roots)
    seen_identities: set[tuple[str, str]] = set()
    probe_deadline = time.monotonic() + _SCAN_PROBE_BUDGET_SEC
    seen_path_keys: set[str] = set()
    scan_roots: dict[str, tuple[Path, Path]] = {}
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
                path_key = _path_key(path)
                seen_path_keys.add(path_key)
                artist, title, probed_duration_ms, probe_ok = _probe_with_cache(
                    path, path_key, stat_result.st_size, stat_result.st_mtime_ns, probe_deadline
                )
                if probe_ok:
                    result.probe_ok_paths.add(path_key)
                    if probed_duration_ms is not None:
                        result.measured_duration_paths.add(path_key)
                elif time.monotonic() >= probe_deadline and result.complete:
                    # Out of probe budget: the rest of this pass carries filename
                    # guesses only. Degrade to an incomplete scan so reconcile
                    # keeps what it already knows and the next pass resumes.
                    result.complete = False
                    result.warnings.append("Still reading song tags. Existing tracks were kept; the scan continues.")
                duration_ms = probed_duration_ms if probed_duration_ms is not None else DEFAULT_LOCAL_DURATION_MS
                digest = hashlib.sha256(str(path).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
                track = Track(
                    title=title.strip() or path.name,
                    artist=artist.strip() or "Unknown",
                    duration_ms=duration_ms,
                    spotify_id=f"local_{digest}",
                    local_path=path,
                    source="local",
                )
                identity = normalized_track_key(track)
                if identity in seen_identities:
                    result.ignored["duplicate"] += 1
                    continue
                seen_identities.add(identity)
                if len(result.tracks) >= MAX_LOCAL_LIBRARY_TRACKS:
                    return _finish_scan(result, f"More than {MAX_LOCAL_LIBRARY_TRACKS} songs found; scan stopped.")
                result.tracks.append(track)
            if entry_limit_reached:
                return _finish_scan(result, f"More than {MAX_LOCAL_LIBRARY_ENTRIES} entries found; scan stopped.")

    _prune_probe_cache(seen_path_keys)
    return _finish_scan(result)


def _path_is_in_root_keys(path: Path | None, root_keys: tuple[str, ...]) -> bool:
    if path is None:
        return False
    candidate = Path(_path_key(path))
    return any(candidate.is_relative_to(Path(root_key)) for root_key in root_keys)


def reconcile_local_library(state: StationState, scan: LocalLibraryScanResult) -> dict[str, int]:
    root_keys = scan.root_keys or tuple(_path_key(root) for root in scan.roots)
    candidates = [
        track
        for track in scan.tracks
        if not song_identity_key_is_blocklisted(normalized_track_key(track), state.blocklist)
    ]
    banned = len(scan.tracks) - len(candidates)
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
        if path_key not in scan.probe_ok_paths:
            # ffprobe could not read this file on this pass, so the replacement
            # carries a filename guess, not a fact. Keep the track we already
            # have: a transient timeout must not rename a tagged song to
            # Unknown, drop its pins and reservations by changing its identity,
            # or overwrite a measured duration with the 3:30 fallback.
            reconciled.append(existing)
            continue
        if normalized_track_key(replacement) in non_managed_identities:
            removed_tracks.append(existing)
            continue
        same_identity = normalized_track_key(existing) == normalized_track_key(replacement)
        if same_identity:
            # Keep the live Track object (reservations / pins), but refresh duration
            # when this pass actually measured one.
            if path_key in scan.measured_duration_paths and replacement.duration_ms != existing.duration_ms:
                existing.duration_ms = replacement.duration_ms
            reconciled.append(existing)
        else:
            reconciled.append(replacement)
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
        state.playlist_source.local_overlay = bool(local_tracks)
    # kind is composition, but only across the bag kinds that carry no loader or
    # refresh semantics of their own. charts/url/jamendo/classic also name a
    # backing provider — the producer's mid-session chart refresh, the
    # _load_source dispatch, the persisted-source restore all branch on it — so
    # overlaying local files onto one of those must not rewrite it. Doing so
    # disabled chart refresh permanently, with no path back to "charts".
    # Rotation reads composition from track.source, so it needs no help here;
    # local_overlay above carries the mixed-crate fact for anything that does.
    if state.playlist_source is not None and state.playlist_source.kind in _COMPOSITION_PROMOTABLE_KINDS:
        if local_tracks:
            state.playlist_source.kind = "local"
            state.playlist_source.source_id = state.playlist_source.source_id or "local_music_dir"
            if not state.playlist_source.label or state.playlist_source.label in {
                "Starter",
                "Bundled starter music",
                "Demo",
            }:
                state.playlist_source.label = "Local music"
        elif scan.complete and state.playlist_source.kind == "local":
            # Locals left the crate; restore a starter composition label when the
            # remaining bag is starter media so kind stays an honest composition fact.
            has_starter = any(track.source == "starter" for track in state.playlist)
            if has_starter:
                state.playlist_source.kind = "starter"
                if state.playlist_source.label == "Local music":
                    state.playlist_source.label = "Bundled starter music"

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
