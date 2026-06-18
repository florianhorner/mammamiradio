"""Unit guards for coverage-ratchet artifact snapshots."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coverage-ratchet.py"


def _load_coverage_ratcheter():
    spec = importlib.util.spec_from_file_location("coverage_ratchet", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_coverage_ratchet_loads_snapshot_input(tmp_path, monkeypatch) -> None:
    snapshot = tmp_path / "coverage-ratchet-current.json"
    snapshot.write_text(json.dumps({"modules": {"mammamiradio.core.config": 91}, "total_pct": 82}))

    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "COVERAGE_INPUT", snapshot)

    modules, total_pct = module.current_coverage()

    assert modules == {"mammamiradio.core.config": 91}
    assert total_pct == 82
