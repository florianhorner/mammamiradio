from __future__ import annotations

import random

from fakeitaliradio.config import PacingSection
from fakeitaliradio.models import SegmentType, StationState, Track


def _make_state(**kwargs) -> StationState:
    return StationState(
        playlist=[Track(title="Test", artist="Test", duration_ms=200000, spotify_id="test1")],
        **kwargs,
    )


def test_first_segment_is_music():
    """First segment produced should always be MUSIC."""
    from fakeitaliradio.scheduler import next_segment_type

    state = _make_state(segments_produced=0)
    pacing = PacingSection()
    assert next_segment_type(state, pacing) == SegmentType.MUSIC


def test_ad_triggers_after_threshold():
    """AD should trigger when songs_since_ad >= songs_between_ads."""
    from fakeitaliradio.scheduler import next_segment_type

    pacing = PacingSection(songs_between_ads=4, songs_between_banter=2)
    state = _make_state(segments_produced=5, songs_since_ad=4, songs_since_banter=0)
    assert next_segment_type(state, pacing) == SegmentType.AD


def test_banter_triggers_with_jitter():
    """BANTER should trigger when songs_since_banter >= threshold (with jitter)."""
    from fakeitaliradio.scheduler import next_segment_type

    pacing = PacingSection(songs_between_banter=2, songs_between_ads=10)
    # With songs_since_banter=2 and threshold=2+randint(-1,0), threshold is 1 or 2.
    # Either way, songs_since_banter(2) >= threshold(1 or 2), so BANTER.
    random.seed(42)
    state = _make_state(segments_produced=3, songs_since_banter=2, songs_since_ad=0)
    result = next_segment_type(state, pacing)
    assert result == SegmentType.BANTER


def test_default_is_music():
    """When no trigger is met, default to MUSIC."""
    from fakeitaliradio.scheduler import next_segment_type

    pacing = PacingSection(songs_between_banter=5, songs_between_ads=10)
    state = _make_state(segments_produced=2, songs_since_banter=1, songs_since_ad=1)
    assert next_segment_type(state, pacing) == SegmentType.MUSIC


def test_reserve_next_track_rotates_upcoming_playlist():
    state = StationState(
        playlist=[
            Track(title="One", artist="A", duration_ms=1, spotify_id="1"),
            Track(title="Two", artist="B", duration_ms=1, spotify_id="2"),
            Track(title="Three", artist="C", duration_ms=1, spotify_id="3"),
        ]
    )

    track = state.reserve_next_track()

    assert track.spotify_id == "1"
    assert [t.spotify_id for t in state.playlist] == ["2", "3", "1"]


def test_preview_upcoming_uses_current_playlist_order():
    from fakeitaliradio.scheduler import preview_upcoming

    tracks = [
        Track(title="One", artist="A", duration_ms=1, spotify_id="1"),
        Track(title="Two", artist="B", duration_ms=1, spotify_id="2"),
        Track(title="Three", artist="C", duration_ms=1, spotify_id="3"),
    ]
    state = StationState(
        playlist=tracks,
        segments_produced=1,
        songs_since_banter=0,
        songs_since_ad=0,
        current_track=Track(title="Old", artist="Z", duration_ms=1, spotify_id="old"),
    )
    pacing = PacingSection(songs_between_banter=99, songs_between_ads=99)

    preview = preview_upcoming(state, pacing, state.playlist, count=3)

    assert [item["playlist_index"] for item in preview] == [0, 1, 2]
    assert [item["label"] for item in preview] == [
        "A – One",
        "B – Two",
        "C – Three",
    ]
