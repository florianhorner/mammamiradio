"""Shared selection helpers for normalized music-cache bridge audio."""

from __future__ import annotations

import random
import re
import time
from pathlib import Path

from mammamiradio.audio.normalizer import humanize_norm_filename, load_track_metadata
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.playlist.downloader import is_rejected_cache_key

_NORM_CACHE_KEY_RE = re.compile(r"^norm_(?P<cache_key>.+)_\d+k\.mp3$")

# A cached song that just aired as a rescue must not be picked again for a full
# hour of real time. This is what stops the same track re-airing three times in
# twenty minutes while the producer is stalled (leadership principle #1). The
# window is counted from the moment a rescue is actually heard by a listener, not
# from selection — see record_rescue_airplay.
RESCUE_COOLDOWN_SECONDS = 3600.0

# The two audio_source values every norm-cache rescue segment carries: the
# producer/live-control bridge stamps "norm_cache"; the playback-gap fill stamps
# "fallback_norm_cache". Both feed the same rotation cooldown.
_RESCUE_AUDIO_SOURCES = frozenset({"norm_cache", "fallback_norm_cache"})


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


def _norm_cache_key(path: Path) -> str:
    match = _NORM_CACHE_KEY_RE.fullmatch(path.name)
    if not match:
        return ""
    return match.group("cache_key")


def _rescue_identity(path: Path) -> str:
    """Return the stable rescue identity, ignoring bitrate-only file variants."""
    return _norm_cache_key(path) or str(path)


def _last_rescue_airplay(airplay: dict[Path, float], path: Path) -> float | None:
    """Return the newest rescue timestamp for the logical cache entry."""
    direct = airplay.get(path)
    identity = _rescue_identity(path)
    matching = [
        timestamp
        for candidate, timestamp in airplay.items()
        if candidate != path and _rescue_identity(candidate) == identity
    ]
    if direct is not None:
        matching.append(direct)
    return max(matching, default=None)


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


def _path_on_cooldown(airplay: dict[Path, float], path: Path, now: float) -> bool:
    """Single source of truth for the rescue cooldown, keyed on the cache path.

    Path-based on purpose: the normalized-cache path is deterministic per song
    (``norm_<cache_key>_<bitrate>k.mp3``), so this needs no sidecar read and stays
    off the identity hot path. A never-heard path is never on cooldown.
    """
    last = _last_rescue_airplay(airplay, path)
    return last is not None and (now - last) < RESCUE_COOLDOWN_SECONDS


def rescue_last_heard_at(state: StationState, path: Path) -> float | None:
    """Return the latest rescue timestamp for ``path`` or its cache-key siblings."""
    airplay = getattr(state, "rescue_airplay", None)
    if not airplay:
        return None
    return _last_rescue_airplay(airplay, path)


def rescue_on_cooldown(state: StationState, path: Path, *, now: float | None = None) -> bool:
    """True if ``path`` aired as a rescue within ``RESCUE_COOLDOWN_SECONDS``."""
    airplay = getattr(state, "rescue_airplay", None)
    if not airplay:
        return False
    return _path_on_cooldown(airplay, path, time.monotonic() if now is None else now)


def _choose_rescue_candidate(paths: list[Path], state: StationState | None = None) -> Path:
    """Pick a rescue file, rotating past songs still inside the airplay cooldown.

    Selection stays off the sidecar hot path: rotation is keyed on the cache
    *path*, never on title/artist. Among candidates that are eligible (never heard
    this session, or heard at least an hour ago) it shuffles, so a warm cache
    rotates naturally. When every candidate is still cooling it returns the
    least-recently-heard one — round-robin, never dead air and never an immediate
    repeat — because a real song the listener heard twenty minutes ago beats a
    sweeper, and beats the same song a third time.
    """
    if not paths:
        raise ValueError("paths must not be empty")
    airplay = getattr(state, "rescue_airplay", None) if state is not None else None
    if not airplay:
        return random.choice(paths)
    now = time.monotonic()
    eligible = [path for path in paths if not _path_on_cooldown(airplay, path, now)]
    if eligible:
        return random.choice(eligible)
    # Everything is cooling: pick the one closest to leaving the window.
    return min(paths, key=lambda path: _last_rescue_airplay(airplay, path) or 0.0)


def record_rescue_airplay(state: StationState, segment: Segment) -> None:
    """Stamp a norm-cache rescue segment as heard so it cools down before re-airing.

    Called from the playback loop's outcome recorder only when the segment truly
    aired to a listener (bytes sent, listeners present) — selecting or opening a
    rescue that never reaches a listener does not consume rotation. Best-effort:
    a bookkeeping error must never affect what plays (leadership principle #1).
    """
    try:
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        if str(metadata.get("audio_source") or "") not in _RESCUE_AUDIO_SOURCES:
            return
        path = segment.path
        if path is None:
            return
        airplay = state.rescue_airplay
        now = time.monotonic()
        airplay[path] = now
        # Keep the map bounded to the warm cache: an entry two cooldowns old can
        # never gate a selection again, so drop it instead of letting evicted
        # files accumulate across a long-running session.
        for stale in [key for key, ts in airplay.items() if now - ts > 2 * RESCUE_COOLDOWN_SECONDS]:
            airplay.pop(stale, None)
    except Exception:
        pass


def rescue_rotation_status(state: StationState) -> dict:
    """Authenticated-only rotation diagnostics (no filesystem paths).

    Derived purely from the in-memory airplay map so a status call never walks the
    cache. ``most_recent`` is the humanized song label of the last rescue heard.
    """
    airplay = getattr(state, "rescue_airplay", None) or {}
    now = time.monotonic()
    latest_by_identity: dict[str, float] = {}
    for path, timestamp in airplay.items():
        identity = _rescue_identity(path)
        latest_by_identity[identity] = max(timestamp, latest_by_identity.get(identity, timestamp))
    cooling = sum(1 for ts in latest_by_identity.values() if (now - ts) < RESCUE_COOLDOWN_SECONDS)
    most_recent = ""
    if airplay:
        newest = max(airplay, key=lambda key: airplay[key])
        most_recent = humanize_norm_filename(Path(newest).name)
    return {
        "cooldown_seconds": RESCUE_COOLDOWN_SECONDS,
        "tracked": len(latest_by_identity),
        "cooling": cooling,
        "most_recent": most_recent,
    }


def select_norm_cache_rescue(cache_dir: Path, state: StationState) -> Path | None:
    """Pick a cache rescue clip without replaying the current/recent song first.

    A banned song must never re-air, even through the rescue path — so blocklisted
    cache files are dropped first, before the recent-identity de-dup. If every file is
    banned (nothing left) the rescue degrades to ``None`` and the caller's next layer
    (canned clip / forced banter) keeps audio flowing rather than airing a banned song."""
    norm_files = sorted(cache_dir.glob("norm_*.mp3"))
    norm_files = [path for path in norm_files if not is_rejected_cache_key(_norm_cache_key(path))]
    blocklist = getattr(state, "blocklist", None)
    if blocklist:
        norm_files = [path for path in norm_files if not _is_blocklisted(path, blocklist)]
    if not norm_files:
        return None

    recent_keys = _recent_music_identity_keys(state)
    if not recent_keys:
        return _choose_rescue_candidate(norm_files, state)

    candidates: list[Path] = []
    for path in norm_files:
        path_keys = _norm_cache_identity_keys(path)
        if not any(_identity_matches(path_key, recent_key) for path_key in path_keys for recent_key in recent_keys):
            candidates.append(path)

    return _choose_rescue_candidate(candidates or norm_files, state)
