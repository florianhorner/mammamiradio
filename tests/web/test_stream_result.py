"""Tests for the Tier-3 stream_result emit in the streamer.

The playback loop calls _emit_stream_result from its finally with the send-loop
results. Verifies the aired_status classification, the segment_id join field,
disabled-is-silent, and that a broken ledger never raises into the stream.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mammamiradio.core.models import SegmentType
from mammamiradio.web.streamer import _emit_stream_result


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


def test_rescue_clip_records_fallback_rescue():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    seg = _segment({"queue_drain_recovery": True})  # rescue flag, no fallback:True
    _emit_stream_result(state, seg, bytes_sent=4000, was_skipped=False, listeners=1)
    assert led.rows[0]["aired_status"] == "fallback_rescue"
    assert led.rows[0]["segment_id"] is None  # pure fallback, no provenance


def test_disabled_ledger_records_nothing():
    led = _FakeLedger(enabled=False)
    state = SimpleNamespace(ledger=led)
    _emit_stream_result(state, _segment({}), bytes_sent=10, was_skipped=False, listeners=1)
    assert led.rows == []


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
