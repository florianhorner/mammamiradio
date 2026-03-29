from __future__ import annotations

import random

from fakeitaliradio.config import PacingSection
from fakeitaliradio.models import SegmentType, StationState


def _decide(
    segments_produced: int,
    songs_since_ad: int,
    songs_since_banter: int,
    pacing: PacingSection,
    deterministic: bool = False,
) -> SegmentType:
    """Core pacing decision. Single source of truth."""
    if segments_produced == 0:
        return SegmentType.MUSIC

    if songs_since_ad >= pacing.songs_between_ads:
        return SegmentType.AD

    threshold = pacing.songs_between_banter
    if not deterministic:
        threshold += random.randint(-1, 0)
    threshold = max(1, threshold)
    if songs_since_banter >= threshold:
        return SegmentType.BANTER

    return SegmentType.MUSIC


def next_segment_type(state: StationState, pacing: PacingSection) -> SegmentType:
    return _decide(state.segments_produced, state.songs_since_ad, state.songs_since_banter, pacing)


def preview_upcoming(state: StationState, pacing: PacingSection, tracks: list, count: int = 8) -> list[dict]:
    """Predict the next N segments without mutating state."""
    preview = []
    songs_since_banter = state.songs_since_banter
    songs_since_ad = state.songs_since_ad
    segments_produced = state.segments_produced
    track_idx = 0

    for _ in range(count):
        seg_type = _decide(segments_produced, songs_since_ad, songs_since_banter, pacing, deterministic=True)

        if seg_type == SegmentType.MUSIC:
            real_idx = track_idx % len(tracks) if tracks else -1
            t = tracks[real_idx] if tracks and real_idx >= 0 else None
            preview.append(
                {
                    "type": "music",
                    "label": t.display if t else "?",
                    "playlist_index": real_idx,
                }
            )
            track_idx += 1
            songs_since_banter += 1
            songs_since_ad += 1
        elif seg_type == SegmentType.BANTER:
            preview.append({"type": "banter", "label": "Host banter"})
            songs_since_banter = 0
        elif seg_type == SegmentType.AD:
            preview.append({"type": "ad", "label": "Ad break"})
            songs_since_ad = 0

        segments_produced += 1

    return preview
