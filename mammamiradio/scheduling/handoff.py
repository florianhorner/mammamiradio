"""Private exact-once music-to-speech handoff ownership.

The live queue is the single source of truth for whether a song tail is still
available.  A prepared speech render may use that tail only after its queued
music predecessor and successor are committed as one pair.  Control paths call
the reconciliation helpers below after every synchronous queue rewrite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.web.mp3_frames import Mp3HandoffSplit

logger = logging.getLogger(__name__)


class HandoffPhase(StrEnum):
    """Private playback lifecycle for one committed music/speech pair."""

    QUEUED = "queued"
    HEAD_ACTIVE = "head_active"
    SUCCESSOR_DUE = "successor_due"
    SUCCESSOR_ACTIVE = "successor_active"


@dataclass(frozen=True)
class PreparedMusicHandoff:
    """Frame-aligned handoff material prepared before any queue mutation."""

    music_segment: Segment
    source_path: Path
    split: Mp3HandoffSplit
    # The egress path can be atomically replaced by cache maintenance while a
    # renderer is working. Keep the cheap stat fingerprint from preparation so
    # final admission refuses a tail cut from older bytes at the same pathname.
    source_size: int | None = None
    source_mtime_ns: int | None = None


@dataclass
class HandoffReservation:
    """One committed music-head / speech-tail queue pair.

    ``original_*`` stays private so a control that drops the successor before
    music begins can restore the listener's complete song instead of silently
    removing its outro.
    """

    handoff_id: str
    music_segment: Segment
    successor_segment: Segment
    original_path: Path
    original_duration_sec: float
    original_ephemeral: bool
    head_path: Path
    phase: HandoffPhase = HandoffPhase.QUEUED


def _queue_items(queue) -> list[Segment] | None:
    raw = getattr(queue, "_queue", None)
    if raw is None:
        return None
    return list(raw)


def _identity_index(items: list[Segment], target: Segment) -> int:
    return next((index for index, item in enumerate(items) if item is target), -1)


def _reservations(state: StationState) -> dict[str, HandoffReservation]:
    """Return the private reservation registry without trusting test doubles.

    Production ``StationState`` always owns this mapping.  Keeping the small
    guard here lets older, intentionally partial state fixtures continue to
    exercise unrelated queue controls without manufacturing handoff state.
    """

    registry = getattr(state, "handoff_reservations", None)
    return registry if isinstance(registry, dict) else {}


def _is_real_music_candidate(segment: Segment) -> bool:
    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    return bool(
        segment.type is SegmentType.MUSIC
        and not segment.handoff_id
        and not metadata.get("air_next")
        and not metadata.get("error")
        and not metadata.get("fallback")
        and not metadata.get("rescue")
        and metadata.get("audio_source") != "emergency_tone"
    )


def _prepared_source_is_current(prepared: PreparedMusicHandoff) -> bool:
    """Verify a prepared tail still matches the queued egress bytes."""

    if prepared.source_size is None or prepared.source_mtime_ns is None:
        # Narrow compatibility for old direct callers; live preparation always
        # supplies the fingerprint below.
        return True
    try:
        stat = prepared.source_path.stat()
    except OSError:
        return False
    return stat.st_size == prepared.source_size and stat.st_mtime_ns == prepared.source_mtime_ns


def peek_music_handoff_candidate(queue) -> Segment | None:
    """Return only an unstarted normal music segment at the queue tail."""

    items = _queue_items(queue)
    if not items:
        return None
    candidate = items[-1]
    if not _is_real_music_candidate(candidate):
        return None
    try:
        return candidate if candidate.path.is_file() else None
    except OSError:
        return None


def update_music_shadow_duration(state: StationState, segment: Segment) -> None:
    """Keep Scaletta's queued duration aligned with a reserved music head."""

    metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
    queue_id = str(metadata.get("queue_id") or "")
    if not queue_id:
        return
    for row in state.queued_segments:
        if str(row.get("id") or "") == queue_id:
            row["duration_sec"] = round(float(segment.duration_sec or 0.0), 1)
            return


def commit_music_handoff(
    queue,
    state: StationState,
    prepared: PreparedMusicHandoff,
    successor: Segment,
    successor_shadow: dict,
) -> bool:
    """Atomically replace a queued song with its head and append its successor.

    This function has no await points.  It refuses a stale candidate rather
    than allowing a rendered tail to replay after the song has started or a
    control has changed program order.
    """

    items = _queue_items(queue)
    music = prepared.music_segment
    if (
        state.session_stopped
        or items is None
        or not items
        or items[-1] is not music
        or music.path != prepared.source_path
        or not _prepared_source_is_current(prepared)
        or not _is_real_music_candidate(music)
        or (queue.maxsize and queue.full())
    ):
        return False

    handoff_id = uuid4().hex
    reservation = HandoffReservation(
        handoff_id=handoff_id,
        music_segment=music,
        successor_segment=successor,
        original_path=music.path,
        original_duration_sec=float(music.duration_sec or 0.0),
        original_ephemeral=bool(music.ephemeral),
        head_path=prepared.split.head_path,
    )
    music.path = prepared.split.head_path
    music.duration_sec = prepared.split.head_duration_sec
    music.ephemeral = True
    music.handoff_id = handoff_id
    successor.handoff_id = handoff_id
    _reservations(state)[handoff_id] = reservation
    update_music_shadow_duration(state, music)
    successor.metadata["has_music_tail"] = True
    queue.put_nowait(successor)
    state.queued_segments.append(successor_shadow)
    return True


