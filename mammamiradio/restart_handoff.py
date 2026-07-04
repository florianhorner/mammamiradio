"""Restart handoff spool for cold-open music continuity.

The producer can copy a small set of already-safe music segments into
``cache/restart_handoff`` before an add-on update. On the next boot, main can load
and admit those entries before the producer has warmed the normal queue.

This module owns no startup/producer state itself — ``main.py`` and
``scheduling/producer.py`` call these functions directly. All file work is
synchronous so callers can run it with ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mammamiradio.audio.normalizer import probe_duration_sec
from mammamiradio.core.models import Segment, SegmentType

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
SEGMENTS_DIRNAME = "segments"
DEFAULT_MAX_ENTRIES = 3
DEFAULT_MAX_ENTRY_AGE_SEC = 6 * 60 * 60

_AUDIO_SUFFIX = ".mp3"
_HANDOFF_TMP_PREFIX = ".handoff-"
_MANIFEST_TMP_PREFIX = ".manifest-"
# Bounds worst-case synchronous startup work if a pre-fix crash loop left an
# unusually large backlog of orphaned scratch files. Applied independently per
# directory (manifest-tmp and segments-tmp each get their own budget), so the
# real combined ceiling per boot is 2x this value, not this value alone. Any
# excess is picked up on a later boot (this cleanup runs every startup, not
# just once).
_MAX_SCRATCH_PRUNE_PER_PASS = 500
# Hard ceiling on raw glob() enumeration itself, independent of the prune cap
# above. Without this, an extreme backlog (far beyond what the prune cap
# anticipates) would still make `list(directory.glob(...))` an unbounded
# memory/IO cost before the prune cap ever gets a chance to apply.
_MAX_SCRATCH_GLOB_CANDIDATES = 5000
_TMP_SUFFIX = ".tmp"
_SPOOL_WRITE_LOCK = threading.Lock()
_METADATA_BLOCK_FLAGS = frozenset(
    {
        "canned",
        "dynamic_overlay",
        "emergency_tone",
        "ephemeral",
        "error",
        "fallback",
        "interrupt",
        "overlay",
        "recycled",
        "rescue",
        "silence_fallback",
        "studio_bleed",
        "temporary",
    }
)

BlockKey = tuple[str, str]
DurationProbe = Callable[[Path], float | None]


@dataclass(frozen=True)
class RestartHandoffEntry:
    """One copied, hash-addressed music file in the restart handoff spool."""

    relative_path: str
    sha256: str
    size_bytes: int
    duration_sec: float
    artist: str
    title: str
    segment_class: str = "music"
    created_at: float = 0.0
    source_path: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "duration_sec": self.duration_sec,
            "artist": self.artist,
            "title": self.title,
            "segment_class": self.segment_class,
            "created_at": self.created_at,
            "source_path": self.source_path,
            "metadata": _json_safe(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: object) -> RestartHandoffEntry | None:
        if not isinstance(data, Mapping):
            return None
        relative_path = _coerce_str(data.get("relative_path"))
        sha256 = _coerce_str(data.get("sha256")).lower()
        size_bytes = _coerce_int(data.get("size_bytes"))
        duration_sec = _coerce_float(data.get("duration_sec"))
        artist = _coerce_str(data.get("artist"))
        title = _coerce_str(data.get("title"))
        segment_class = _coerce_str(data.get("segment_class") or "music")
        created_at = _coerce_float(data.get("created_at"))
        source_path = _coerce_str(data.get("source_path"))
        metadata = data.get("metadata")
        if (
            not relative_path
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or size_bytes is None
            or duration_sec is None
            or created_at is None
        ):
            return None
        if not isinstance(metadata, Mapping):
            metadata = {}
        return cls(
            relative_path=relative_path,
            sha256=sha256,
            size_bytes=size_bytes,
            duration_sec=duration_sec,
            artist=artist,
            title=title,
            segment_class=segment_class,
            created_at=created_at,
            source_path=source_path,
            metadata=dict(metadata),
        )

    @property
    def block_key(self) -> BlockKey:
        return (_normalize_identity(self.artist), _normalize_identity(self.title))

    def path(self, cache_dir: Path | str) -> Path | None:
        return _resolve_relative_to_handoff(cache_dir, self.relative_path)

    def to_segment(self, cache_dir: Path | str) -> Segment | None:
        path = self.path(cache_dir)
        if path is None:
            return None
        metadata = dict(self.metadata)
        metadata.update(
            {
                "artist": self.artist,
                "title": f"{self.artist} – {self.title}" if self.artist else self.title,
                "title_only": self.title,
                "audio_source": "restart_handoff",
                "source_kind": "restart_handoff",
                "restart_handoff": True,
            }
        )
        return Segment(
            type=SegmentType.MUSIC,
            path=path,
            duration_sec=self.duration_sec,
            metadata=metadata,
            ephemeral=False,
        )


@dataclass(frozen=True)
class RestartHandoffManifest:
    """Versioned restart handoff manifest."""

    entries: tuple[RestartHandoffEntry, ...] = ()
    schema_version: int = SCHEMA_VERSION
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def empty(cls) -> RestartHandoffManifest:
        return cls(entries=(), schema_version=SCHEMA_VERSION, created_at=0.0)

    @classmethod
    def from_dict(cls, data: object) -> RestartHandoffManifest:
        if not isinstance(data, Mapping) or data.get("schema_version") != SCHEMA_VERSION:
            return cls.empty()
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, str | bytes):
            return cls.empty()
        entries = tuple(entry for raw in raw_entries if (entry := RestartHandoffEntry.from_dict(raw)) is not None)
        return cls(
            entries=entries,
            schema_version=SCHEMA_VERSION,
            created_at=_coerce_float(data.get("created_at")) or 0.0,
        )


@dataclass(frozen=True)
class RestartHandoffCandidate:
    """Producer-facing candidate to copy into the restart handoff spool."""

    path: Path
    duration_sec: float
    artist: str
    title: str
    segment_class: str = "music"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    ephemeral: bool = False

    @classmethod
    def from_segment(cls, segment: Segment) -> RestartHandoffCandidate:
        metadata = segment.metadata if isinstance(segment.metadata, Mapping) else {}
        artist = _coerce_str(metadata.get("artist"))
        title = _coerce_str(metadata.get("title_only") or metadata.get("title"))
        if " – " in title and not metadata.get("title_only"):
            maybe_artist, maybe_title = title.split(" – ", 1)
            artist = artist or maybe_artist.strip()
            title = maybe_title.strip()
        return cls(
            path=segment.path,
            duration_sec=segment.duration_sec,
            artist=artist,
            title=title,
            segment_class=segment.type.segment_class,
            metadata=dict(metadata),
            ephemeral=segment.ephemeral,
        )


@dataclass(frozen=True)
class RestartHandoffRejection:
    reason: str
    detail: str = ""
    entry: RestartHandoffEntry | None = None


@dataclass(frozen=True)
class RestartHandoffAdmission:
    accepted: tuple[RestartHandoffEntry, ...] = ()
    rejected: tuple[RestartHandoffRejection, ...] = ()

    def to_segments(self, cache_dir: Path | str) -> list[Segment]:
        segments: list[Segment] = []
        for entry in self.accepted:
            segment = entry.to_segment(cache_dir)
            if segment is not None:
                segments.append(segment)
        return segments


def restart_handoff_dir(cache_dir: Path | str) -> Path:
    return Path(cache_dir) / "restart_handoff"


def restart_handoff_manifest_path(cache_dir: Path | str) -> Path:
    return restart_handoff_dir(cache_dir) / MANIFEST_FILENAME


def load_restart_handoff_manifest(cache_dir: Path | str) -> RestartHandoffManifest:
    """Load the manifest, returning an empty manifest for missing/corrupt input."""

    path = restart_handoff_manifest_path(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        return RestartHandoffManifest.from_dict(json.loads(raw))
    except FileNotFoundError:
        return RestartHandoffManifest.empty()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("restart handoff manifest is unreadable; ignoring it: %s", exc)
        return RestartHandoffManifest.empty()


def admit_restart_handoff_entries(
    cache_dir: Path | str,
    *,
    blocklist: Mapping[BlockKey, object] | None = None,
    now: float | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_age_sec: float = DEFAULT_MAX_ENTRY_AGE_SEC,
    duration_probe: DurationProbe = probe_duration_sec,
) -> RestartHandoffAdmission:
    """Load and validate restart handoff entries for startup use."""

    manifest = load_restart_handoff_manifest(cache_dir)
    return admit_restart_handoff_manifest(
        cache_dir,
        manifest,
        blocklist=blocklist,
        now=now,
        max_entries=max_entries,
        max_age_sec=max_age_sec,
        duration_probe=duration_probe,
    )


def admit_restart_handoff_manifest(
    cache_dir: Path | str,
    manifest: RestartHandoffManifest,
    *,
    blocklist: Mapping[BlockKey, object] | None = None,
    now: float | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_age_sec: float = DEFAULT_MAX_ENTRY_AGE_SEC,
    duration_probe: DurationProbe = probe_duration_sec,
) -> RestartHandoffAdmission:
    """Validate a manifest without raising into the startup audio path."""

    now = time.time() if now is None else now
    if len(manifest.entries) > max_entries:
        return RestartHandoffAdmission(
            rejected=(
                RestartHandoffRejection(
                    reason="too_many_segments",
                    detail=f"{len(manifest.entries)} entries > max {max_entries}",
                ),
            )
        )

    accepted: list[RestartHandoffEntry] = []
    rejected: list[RestartHandoffRejection] = []
    for entry in manifest.entries:
        reason = _entry_rejection_reason(
            cache_dir,
            entry,
            blocklist=blocklist,
            now=now,
            max_age_sec=max_age_sec,
            duration_probe=duration_probe,
        )
        if reason is None:
            accepted.append(entry)
        else:
            rejected.append(RestartHandoffRejection(reason=reason, entry=entry))
    return RestartHandoffAdmission(accepted=tuple(accepted), rejected=tuple(rejected))


def write_restart_handoff_spool(
    cache_dir: Path | str,
    candidates: Iterable[RestartHandoffCandidate],
    *,
    blocklist: Mapping[BlockKey, object] | None = None,
    now: float | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    duration_probe: DurationProbe = probe_duration_sec,
    clear_when_empty: bool = False,
    protected_paths: Iterable[Path | str] | None = None,
) -> RestartHandoffManifest:
    """Copy safe candidates into the handoff dir and atomically publish a manifest.

    Synchronous by design; scheduling code should call it through
    ``asyncio.to_thread`` or ``loop.run_in_executor``.

    ``protected_paths`` are segment files the prune must never delete even though
    they are not in the freshly-written manifest — e.g. handoff files still queued
    for playback in the live queue this session. Without this the single-candidate
    rewrite prunes them out from under the playback loop (dead air).
    """

    now = time.time() if now is None else now
    with _SPOOL_WRITE_LOCK:
        entries: list[RestartHandoffEntry] = []
        for candidate in candidates:
            if len(entries) >= max_entries:
                break
            entry = _entry_from_candidate(
                cache_dir,
                candidate,
                blocklist=blocklist,
                now=now,
                duration_probe=duration_probe,
            )
            if entry is not None:
                entries.append(entry)
        if not entries and not clear_when_empty:
            return load_restart_handoff_manifest(cache_dir)

        manifest = RestartHandoffManifest(entries=tuple(entries), schema_version=SCHEMA_VERSION, created_at=now)
        _publish_manifest(cache_dir, manifest)
        _prune_unreferenced_segments(cache_dir, manifest, protected_paths=protected_paths)
        return manifest


def try_write_restart_handoff_spool(
    cache_dir: Path | str,
    candidates: Iterable[RestartHandoffCandidate],
    *,
    blocklist: Mapping[BlockKey, object] | None = None,
    now: float | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    duration_probe: DurationProbe = probe_duration_sec,
    clear_when_empty: bool = False,
    protected_paths: Iterable[Path | str] | None = None,
) -> bool:
    """Best-effort public helper for producer scheduling paths."""

    try:
        write_restart_handoff_spool(
            cache_dir,
            candidates,
            blocklist=blocklist,
            now=now,
            max_entries=max_entries,
            duration_probe=duration_probe,
            clear_when_empty=clear_when_empty,
            protected_paths=protected_paths,
        )
    except Exception as exc:
        logger.warning("Failed to write restart handoff spool: %s", exc)
        return False
    return True


def prune_stale_handoff_tmp_files(cache_dir: Path | str, max_age_hours: float = 6) -> int:
    """Prune orphaned restart-handoff scratch files left by hard kills."""

    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        # A zero/negative/NaN age would prune everything (including a tmp file
        # from a write in progress) instead of only true orphans. Fail safe by
        # pruning nothing rather than pruning everything.
        logger.warning(
            "Ignoring restart handoff scratch cleanup: max_age_hours must be positive, got %r",
            max_age_hours,
        )
        return 0
    cutoff = time.time() - max_age_hours * 3600
    try:
        cache_root = Path(cache_dir).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        # RuntimeError: Path.resolve() raises this (not OSError) on a symlink
        # loop. Startup cleanup must never crash the app over a broken cache dir.
        logger.warning("Failed to resolve restart handoff cache dir for scratch cleanup: %s", exc)
        return 0
    return _prune_stale_tmp_glob(
        restart_handoff_dir(cache_dir),
        f"{_MANIFEST_TMP_PREFIX}*{_TMP_SUFFIX}",
        cutoff,
        cache_root=cache_root,
    ) + _prune_stale_tmp_glob(
        _segments_dir(cache_dir),
        f"{_HANDOFF_TMP_PREFIX}*{_TMP_SUFFIX}",
        cutoff,
        cache_root=cache_root,
    )


def _entry_from_candidate(
    cache_dir: Path | str,
    candidate: RestartHandoffCandidate,
    *,
    blocklist: Mapping[BlockKey, object] | None,
    now: float,
    duration_probe: DurationProbe,
) -> RestartHandoffEntry | None:
    if (
        _candidate_rejection_reason(cache_dir, candidate, blocklist=blocklist, duration_probe=duration_probe)
        is not None
    ):
        return None
    source = candidate.path
    copied, digest, size_bytes = _copy_and_hash(source, _segments_dir(cache_dir), candidate.artist, candidate.title)
    duration = _validated_duration(source, candidate.duration_sec, duration_probe)
    if duration is None:
        copied.unlink(missing_ok=True)
        return None
    return RestartHandoffEntry(
        relative_path=_relative_to_handoff(cache_dir, copied),
        sha256=digest,
        size_bytes=size_bytes,
        duration_sec=duration,
        artist=candidate.artist.strip(),
        title=candidate.title.strip(),
        segment_class="music",
        created_at=now,
        source_path=str(source),
        metadata=_json_safe(candidate.metadata),
    )


def _candidate_rejection_reason(
    cache_dir: Path | str,
    candidate: RestartHandoffCandidate,
    *,
    blocklist: Mapping[BlockKey, object] | None,
    duration_probe: DurationProbe,
) -> str | None:
    if candidate.segment_class != "music":
        return "non_music_segment_class"
    if candidate.ephemeral:
        return "ephemeral"
    if not candidate.artist.strip() or not candidate.title.strip():
        return "missing_identity"
    if blocklist and (_normalize_identity(candidate.artist), _normalize_identity(candidate.title)) in blocklist:
        return "blocklisted"
    if _has_blocked_metadata_marker(candidate.metadata):
        return "ephemeral_or_dynamic_marker"
    if not _is_cache_backed(candidate.path, cache_dir):
        return "not_cache_backed"
    if _looks_temporary_path(candidate.path):
        return "temporary_path"
    if not candidate.path.exists():
        return "missing_file"
    try:
        if candidate.path.stat().st_size <= 0:
            return "empty_file"
    except OSError:
        return "missing_file"
    if _validated_duration(candidate.path, candidate.duration_sec, duration_probe) is None:
        return "invalid_duration"
    return None


def _entry_rejection_reason(
    cache_dir: Path | str,
    entry: RestartHandoffEntry,
    *,
    blocklist: Mapping[BlockKey, object] | None,
    now: float,
    max_age_sec: float,
    duration_probe: DurationProbe,
) -> str | None:
    if entry.segment_class != "music":
        return "non_music_segment_class"
    if not entry.artist.strip() or not entry.title.strip():
        return "missing_identity"
    if blocklist and entry.block_key in blocklist:
        return "blocklisted"
    if _has_blocked_metadata_marker(entry.metadata):
        return "ephemeral_or_dynamic_marker"
    if entry.created_at <= 0 or entry.created_at > now + 60:
        return "invalid_created_at"
    if now - entry.created_at > max_age_sec:
        return "too_old"
    if not _valid_positive_duration(entry.duration_sec):
        return "invalid_duration"

    path = entry.path(cache_dir)
    if path is None:
        return "invalid_path"
    if _looks_temporary_path(path):
        return "temporary_path"
    if not path.exists():
        return "missing_file"
    try:
        stat = path.stat()
    except OSError:
        return "missing_file"
    if stat.st_size <= 0:
        return "empty_file"
    if stat.st_size != entry.size_bytes:
        return "size_mismatch"
    try:
        if _sha256_file(path) != entry.sha256:
            return "hash_mismatch"
    except OSError:
        # File vanished/became unreadable between stat() and the hash read
        # (concurrent prune, disk hiccup) — reject, never raise into startup.
        return "missing_file"
    if _validated_duration(path, entry.duration_sec, duration_probe) is None:
        return "invalid_duration"
    return None


def _publish_manifest(cache_dir: Path | str, manifest: RestartHandoffManifest) -> None:
    path = restart_handoff_manifest_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=_MANIFEST_TMP_PREFIX, suffix=_TMP_SUFFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _prune_unreferenced_segments(
    cache_dir: Path | str,
    manifest: RestartHandoffManifest,
    *,
    protected_paths: Iterable[Path | str] | None = None,
) -> None:
    segments_dir = _segments_dir(cache_dir)
    try:
        segments_root = segments_dir.resolve(strict=False)
    except OSError as exc:
        logger.warning("Failed to resolve restart handoff segments dir for pruning: %s", exc)
        return

    referenced: set[Path] = set()
    for entry in manifest.entries:
        path = _resolve_relative_to_handoff(cache_dir, entry.relative_path)
        if path is None:
            continue
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(segments_root)
        except ValueError:
            continue
        referenced.add(resolved)

    # Never delete files still referenced by the live queue (startup-admitted
    # handoff segments not yet played). Resolved to match the deletion check.
    for raw in protected_paths or ():
        try:
            referenced.add(Path(raw).resolve(strict=False))
        except OSError:
            continue

    try:
        paths = list(segments_dir.rglob(f"*{_AUDIO_SUFFIX}"))
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Failed to scan restart handoff segments for pruning: %s", exc)
        return

    for path in paths:
        try:
            if not path.is_file():
                continue
            if path.resolve(strict=False) in referenced:
                continue
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Failed to prune unreferenced restart handoff segment %s: %s", path, exc)


def _copy_and_hash(source: Path, segments_dir: Path, artist: str, title: str) -> tuple[Path, str, int]:
    segments_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=str(segments_dir), prefix=_HANDOFF_TMP_PREFIX, suffix=_TMP_SUFFIX)
    try:
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            while chunk := src.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
        hexdigest = digest.hexdigest()
        final = segments_dir / f"{hexdigest[:16]}_{_slugify_label(artist, title)}{_AUDIO_SUFFIX}"
        os.replace(tmp_name, final)
        return final, hexdigest, size
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _segments_dir(cache_dir: Path | str) -> Path:
    return restart_handoff_dir(cache_dir) / SEGMENTS_DIRNAME


def _safe_mtime_for_sort(path: Path) -> float:
    """mtime for cap-sorting only; a vanished/unreadable file sorts first (oldest) so the
    per-file loop's own FileNotFoundError/OSError handling still gets a chance at it."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune_stale_tmp_glob(directory: Path, pattern: str, cutoff: float, *, cache_root: Path) -> int:
    try:
        resolved_dir = directory.resolve(strict=False)
        resolved_dir.relative_to(cache_root)
    except ValueError:
        logger.warning(
            "Skipping restart handoff scratch cleanup outside cache dir: %s -> %s",
            directory,
            resolved_dir,
        )
        return 0
    except (OSError, RuntimeError) as exc:
        # RuntimeError: Path.resolve() raises this (not OSError) on a symlink
        # loop. Startup cleanup must never crash the app over a broken cache dir.
        logger.warning("Failed to resolve restart handoff scratch cleanup dir %s: %s", directory, exc)
        return 0

    if directory.is_symlink() and not directory.exists():
        logger.warning(
            "Failed to resolve restart handoff scratch cleanup dir %s: symlink target unavailable",
            directory,
        )
        return 0

    if not directory.is_dir():
        if directory.is_symlink():
            try:
                directory.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                logger.warning("Failed to resolve restart handoff scratch cleanup dir %s: %s", directory, exc)
        return 0
    pruned = 0
    try:
        # Bounded at the enumeration itself, not just after the fact: a plain
        # list(directory.glob(pattern)) would materialize (and, below, stat-sort)
        # every match before _MAX_SCRATCH_PRUNE_PER_PASS ever gets a chance to
        # apply, so a truly pathological backlog could still cost unbounded
        # scan time/memory even though unlink() calls stay capped.
        # Not unit-tested past the cap: simulating a glob()-time OSError (e.g. a
        # permission change mid-scan) isn't portable across CI environments
        # (chmod tricks don't reproduce reliably when tests run as root).
        paths: list[Path] = []
        overflowed = False
        for candidate in directory.glob(pattern):
            if len(paths) >= _MAX_SCRATCH_GLOB_CANDIDATES:
                overflowed = True
                break
            paths.append(candidate)
    except OSError as exc:
        logger.warning("Failed to scan restart handoff scratch files in %s: %s", directory, exc)
        return 0
    if overflowed:
        logger.warning(
            "Restart handoff scratch cleanup in %s exceeded %d raw candidates; "
            "stopped scanning early (remainder is picked up on a future boot)",
            directory,
            _MAX_SCRATCH_GLOB_CANDIDATES,
        )
    if len(paths) > _MAX_SCRATCH_PRUNE_PER_PASS:
        logger.warning(
            "Restart handoff scratch cleanup in %s found %d candidates; capping this pass at %d "
            "(oldest first — the remainder is picked up on a future boot)",
            directory,
            len(paths),
            _MAX_SCRATCH_PRUNE_PER_PASS,
        )
        paths = sorted(paths, key=_safe_mtime_for_sort)[:_MAX_SCRATCH_PRUNE_PER_PASS]
    for path in paths:
        try:
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            pruned += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Failed to prune restart handoff scratch file %s: %s", path, exc)
    return pruned


