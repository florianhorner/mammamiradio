"""Pure serializer mapping a frozen radio-state snapshot to the v1 contract.

The route in ``now_playing.py`` captures an atomic snapshot, then this
serializer transforms it. The function is pure (no I/O, no clock reads) so
the response is deterministic and the same snapshot drives both the body
and the ETag fingerprint.

The ``SAFE_METADATA_KEYS`` allowlist locks the contract against accidental
leakage of internal fields like ``direct_url``, ``local_path``, signed URLs,
or error strings carried inside ``Segment.metadata``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from mammamiradio.core.models import SegmentType
from mammamiradio.integrations.schema import (
    AudioFormat,
    NowPlayingBlock,
    NowPlayingResponse,
    SegmentClass,
    SessionState,
    StationBlock,
    StreamBlock,
    UpNextItem,
)

SCHEMA_VERSION = "1"

SAFE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "title",
        "title_only",
        "artist",
        "album",
        "album_art",
        "spotify_id",
        "youtube_id",
        "musicbrainz_id",
        "host",
        "year",
        "source_kind",
    }
)

EXTERNAL_ID_FIELDS: tuple[tuple[str, str], ...] = (
    ("spotify", "spotify_id"),
    ("youtube", "youtube_id"),
    ("musicbrainz", "musicbrainz_id"),
)


@dataclass(frozen=True)
class NowPlayingSnapshot:
    """Atomic snapshot of the radio state needed to render the v1 contract.

    Captured at the top of the route handler so the serializer never reads
    mutable state during a segment transition.
    """

    now_streaming: dict
    queued_segments: tuple[dict, ...]
    upcoming_predicted: tuple[dict, ...]
    session_stopped: bool
    playback_epoch: int
    station: StationBlock
    audio_format: dict[str, Any]
    relative_stream_url: str
    absolute_stream_url: str | None
    changed_at: float
    up_next_limit: int = 8
    upcoming_mode: str = "queued"
    extra_context: dict = field(default_factory=dict)


def _segment_class_for_now_streaming(now: dict) -> SegmentClass:
    """Map an in-flight ``now_streaming`` dict to its display bucket.

    Handles the two transient sentinel shapes (``{"type": "stopped"}``
    written by /api/stop and ``{"type": "skipping"}`` written by /api/skip)
    by returning ``"unavailable"`` so consumers don't render them as music.
    """
    raw_type = str(now.get("type") or "")
    if not raw_type:
        return "unavailable"
    try:
        return SegmentType(raw_type).segment_class  # type: ignore[attr-defined]
    except (ValueError, AttributeError):
        return "unavailable"


def _safe_metadata(metadata: Any) -> dict:
    """Allowlist-filter a segment's metadata before returning to a consumer."""
    if not isinstance(metadata, dict):
        return {}
    return {k: metadata[k] for k in metadata.keys() & SAFE_METADATA_KEYS}


def _external_ids_from_metadata(safe_meta: dict) -> dict[str, str]:
    """Map allowlisted metadata fields to a provider->id map."""
    result: dict[str, str] = {}
    for provider, source_key in EXTERNAL_ID_FIELDS:
        raw = safe_meta.get(source_key)
        if isinstance(raw, str) and raw.strip():
            result[provider] = raw.strip()
    return result


def _title_for_segment(now: dict, safe_meta: dict) -> str | None:
    """Pick the cleanest display title for the current segment."""
    for key in ("title_only", "title"):
        value = safe_meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    label = now.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return None


def _build_now_playing(now: dict) -> NowPlayingBlock:
    """Render the ``now_playing`` block from a non-empty segment dict.

    Always returns the same shape — absent optional fields appear as
    ``null`` (or ``{}`` for ``external_ids`` and ``context``) so consumers
    can rely on the keyset. The caller (``serialize_now_playing``) only
    invokes this when ``session_state == "live"``, which the classifier
    guarantees by checking ``now_streaming`` is non-empty first.
    """
    seg_class = _segment_class_for_now_streaming(now)
    raw_type = str(now.get("type") or "")
    safe_meta = _safe_metadata(now.get("metadata"))
    started = now.get("started")
    duration = now.get("duration_sec")
    block: NowPlayingBlock = {
        "segment_class": seg_class,
        "segment_type": raw_type,
        "title": _title_for_segment(now, safe_meta),
        "started_at": float(started) if isinstance(started, int | float) else None,
        "duration_estimate_sec": (float(duration) if isinstance(duration, int | float) and duration > 0 else None),
        "artist": None,
        "artwork": None,
        "album": None,
        "year": None,
        "external_ids": {},
        "host": None,
        "context": {},
    }
    if seg_class == "music":
        artist = safe_meta.get("artist")
        if isinstance(artist, str) and artist.strip():
            block["artist"] = artist.strip()
        artwork = safe_meta.get("album_art")
        if isinstance(artwork, str) and artwork.strip():
            block["artwork"] = artwork.strip()
        album = safe_meta.get("album")
        if isinstance(album, str) and album.strip():
            block["album"] = album.strip()
        year = safe_meta.get("year")
        if isinstance(year, int) and year > 0:
            block["year"] = year
        block["external_ids"] = _external_ids_from_metadata(safe_meta)
    elif seg_class == "voice":
        host = safe_meta.get("host")
        if isinstance(host, str) and host.strip():
            block["host"] = host.strip()
    return block