def _unlink_best_effort(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Handoff artifact cleanup failed: %s", path, exc_info=True)


def discard_prepared_music_handoff(prepared: PreparedMusicHandoff) -> None:
    """Remove uncommitted head/tail artifacts after a failed render or admission."""

    _unlink_best_effort(prepared.split.head_path)
    _unlink_best_effort(prepared.split.tail_path)


def _release_reservation(
    state: StationState,
    reservation: HandoffReservation,
    *,
    restore_music: bool,
) -> None:
    """Release one pair, restoring the original song only while unstarted."""

    music = reservation.music_segment
    successor = reservation.successor_segment
    if restore_music:
        _unlink_best_effort(reservation.head_path)
        music.path = reservation.original_path
        music.duration_sec = reservation.original_duration_sec
        music.ephemeral = reservation.original_ephemeral
        update_music_shadow_duration(state, music)
    elif reservation.original_ephemeral:
        _unlink_best_effort(reservation.original_path)
    music.handoff_id = None
    successor.handoff_id = None
    _reservations(state).pop(reservation.handoff_id, None)


def reconcile_handoff_queue_items(state: StationState, items: list[Segment]) -> list[Segment]:
    """Repair handoff pairs after a synchronous queue rewrite.

    A valid unstarted pair must be adjacent. If the successor disappeared while
    its music head remains queued, the original song is restored. If the music
    head itself was removed, the successor is discarded and the removed song is
    not silently reinserted.

    Once the head has started, its successor is a protected continuation of the
    same listener timeline. Ordinary rewrites keep it at queue-head, including
    the small cooperative-scheduling gap after clean head EOF and before the
    successor is selected. Explicit cuts/removals delete the successor before
    calling this helper, which then releases the reservation without restoring
    already-started music.
    """

    discarded: list[Segment] = []
    changed = False
    active = getattr(state, "active_playback_segment", None)
    for reservation in list(_reservations(state).values()):
        if not isinstance(reservation, HandoffReservation):
            continue
        music_index = _identity_index(items, reservation.music_segment)
        successor_index = _identity_index(items, reservation.successor_segment)
        music_is_active = active is reservation.music_segment
        successor_is_active = active is reservation.successor_segment

        if music_is_active and reservation.phase is HandoffPhase.QUEUED:
            # Compatibility for a caller that selected the head before adopting
            # mark_handoff_segment_selected().
            reservation.phase = HandoffPhase.HEAD_ACTIVE

        if successor_is_active and reservation.phase is not HandoffPhase.SUCCESSOR_ACTIVE:
            reservation.phase = HandoffPhase.SUCCESSOR_ACTIVE

        if reservation.phase in (HandoffPhase.HEAD_ACTIVE, HandoffPhase.SUCCESSOR_DUE):
            if successor_index == 0:
                continue
            if successor_index >= 0:
                successor = items.pop(successor_index)
                items.insert(0, successor)
                changed = True
                continue
            _release_reservation(state, reservation, restore_music=False)
            changed = True
            continue

        if reservation.phase is HandoffPhase.SUCCESSOR_ACTIVE:
            # The successor has already left the queue. Its send finalizer owns
            # terminal cleanup, so a simultaneous rewrite must not retire it.
            continue

        if music_index >= 0:
            if successor_index == music_index + 1:
                continue
            # The predecessor has not begun.  Even if a control merely wedged
            # another item between the pair, replaying neither a truncated
            # song nor a tail-bearing successor is acceptable: drop the
            # successor when present and put the original complete song back.
            if successor_index >= 0:
                discarded.append(items.pop(successor_index))
            _release_reservation(state, reservation, restore_music=True)
            changed = True
            continue

        if successor_index >= 0:
            discarded.append(items.pop(successor_index))
            changed = True
        _release_reservation(state, reservation, restore_music=False)

    if changed:
        state.continuity_epoch += 1
    return discarded


def cancel_active_music_handoff(state: StationState, items: list[Segment]) -> list[Segment]:
    """Drop a successor after an active/due handoff is explicitly cut.

    The compatibility name remains for producer/control callers. Besides a
    currently selected member, the function handles ``SUCCESSOR_DUE``: during
    the post-EOF queue-get yield there is intentionally no active Segment, but
    Skip/Panic/Stop still need an explicit way to cancel that protected tail.
    """

    active = getattr(state, "active_playback_segment", None)
    discarded: list[Segment] = []
    changed = False
    for reservation in list(_reservations(state).values()):
        if not isinstance(reservation, HandoffReservation):
            continue
        selected_member = active is reservation.music_segment or active is reservation.successor_segment
        if not selected_member and reservation.phase is not HandoffPhase.SUCCESSOR_DUE:
            continue
        successor_index = _identity_index(items, reservation.successor_segment)
        if successor_index >= 0:
            discarded.append(items.pop(successor_index))
        _release_reservation(state, reservation, restore_music=False)
        changed = True
    if changed:
        state.continuity_epoch += 1
    return discarded


def mark_handoff_segment_selected(state: StationState, segment: Segment) -> None:
    """Synchronously mark a queued pair member as selected by playback."""

    handoff_id = segment.handoff_id
    if not handoff_id:
        return
    reservation = _reservations(state).get(handoff_id)
    if not isinstance(reservation, HandoffReservation):
        segment.handoff_id = None
        return
    if segment is reservation.music_segment:
        reservation.phase = HandoffPhase.HEAD_ACTIVE
        # Air Next's legacy stale-claim guard predates paired handoffs and drops
        # a queue-head segment carrying this marker before reconciliation runs.
        # Once the exact predecessor is selected, reservation ownership is the
        # stronger adjacency proof: reconciliation keeps this successor first,
        # so the private marker is no longer needed and must not bypass it.
        reservation.successor_segment.metadata.pop("transition_track_ref", None)
    elif segment is reservation.successor_segment:
        reservation.phase = HandoffPhase.SUCCESSOR_ACTIVE


def has_cancellable_music_handoff(state: StationState) -> bool:
    """Return whether playback owns an active or post-EOF handoff."""

    active = getattr(state, "active_playback_segment", None)
    for reservation in _reservations(state).values():
        if not isinstance(reservation, HandoffReservation):
            continue
        if reservation.phase is HandoffPhase.SUCCESSOR_DUE:
            return True
        if active is reservation.music_segment or active is reservation.successor_segment:
            return True
    return False


def is_protected_handoff_successor(state: StationState, segment: Segment) -> bool:
    """Return whether capacity management must preserve this queued successor.

    Queue rewrites reconcile pair ordering after they have planned their final
    topology.  Their capacity planners need the same ownership signal earlier:
    once a head is active or has reached clean EOF, its successor is no longer
    an ordinary evictable speech item.
    """

    active = getattr(state, "active_playback_segment", None)
    for reservation in _reservations(state).values():
        if not isinstance(reservation, HandoffReservation) or reservation.successor_segment is not segment:
            continue
        return reservation.phase in (HandoffPhase.HEAD_ACTIVE, HandoffPhase.SUCCESSOR_DUE) or (
            active is reservation.music_segment
        )
    return False


def handoff_original_sources(state: StationState) -> list[tuple[Path, Segment]]:
    """Return full sources and identities owned by live reservations."""

    return [
        (reservation.original_path, reservation.music_segment)
        for reservation in _reservations(state).values()
        if isinstance(reservation, HandoffReservation)
    ]


def handoff_original_paths(state: StationState) -> set[Path]:
    """Return full paths that remain owned by live handoff reservations.

    A queued pair exposes only its shortened head and speech successor in the
    playback queue. The hidden original must remain cache-eviction protected
    until the reservation either restores it or retires.
    """

    return {path for path, _music in handoff_original_sources(state)}


def handoff_original_source(state: StationState, segment: Segment) -> tuple[Path, Segment] | None:
    """Return the full source and music identity affected by removing a pair member."""

    handoff_id = segment.handoff_id
    if not handoff_id:
        return None
    reservation = _reservations(state).get(handoff_id)
    if not isinstance(reservation, HandoffReservation):
        return None
    return reservation.original_path, reservation.music_segment


def finish_handoff_segment(
    state: StationState,
    segment: Segment,
    *,
    completed_cleanly: bool,
) -> None:
    """Advance or retire a selected pair member at the send boundary.

    A clean music-head EOF makes the successor due *before* playback clears the
    active pointer. An unsuccessful head must first be cancelled through
    :func:`cancel_active_music_handoff` so its queued successor is removed. A
    successor is terminal regardless of send outcome because its predecessor
    has already aired and can never be restored safely.
    """

    handoff_id = segment.handoff_id
    if not handoff_id:
        return
    reservation = _reservations(state).get(handoff_id)
    if not isinstance(reservation, HandoffReservation):
        segment.handoff_id = None
        return
    if segment is reservation.music_segment:
        if completed_cleanly:
            reservation.phase = HandoffPhase.SUCCESSOR_DUE
        return
    if segment is reservation.successor_segment:
        _release_reservation(state, reservation, restore_music=False)


def retire_handoff_for_started_segment(state: StationState, segment: Segment) -> None:
    """Compatibility shim for older callers that retired on selection.

    New playback code uses ``mark_handoff_segment_selected`` plus
    ``finish_handoff_segment`` so the clean-head boundary has an explicit due
    phase. Preserve the old successor-release behavior for external callers.
    """

    mark_handoff_segment_selected(state, segment)
    handoff_id = segment.handoff_id
    reservation = _reservations(state).get(handoff_id) if handoff_id else None
    if isinstance(reservation, HandoffReservation) and segment is reservation.successor_segment:
        _release_reservation(state, reservation, restore_music=False)
