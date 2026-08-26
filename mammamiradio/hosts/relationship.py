"""Host-pair exchange-shape selection for banter (Phase 1).

Phase 1 is in-memory only. ``HostRelationshipState`` is the thin seam Phase 2
will hydrate from a durable store; ``recent_shapes`` / ``recent_lore`` live on
``StationState`` so recency dies with the process (one repeated shape after a
restart is acceptable; dead air is not).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mammamiradio.hosts.prompt_world import (
    EXCHANGE_SHAPE_WEIGHTS,
    EXCHANGE_SHAPES,
    GENERIC_EXCHANGE_SHAPE_WEIGHTS,
    GENERIC_EXCHANGE_SHAPES,
    HOST_SEED_LORE_BY_PAIR,
    LORE_SAMPLE_RATE,
)

if TYPE_CHECKING:
    from mammamiradio.core.models import ChaosSubtype, StationState

logger = logging.getLogger(__name__)

ARC_PHASE_ESTABLISHED_PAIR = "established_pair"


@dataclass(frozen=True)
class HostRelationshipState:
    """Phase-1 host-pair relationship. Durable fields land in Phase 2."""

    seed_lore: tuple[tuple[str, str], ...]
    arc_phase: str = ARC_PHASE_ESTABLISHED_PAIR


@dataclass(frozen=True)
class ExchangeShapeSelection:
    """Result of one banter-break shape draw."""

    shape_id: str | None
    directive: str | None
    lore_id: str | None
    lore_text: str | None
    skip_reason: str | None


def _normalized_host_pair(host_names: Sequence[str]) -> tuple[str, ...]:
    """Return distinct configured host names in stable, case-insensitive form."""

    return tuple(dict.fromkeys(name.strip().casefold() for name in host_names if name.strip()))


def relationship_state(host_names: Sequence[str]) -> HostRelationshipState:
    """Return pair-scoped seed lore plus the Phase-1 constant arc."""

    pair = frozenset(_normalized_host_pair(host_names))
    return HostRelationshipState(
        seed_lore=HOST_SEED_LORE_BY_PAIR.get(pair, ()),
        arc_phase=ARC_PHASE_ESTABLISHED_PAIR,
    )


def select_exchange_shape(
    state: StationState,
    *,
    host_names: Sequence[str],
    chaos_subtype: ChaosSubtype | None,
    festival: bool,
    guest_invited: bool,
    rng: random.Random | None = None,
) -> ExchangeShapeSelection:
    """Pick a shape for an eligible normal two-host break, or skip with a reason.

    Precedence: ``chaos`` > ``festival`` > ``guest`` > shape. There is no
    ``not_warranted`` skip — every eligible normal break gets a shape.
    """

    if chaos_subtype is not None or getattr(state, "chaos_mode_active", False):
        return ExchangeShapeSelection(None, None, None, None, "chaos")
    if festival:
        return ExchangeShapeSelection(None, None, None, None, "festival")
    if guest_invited:
        return ExchangeShapeSelection(None, None, None, None, "guest")

    normalized_hosts = _normalized_host_pair(host_names)
    if len(normalized_hosts) != 2:
        return ExchangeShapeSelection(None, None, None, None, "ineligible_roster")

    canonical_pair = frozenset(normalized_hosts) in HOST_SEED_LORE_BY_PAIR
    shape_bank = EXCHANGE_SHAPES if canonical_pair else GENERIC_EXCHANGE_SHAPES
    shape_weights = EXCHANGE_SHAPE_WEIGHTS if canonical_pair else GENERIC_EXCHANGE_SHAPE_WEIGHTS

    picker = rng if rng is not None else random
    recent_shapes = list(getattr(state, "recent_shapes", ()))
    candidates = [shape_id for shape_id in shape_bank if shape_id not in recent_shapes]
    if not candidates:
        candidates = list(shape_bank)
    weights = [shape_weights[shape_id] for shape_id in candidates]
    shape_id = picker.choices(candidates, weights=weights, k=1)[0]

    lore_id: str | None = None
    lore_text: str | None = None
    pair_state = relationship_state(normalized_hosts)
    if pair_state.seed_lore and picker.random() < LORE_SAMPLE_RATE:
        recent_lore = list(getattr(state, "recent_lore", ()))
        recent_lore_ids = set(recent_lore)
        lore_by_id = dict(pair_state.seed_lore)
        lore_candidates = [item_id for item_id in lore_by_id if item_id not in recent_lore_ids]
        if not lore_candidates:
            # A full recency window must rotate, not dead-end. Re-offer the
            # oldest still-valid item; recording it moves it to the deque tail.
            oldest = next((item_id for item_id in recent_lore if item_id in lore_by_id), None)
            if oldest is not None:
                lore_candidates = [oldest]
        if lore_candidates:
            lore_id = picker.choice(lore_candidates)
            lore_text = lore_by_id[lore_id]

    return ExchangeShapeSelection(
        shape_id=shape_id,
        directive=shape_bank[shape_id],
        lore_id=lore_id,
        lore_text=lore_text,
        skip_reason=None,
    )


def record_shape_recency(state: StationState, selection: ExchangeShapeSelection) -> None:
    """Record a selection accepted at the caller's final lifecycle boundary."""

    if selection.shape_id:
        state.recent_shapes.append(selection.shape_id)
    if selection.lore_id:
        state.recent_lore.append(selection.lore_id)
    logger.debug(
        "banter shape recorded: %s skip=%s lore=%s",
        selection.shape_id,
        selection.skip_reason,
        selection.lore_id,
    )
