"""Shared selection helpers for normalized music-cache bridge audio."""

from __future__ import annotations

import random
import re
from pathlib import Path

from mammamiradio.audio.normalizer import humanize_norm_filename, load_track_metadata
from mammamiradio.core.models import SegmentType, StationState, Track


def _identity_key(value: str) -> str:
    """Normalize listener-facing titles enough to compare cache fallbacks."""
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _identity_matches(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return min(len(left), len(right)) >= 12 and (left in right or right in left)


def _track_identity_keys(track: Track) -> set[str]:
    keys = {_identity_key(track.title), _identity_key(track.display)}
    if track.artist and track.title:
        keys.add(_identity_key(f"{track.artist} {track.title}"))
    return {key for key in keys if key}


def _segment_identity_keys(segment: dict) -> set[str]:
    """Return comparable labels for a streamed segment."""
    keys: set[str] = set()
    label = str(segment.get("label") or "").strip()
    if label:
        keys.add(_identity_key(label))
    metadata = segment.get("metadata") or {}
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()
        title_only = str(metadata.get("title_only") or "").strip()
        artist = str(metadata.get("artist") or "").strip()
        for value in (title, title_only):
            if value:
                keys.add(_identity_key(value))
            if value and artist:
                keys.add(_identity_key(f"{artist} {value}"))
    return {key for key in keys if key}


def _norm_cache_identity_keys(path: Path) -> set[str]:
    """Return comparable title/artist labels for a normalized cache file."""
    keys = {_identity_key(humanize_norm_filename(path.name))}
    sidecar = load_track_metadata(path)
    if sidecar:
        title = str(sidecar.get("title") or "").strip()
        artist = str(sidecar.get("artist") or "").strip()
        if title:
            keys.add(_identity_key(title))
        if title and artist:
            keys.add(_identity_key(f"{artist} {title}"))
    return {key for key in keys if key}


def _recent_music_identity_keys(state: StationState) -> set[str]:
    recent_keys: set[str] = set()
    if state.now_streaming:
        recent_keys.update(_segment_identity_keys(state.now_streaming))
    if state.current_track is not None:
        recent_keys.update(_track_identity_keys(state.current_track))
    for entry in list(state.stream_log)[-5:]:
        if entry.type == SegmentType.MUSIC.value:
            recent_keys.update(_segment_identity_keys({"label": entry.label, "metadata": entry.metadata}))
    return recent_keys


def _is_blocklisted(path: Path, blocklist: object) -> bool:
    """True if this cache file's ``(artist, title)`` is on the operator blocklist.

    The blocklist key is ``(track.artist.lower(), track.title.lower())`` and the norm
    sidecar stores exactly ``track.title``/``track.artist`` (producer.save_track_metadata),
    so the sidecar maps straight onto the ban identity. A file with no sidecar can't be
    identified and is left selectable (best-effort — banned songs almost always carry one)."""
    if not blocklist or not isinstance(blocklist, dict):
        return False
    sidecar = load_track_metadata(path)
    if not sidecar:
        return False
    key = (str(sidecar.get("artist") or "").strip().lower(), str(sidecar.get("title") or "").strip().lower())
    return key in blocklist


def select_norm_cache_rescue(cache_dir: Path, state: StationState) -> Path | None:
    """Pick a cache rescue clip without replaying the current/recent song first.

    A banned song must never re-air, even through the rescue path — so blocklisted
    cache files are dropped first, before the recent-identity de-dup. If every file is
    banned (nothing left) the rescue degrades to ``None`` and the caller's next layer
    (canned clip / forced banter) keeps audio flowing rather than airing a banned song."""
    norm_files = sorted(cache_dir.glob("norm_*.mp3"))
    blocklist = getattr(state, "blocklist", None)
    if blocklist:
        norm_files = [path for path in norm_files if not _is_blocklisted(path, blocklist)]
    if not norm_files:
        return None

    recent_keys = _recent_music_identity_keys(state)
    if not recent_keys:
        return random.choice(norm_files)

    candidates: list[Path] = []
    for path in norm_files:
        path_keys = _norm_cache_identity_keys(path)
        if not any(_identity_matches(path_key, recent_key) for path_key in path_keys for recent_key in recent_keys):
            candidates.append(path)

    return random.choice(candidates or norm_files)
