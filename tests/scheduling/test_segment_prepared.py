"""Tests for the Tier-2 segment_prepared emit in the producer.

After a segment's LLM calls fan out under a CallCollector, the producer records
the FINAL spoken script joined to the Tier-1 calls via llm_call_refs. Verifies
the row shape, the join fields, disabled-is-silent, no-ledger safety, and that a
broken ledger never raises into the producer.
"""

from __future__ import annotations

from types import SimpleNamespace

from mammamiradio.core.provenance_ctx import CallCollector
from mammamiradio.scheduling.producer import (
    _banter_ledger_segment_id,
    _emit_segment_prepared,
    _shape_fields_for_final_banter,
)


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


def test_records_host_attributed_exchange_without_transition():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    exchange_lines = [
        {"host": "Marco", "text": "Io parto."},
        {"host": "Giulia", "text": "E io chiudo."},
    ]
    _emit_segment_prepared(
        state,
        segment_id="seg-exchange",
        role="banter",
        final_script=["Dopo questa canzone.", "Io parto.", "E io chiudo."],
        exchange_lines=exchange_lines,
        collector=_collector(["llm-1"]),
    )

    row = led.rows[0]
    assert row["final_script"][0] == "Dopo questa canzone."
    assert row["exchange_lines"] == exchange_lines
    assert row["exchange_lines"] is not exchange_lines


def test_truth_repair_invalidates_shape_metadata_with_auditable_reason():
    commit = SimpleNamespace(
        exchange_shape_id="temporary_alliance",
        exchange_shape_skip_reason=None,
    )

    assert _shape_fields_for_final_banter(commit, truth_changed=False) == ("temporary_alliance", None)
    assert _shape_fields_for_final_banter(commit, truth_changed=True) == (None, "listener_truth_repair")


def test_only_generated_audio_keeps_the_tier2_join_id():
    assert _banter_ledger_segment_id("seg-generated", canned=None, impossible_tts=False) == "seg-generated"
    assert _banter_ledger_segment_id("seg-canned", canned=object(), impossible_tts=False) is None
    assert _banter_ledger_segment_id("seg-impossible", canned=None, impossible_tts=True) is None


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


def test_line_accounting_is_recorded_when_lines_were_lost():
    """Without this field a short break and a full one look identical on the row.

    ``final_script`` carries the survivors, never the authored count, so the
    accounting is the only thing that makes the loss legible to a debrief.
    """
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    accounting = {
        "authored": 4,
        "aired": 3,
        "dropped_empty": 1,
        "dropped_malformed": 0,
        "dropped_guest_host": 0,
        "dropped_duplicate": 0,
    }
    _emit_segment_prepared(
        state,
        segment_id="seg-line-loss",
        role="banter",
        final_script=["One.", "Two.", "Three."],
        collector=_collector(["llm-1"]),
        line_accounting=accounting,
    )
    row = led.rows[0]
    assert row["line_accounting"] == accounting
    assert len(row["final_script"]) == accounting["aired"]


def test_line_accounting_is_absent_for_a_full_exchange():
    """Absence means no loss, so a full break stays byte-identical to today's row."""
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(
        state,
        segment_id="seg-full",
        role="banter",
        final_script=["One.", "Two."],
        collector=_collector(["llm-1"]),
        line_accounting=None,
    )

    assert "line_accounting" not in led.rows[0]


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


def test_exchange_shape_fields_are_omitted_when_none():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(
        state,
        segment_id="seg-shape-absent",
        role="banter",
        final_script=["Ciao.", "Andiamo."],
        collector=_collector(["llm-1"]),
    )
    row = led.rows[0]
    assert "exchange_shape_id" not in row
    assert "exchange_shape_skip_reason" not in row


def test_exchange_shape_fields_are_recorded_when_passed():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(
        state,
        segment_id="seg-shape-present",
        role="banter",
        final_script=["Ciao.", "Andiamo."],
        collector=_collector(["llm-1"]),
        exchange_shape_id="giulia_leads_marco_derails",
        exchange_shape_skip_reason=None,
    )
    _emit_segment_prepared(
        state,
        segment_id="seg-shape-skip",
        role="banter",
        final_script=["Chaos now."],
        collector=_collector(["llm-2"]),
        exchange_shape_skip_reason="chaos",
    )
    present, skipped = led.rows
    assert present["exchange_shape_id"] == "giulia_leads_marco_derails"
    assert "exchange_shape_skip_reason" not in present
    assert skipped["exchange_shape_skip_reason"] == "chaos"
    assert "exchange_shape_id" not in skipped


def test_ad_break_rows_do_not_carry_exchange_shape_fields():
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit_segment_prepared(
        state,
        segment_id="seg-ad",
        role="ad_break",
        final_script=["Buy now"],
        collector=_collector(["ad-1"]),
    )
    row = led.rows[0]
    assert row["role"] == "ad_break"
    assert "exchange_shape_id" not in row
    assert "exchange_shape_skip_reason" not in row
