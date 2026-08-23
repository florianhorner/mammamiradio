"""Observed-skeleton classifier used by the operator banter-shape checker."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_banter_shape_compliance.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_banter_shape_compliance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepared(
    segment_id: str,
    *,
    lines: list[dict[str, str]] | None = None,
    shape_id: str | None = "temporary_alliance",
    skip_reason: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "record": "segment_prepared",
        "segment_id": segment_id,
        "role": "banter",
        "final_script": ["bare transition", "bare dialogue"],
    }
    if lines is not None:
        row["exchange_lines"] = lines
    if shape_id is not None:
        row["exchange_shape_id"] = shape_id
    if skip_reason is not None:
        row["exchange_shape_skip_reason"] = skip_reason
    return row


def _stream(
    segment_id: str | None,
    *,
    aired_status: str = "aired",
    segment_type: str = "banter",
) -> dict:
    return {
        "record": "stream_result",
        "segment_id": segment_id,
        "segment_type": segment_type,
        "aired_status": aired_status,
    }


def test_classifies_opener_cutoff_direction_and_landing():
    checker = _load()
    marco_opens = checker.classify_skeleton(
        [
            {"host": "Marco", "text": "Aspetta, questa cosa—"},
            {"host": "Giulia", "text": "Basta. Torniamo alla musica."},
        ]
    )
    giulia_opens = checker.classify_skeleton(
        [
            "Giulia: Che luce strana stasera.",
            "Marco: Eh. La lasciamo stare.",
        ]
    )

    assert marco_opens == {
        "opener": "marco",
        "cutoff": "cutoff",
        "cutoff_direction": "giulia_cuts_marco",
        "landing": "clean",
        "skeleton_id": "marco|giulia_cuts_marco|clean",
    }
    assert giulia_opens["opener"] == "giulia"
    assert giulia_opens["cutoff"] == "through"
    assert giulia_opens["cutoff_direction"] == "through"
    assert giulia_opens["landing"] == "clean"
    assert marco_opens["skeleton_id"] != giulia_opens["skeleton_id"]


@pytest.mark.parametrize(
    "unfinished",
    [
        "Aspetta—",
        "Aspetta–",
        "Aspetta--",
        "Aspetta-",
        "Aspetta...",
        "Aspetta…",
        "Aspetta—”",
        "Aspetta...')",
        "Aspetta",
    ],
)
def test_cutoff_markers_match_runtime_semantics(unfinished: str):
    checker = _load()
    observed = checker.classify_skeleton(
        [
            {"host": "Giulia", "text": unfinished},
            {"host": "Marco", "text": "La chiudo io."},
        ]
    )
    assert observed["cutoff"] == "cutoff"
    assert observed["cutoff_direction"] == "marco_cuts_giulia"


@pytest.mark.parametrize("finished", ["Aspetta.", "Aspetta!", "Aspetta?", "Va bene."])
def test_complete_endings_are_not_cutoffs(finished: str):
    checker = _load()
    observed = checker.classify_skeleton(
        [
            {"host": "Giulia", "text": finished},
            {"host": "Marco", "text": "La chiudo io."},
        ]
    )
    assert observed["cutoff"] == "through"


def test_unfinished_final_line_is_an_open_landing():
    checker = _load()
    observed = checker.classify_skeleton(
        [
            {"host": "Marco", "text": "Una cosa completa."},
            {"host": "Giulia", "text": "Aspetta—"},
        ]
    )
    assert observed["cutoff"] == "through"
    assert observed["landing"] == "open"


def test_classification_ignores_selected_shape_ids():
    checker = _load()
    lines = [
        {"host": "Giulia", "text": "Niente da tagliare oggi."},
        {"host": "Marco", "text": "D'accordo, amici."},
    ]
    summary = checker.summarize(
        [
            {"lines": lines, "exchange_shape_id": "marco_runaway_giulia_contains"},
            {"lines": lines, "exchange_shape_id": "no_conflict_joint_observation"},
        ]
    )
    assert summary["distinct_skeletons"] == 1
    assert {row["selected_shape_id"] for row in summary["rows"]} == {
        "marco_runaway_giulia_contains",
        "no_conflict_joint_observation",
    }


def test_mixed_ledger_joins_only_aired_shape_bearing_banter():
    checker = _load()
    lines = [
        {"host": "Marco", "text": "Io parto—"},
        {"host": "Giulia", "text": "Chiudiamo qui."},
    ]
    rows = [
        _stream("aired-shape"),
        _stream("not-aired", aired_status="skipped"),
        _stream("ad", segment_type="ad"),
        _stream(None),  # canned audio has no generated-dialogue join
        _stream("allowed-skip"),
        _prepared("orphan", lines=lines),
        _prepared("not-aired", lines=lines),
        {"record": "segment_prepared", "segment_id": "ad", "role": "ad_break"},
        _prepared("allowed-skip", shape_id=None, skip_reason="chaos"),
        _prepared("aired-shape", lines=lines),
    ]

    summary = checker.summarize(rows)

    assert summary["eligible_segments"] == 1
    assert summary["segments"] == 1
    assert summary["excluded_segments"] == 1
    assert summary["excluded_by_reason"] == {"chaos": 1}
    assert summary["unclassifiable_segments"] == 0
    assert summary["evidence_errors"] == []
    assert summary["rows"][0]["segment_id"] == "aired-shape"


def test_mixed_ledger_never_infers_speakers_from_bare_final_script(tmp_path: Path):
    checker = _load()
    path = tmp_path / "hostless.jsonl"
    rows = [_prepared("hostless", lines=None), _stream("hostless")]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    summary = checker.summarize(checker.load_jsonl(path))

    assert summary["eligible_segments"] == 1
    assert summary["segments"] == 0
    assert summary["unclassifiable_segments"] == 1
    assert checker.main([str(path)]) == 2


@pytest.mark.parametrize(
    "rows, message",
    [
        (
            [_prepared("missing", shape_id=None), _stream("missing")],
            "neither shape nor skip reason",
        ),
        (
            [
                _prepared("duplicate"),
                _prepared("duplicate"),
                _stream("duplicate"),
            ],
            "duplicate segment_prepared",
        ),
        (
            [_prepared("unknown", shape_id=None, skip_reason="mystery"), _stream("unknown")],
            "unknown exchange_shape_skip_reason",
        ),
    ],
)
def test_malformed_provenance_is_reported(rows: list[dict], message: str):
    checker = _load()
    summary = checker.summarize(rows)
    assert any(message in error for error in summary["evidence_errors"])


def test_summarize_tracks_variety_across_curated_segments():
    checker = _load()
    rows = [
        {
            "lines": [
                {"host": "Marco", "text": "Io parto e poi—"},
                {"host": "Giulia", "text": "Chiudiamo qui."},
            ]
        },
        {
            "lines": [
                {"host": "Giulia", "text": "Osserviamo solo la pioggia."},
                {"host": "Marco", "text": "Sì, senza litigare."},
            ]
        },
        {
            "lines": [
                {"host": "Giulia", "text": "Una piccola cosa, poi basta."},
                {"host": "Marco", "text": "Ho capito tutto io."},
            ]
        },
    ]

    summary = checker.summarize(rows)

    assert summary["segments"] == 3
    assert summary["distinct_skeletons"] >= 2
    assert summary["openers"]["giulia"] >= 1
    assert summary["openers"]["marco"] >= 1
    assert 0.0 <= summary["cutoff_rate"] <= 1.0


def test_cli_reports_distinct_observed_skeletons(tmp_path: Path):
    checker = _load()
    path = tmp_path / "sample.jsonl"
    path.write_text(
        json.dumps(
            {
                "lines": [
                    {"host": "Giulia", "text": "Apriamo noi."},
                    {"host": "Marco", "text": "Va bene così."},
                ]
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert checker.main([str(path)]) == 0
    assert checker.main([str(path), "--fail-below-distinct", "8"]) == 1
    assert checker.main([str(tmp_path / "missing.jsonl")]) == 2
