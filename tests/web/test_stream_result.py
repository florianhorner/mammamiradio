"""Tests for the Tier-3 stream_result emit in the streamer.

The playback loop calls _emit_stream_result from its finally with the send-loop
results. Verifies the aired_status classification, the segment_id join field,
disabled-is-silent, and that a broken ledger never raises into the stream.
"""

from __future__ import annotations

import asyncio
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
        "terminal_reason": "eof",
    }


def test_release_campaign_runs_even_when_ledger_disabled():
    class _Campaign:
        def __init__(self):
            self.calls = []
            self.saved = False

        def record_stream_result(self, metadata, *, bytes_sent, was_skipped, listeners):
            self.calls.append(
                {
                    "metadata": metadata,
                    "bytes_sent": bytes_sent,
                    "was_skipped": was_skipped,
                    "listeners": listeners,
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
    )

    assert led.rows == []
    assert campaign.calls == [
        {
            "metadata": {"release_beat_id": "beat-1"},
            "bytes_sent": 5000,
            "was_skipped": False,
            "listeners": 2,
        }
    ]
    assert campaign.saved is True


def test_release_campaign_failure_does_not_block_provenance():
    class _BoomCampaign:
        def record_stream_result(self, metadata, *, bytes_sent, was_skipped, listeners):
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
