from pathlib import Path

from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.home.context_director import DirectorObservation, HomeContextDirector


def _reserved_fact(director: HomeContextDirector):
    director.observe(
        [
            DirectorObservation(
                entity_id="weather.home",
                domain="weather",
                state="sunny",
                score=1.0,
                temperature_c=24.0,
            )
        ],
        policy_revision=1,
    )
    fact = director.select()
    assert fact is not None
    assert director.reserve("queue-1", fact)
    return fact


def test_stream_start_activates_fact_cooldown_and_later_discard_cannot_release_it():
    director = HomeContextDirector(clock=lambda: 100.0, id_factory=lambda: "fact-1")
    fact = _reserved_fact(director)
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/banter.mp3"),
        metadata={"queue_id": "queue-1", **fact.segment_metadata()},
    )
    state = StationState(home_context_director=director)

    state.on_stream_segment(segment)
    assert director.admin_status()["cooling_count"] == 1

    state.record_discard(segment, reason="test")
    assert director.admin_status()["cooling_count"] == 1


def test_discard_releases_only_unstarted_fact_reservation():
    director = HomeContextDirector(clock=lambda: 100.0, id_factory=lambda: "fact-1")
    fact = _reserved_fact(director)
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/banter.mp3"),
        metadata={"queue_id": "queue-1", **fact.segment_metadata()},
    )
    state = StationState(home_context_director=director)

    state.record_discard(segment, reason="test")

    status = director.admin_status()
    assert status["reserved_count"] == 0
    assert status["cooling_count"] == 0


class _RaisingDirector:
    """Stand-in whose lifecycle hooks always raise.

    A director bug must never become an audio bug (CLAUDE.md audio-delivery
    rule / leadership principle #2). These guards in StationState exist only to
    absorb such a failure; this stub exercises them.
    """

    def activate(self, *args, **kwargs):
        raise RuntimeError("director activate boom")

    def release(self, *args, **kwargs):
        raise RuntimeError("director release boom")


def test_on_stream_segment_survives_raising_director():
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/banter.mp3"),
        metadata={"queue_id": "queue-1", "home_fact_id": "fact-1", "title": "Break"},
    )
    state = StationState(home_context_director=_RaisingDirector())
    before = state.playback_epoch

    # Must not raise; the segment still becomes the streaming one.
    state.on_stream_segment(segment)

    assert state.playback_epoch == before + 1
    assert state.now_streaming is not None


def test_record_discard_survives_raising_director():
    segment = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/banter.mp3"),
        metadata={"queue_id": "queue-1", "home_fact_id": "fact-1"},
    )
    state = StationState(home_context_director=_RaisingDirector())

    # Must not raise; discard accounting still records the waste.
    state.record_discard(segment, reason="test")

    assert state.discard_by_reason.get("test", 0) >= 1


class _RecordingDirector:
    """Counts lifecycle calls so we can prove which segments reach the director."""

    def __init__(self) -> None:
        self.activate_calls = 0
        self.release_calls = 0

    def activate(self, *args, **kwargs) -> None:
        self.activate_calls += 1

    def release(self, *args, **kwargs) -> None:
        self.release_calls += 1


def test_ordinary_segment_without_home_fact_never_touches_director():
    # An ordinary segment carries a queue_id but no home_fact_id. Only home-fact
    # segments hold reservations, so the director must not be called — otherwise a
    # fact_id=None release would wildcard-match an unrelated reservation.
    director = _RecordingDirector()
    state = StationState(home_context_director=director)
    ordinary = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/song.mp3"),
        metadata={"queue_id": "queue-ordinary", "title": "Song"},
    )

    state.on_stream_segment(ordinary)
    state.record_discard(ordinary, reason="test")

    assert director.activate_calls == 0
    assert director.release_calls == 0

    # A home-fact segment still reaches the director.
    home = Segment(
        type=SegmentType.BANTER,
        path=Path("/tmp/banter.mp3"),
        metadata={"queue_id": "queue-home", "home_fact_id": "fact-1"},
    )
    state.on_stream_segment(home)
    state.record_discard(home, reason="test")

    assert director.activate_calls == 1
    assert director.release_calls == 1
