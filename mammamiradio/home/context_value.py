"""Product-value classification for privacy-safe Home context.

Privacy authorization answers whether a fact may be retained.  This module
answers the narrower product question of whether the retained facts make the
radio meaningfully more personal.  Keeping those decisions separate prevents a
low-value ambient basic from being promoted as a Connected Home experience
without weakening the authorization boundary that admitted it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from mammamiradio.home.authorization import NARROW_DAYLIGHT_ENTITY_ID

HomeContextValue = Literal["empty", "ambient_only", "useful"]

# ``sun.sun`` covers legacy/status projections while ``sun.ambient`` is the
# synthetic narrow-mode fact.  Daylight remains privacy-safe and eligible for
# host use; by itself it is simply not enough to claim a meaningful Home
# context experience.
LOW_VALUE_AMBIENT_ENTITY_IDS = frozenset({NARROW_DAYLIGHT_ENTITY_ID, "sun.sun"})


def classify_home_context_entity_ids(entity_ids: Iterable[object]) -> HomeContextValue:
    """Classify retained entity ids without changing which facts are allowed."""
    retained = {str(entity_id or "").strip() for entity_id in entity_ids}
    retained.discard("")
    if not retained:
        return "empty"
    if retained <= LOW_VALUE_AMBIENT_ENTITY_IDS:
        return "ambient_only"
    return "useful"


def has_useful_home_context_entity_ids(entity_ids: Iterable[object]) -> bool:
    """Return whether at least one retained fact adds meaningful radio context."""
    return classify_home_context_entity_ids(entity_ids) == "useful"
