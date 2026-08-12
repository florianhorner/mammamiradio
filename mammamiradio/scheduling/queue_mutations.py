"""Synchronous mutations of queued, not-yet-airing segments.

The real playback queue and ``StationState.queued_segments`` are two views of
the same admitted work.  Mutate them in one no-``await`` critical section:

    now_streaming (already dequeued) --------------------------> untouched
    playback queue -> drain -> predicate -> survivors -> rebuild playback queue
                                  |                     \
                                  +-> discard/receipt    +-> preserve order
                                      cleanup
    queue shadow  -------------------- drop matching ids --------> rebuilt view

Keeping this boundary in ``scheduling`` lets future authorization revocations
reuse the physical-queue accounting and cleanup without reimplementing a
drain/filter/rebuild loop in an HTTP route module. Callers still own any
out-of-band slot invalidation and revision/continuity fence required by their
control action; this helper deliberately mutates only the real queue and shadow.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable

from mammamiradio.core.models import (
    LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY,
    LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY,
    LISTENER_REQUEST_INTERNAL_METADATA_KEYS,
    Segment,
    StationState,
)
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR as _DEMO_ASSETS_DIR
from mammamiradio.core.packaged_assets import is_packaged_asset

logger = logging.getLogger(__name__)

QueueDropPredicate = Callable[[Segment], bool]


def settle_listener_request_queue_dependencies(
    dropped: list[Segment],
    survivors: list[Segment],
    *,
    state: StationState,
) -> tuple[list[Segment], list[Segment]]:
    """Settle admitted music whose still-queued dedication was discarded.

    Request-exclusive music follows its removed announcement out of the queue.
    A same-recording pin borrowed from a newer operator remains queued, but loses
    the older request's admission/link metadata so it cannot masquerade as that
    dedication or bypass a later listener reservation.
    """
    linked_dedication_ids = {
        str(segment.metadata.get(LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY) or "")
        for segment in dropped
        if isinstance(segment.metadata, dict) and segment.metadata.get(LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY)
    }
    if linked_dedication_ids:
        still_surviving: list[Segment] = []
        for segment in survivors:
            metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
            if str(metadata.get("queue_id") or "") in linked_dedication_ids:
                dropped.append(segment)
            else:
                still_surviving.append(segment)
        survivors = still_surviving

    dropped_queue_ids = {
        queue_id
        for segment in dropped
        if isinstance(segment.metadata, dict)
        and isinstance((queue_id := segment.metadata.get("queue_id")), str)
        and queue_id
    }
    if not dropped_queue_ids:
        return dropped, survivors

    kept: list[Segment] = []
    for segment in survivors:
        metadata = segment.metadata if isinstance(segment.metadata, dict) else {}
        dedication_queue_id = str(metadata.get(LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY) or "")
        if dedication_queue_id not in dropped_queue_ids:
            kept.append(segment)
            continue
        if metadata.get(LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY):
            dropped.append(segment)
            continue
        state.release_listener_request_admitted_reservation(segment)
        for key in LISTENER_REQUEST_INTERNAL_METADATA_KEYS:
            metadata.pop(key, None)
        kept.append(segment)
    return dropped, kept


def drop_segment_moment_receipts(state: StationState, segment: Segment, reason: str) -> None:
    """Settle any receipt carried by a segment that can no longer air."""
    store = getattr(state, "moment_store", None)
    if store is None or not isinstance(segment.metadata, dict):
        return
    try:
        for key in ("ritual_moment_id", "gag_moment_id"):
            moment_id = str(segment.metadata.get(key) or "")
            if moment_id:
                store.mark_dropped(moment_id, reason)
    except Exception:  # pragma: no cover - receipts must never break the queue
        logger.debug("Moment receipt queue drop failed", exc_info=True)


def unlink_ephemeral_best_effort(segment: Segment) -> None:
    """Remove a discarded temporary render while preserving package data."""
    if not segment.ephemeral or is_packaged_asset(segment.path, _DEMO_ASSETS_DIR):
        return
    try:
        segment.path.unlink(missing_ok=True)
    except Exception:
        # Cleanup is subordinate to restoring the queue. A malformed path or an
        # I/O error must not strand survivors outside the playback queue.
        logger.debug("Ephemeral queue-drop unlink failed for %s", segment.path, exc_info=True)


def discard_queued_segment(
    state: StationState,
    segment: Segment,
    *,
    reason: str,
    revoke_listener_handoff: bool = True,
) -> None:
    """Settle one admitted segment that leaves the queue before it can air.

    The four steps are an ordered sequence, not a bag: the listener promise is
    revoked while the segment still describes it, the discard is recorded before
    any best-effort observer runs, the moment receipt is demoted, and only then
    is the temporary render unlinked. Every queue-mutation site owes all four, so
    they live here rather than being retyped per caller.

    ``revoke_listener_handoff=False`` is for a caller that already revoked the
    owning dedication itself (and needs its return value) and is settling only
    the dependents that followed it out of the queue.
    """
    if revoke_listener_handoff:
        state.revoke_listener_request_handoff_for_discarded_dedication(segment)
    state.record_discard(segment, reason=reason, already_counted_in_produced=True)
    drop_segment_moment_receipts(state, segment, reason)
    unlink_ephemeral_best_effort(segment)


def discard_queued_segments(
    state: StationState,
    segments: Iterable[Segment],
    *,
    reason: str,
    revoke_listener_handoff: bool = True,
) -> None:
    """Run the queued-segment discard sequence over every dropped segment."""
    for segment in segments:
        discard_queued_segment(
            state,
            segment,
            reason=reason,
            revoke_listener_handoff=revoke_listener_handoff,
        )


def drop_matching_segments(
    queue: asyncio.Queue[Segment],
    state: StationState,
    *,
    should_drop: QueueDropPredicate,
    reason: str,
) -> int:
    """Drop matching queued segments and rebuild the queue and its shadow.

    The segment currently emitting is not in ``queue`` and is therefore never
    interrupted. Survivors retain their order. Every drained item is paired
    with ``task_done()`` and every survivor is re-enqueued, so ``join()``
    accounting remains balanced. Returns the number of real queued segments
    removed; route callers can expose that count unchanged.
    """
    items: list[Segment] = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
            queue.task_done()
        except asyncio.QueueEmpty:
            break

    dropped: list[Segment] = []
    survivors: list[Segment] = []
    try:
        for segment in items:
            (dropped if should_drop(segment) else survivors).append(segment)
    except Exception:
        # Restore the complete original queue before propagating a faulty
        # internal predicate; a classification bug must not create dead air.
        for segment in items:
            queue.put_nowait(segment)
        raise

    dropped, survivors = settle_listener_request_queue_dependencies(dropped, survivors, state=state)

    for segment in survivors:
        queue.put_nowait(segment)

    dropped_ids = {
        queue_id
        for segment in dropped
        if isinstance(segment.metadata, dict) and isinstance((queue_id := segment.metadata.get("queue_id")), str)
    }
    if dropped_ids:
        state.queued_segments = [entry for entry in state.queued_segments if entry.get("id") not in dropped_ids]

    discard_queued_segments(state, dropped, reason=reason)

    return len(dropped)
