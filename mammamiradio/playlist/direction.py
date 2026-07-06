"""Free-text music direction expansion for host-directed blocks."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from mammamiradio.core.config import StationConfig, resolve_model
from mammamiradio.core.models import StationState, Track
from mammamiradio.hosts.scriptwriter import _generate_json_response, _sanitize_prompt_data, has_script_llm
from mammamiradio.playlist.music_admission import (
    YOUTUBE_ADMISSION_SEARCH_DEPTH,
    classify_youtube_candidate,
)
from mammamiradio.playlist.playlist import normalized_track_key

logger = logging.getLogger(__name__)

MAX_DIRECTION_TEXT_CHARS = 120
MAX_DIRECTION_TARGETS = 10
DEFAULT_DIRECTION_TARGETS = 8
MIN_DIRECTION_TARGETS = 4
# Overall wall-clock ceiling for resolving a target set to tracks (concurrent
# yt-dlp searches). Bounds the background restore so a stalled search can't leave
# a restored course 'resolving' indefinitely.
RESOLVE_DIRECTION_TIMEOUT_SECONDS = 45


@dataclass(frozen=True)
class DirectionTarget:
    artist: str
    title: str

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()

    def to_dict(self) -> dict[str, str]:
        return {"artist": self.artist, "title": self.title}


@dataclass(frozen=True)
class DirectionExpansion:
    label: str
    targets: list[DirectionTarget]
    source: str

    @property
    def target_dicts(self) -> list[dict[str, str]]:
        return [target.to_dict() for target in self.targets]


@dataclass(frozen=True)
class DirectionResolution:
    track: Track | None
    rejected_track: Track | None = None
    rejected_reason: str = ""


_CONTROL_RE = re.compile(r"[\x00-\x1f<>]+")
_SPACE_RE = re.compile(r"\s+")

_CURATED_TARGETS: tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], ...] = (
    (
        ("2000", "female", "vocals", "divas"),
        (
            ("Britney Spears", "Toxic"),
            ("Christina Aguilera", "Fighter"),
            ("Fergie", "Big Girls Don't Cry"),
            ("Nelly Furtado", "Maneater"),
            ("Shakira", "Hips Don't Lie"),
            ("Gwen Stefani", "Hollaback Girl"),
            ("Rihanna", "Don't Stop The Music"),
            ("Alicia Keys", "No One"),
        ),
    ),
    (
        ("sunday", "morning", "italian", "domenica"),
        (
            ("Lucio Battisti", "Il mio canto libero"),
            ("Fabio Concato", "Domenica bestiale"),
            ("Paolo Conte", "Via con me"),
            ("Pino Daniele", "Napule e"),
            ("Ornella Vanoni", "L'appuntamento"),
            ("Lucio Dalla", "Caruso"),
            ("Mina", "Parole parole"),
            ("Negramaro", "Meraviglioso"),
        ),
    ),
    (
        ("gym", "workout", "high energy", "energia"),
        (
            ("The Black Eyed Peas", "Pump It"),
            ("Lady Gaga", "Bad Romance"),
            ("David Guetta", "Titanium"),
            ("Avicii", "Levels"),
            ("Dua Lipa", "Physical"),
            ("Rihanna", "Don't Stop The Music"),
            ("Gigi D'Agostino", "L'amour Toujours"),
            ("Benny Benassi", "Satisfaction"),
        ),
    ),
    (
        ("80", "eighties", "anni 80"),
        (
            ("Eurythmics", "Sweet Dreams"),
            ("Madonna", "Like a Prayer"),
            ("Whitney Houston", "I Wanna Dance with Somebody"),
            ("Sabrina", "Boys"),
            ("Spagna", "Call Me"),
            ("Gianna Nannini", "Bello e impossibile"),
            ("Raf", "Self Control"),
            ("Europe", "The Final Countdown"),
        ),
    ),
)

_GENERIC_TARGETS: tuple[tuple[str, str], ...] = (
    ("Tiziano Ferro", "Xdono"),
    ("Laura Pausini", "La solitudine"),
    ("Eros Ramazzotti", "Piu bella cosa"),
    ("Giorgia", "Gocce di memoria"),
    ("Cesare Cremonini", "Marmellata #25"),
    ("Jovanotti", "A te"),
    ("Elisa", "Luce"),
    ("Negramaro", "Estate"),
)


def normalize_direction_text(text: str) -> str:
    """Return safe operator direction text for prompts, labels, and persistence."""
    cleaned = _CONTROL_RE.sub(" ", str(text or ""))
    cleaned = _SPACE_RE.sub(" ", cleaned).strip()
    return cleaned[:MAX_DIRECTION_TEXT_CHARS]


def _normalize_field(value: object, *, max_len: int = 80) -> str:
    text = _sanitize_prompt_data(str(value or ""), max_len=max_len)
    text = _SPACE_RE.sub(" ", text).strip(" -")
    return text[:max_len]


def _coerce_targets(raw_targets: object, *, limit: int) -> list[DirectionTarget]:
    if not isinstance(raw_targets, list):
        return []
    targets: list[DirectionTarget] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        artist = _normalize_field(item.get("artist"))
        title = _normalize_field(item.get("title"))
        if not artist or not title:
            continue
        key = (artist.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        targets.append(DirectionTarget(artist=artist, title=title))
        if len(targets) >= limit:
            break
    return targets


def _fallback_targets(text: str, *, limit: int) -> list[DirectionTarget]:
    lowered = text.lower()
    best: tuple[tuple[str, str], ...] = _GENERIC_TARGETS
    best_hits = 0
    for tokens, targets in _CURATED_TARGETS:
        hits = sum(1 for token in tokens if token in lowered)
        if hits > best_hits:
            best_hits = hits
            best = targets
    return [DirectionTarget(artist=artist, title=title) for artist, title in best[:limit]]


def _fallback_expansion(text: str, *, limit: int) -> DirectionExpansion:
    label = _normalize_field(text, max_len=70) or "a requested set"
    return DirectionExpansion(label=label, targets=_fallback_targets(label, limit=limit), source="curated")


def _direction_prompt(text: str, *, limit: int) -> str:
    safe_text = _sanitize_prompt_data(text, max_len=MAX_DIRECTION_TEXT_CHARS)
    return f"""Expand this radio music direction into a coherent, findable block.

