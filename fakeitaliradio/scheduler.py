from __future__ import annotations

import random

from fakeitaliradio.config import PacingSection
from fakeitaliradio.models import SegmentType, StationState


def next_segment_type(state: StationState, pacing: PacingSection) -> SegmentType:
    # First segment is always music
    if state.segments_produced == 0:
        return SegmentType.MUSIC

    # Check ads first (less frequent, higher priority when due)
    if state.songs_since_ad >= pacing.songs_between_ads:
        return SegmentType.AD

    # Check banter with ±1 jitter
    threshold = pacing.songs_between_banter + random.randint(-1, 0)
    threshold = max(1, threshold)
    if state.songs_since_banter >= threshold:
        return SegmentType.BANTER

    return SegmentType.MUSIC
