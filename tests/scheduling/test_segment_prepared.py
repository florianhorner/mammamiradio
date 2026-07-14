"""Tests for the Tier-2 segment_prepared emit in the producer.

After a segment's LLM calls fan out under a CallCollector, the producer records
the FINAL spoken script joined to the Tier-1 calls via llm_call_refs. Verifies
the row shape, the join fields, disabled-is-silent, no-ledger safety, and that a
broken ledger never raises into the producer.
"""

from __future__ import annotations

from types import SimpleNamespace

from mammamiradio.core.provenance_ctx import CallCollector
from mammamiradio.scheduling.producer import _emit_segment_prepared


class _FakeLedger:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.rows: list[dict] = []

    def record(self, row):
        self.rows.append(row)


def _collector(ids):
    c = CallCollector(attempt_id="seg-1")
    c.calls = [{"llm_call_id": i, "role": "banter", "spot_index": None, "ok": True} for i in ids]
    return c


def test_records_final_script_and_call_refs():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(
        state,
        segment_id="seg-1",
        role="banter",
        final_script=["Buongiorno!", "Che caldo oggi."],
        collector=_collector(["a", "b"]),
    )
    assert len(led.rows) == 1
    row = led.rows[0]
    assert row["record"] == "segment_prepared"
    assert row["segment_id"] == "seg-1"
    assert row["role"] == "banter"
    assert row["final_script"] == ["Buongiorno!", "Che caldo oggi."]
    assert row["llm_call_refs"] == ["a", "b"]


def test_empty_collector_yields_empty_refs():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(
        state, segment_id="seg-2", role="ad_break", final_script=["Buy now"], collector=_collector([])
    )
    assert led.rows[0]["llm_call_refs"] == []


def test_language_assessment_is_recorded_when_available():
    """Optional policy telemetry stays nested and does not change the join keys."""
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    assessment = {
        "english_tokens": 8,
        "italian_tokens": 2,
        "accepted": True,
    }
    _emit_segment_prepared(
        state,
        segment_id="seg-language",
        role="ad_break",
        final_script=["A word from our sponsors.", "Back to the music."],
        collector=_collector(["llm-1"]),
        language_assessment=assessment,
    )
    row = led.rows[0]
    assert row["final_script"] == ["A word from our sponsors.", "Back to the music."]
    assert row["language_assessment"] == assessment


def test_none_collector_is_safe():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(state, segment_id="seg-3", role="banter", final_script=[], collector=None)
    assert led.rows[0]["llm_call_refs"] == []


def test_disabled_ledger_records_nothing():
    led = _FakeLedger(enabled=False)
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(state, segment_id="x", role="banter", final_script=["hi"], collector=_collector(["a"]))
    assert led.rows == []


def test_no_ledger_attr_is_safe():
    state = SimpleNamespace()  # no .ledger at all
    _emit_segment_prepared(state, segment_id="x", role="banter", final_script=["hi"], collector=_collector(["a"]))


def test_broken_ledger_never_raises():
    class _Boom:
        enabled = True

        def record(self, row):
            raise RuntimeError("disk gone")

    state = SimpleNamespace(ledger=_Boom())
    # Must swallow — provenance cannot raise into the producer.
    _emit_segment_prepared(state, segment_id="x", role="banter", final_script=["hi"], collector=_collector(["a"]))