Direction from operator: {safe_text}

Rules:
- Return only real, widely findable songs with artist and title.
- Build one coherent set, not a generic chart dump.
- Prefer recognizable tracks that yt-dlp/YouTube search can resolve.
- No ad-style claims, no product endorsements, no fictional brands.
- Do not include commentary, URLs, albums, years, or reasons.
- Keep the label short enough for a radio host to say naturally.

Return JSON exactly:
{{"label":"short course label","targets":[{{"artist":"Artist","title":"Song"}}]}}

Return {limit} targets."""


async def expand_direction(
    text: str,
    config: StationConfig,
    state: StationState,
    *,
    limit: int = DEFAULT_DIRECTION_TARGETS,
) -> DirectionExpansion:
    """Expand free text into concrete song targets; never raise into route/audio paths."""
    safe_text = normalize_direction_text(text)
    limit = max(MIN_DIRECTION_TARGETS, min(int(limit or DEFAULT_DIRECTION_TARGETS), MAX_DIRECTION_TARGETS))
    if not safe_text:
        return DirectionExpansion(label="", targets=[], source="empty")
    if has_script_llm(config):
        try:
            data = await _generate_json_response(
                prompt=_direction_prompt(safe_text, limit=limit),
                config=config,
                state=state,
                model=resolve_model(config.models, "direction", "anthropic"),
                max_tokens=900,
                caller="direction",
                role="creative",
            )
            targets = _coerce_targets(data.get("targets"), limit=limit)
            label = _normalize_field(data.get("label"), max_len=70) or _normalize_field(safe_text, max_len=70)
            if targets:
                return DirectionExpansion(label=label, targets=targets, source="llm")
        except Exception:
            logger.warning("Direction expansion failed; using curated fallback", exc_info=True)
    return _fallback_expansion(safe_text, limit=limit)


def target_dicts_to_targets(
    raw_targets: list[dict[str, str]], *, limit: int = MAX_DIRECTION_TARGETS
) -> list[DirectionTarget]:
    return _coerce_targets(raw_targets, limit=max(1, min(limit, MAX_DIRECTION_TARGETS)))


def find_existing_direction_tracks(playlist: list[Track], targets: list[DirectionTarget]) -> list[Track]:
    """Return playlist tracks that already satisfy direction targets."""
    by_key = {normalized_track_key(track): track for track in playlist}
    existing: list[Track] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        probe = Track(title=target.title, artist=target.artist, duration_ms=0)
        key = normalized_track_key(probe)
        if key in seen:
            continue
        seen.add(key)
        track = by_key.get(key)
        if track is not None:
            existing.append(track)
    return existing


def track_from_direction_search(
    target: DirectionTarget,
    metadata: dict[str, Any],
    *,
    default_duration_ms: int | None = 180_000,
) -> Track | None:
    youtube_id = str(metadata.get("youtube_id") or "").strip()
    if not youtube_id:
        return None
    try:
        raw_duration_ms = metadata.get("duration_ms")
        if raw_duration_ms is None:
            raise ValueError("missing duration")
        duration_ms = int(raw_duration_ms)
    except (TypeError, ValueError):
        duration_ms = default_duration_ms or 0
    return Track(
        title=target.title,
        artist=target.artist,
        duration_ms=max(0, duration_ms),
        youtube_id=youtube_id,
        album_art=str(metadata.get("album_art") or "").strip(),
        source="youtube",
    )


def resolve_direction_search_results(
    target: DirectionTarget,
    metadata_results: list[dict[str, Any]],
    *,
    playlist: list[Track] | None = None,
    pacing: Any | None = None,
) -> DirectionResolution:
    """Pick the first admissible search result for a direction target."""
    first_rejected_track: Track | None = None
    first_rejected_reason = ""
    for metadata in metadata_results:
        admission_playlist = playlist
        admission_pacing = pacing
        admission_enabled = admission_playlist is not None and admission_pacing is not None
        track = track_from_direction_search(
            target,
            metadata,
            default_duration_ms=0 if admission_enabled else 180_000,
        )
        if track is None:
            continue
        if admission_playlist is not None and admission_pacing is not None:
            verdict = classify_youtube_candidate(track, admission_playlist, admission_pacing, metadata=metadata)
            if not verdict.accepted:
                logger.info(
                    "Direction candidate held out of rotation before download: %s (query=%r reason=%s)",
                    track.display,
                    target.query,
                    verdict.reason,
                )
                if first_rejected_track is None:
                    first_rejected_track = track
                    first_rejected_reason = verdict.reason
                continue
        return DirectionResolution(track=track)
    return DirectionResolution(track=None, rejected_track=first_rejected_track, rejected_reason=first_rejected_reason)


def resolve_direction_tracks_sync(
    targets: list[DirectionTarget],
    *,
    max_targets: int = DEFAULT_DIRECTION_TARGETS,
    playlist: list[Track] | None = None,
    pacing: Any | None = None,
) -> list[Track]:
    """Search yt-dlp metadata for direction targets; blocking, bounded, and best-effort."""
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    tracks: list[Track] = []
    seen: set[tuple[str, str]] = set()
    depth = YOUTUBE_ADMISSION_SEARCH_DEPTH if playlist is not None and pacing is not None else 1
    for target in targets[: max(1, max_targets)]:
        try:
            metadata = search_ytdlp_metadata(target.query, depth)
        except Exception:
            logger.debug("Direction target search failed for %s", target.query, exc_info=True)
            metadata = []
        if not metadata:
            continue
        track = resolve_direction_search_results(target, metadata, playlist=playlist, pacing=pacing).track
        if track is None:
            continue
        key = normalized_track_key(track)
        if key in seen:
            continue
        seen.add(key)
        tracks.append(track)
    return tracks


async def resolve_direction_tracks(
    targets: list[DirectionTarget],
    *,
    max_targets: int = DEFAULT_DIRECTION_TARGETS,
    playlist: list[Track] | None = None,
    pacing: Any | None = None,
) -> list[Track]:
    """Resolve targets to tracks concurrently and best-effort, bounded by an overall
    timeout. Each per-target yt-dlp search runs off the event loop via
    ``asyncio.to_thread``; used by the post-restart background restore, so it must
    never hang (a stalled search would leave the course 'resolving' forever)."""
    from mammamiradio.playlist.downloader import search_ytdlp_metadata

    bounded = targets[: max(1, max_targets)]
    depth = YOUTUBE_ADMISSION_SEARCH_DEPTH if playlist is not None and pacing is not None else 1

    async def _resolve_one(target: DirectionTarget) -> Track | None:
        try:
            metadata = await asyncio.to_thread(search_ytdlp_metadata, target.query, depth)
        except Exception:
            logger.debug("Direction target search failed for %s", target.query, exc_info=True)
            return None
        if not metadata:
            return None
        return resolve_direction_search_results(target, metadata, playlist=playlist, pacing=pacing).track

    try:
        resolved = await asyncio.wait_for(
            asyncio.gather(*[_resolve_one(target) for target in bounded], return_exceptions=True),
            timeout=RESOLVE_DIRECTION_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Direction target resolution timed out/failed", exc_info=True)
        return []

    tracks: list[Track] = []
    seen: set[tuple[str, str]] = set()
    for item in resolved:
        if not isinstance(item, Track):
            continue
        key = normalized_track_key(item)
        if key in seen:
            continue
        seen.add(key)
        tracks.append(item)
    return tracks
