"""Tests for the Tier-1 llm_call emit in the scriptwriter.

_emit_llm_call records one raw LLM attempt (success OR failure) to the ledger,
records the system-prompt sidecar once, and appends the call id to the active
CallCollector so the Tier-2 segment_prepared row can join back. These mirror the
Tier-2 (test_segment_prepared) and Tier-3 (test_stream_result) emit tests:
shape, both ok arcs, collector wiring, disabled-silent, no-ledger, never-raise.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mammamiradio.core import provenance_ctx as pc
from mammamiradio.core.config import load_config
from mammamiradio.hosts.scriptwriter import _emit_llm_call

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


class _FakeLedger:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.rows: list[dict] = []
        self.prompts: list[tuple[str, str]] = []

    def record(self, row):
        self.rows.append(row)

    def record_system_prompt(self, prompt_hash, system_prompt):
        self.prompts.append((prompt_hash, system_prompt))


def _emit(state, config, *, ok=True, provider="anthropic", role="banter", spot_index=None, fallback_reason=None):
    _emit_llm_call(
        state=state,
        config=config,
        caller=role,
        role=role,
        spot_index=spot_index,
        provider=provider,
        model="claude-x",
        prompt="CONTEXT PROMPT",
        raw_output='{"text": "ciao"}' if ok else None,
        ok=ok,
        fallback_reason=fallback_reason,
        input_tokens=11,
        output_tokens=22,
        duration_ms=33,
        openai_fallback=not ok,
    )


def test_success_records_llm_call_row_and_sidecar():
    config = load_config(TOML_PATH)
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit(state, config, ok=True)
    assert len(led.rows) == 1
    row = led.rows[0]
    assert row["record"] == "llm_call"
    assert row["ok"] is True
    assert row["provider"] == "anthropic"
    assert row["role"] == "banter"
    assert row["input_tokens"] == 11 and row["output_tokens"] == 22
    assert "tags" in row and "festival" in row["tags"]
    assert row["llm_call_id"]  # a uuid hex was assigned
    # The system prompt was recorded to the sidecar exactly once.
    assert len(led.prompts) == 1
    assert row["system_prompt_hash"] == led.prompts[0][0]


def test_failure_records_ok_false_with_reason():
    config = load_config(TOML_PATH)
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    _emit(state, config, ok=False, provider="anthropic", fallback_reason="anthropic_TimeoutError")
    row = led.rows[0]
    assert row["ok"] is False
    assert row["fallback_reason"] == "anthropic_TimeoutError"
    assert row["raw_output"] is None


def test_appends_to_active_collector():
    config = load_config(TOML_PATH)
    led = _FakeLedger()
    state = SimpleNamespace(ledger=led)
    collector = pc.CallCollector(attempt_id="seg-9")
    token = pc.set_collector(collector)
    try:
        _emit(state, config, ok=True, role="banter")
    finally:
        pc.reset_collector(token)
    assert len(collector.calls) == 1
    assert collector.calls[0]["llm_call_id"] == led.rows[0]["llm_call_id"]
    assert collector.calls[0]["role"] == "banter"
    # The row carries the collector's attempt_id for the Tier-1<->Tier-2 join.
    assert led.rows[0]["attempt_id"] == "seg-9"


def test_disabled_ledger_records_nothing():
    config = load_config(TOML_PATH)
    led = _FakeLedger(enabled=False)
    state = SimpleNamespace(ledger=led)
    _emit(state, config, ok=True)
    assert led.rows == []


def test_no_ledger_attr_is_safe():
    config = load_config(TOML_PATH)
    state = SimpleNamespace()  # no .ledger at all
    _emit(state, config, ok=True)  # must not raise


def test_broken_ledger_never_raises():
    config = load_config(TOML_PATH)

    class _Boom:
        enabled = True

        def record(self, row):
            raise RuntimeError("disk gone")

        def record_system_prompt(self, *a):
            raise RuntimeError("disk gone")

    state = SimpleNamespace(ledger=_Boom())
    # Must swallow — provenance cannot raise into _generate_json_response.
    _emit(state, config, ok=True)