def _relative_to_handoff(cache_dir: Path | str, path: Path) -> str:
    return path.relative_to(restart_handoff_dir(cache_dir)).as_posix()


def _resolve_relative_to_handoff(cache_dir: Path | str, relative_path: str) -> Path | None:
    if not relative_path:
        return None
    raw = Path(relative_path)
    if raw.is_absolute():
        return None
    root = restart_handoff_dir(cache_dir).resolve(strict=False)
    resolved = (root / raw).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if resolved.suffix.lower() != _AUDIO_SUFFIX:
        return None
    return resolved


def _is_cache_backed(path: Path, cache_dir: Path | str) -> bool:
    try:
        path.resolve(strict=False).relative_to(Path(cache_dir).resolve(strict=False))
    except ValueError:
        return False
    return True


def _looks_temporary_path(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith(".") or ".tmp" in name or ".staging" in name or name.endswith((".part", ".download", ".tmp"))


def _has_blocked_metadata_marker(metadata: Mapping[str, Any] | object) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    return any(bool(metadata.get(flag)) for flag in _METADATA_BLOCK_FLAGS)


def _validated_duration(path: Path, fallback_duration: float, duration_probe: DurationProbe) -> float | None:
    probed = duration_probe(path)
    if probed is None:
        return None
    duration = _coerce_float(probed)
    if duration is None or not _valid_positive_duration(duration):
        return None
    fallback = _coerce_float(fallback_duration)
    return fallback if fallback is not None and _valid_positive_duration(fallback) else duration


def _valid_positive_duration(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _slugify_label(artist: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", f"{artist}_{title}".lower()).strip("_")
    return slug[:64] or "music"


def _normalize_identity(value: str) -> str:
    return value.strip().lower()


def _coerce_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str | float):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        out = float(value)
        return out if math.isfinite(out) else None
    if not isinstance(value, str | int | float):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _json_safe(value: object) -> Any:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    return str(value)
