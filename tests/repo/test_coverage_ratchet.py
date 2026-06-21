"""Unit guards for coverage-ratchet artifact snapshots."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coverage-ratchet.py"


def _load_coverage_ratcheter():
    spec = importlib.util.spec_from_file_location("coverage_ratchet", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(tmp_path, payload) -> Path:
    snap = tmp_path / "coverage-ratchet-current.json"
    snap.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return snap


def test_coverage_ratchet_loads_snapshot_input(tmp_path, monkeypatch) -> None:
    snapshot = _snapshot(tmp_path, {"modules": {"mammamiradio.core.config": 91}, "total_pct": 82})

    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "COVERAGE_INPUT", snapshot)

    modules, total_pct = module.current_coverage()

    assert modules == {"mammamiradio.core.config": 91}
    assert total_pct == 82


# ---------------------------------------------------------------------------
# Snapshot validation — the catastrophic case is an empty/garbage snapshot
# silently wiping every floor on the main-push write job (#616).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"modules": {}, "total_pct": 82}, id="empty-modules"),
        pytest.param({"modules": {"m": 91}, "total_pct": True}, id="bool-total"),
        pytest.param({"modules": {"m": False}, "total_pct": 82}, id="bool-module-pct"),
        pytest.param({"modules": {"m": 91}, "total_pct": 999}, id="total-over-100"),
        pytest.param({"modules": {"m": -1}, "total_pct": 82}, id="module-below-0"),
        pytest.param({"modules": {"m": 101}, "total_pct": 82}, id="module-over-100"),
        pytest.param({"modules": {"": 91}, "total_pct": 82}, id="empty-module-key"),
        pytest.param({"modules": {"m": 91}}, id="missing-total"),
        pytest.param({"total_pct": 82}, id="missing-modules"),
        pytest.param({"modules": [], "total_pct": 82}, id="modules-not-a-dict"),
        pytest.param("[1, 2, 3]", id="top-level-not-a-dict"),
        pytest.param("{not valid json", id="malformed-json"),
    ],
)
def test_load_coverage_input_rejects_bad_snapshot(tmp_path, monkeypatch, payload) -> None:
    snapshot = _snapshot(tmp_path, payload)
    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "COVERAGE_INPUT", snapshot)

    with pytest.raises(ValueError):
        module.load_coverage_input()


def test_load_coverage_input_accepts_boundary_percentages(tmp_path, monkeypatch) -> None:
    """0 and 100 are valid — the bounds are inclusive."""
    snapshot = _snapshot(tmp_path, {"modules": {"a": 0, "b": 100}, "total_pct": 0})
    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "COVERAGE_INPUT", snapshot)

    modules, total_pct = module.load_coverage_input()
    assert modules == {"a": 0, "b": 100}
    assert total_pct == 0


# ---------------------------------------------------------------------------
# Floors file validation — a corrupt committed baseline must not poison checks.
# ---------------------------------------------------------------------------


def test_load_floors_rejects_corrupt_baseline(tmp_path, monkeypatch) -> None:
    floors = tmp_path / ".coverage-floors.json"
    floors.write_text(json.dumps({"mammamiradio.core.config": 150}))  # out of range
    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "FLOORS_FILE", floors)

    with pytest.raises(ValueError):
        module.load_floors()


def test_load_floors_allows_empty_and_missing(tmp_path, monkeypatch) -> None:
    module = _load_coverage_ratcheter()
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(module, "FLOORS_FILE", missing)
    assert module.load_floors() == {}

    empty = tmp_path / ".coverage-floors.json"
    empty.write_text("{}")
    monkeypatch.setattr(module, "FLOORS_FILE", empty)
    assert module.load_floors() == {}


# ---------------------------------------------------------------------------
# cmd_update must refuse to ratchet on an empty coverage map — the wipe path.
# ---------------------------------------------------------------------------


def test_cmd_update_refuses_to_wipe_floors_on_empty_coverage(tmp_path, monkeypatch) -> None:
    floors = tmp_path / ".coverage-floors.json"
    original = {"mammamiradio.core.config": 91, "mammamiradio.web.streamer": 80}
    floors.write_text(json.dumps(original, indent=2, sort_keys=True) + "\n")

    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "FLOORS_FILE", floors)
    # Simulate a coverage run that produced zero modules but a valid total.
    monkeypatch.setattr(module, "current_coverage", lambda: ({}, 82))

    rc = module.cmd_update()

    assert rc == 1  # refused, job fails — no silent wipe
    # The floors file is untouched: every original floor survives.
    assert json.loads(floors.read_text()) == original


def test_cmd_init_refuses_empty_coverage(tmp_path, monkeypatch) -> None:
    """cmd_init has the same wipe risk as cmd_update — guard the sibling path."""
    floors = tmp_path / ".coverage-floors.json"
    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "FLOORS_FILE", floors)
    monkeypatch.setattr(module, "current_coverage", lambda: ({}, 82))

    assert module.cmd_init() == 1
    assert not floors.exists()  # nothing written


def test_main_reports_corrupt_snapshot_cleanly(tmp_path, monkeypatch) -> None:
    """A corrupt snapshot must fail the job (rc 1) via main()'s ValueError guard,
    not crash with an uncaught traceback."""
    snapshot = _snapshot(tmp_path, {"modules": {}, "total_pct": 82})
    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "COVERAGE_INPUT", snapshot)
    monkeypatch.setattr(module.sys, "argv", ["coverage-ratchet.py", "update"])

    assert module.main() == 1


# ---------------------------------------------------------------------------
# cmd_update must only delete a floor when the source file is actually gone —
# a module absent from coverage but still on disk keeps its floor (#636).
# ---------------------------------------------------------------------------


def test_cmd_update_keeps_floor_when_source_file_still_exists(tmp_path, monkeypatch) -> None:
    """A module missing from coverage (excluded / 0% / fully skipped) but whose
    .py still exists must NOT have its floor deleted — that would retire a guard."""
    floors_file = tmp_path / ".coverage-floors.json"
    floors_file.write_text(json.dumps({"mammamiradio.audio.normalizer": 88}) + "\n")

    # The source file still exists under SOURCE_ROOT.
    src = tmp_path / "mammamiradio" / "audio" / "normalizer.py"
    src.parent.mkdir(parents=True)
    src.write_text("# still here\n")

    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "FLOORS_FILE", floors_file)
    monkeypatch.setattr(module, "SOURCE_ROOT", tmp_path)
    # Coverage run produced a real total but no row for this module.
    monkeypatch.setattr(module, "current_coverage", lambda: ({"mammamiradio.core.config": 91}, 82))

    rc = module.cmd_update()

    assert rc == 0
    assert json.loads(floors_file.read_text())["mammamiradio.audio.normalizer"] == 88


def test_cmd_update_removes_floor_when_source_file_deleted(tmp_path, monkeypatch) -> None:
    """When a module is absent from coverage AND its .py is gone, the floor is dropped."""
    floors_file = tmp_path / ".coverage-floors.json"
    floors_file.write_text(json.dumps({"mammamiradio.audio.gone_module": 88}) + "\n")
    # Deliberately do NOT create the source file under SOURCE_ROOT (tmp_path).

    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "FLOORS_FILE", floors_file)
    monkeypatch.setattr(module, "SOURCE_ROOT", tmp_path)
    monkeypatch.setattr(module, "current_coverage", lambda: ({"mammamiradio.core.config": 91}, 82))

    rc = module.cmd_update()

    assert rc == 0
    assert "mammamiradio.audio.gone_module" not in json.loads(floors_file.read_text())


def test_cmd_update_ratchets_floor_up_for_present_module(tmp_path, monkeypatch) -> None:
    """The core ratchet-up invariant: a module present in coverage with higher
    coverage than its floor raises the floor (never lowers it). Guards against the
    deletion-predicate change accidentally skipping the normal ratchet path."""
    floors_file = tmp_path / ".coverage-floors.json"
    floors_file.write_text(json.dumps({"mammamiradio.core.config": 80}) + "\n")

    module = _load_coverage_ratcheter()
    monkeypatch.setattr(module, "FLOORS_FILE", floors_file)
    monkeypatch.setattr(module, "SOURCE_ROOT", tmp_path)
    monkeypatch.setattr(module, "current_coverage", lambda: ({"mammamiradio.core.config": 91}, 82))

    rc = module.cmd_update()

    assert rc == 0
    assert json.loads(floors_file.read_text())["mammamiradio.core.config"] == 91
