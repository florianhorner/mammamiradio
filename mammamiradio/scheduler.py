"""Scheduling rules for the station timeline."""

from __future__ import annotations

import random

from mammamiradio.config import PacingSection
from mammamiradio.models import SegmentType, StationState


def _decide(
    segments_produced: int,
    songs_since_ad: int,
    songs_since_banter: int,
    pacing: PacingSection,
    deterministic: bool = False,
    songs_since_news: int = 0,
    segments_since_station_id: int = 0,
    segments_since_time_check: int = 0,
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
        # 30% chance of news flash instead of banter (every ~6-8 songs)
        if songs_since_news >= 6 and (deterministic or random.random() < 0.3):
            return SegmentType.NEWS_FLASH
        return SegmentType.BANTER

    # Micro-segments (station ID, time check) only fire on music slots.
    # Guard: require at least 1 music segment since the last micro-segment
    # to prevent back-to-back non-music starvation.
    last_micro = min(segments_since_station_id, segments_since_time_check)
    if last_micro >= 2:  # at least 2 segments (incl. >=1 music) since last micro
        # Station ID stinger: every 5-7 segments, 40% chance
        if segments_since_station_id >= 5 and (deterministic or random.random() < 0.4):
            return SegmentType.STATION_ID

        # Time check: every 8-10 segments, 25% chance
        if segments_since_time_check >= 8 and (deterministic or random.random() < 0.25):
            return SegmentType.TIME_CHECK

    return SegmentType.MUSIC


def next_segment_type(state: StationState, pacing: PacingSection) -> SegmentType:
    """Choose the next segment type from the current mutable station state."""
    return _decide(
        state.segments_produced,
        state.songs_since_ad,
        state.songs_since_banter,
        pacing,
        songs_since_news=state.songs_since_news,
        segments_since_station_id=state.segments_since_station_id,
        segments_since_time_check=state.segments_since_time_check,
    )


def preview_upcoming(state: StationState, pacing: PacingSection, tracks: list, count: int = 8) -> list[dict]:
    """Predict the next N segments without mutating state."""
    preview = []
    songs_since_banter = state.songs_since_banter
    songs_since_ad = state.songs_since_ad
    songs_since_news = state.songs_since_news
    segments_since_station_id = state.segments_since_station_id
    segments_since_time_check = state.segments_since_time_check
    segments_produced = state.segments_produced
    track_idx = 0

    for _ in range(count):
        seg_type = _decide(
            segments_produced,
            songs_since_ad,
            songs_since_banter,
            pacing,
            deterministic=True,
            songs_since_news=songs_since_news,
            segments_since_station_id=segments_since_station_id,
            segments_since_time_check=segments_since_time_check,
        )

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
            songs_since_news += 1
            segments_since_station_id += 1
            segments_since_time_check += 1
        elif seg_type == SegmentType.BANTER:
            preview.append({"type": "banter", "label": "Host banter"})
            songs_since_banter = 0
        elif seg_type == SegmentType.NEWS_FLASH:
            preview.append({"type": "news_flash", "label": "Notizie Flash"})
            songs_since_banter = 0
            songs_since_news = 0
        elif seg_type == SegmentType.AD:
            preview.append({"type": "ad", "label": "Ad break"})
            songs_since_ad = 0
        elif seg_type == SegmentType.STATION_ID:
            preview.append({"type": "station_id", "label": "Station ID"})
            segments_since_station_id = 0
        elif seg_type == SegmentType.TIME_CHECK:
            preview.append({"type": "time_check", "label": "Ora esatta"})
            segments_since_time_check = 0

        segments_produced += 1

    return preview
