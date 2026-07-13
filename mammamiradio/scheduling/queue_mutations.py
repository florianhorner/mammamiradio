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
from collections.abc import Callable

from mammamiradio.core.models import Segment, StationState
from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR as _DEMO_ASSETS_DIR
from mammamiradio.core.packaged_assets import is_packaged_asset

logger = logging.getLogger(__name__)

QueueDropPredicate = Callable[[Segment], bool]


def _drop_moment_receipts(state: StationState, segment: Segment, reason: str) -> None:
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


def _unlink_ephemeral_best_effort(segment: Segment) -> None:
    """Remove a discarded temporary render while preserving package data."""
    if not segment.ephemeral or is_packaged_asset(segment.path, _DEMO_ASSETS_DIR):
        return
    try:
        segment.path.unlink(missing_ok=True)
    except Exception:
        # Cleanup is subordinate to restoring the queue. A malformed path or an
        # I/O error must not strand survivors outside the playback queue.
        logger.debug("Ephemeral queue-drop unlink failed for %s", segment.path, exc_info=True)


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

    for segment in survivors:
        queue.put_nowait(segment)

    dropped_ids = {
        queue_id
        for segment in dropped
        if isinstance(segment.metadata, dict) and isinstance((queue_id := segment.metadata.get("queue_id")), str)
    }
    if dropped_ids:
        state.queued_segments = [entry for entry in state.queued_segments if entry.get("id") not in dropped_ids]

    for segment in dropped:
        state.record_discard(segment, reason=reason, already_counted_in_produced=True)
        _drop_moment_receipts(state, segment, reason)
        _unlink_ephemeral_best_effort(segment)

    return len(dropped)
