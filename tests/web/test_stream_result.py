"""Tests for the Tier-3 stream_result emit in the streamer.

The playback loop calls _emit_stream_result from its finally with the send-loop
results. Verifies the aired_status classification, the segment_id join field,
disabled-is-silent, and that a broken ledger never raises into the stream.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.models import SegmentType, StationState
from mammamiradio.web.streamer import _emit_stream_result, _schedule_banter_memory_extraction_after_send


class _FakeLedger:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.rows: list[dict] = []

    def record(self, row):
        self.rows.append(row)


def _segment(meta: dict, seg_type=SegmentType.BANTER):
    return SimpleNamespace(metadata=meta, type=seg_type, path=Path("/x"), ephemeral=False)


def test_clean_air_records_aired():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    seg = _segment({"ledger_segment_id": "seg-1", "title": "Banter"})
    _emit_stream_result(state, seg, bytes_sent=5000, was_skipped=False, listeners=2)
    assert len(led.rows) == 1
    row = led.rows[0]
    assert row["record"] == "stream_result"
    assert row["aired_status"] == "aired"
    assert row["segment_id"] == "seg-1"
    assert row["listeners"] == 2


def test_skip_records_skipped():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_stream_result(state, _segment({}), bytes_sent=10, was_skipped=True, listeners=1)
    assert led.rows[0]["aired_status"] == "skipped"


@pytest.mark.parametrize(
    "metadata",
    [
        {"queue_drain_recovery": True},
        {"rescue": True},
        {"error_recovery": True},
    ],
)
def test_rescue_clip_records_fallback_rescue(metadata):
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    seg = _segment(metadata)  # rescue flag, no fallback:True
    _emit_stream_result(state, seg, bytes_sent=4000, was_skipped=False, listeners=1)
    assert led.rows[0]["aired_status"] == "fallback_rescue"
    assert led.rows[0]["segment_id"] is None  # pure fallback, no provenance


def test_disabled_ledger_records_nothing():
    led = _FakeLedger(enabled=False)
    state = SimpleNamespace(ledger=led)
    _emit_stream_result(state, _segment({}), bytes_sent=10, was_skipped=False, listeners=1)
    assert led.rows == []


def test_jamendo_stream_result_keeps_observability_in_memory_and_out_of_ledger(tmp_path):
    from mammamiradio.core.ledger import ProvenanceLedger

    ledger = ProvenanceLedger(tmp_path / "ledger", enabled=True)
    state = StationState()
    state.ledger = ledger
    ledger.start()
    try:
        _emit_stream_result(
            state,
            _segment({"ledger_segment_id": "control", "title": "Control banter"}),
            bytes_sent=2048,
            was_skipped=False,
            listeners=1,
        )
        _emit_stream_result(
            state,
            _segment(
                {
                    "title": "Private Jamendo title",
                    "artist": "Private Jamendo artist",
                    "provider_track_id": "987654",
                    "source_kind": "jamendo",
                    "audio_source": "jamendo_transient",
                    "music_attribution": {
                        "provider": "jamendo",
                        "source_url": "https://www.jamendo.com/track/987654/private",
                    },
                },
                seg_type=SegmentType.MUSIC,
            ),
            bytes_sent=4096,
            was_skipped=False,
            listeners=1,
        )
    finally:
        ledger.stop()

    outcome = list(state.stream_outcome_history)[-1]
    assert outcome["segment_type"] == "music"
    assert outcome["result"] == "aired"

    ledger_files = list((tmp_path / "ledger").glob("provenance-*.jsonl"))
    assert len(ledger_files) == 1
    ledger_text = ledger_files[0].read_text(encoding="utf-8")
    rows = [json.loads(line) for line in ledger_text.splitlines()]
    assert len(rows) == 1
    assert rows[0]["title"] == "Control banter"
    for private_fact in (
        "jamendo",
        "Private Jamendo title",
        "Private Jamendo artist",
        "987654",
        "https://www.jamendo.com/track/987654/private",
    ):
        assert private_fact not in ledger_text


def test_station_id_outcome_is_retained_when_provenance_ledger_is_disabled():
    led = _FakeLedger(enabled=False)
    state = StationState()
    state.ledger = led

    _emit_stream_result(
        state,
        _segment({}, seg_type=SegmentType.STATION_ID),
        bytes_sent=4096,
        was_skipped=False,
        listeners=1,
    )

    assert led.rows == []
    outcome = list(state.stream_outcome_history)[-1]
    assert outcome["timestamp"] > 0
    assert {key: value for key, value in outcome.items() if key != "timestamp"} == {
        "segment_type": "station_id",
        "result": "aired",
        "bytes_sent": 4096,
        "starting_listener_count": 1,
        "accepted_listener_count": 1,
        "terminal_reason": "eof",
    }


def test_release_campaign_runs_even_when_ledger_disabled():
    class _Campaign:
        def __init__(self):
            self.calls = []
            self.saved = False

        def record_stream_result(self, metadata, *, bytes_sent, was_skipped, listeners, accepted_listeners=None):
            self.calls.append(
                {
                    "metadata": metadata,
                    "bytes_sent": bytes_sent,
                    "was_skipped": was_skipped,
                    "listeners": listeners,
                    "accepted_listeners": accepted_listeners,
                }
            )

        def save_if_dirty(self):
            self.saved = True

    led = _FakeLedger(enabled=False)
    campaign = _Campaign()
    state = SimpleNamespace(ledger=led, release_campaign=campaign)
    _emit_stream_result(
        state,
        _segment({"release_beat_id": "beat-1"}),
        bytes_sent=5000,
        was_skipped=False,
        listeners=2,
        accepted_listener_count=1,
    )

    assert led.rows == []
    assert campaign.calls == [
        {
            "metadata": {"release_beat_id": "beat-1"},
            "bytes_sent": 5000,
            "was_skipped": False,
            "listeners": 2,
            "accepted_listeners": 1,
        }
    ]
    assert campaign.saved is True


def test_release_campaign_failure_does_not_block_provenance():
    class _BoomCampaign:
        def record_stream_result(self, metadata, *, bytes_sent, was_skipped, listeners, accepted_listeners=None):
            raise RuntimeError("campaign disk gone")

    led = _FakeLedger()
    state = SimpleNamespace(ledger=led, release_campaign=_BoomCampaign())
    _emit_stream_result(state, _segment({"ledger_segment_id": "seg-1"}), bytes_sent=10, was_skipped=False, listeners=1)
    assert led.rows[0]["record"] == "stream_result"


def test_no_ledger_is_safe():
    state = SimpleNamespace()  # no .ledger attribute at all
    _emit_stream_result(state, _segment({}), bytes_sent=10, was_skipped=False, listeners=1)


def test_broken_ledger_never_raises():
    class _Boom:
        enabled = True

        def record(self, row):
            raise RuntimeError("disk gone")

    state = SimpleNamespace(ledger=_Boom())
    # Must swallow — the stream's finally cannot raise.
    _emit_stream_result(state, _segment({}), bytes_sent=10, was_skipped=False, listeners=1)


def test_emit_stream_result_no_longer_owns_the_rotation_stamp():
    """The rotation cooldown moved OUT of the end-of-segment recorder.

    It is stamped in the send loop on the first chunk a listener queue accepted.
    That predicate is strictly tighter than the one available here: ``bytes_sent``
    counts bytes the loop wrote even when nobody took them, and ``listeners`` is
    sampled at segment start, so an empty room could be credited. Stamping at EOF
    also left a song looking unheard for its whole play, which is how a live
    control came to re-reserve the song already on the air.
    """
    state = StationState()
    path = Path("/cache/norm_bad_bunny_192k.mp3")
    seg = SimpleNamespace(
        metadata={"audio_source": "fallback_norm_cache", "fallback": True},
        type=SegmentType.MUSIC,
        path=path,
        ephemeral=False,
    )
    _emit_stream_result(state, seg, bytes_sent=4000, was_skipped=False, listeners=1)

    assert path not in state.rescue_airplay
    # Still does its real job: the aired-outcome record.
    assert state.stream_outcome_history[-1]["result"] == "fallback_rescue"


@pytest.mark.asyncio
async def test_clean_banter_send_schedules_memory_extraction_even_without_ledger():
    app_state = SimpleNamespace(background_tasks=set())
    state = SimpleNamespace(ledger=None)
    config = SimpleNamespace()
    seg = _segment({"memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]}})
    task = asyncio.create_task(asyncio.sleep(0))

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction", return_value=task) as schedule:
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=4096,
            send_completed_cleanly=True,
            listeners=1,
        )

    schedule.assert_called_once_with(config=config, state=state, metadata=seg.metadata)
    assert task in app_state.background_tasks
    await task


def test_memory_extraction_not_scheduled_for_partial_or_empty_send():
    app_state = SimpleNamespace(background_tasks=set())
    state = SimpleNamespace()
    config = SimpleNamespace()
    seg = _segment({"memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]}})

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=4096,
            send_completed_cleanly=False,
            listeners=1,
        )
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=0,
            send_completed_cleanly=True,
            listeners=1,
        )

    schedule.assert_not_called()


def test_home_memory_not_scheduled_when_privacy_is_revoked_mid_air():
    """An airing Home segment may finish, but must not submit context afterward."""
    app_state = SimpleNamespace(background_tasks=set())
    config = SimpleNamespace(homeassistant=SimpleNamespace(context_enabled=False))
    state = SimpleNamespace(home_context_policy_generation=10)
    seg = _segment(
        {
            "home_context_generation": 9,
            "memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]},
        }
    )

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=4096,
            send_completed_cleanly=True,
            listeners=1,
        )

    schedule.assert_not_called()
    assert app_state.background_tasks == set()


@pytest.mark.asyncio
async def test_memory_extraction_not_scheduled_for_zero_listener_clean_send():
    app_state = SimpleNamespace(background_tasks=set())
    state = SimpleNamespace()
    config = SimpleNamespace()
    seg = _segment({"memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]}})

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=4096,
            send_completed_cleanly=True,
            listeners=0,
        )

    schedule.assert_not_called()
    assert app_state.background_tasks == set()


@pytest.mark.asyncio
async def test_memory_extraction_not_scheduled_when_connected_listener_rejects_send():
    app_state = SimpleNamespace(background_tasks=set())
    state = SimpleNamespace()
    config = SimpleNamespace()
    seg = _segment({"memory_extraction": {"script_lines": [{"host": "Marco", "text": "unheard"}]}})

    with patch("mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction") as schedule:
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=4096,
            send_completed_cleanly=True,
            listeners=1,
            accepted_listeners=0,
        )

    schedule.assert_not_called()
    assert app_state.background_tasks == set()


@pytest.mark.asyncio
async def test_memory_extraction_counts_listener_that_joins_after_start_sample():
    app_state = SimpleNamespace(background_tasks=set())
    state = SimpleNamespace()
    config = SimpleNamespace()
    seg = _segment({"memory_extraction": {"script_lines": [{"host": "Marco", "text": "heard"}]}})
    task = asyncio.create_task(asyncio.sleep(0))

    with patch(
        "mammamiradio.hosts.memory_extractor.schedule_banter_memory_extraction",
        return_value=task,
    ) as schedule:
        _schedule_banter_memory_extraction_after_send(
            app_state,
            config,
            state,
            seg,
            bytes_sent=4096,
            send_completed_cleanly=True,
            listeners=0,
            accepted_listeners=1,
        )

    schedule.assert_called_once_with(config=config, state=state, metadata=seg.metadata)
    assert task in app_state.background_tasks
    await task
