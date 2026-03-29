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


def preview_upcoming(
    state: StationState, pacing: PacingSection, tracks: list, count: int = 8
) -> list[dict]:
    """Predict the next N segments (type + label) without mutating state."""
    preview = []
    # Simulate state counters
    songs_since_banter = state.songs_since_banter
    songs_since_ad = state.songs_since_ad
    segments_produced = state.segments_produced
    track_idx = 0

    # Find current position in playlist
    if state.current_track and tracks:
        for i, t in enumerate(tracks):
            if t.spotify_id == state.current_track.spotify_id:
                track_idx = (i + 1) % len(tracks)
                break

    for _ in range(count):
        if segments_produced == 0:
            seg_type = SegmentType.MUSIC
        elif songs_since_ad >= pacing.songs_between_ads:
            seg_type = SegmentType.AD
        elif songs_since_banter >= pacing.songs_between_banter:
            seg_type = SegmentType.BANTER
        else:
            seg_type = SegmentType.MUSIC

        if seg_type == SegmentType.MUSIC:
            real_idx = track_idx % len(tracks) if tracks else -1
            t = tracks[real_idx] if tracks and real_idx >= 0 else None
            preview.append({
                "type": "music",
                "label": t.display if t else "?",
                "playlist_index": real_idx,
            })
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
