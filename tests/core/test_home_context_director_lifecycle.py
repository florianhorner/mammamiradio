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
