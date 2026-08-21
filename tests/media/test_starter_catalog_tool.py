from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "starter-catalog.py"
VALIDATOR = ROOT / "scripts" / "validate-starter-media.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("starter_catalog_tool", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_strict_check_stays_red_for_pending_human_auditions() -> None:
    result = subprocess.run(
        [sys.executable, os.fspath(SCRIPT), "check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "full-track human audition is pending" in result.stdout
    assert "approved derivatives 0/12" in result.stdout
    assert "source receipt is missing" not in result.stdout


def test_incomplete_check_is_green_only_as_explicit_scaffold_mode() -> None:
    result = subprocess.run(
        [sys.executable, os.fspath(SCRIPT), "check", "--allow-incomplete", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["ok"] is True
    assert report["allow_incomplete"] is True
    assert report["groups"]["MEDIA-EVIDENCE"]["status"] == "WARN"
    assert report["groups"]["MEDIA-PACKAGE"]["status"] == "PASS"


def test_validator_wrapper_forwards_scaffold_mode() -> None:
    result = subprocess.run(
        [sys.executable, os.fspath(VALIDATOR), "--allow-incomplete"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "MEDIA-EVIDENCE WARN" in result.stdout


@pytest.mark.parametrize(
    "url",
    [
        "http://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://evil.example/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://user@incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://incompetech.com:444/music/royalty-free/mp3-royaltyfree/Carefree.mp3",
        "https://incompetech.com/other/Carefree.mp3",
    ],
)
def test_acquire_url_boundary_rejects_non_official_inputs(tool, url: str) -> None:
    with pytest.raises(tool.ToolError):
        tool._validate_official_url(url, audio=True)


def test_acquire_url_boundary_accepts_exact_official_directory(tool) -> None:
    tool._validate_official_url("https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3", audio=True)
    tool._validate_piece_url(
        "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037",
        isrc="USUAN1400037",
    )
    with pytest.raises(tool.ToolError, match="does not name exact ISRC"):
        tool._validate_piece_url(
            "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN0000000",
            isrc="USUAN1400037",
        )


def test_acquire_rejects_isrc_not_predeclared_in_manifest(tool) -> None:
    with pytest.raises(tool.ToolError, match="not one exact canonical manifest row"):
        tool._catalog_row("USUAN0000000")


def test_duration_parser_rejects_malformed_official_value(tool) -> None:
    assert tool._duration_from_clock("00:03:25") == 205
    with pytest.raises(tool.ToolError, match="unexpected official duration"):
        tool._duration_from_clock("3:25")


def test_full_human_decision_is_required_and_redacted(tmp_path: Path, tool) -> None:
    decision = {
        "schema_version": "1",
        "isrc": "USUAN1400037",
        "reviewed_at": "2026-07-16T00:00:00Z",
        "reviewer_role": "project-maintainer",
        "listened_from_start_to_finish": True,
        "title_and_artist_match": True,
        "no_unexpected_speech_or_restricted_content": True,
        "editorially_approved": True,
        "license_evidence_reviewed": True,
    }
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(decision), encoding="utf-8")
    assert tool._validate_decisions(path, isrc="USUAN1400037") == decision

    decision["listened_from_start_to_finish"] = False
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(tool.ToolError, match="human approval is incomplete"):
        tool._validate_decisions(path, isrc="USUAN1400037")

    decision["listened_from_start_to_finish"] = True
    decision["reviewer"] = "A Personal Name"
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(tool.ToolError, match="reviewer_role"):
        tool._validate_decisions(path, isrc="USUAN1400037")

    decision.pop("reviewer")
    decision["reviewed_at"] = "not-a-timestampZ"
    path.write_text(json.dumps(decision), encoding="utf-8")
    with pytest.raises(tool.ToolError, match="valid ISO timestamp"):
        tool._validate_decisions(path, isrc="USUAN1400037")


def test_candidate_identifier_accepts_acquire_timestamp(tmp_path: Path, monkeypatch, tool) -> None:
    work = tmp_path / "starter-work"
    candidate = "usuan1400037-20260715T233405Z"
    (work / "candidates" / candidate).mkdir(parents=True)
    monkeypatch.setenv("MAMMAMIRADIO_STARTER_WORK_DIR", os.fspath(work))

    assert tool._candidate_dir(candidate).name == candidate
    with pytest.raises(tool.ToolError, match="identifier printed by acquire"):
        tool._candidate_dir("../escape")


def test_receipt_boundary_accepts_proof_and_rejects_escape(tool) -> None:
    assert tool._repo_receipt_path("proof/media/starter-source-evidence.json").is_file()
    with pytest.raises(tool.ToolError, match="unsafe media receipt path"):
        tool._repo_receipt_path("../outside.json")


def test_approved_audio_is_remeasured_for_loudness_and_duration(tmp_path: Path, monkeypatch, tool) -> None:
    entries = tuple(
        SimpleNamespace(
            isrc=f"USUAN990000{index}",
            path=tmp_path / f"track-{index}.mp3",
            duration_seconds=duration,
        )
        for index, duration in enumerate((1350.0, 1349.0))
    )
    probe_facts = {
        entry.path: {
            "codec": "mp3",
            "sample_rate_hz": 48000,
            "channels": 2,
            "bitrate_kbps": 192,
            "duration_seconds": entry.duration_seconds,
        }
        for entry in entries
    }
    loudness = iter((-16.0, -14.9))
    monkeypatch.setattr(tool, "_probe", lambda path: probe_facts[path])
    monkeypatch.setattr(tool, "_measure_lufs", lambda _path: next(loudness))

    measured_duration, failures = tool._measure_approved_entries(
        entries,
        expected_loudness_by_isrc={entries[0].isrc: -16.0, entries[1].isrc: -16.0},
    )

    assert measured_duration == 2699.0
    assert any("outside -17.0..-15.0 LUFS" in failure for failure in failures)
    assert any("loudness does not match the manifest" in failure for failure in failures)


def test_strict_check_uses_measured_duration_not_expected_duration(tmp_path: Path, monkeypatch, tool) -> None:
    isrc = "USUAN9900001"
    derivative = {
        "path": "tracks/fixture.mp3",
        "sha256": "b" * 64,
        "bytes": 100,
        "codec": "mp3",
        "sample_rate_hz": 48000,
        "channels": 2,
        "bitrate_kbps": 192,
        "duration_seconds": 3000.0,
        "integrated_loudness_lufs": -16.0,
    }
    catalog = {
        "schema_version": "1",
        "catalog_id": "starter-v1",
        "release_requirements": {
            "exact_track_count": 1,
            "minimum_duration_seconds": 2700,
            "maximum_payload_bytes": 1_000_000,
        },
        "tracks": [
            {
                "isrc": isrc,
                "expected_duration_seconds": 3000,
                "license": {"id": "CC-BY-4.0"},
                "source": {
                    "url": "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Fixture.mp3",
                    "evidence_url": f"https://incompetech.com/music/royalty-free/index.html?isrc={isrc}",
                    "sha256": "a" * 64,
                    "bytes": 200,
                },
                "evidence": {
                    "source_receipt": "proof/media/fixture-approval.json",
                    "audition_receipt": "proof/media/fixture-approval.json",
                },
                "approval": {"status": "approved"},
                "derivative": derivative,
            }
        ],
    }
    receipt = {
        "schema_version": "1",
        "receipt_type": "starter-track-approval",
        "isrc": isrc,
        "decisions": {field: True for field in tool.REQUIRED_DECISIONS},
        "source": {"sha256": "a" * 64, "bytes": 200},
        "derivative": {"sha256": "b" * 64},
    }
    catalog_path = tmp_path / "starter" / "catalog.json"
    catalog_path.parent.mkdir()
    catalog_path.write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('"assets/**/*"\n', encoding="utf-8")
    entry = SimpleNamespace(isrc=isrc)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(tool, "CATALOG_PATH", catalog_path)
    monkeypatch.setattr(tool, "_catalog_data", lambda: catalog)
    monkeypatch.setattr(tool, "_repo_receipt_path", lambda _path: tmp_path / "receipt.json")
    monkeypatch.setattr(tool, "_read_json", lambda _path: receipt)
    monkeypatch.setattr(
        tool,
        "load_starter_catalog",
        lambda **_kwargs: SimpleNamespace(entries=(entry,)),
    )
    monkeypatch.setattr(tool, "_measure_approved_entries", lambda *_args, **_kwargs: (2699.0, []))

    report = tool.check()

    assert report["groups"]["MEDIA-AUDIO"]["status"] == "FAIL"
    assert "measured approved duration 2699.000s is below 2700s" in report["groups"]["MEDIA-AUDIO"]["messages"]


@pytest.mark.requires_ffmpeg
def test_ebur128_measurement_accepts_a_real_in_tolerance_derivative(tmp_path: Path, tool) -> None:
    path = tmp_path / "in-tolerance.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-af",
            "volume=5dB",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            os.fspath(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    measured_lufs = tool._measure_lufs(path)

    assert measured_lufs >= tool.STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS
    assert measured_lufs <= tool.STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS
