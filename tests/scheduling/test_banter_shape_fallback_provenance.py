"""Regression coverage for stock banter fallback provenance."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from mammamiradio.scheduling.producer import _shape_fields_for_final_banter

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_banter_shape_compliance.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_banter_shape_compliance_fallback", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stock_script_fallback_is_an_explicit_compliance_exclusion() -> None:
    shape_id, skip_reason = _shape_fields_for_final_banter(None, truth_changed=False)
    assert shape_id is None
    assert skip_reason == "script_fallback"

    checker = _load_checker()
    rows = [
        {
            "record": "segment_prepared",
            "segment_id": "fallback",
            "role": "banter",
            "exchange_lines": [
                {"host": "Marco", "text": "Anyway. Not bad."},
                {"host": "Giulia", "text": "Music. Now."},
            ],
            "exchange_shape_skip_reason": skip_reason,
        },
        {
            "record": "stream_result",
            "segment_id": "fallback",
            "segment_type": "banter",
            "aired_status": "aired",
        },
    ]

    summary = checker.summarize(rows)
    assert summary["eligible_segments"] == 0
    assert summary["excluded_by_reason"] == {"script_fallback": 1}
    assert summary["evidence_errors"] == []