def _segment_class_for_up_next(item: dict) -> SegmentClass:
    """Map an up_next item dict to its display bucket."""
    raw_type = str(item.get("type") or "")
    if not raw_type:
        return "unavailable"
    try:
        return SegmentType(raw_type).segment_class  # type: ignore[attr-defined]
    except (ValueError, AttributeError):
        return "unavailable"


def _build_up_next(snapshot: NowPlayingSnapshot) -> list[UpNextItem]:
    """Render the up_next list from queued + predicted items in snapshot.

    Queued items always render. Predicted items render only when they carry
    a meaningful title — the scheduler emits ``label="?"`` for music slots
    with no playable tracks, which would be useless noise to a consumer.
    """
    items: list[UpNextItem] = []
    for raw in snapshot.queued_segments[: snapshot.up_next_limit]:
        items.append(
            UpNextItem(
                segment_class=_segment_class_for_up_next(raw),
                segment_type=str(raw.get("type") or ""),
                title=str(raw.get("label") or ""),
                predicted=False,
            )
        )
    remaining = snapshot.up_next_limit - len(items)
    if remaining > 0:
        for raw in snapshot.upcoming_predicted[:remaining]:
            title = str(raw.get("label") or "").strip()
            if not title or title == "?":
                continue
            items.append(
                UpNextItem(
                    segment_class=_segment_class_for_up_next(raw),
                    segment_type=str(raw.get("type") or ""),
                    title=title,
                    predicted=True,
                )
            )
    return items


def _classify_session_state(snapshot: NowPlayingSnapshot) -> SessionState:
    """Map snapshot fields to the stable session_state literal.

    Order matters:
    1. ``session_stopped`` flag is the only authority for ``stopped``. It is
       admin-driven and persisted across restart, so consumers can trust it.
    2. When ``session_stopped`` is False, a leftover ``{"type": "stopped"}``
       sentinel in ``now_streaming`` is treated as ``empty_queue``: /api/resume
       clears ``session_stopped`` first but the sentinel lingers until the
       producer fires ``on_stream_segment``. Classifying that window as
       ``stopped`` would lie to consumers right after the operator pressed
       resume.
    3. A live ``{"type": "skipping"}`` sentinel is mid-transition, so the
       session is ``live`` (now_playing renders as ``unavailable``).
    4. Any other non-empty ``now_streaming`` is ``live``.
    5. Empty snapshot means ``empty_queue`` regardless of ``up_next`` content
       — predictions are speculation, not a real queue.
    """
    if snapshot.session_stopped:
        return "stopped"
    transient = str(snapshot.now_streaming.get("type") or "")
    if transient == "stopped":
        # Stale sentinel after /api/resume — treat as empty_queue.
        return "empty_queue"
    if snapshot.now_streaming:
        return "live"
    return "empty_queue"


def serialize_now_playing(snapshot: NowPlayingSnapshot) -> NowPlayingResponse:
    """Pure transform: ``NowPlayingSnapshot`` → v1 response dict."""
    stream: StreamBlock = {
        "relative_url": snapshot.relative_stream_url,
        "audio_format": cast(AudioFormat, snapshot.audio_format),
    }
    if snapshot.absolute_stream_url:
        stream["absolute_url"] = snapshot.absolute_stream_url
    session_state = _classify_session_state(snapshot)
    # stopped → no segment is meaningful. empty_queue → nothing playing yet
    # (covers the post-resume window where the stale sentinel still sits in
    # ``now_streaming`` but session_stopped is False).
    now_playing = _build_now_playing(snapshot.now_streaming) if session_state == "live" else None
    return NowPlayingResponse(
        schema_version=SCHEMA_VERSION,
        station=snapshot.station,
        stream=stream,
        now_playing=now_playing,
        up_next=_build_up_next(snapshot),
        session_state=session_state,
        changed_at=snapshot.changed_at,
    )
