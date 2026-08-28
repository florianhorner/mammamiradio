"""Offline contracts for the developer-only banter authoring kit."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "banter-workshop.py"
TEMPLATE = ROOT / "scripts" / "banter-review-board.template.html"
PACK = ROOT / "scripts" / "banter-pack-v1.json"
FEEDBACK = ROOT / "scripts" / "banter-pack-v1-feedback.json"
SPOKEN = ROOT / "mammamiradio" / "assets" / "demo" / "spoken_assets.json"


def _load_workshop():
    spec = importlib.util.spec_from_file_location("banter_workshop", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(args: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _mini_report(tmp_path: Path, *, clip_id: str = "01-demo-clip", sha256: str = "a" * 64) -> Path:
    audio = tmp_path / "normal"
    audio.mkdir(parents=True)
    mp3 = audio / f"{clip_id}.mp3"
    mp3.write_bytes(b"ID3fake-audio")
    report = {
        "schema_version": "1",
        "complete": True,
        "candidate_count": 1,
        "total_bytes": mp3.stat().st_size,
        "total_duration_seconds": 12.0,
        "clips": [
            {
                "id": clip_id,
                "mode": "normal",
                "variant": "original",
                "context": "evergreen",
                "required_previous_starter_id": None,
                "required_previous_track": None,
                "creative_direction": "Keep Studio B affectionate and timeless.",
                "file": f"normal/{clip_id}.mp3",
                "bytes": mp3.stat().st_size,
                "sha256": sha256,
                "duration_seconds": 12.0,
                "audio_measurements": {
                    "silence_ratio": 0.0,
                    "longest_silence_seconds": 0.0,
                    "mean_volume_db": -19.0,
                    "peak_volume_db": -1.5,
                },
                "generation_attempt": 1,
                "transcript": [
                    {"host": "Marco", "text": "The espresso machine filed a complaint."},
                    {"host": "Giulia", "text": "Good. Put it on air."},
                ],
            }
        ],
    }
    path = tmp_path / "review-report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def test_script_and_pack_artifacts_exist() -> None:
    assert SCRIPT.is_file()
    assert TEMPLATE.is_file()
    assert PACK.is_file()
    assert FEEDBACK.is_file()
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    assert len(pack["clips"]) == 21
    assert len(feedback["clips"]) == 21
    assert "Studio B" in pack["description"]


def test_sync_check_passes_against_spoken_assets() -> None:
    result = _run(["sync-check"])
    assert result.returncode == 0, result.stderr
    assert "synchronized" in result.stdout


def test_sync_check_fails_on_hash_drift(tmp_path: Path) -> None:
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    feedback["clips"][0]["sha256"] = "0" * 64
    bad = tmp_path / "bad-feedback.json"
    bad.write_text(json.dumps(feedback), encoding="utf-8")
    result = _run(["sync-check", "--feedback", str(bad)])
    assert result.returncode == 1
    assert "does not match" in result.stderr


def test_sync_check_rejects_missing_feedback_row_and_duplicates(tmp_path: Path) -> None:
    workshop = _load_workshop()
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    removed = json.loads(json.dumps(feedback))
    removed["clips"] = removed["clips"][1:]
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps(removed), encoding="utf-8")
    with pytest.raises(ValueError, match="plan/feedback id sets diverge"):
        workshop.assert_pack_synchronized_with_spoken_assets(feedback_path=missing)

    dup = json.loads(json.dumps(feedback))
    dup["clips"].append(dup["clips"][0])
    duplicate = tmp_path / "dup.json"
    duplicate.write_text(json.dumps(dup), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate id"):
        workshop.assert_pack_synchronized_with_spoken_assets(feedback_path=duplicate)


def test_board_generation_is_deterministic_and_escapes_injection(tmp_path: Path) -> None:
    report_path = _mini_report(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["clips"][0]["creative_direction"] = '</script><img src=x onerror="alert(1)">'
    report["clips"][0]["transcript"][0]["text"] = '<b>raw</b> & "quoted"'
    report_path.write_text(json.dumps(report), encoding="utf-8")

    out_a = tmp_path / "a" / "listening-board.html"
    out_b = tmp_path / "b" / "listening-board.html"
    for out in (out_a, out_b):
        result = _run(
            [
                "board",
                "--report",
                str(report_path),
                "--output",
                str(out),
            ]
        )
        assert result.returncode == 0, result.stderr

    text_a = out_a.read_text(encoding="utf-8")
    text_b = out_b.read_text(encoding="utf-8")
    assert text_a == text_b
    assert "__REVIEW_REPORT__" not in text_a
    assert "__CUTS_HEADLINE__" not in text_a
    assert "One cut." in text_a
    assert "Original 1" in text_a
    assert "fonts.googleapis.com" not in text_a
    assert "fonts.gstatic.com" not in text_a
    assert '<img src=x onerror="alert(1)">' not in text_a
    assert "\\u003c/script\\u003e" in text_a
    assert "normal/01-demo-clip.mp3" in text_a or "../normal/01-demo-clip.mp3" in text_a
    assert "alert(1)" in text_a  # present only inside escaped JSON


def test_board_rejects_malformed_report_fields(tmp_path: Path) -> None:
    report_path = _mini_report(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["clips"][0]["id"] = "bad id<script>"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    out = tmp_path / "board.html"
    result = _run(["board", "--report", str(report_path), "--output", str(out)])
    assert result.returncode == 1
    assert "invalid or duplicate id" in result.stderr


def test_board_rewrites_audio_paths_relative_to_output(tmp_path: Path) -> None:
    report_dir = tmp_path / "run"
    board_dir = tmp_path / "review"
    report_path = _mini_report(report_dir)
    out = board_dir / "listening-board.html"
    result = _run(["board", "--report", str(report_path), "--output", str(out)])
    assert result.returncode == 0, result.stderr
    html = out.read_text(encoding="utf-8")
    assert "../run/normal/01-demo-clip.mp3" in html
    assert "review/normal/" not in html


def test_board_round_trips_feedback_and_isolates_candidate_sets(tmp_path: Path) -> None:
    report_path = _mini_report(tmp_path, clip_id="01-demo-clip", sha256="b" * 64)
    feedback = {
        "schema_version": "1",
        "status": "accepted",
        "clips": [
            {
                "id": "01-demo-clip",
                "title": "Demo Clip",
                "mode": "normal",
                "variant": "original",
                "context": "evergreen",
                "sha256": "b" * 64,
                "decision": "keep",
                "writing_score": 5,
                "audio_score": 4,
                "issues": [],
                "notes": "chemistry stays",
                "updated_at": None,
            }
        ],
    }
    feedback_path = tmp_path / "feedback.json"
    feedback_path.write_text(json.dumps(feedback), encoding="utf-8")
    board = tmp_path / "board.html"
    result = _run(
        [
            "board",
            "--report",
            str(report_path),
            "--feedback",
            str(feedback_path),
            "--output",
            str(board),
        ]
    )
    assert result.returncode == 0, result.stderr
    html = board.read_text(encoding="utf-8")
    assert "chemistry stays" in html
    assert "Demo Clip" in html
    assert "mmr-banter-feedback-" in html
    assert "candidate_set" in html
    assert '"sha256":"' + ("b" * 64) + '"' in html
    assert "Accepted pack · 1/1 Keep" in html

    # Changed audio under the same clip id must drop the prior Keep verdict.
    mismatched = _mini_report(tmp_path / "mismatch", clip_id="01-demo-clip", sha256="c" * 64)
    mismatched_board = tmp_path / "mismatch-board.html"
    result = _run(
        [
            "board",
            "--report",
            str(mismatched),
            "--feedback",
            str(feedback_path),
            "--output",
            str(mismatched_board),
        ]
    )
    assert result.returncode == 0, result.stderr
    mismatch_html = mismatched_board.read_text(encoding="utf-8")
    assert "chemistry stays" not in mismatch_html
    assert "Accepted pack · 1/1 Keep" not in mismatch_html
    assert "Listening room · 1 candidates" in mismatch_html
    assert '"sha256":"' + ("c" * 64) + '"' in mismatch_html

    # A second board with a different hash embeds a different candidate identity.
    other_report = _mini_report(tmp_path / "other", clip_id="01-demo-clip", sha256="d" * 64)
    other_board = tmp_path / "other-board.html"
    result = _run(["board", "--report", str(other_report), "--output", str(other_board)])
    assert result.returncode == 0, result.stderr
    other_html = other_board.read_text(encoding="utf-8")
    assert '"sha256":"' + ("d" * 64) + '"' in other_html
    assert '"sha256":"' + ("b" * 64) + '"' not in other_html


def test_shipped_audio_board_from_tracked_baseline_without_copying_mp3s(tmp_path: Path) -> None:
    # pytest tmp dirs sit outside the repo; write under ROOT/tmp so shipped relatives stay portable.
    out_dir = ROOT / "tmp" / f"banter-accepted-board-test-{tmp_path.name}"
    out = out_dir / "listening-board.html"
    try:
        result = _run(
            [
                "board",
                "--from-accepted-baseline",
                "--shipped-audio",
                "--output",
                str(out),
            ]
        )
        assert result.returncode == 0, result.stderr
        html = out.read_text(encoding="utf-8")
        assert "01-normal-espresso-machine-union.mp3" in html
        assert "normal/01-normal-espresso-machine-union.mp3" not in html
        assert "Twenty-one cuts." in html
        assert "Original 12" in html
        assert "Accepted pack · 21/21 Keep" in html
        assert "../../mammamiradio/assets/demo/banter/" in html
        assert not any(out_dir.rglob("01-normal-espresso-machine-union.mp3"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_render_refuses_nonempty_output(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    (output / "stale.txt").write_text("nope", encoding="utf-8")
    result = _run(["render", "--plan", str(PACK), "--output", str(output), "--no-board"])
    assert result.returncode == 1
    assert "not empty" in result.stderr


def test_plan_loader_rejects_unknown_mode(tmp_path: Path) -> None:
    workshop = _load_workshop()
    plan = tmp_path / "bad.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "id": "bad-row",
                    "mode": "party",
                    "context": "evergreen",
                    "track_id": None,
                    "direction": "nope",
                }
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid mode or context"):
        workshop._load_plan(plan, set())


def test_packaged_script_safety_helpers() -> None:
    workshop = _load_workshop()
    row: dict[str, Any] = {"context": "evergreen"}
    with pytest.raises(ValueError, match="not timeless"):
        workshop._assert_packaged_script_safe(row, ["See you tonight at Studio B."])
    with pytest.raises(ValueError, match="not timeless"):
        workshop._assert_packaged_script_safe(row, ["Meet us at 8 p.m. in Studio B"])
    with pytest.raises(ValueError, match="not timeless"):
        workshop._assert_packaged_script_safe(row, ["This summer in Studio B is chaos"])
    with pytest.raises(ValueError, match="not timeless"):
        workshop._assert_packaged_script_safe(row, ["Ci sentiamo lunedì a Studio B"])
    with pytest.raises(ValueError, match="not timeless"):
        workshop._assert_packaged_script_safe(row, ["See you in December at Studio B"])
    exact = {"context": "exact_track"}
    with pytest.raises(ValueError, match="unprovided audio context"):
        workshop._assert_packaged_script_safe(exact, ["That track sounds like a festival banger."])
    with pytest.raises(ValueError, match="unprovided audio context"):
        workshop._assert_packaged_script_safe(exact, ["That bass is impossible to ignore"])
    with pytest.raises(ValueError, match="unprovided audio context"):
        workshop._assert_packaged_script_safe(exact, ["Quel basso è impossibile da ignorare"])
    with pytest.raises(ValueError, match="unprovided audio context"):
        workshop._assert_packaged_script_safe(exact, ["Those vocals are slow and piano-soft"])


def test_baseline_hashes_match_shipped_files() -> None:
    spoken = json.loads(SPOKEN.read_text(encoding="utf-8"))
    feedback = json.loads(FEEDBACK.read_text(encoding="utf-8"))
    by_id = {Path(entry["path"]).stem: entry for entry in spoken["assets"] if str(entry["path"]).startswith("banter/")}
    for clip in feedback["clips"]:
        asset = ROOT / "mammamiradio" / "assets" / "demo" / by_id[clip["id"]]["path"]
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        assert digest == clip["sha256"] == by_id[clip["id"]]["sha256"]


def _host(name: str = "Marco") -> MagicMock:
    host = MagicMock()
    host.name = name
    return host


@pytest.mark.asyncio
async def test_render_plan_success_retries_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = _load_workshop()
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "clips": [
                    {
                        "id": "01-demo-clip",
                        "title": "Demo",
                        "mode": "normal",
                        "variant": "original",
                        "context": "evergreen",
                        "track_id": None,
                        "direction": "Keep it timeless.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    config = MagicMock()
    config.hosts = [_host("Marco"), _host("Giulia")]
    config.super_italian_mode = False
    config.homeassistant = MagicMock()
    config.party_mode = None
    config.ledger_enabled = False

    attempts = {"n": 0}

    async def fake_render_attempt(base_config, row, starter_by_id, work_dir):
        attempts["n"] += 1
        work_dir.mkdir(parents=True, exist_ok=True)
        if attempts["n"] == 1:
            raise RuntimeError("transient provider failure")
        final_path = work_dir / "final.mp3"
        final_path.write_bytes(b"ID3rendered-audio-bytes")
        transcript = [
            {"host": "Marco", "text": "The mug is back."},
            {"host": "Giulia", "text": "Leave it alone."},
        ]
        measurements = {
            "silence_ratio": 0.0,
            "longest_silence_seconds": 0.0,
            "mean_volume_db": -18.0,
            "peak_volume_db": -1.0,
        }
        return final_path, transcript, 12.5, measurements

    monkeypatch.setattr(workshop, "load_starter_tracks", lambda require_complete=True: [])
    monkeypatch.setattr(workshop, "load_config", lambda _path: config)
    monkeypatch.setattr(workshop, "_render_attempt", fake_render_attempt)

    def _fake_board(*args, **kwargs):
        return Path(kwargs.get("output_path", args[1]))

    monkeypatch.setattr(workshop, "build_listening_board", _fake_board)

    report_path = await workshop.render_plan(plan, output, build_board=False)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["candidate_count"] == 1
    assert report["clips"][0]["generation_attempt"] == 2
    assert report["clips"][0]["sha256"] == hashlib.sha256(b"ID3rendered-audio-bytes").hexdigest()
    assert (output / "normal" / "01-demo-clip.mp3").is_file()
    assert not (output / "_work").exists()
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_render_plan_exhausted_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workshop = _load_workshop()
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "id": "01-demo-clip",
                    "mode": "normal",
                    "context": "evergreen",
                    "track_id": None,
                    "direction": "Keep it timeless.",
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    config = MagicMock()
    config.hosts = [_host("Marco"), _host("Giulia")]
    config.homeassistant = MagicMock()

    async def always_fail(*_args, **_kwargs):
        raise RuntimeError("still broken")

    monkeypatch.setattr(workshop, "load_starter_tracks", lambda require_complete=True: [])
    monkeypatch.setattr(workshop, "load_config", lambda _path: config)
    monkeypatch.setattr(workshop, "_render_attempt", always_fail)
    monkeypatch.setattr(workshop, "MAX_ATTEMPTS", 2)

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        await workshop.render_plan(plan, output, build_board=False)
