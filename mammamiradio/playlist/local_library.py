"""Scan and reconcile local music."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
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
SUPPORTED_LOCAL_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav"})


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
    started_at: float = 0.0
    finished_at: float = 0.0

    def status_payload(self) -> dict[str, Any]:
        return {
            "in_progress": False,
            "complete": self.complete,
            "roots": [str(root) for root in self.roots],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "entries_seen": self.entries_seen,
            "files_found": self.supported_files,
            "ignored": dict(sorted(self.ignored.items())),
            "warnings": self.warnings[:5],
            "error": "",
            "already_in_progress": False,
        }


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def local_library_roots(config: StationConfig) -> tuple[Path, ...]:
    roots: dict[str, Path] = {}
    for root in map(Path, (config.music_dir, *config.legacy_music_dirs)):
        roots.setdefault(_path_key(root), root)
    return tuple(roots.values())


def _track_from_path(path: Path) -> Track:
    stem = path.stem.strip()
    artist, title = stem.split(" - ", 1) if " - " in stem else ("Unknown", stem)
    digest = hashlib.sha256(str(path).encode("utf-8", errors="surrogatepass")).hexdigest()[:20]
    return Track(
        title=title.strip() or path.name,
        artist=artist.strip() or "Unknown",
        duration_ms=210_000,
        spotify_id=f"local_{digest}",
        local_path=path,
        source="local",
    )


def _finish_scan(result: LocalLibraryScanResult, warning: str = "") -> LocalLibraryScanResult:
    if warning:
        result.complete = False
        result.warnings.append(warning)
    result.finished_at = time.time()
    return result


def scan_local_library(source: StationConfig | Path) -> LocalLibraryScanResult:
    """Find supported audio files recursively without following symlinks."""
    roots = (source,) if isinstance(source, Path) else local_library_roots(source)
    result = LocalLibraryScanResult(roots=roots, started_at=time.time())
    seen_identities: set[tuple[str, str]] = set()
    scan_roots: dict[str, tuple[Path, Path]] = {}
    for root in roots:
        try:
            resolved_root = root.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved_root = Path(_path_key(root))
        scan_roots.setdefault(_path_key(resolved_root), (root, resolved_root))
    result.root_keys = tuple(scan_roots)

    for root, resolved_root in scan_roots.values():
        try:
            root_is_dir = resolved_root.is_dir()
        except OSError as exc:
            root_is_dir = False
            result.warnings.append(f"Could not inspect {root}: {exc}")
        if not root_is_dir:
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
                    result.ignored["unreadable"] += 1
                    continue

                path = Path(entry.path)
                if path.suffix.casefold() not in SUPPORTED_LOCAL_AUDIO_SUFFIXES:
                    result.ignored["unsupported_format"] += 1
                    continue
                try:
                    if entry.stat(follow_symlinks=False).st_size <= 0:
                        result.ignored["empty_file"] += 1
                        continue
                except OSError:
                    result.ignored["unreadable"] += 1
                    continue

                result.supported_files += 1
                track = _track_from_path(path)
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

    return _finish_scan(result)


def _path_is_in_root_keys(path: Path | None, root_keys: tuple[str, ...]) -> bool:
    if path is None:
        return False
    candidate = Path(_path_key(path))
    return any(candidate.is_relative_to(Path(root_key)) for root_key in root_keys)


def reconcile_local_library(state: StationState, scan: LocalLibraryScanResult) -> dict[str, int]:
    """Apply a scan without changing queued or on-air audio."""
    root_keys = scan.root_keys or tuple(_path_key(root) for root in scan.roots)
    candidates = [
        track
        for track in scan.tracks
        if not song_identity_key_is_blocklisted(normalized_track_key(track), state.blocklist)
    ]
    banned = len(scan.tracks) - len(candidates)
    scanned_by_path = {_path_key(track.local_path): track for track in candidates if track.local_path is not None}
    used_paths: set[str] = set()
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
        replacement = scanned_by_path.get(path_key)
        if replacement is None:
            if scan.complete:
                removed_tracks.append(existing)
            else:
                reconciled.append(existing)
            continue
        used_paths.add(path_key)
        if normalized_track_key(replacement) in non_managed_identities:
            removed_tracks.append(existing)
            continue
        if normalized_track_key(existing) == normalized_track_key(replacement):
            reconciled.append(existing)
        else:
            reconciled.append(replacement)
            removed_tracks.append(existing)

    active_identities = {normalized_track_key(track) for track in reconciled}
    added_tracks: list[Track] = []
    for path_key, track in scanned_by_path.items():
        identity = normalized_track_key(track)
        if path_key in used_paths or identity in active_identities:
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
    if local_tracks:
        readiness.exhausted = False
        readiness.failure = ""
    elif scan.complete:
        readiness.playable = 0
        readiness.exhausted = True
        readiness.failure = "No supported audio files were found in the local music library."
    elif scan.warnings:
        readiness.failure = scan.warnings[0]
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
        "management": "home_assistant" if config.is_addon else "filesystem",
        **LocalLibraryScanResult(roots=local_library_roots(config), complete=False).status_payload(),
        "active": 0,
        "added": 0,
        "removed": 0,
        "banned": 0,
    }


async def scan_and_reconcile_local_library(app_state: Any) -> dict[str, Any]:
    """Run one serialized scan for HTTP and background callers."""
    lock = app_state.local_library_scan_lock
    if lock.locked():
        return {**app_state.local_library_status, "in_progress": True, "already_in_progress": True}
    async with lock:
        previous = dict(app_state.local_library_status)
        app_state.local_library_status = {
            **previous,
            "in_progress": True,
            "started_at": time.time(),
            "error": "",
        }
        try:
            scan = await asyncio.to_thread(scan_local_library, app_state.config)
            async with app_state.source_switch_lock:
                outcome = reconcile_local_library(app_state.station_state, scan)
            status = {
                **scan.status_payload(),
                **outcome,
                "management": "home_assistant" if app_state.config.is_addon else "filesystem",
            }
        except Exception as exc:  # pragma: no cover - fail-soft audio boundary
            logger.warning("Local music scan failed", exc_info=True)
            status = {
                **previous,
                "in_progress": False,
                "complete": False,
                "finished_at": time.time(),
                "error": str(exc)[:160] or "The local music scan could not finish.",
            }
        app_state.local_library_status = status
        return dict(status)


async def run_local_library_scanner(app_state: Any) -> None:
    """Refresh the local library without entering the audio hot path."""
    while True:
        await scan_and_reconcile_local_library(app_state)
        await asyncio.sleep(LOCAL_LIBRARY_SCAN_INTERVAL_SECONDS)
