from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts import audition_sonic_brand as audition
from scripts import build_public_imaging_pack as pack_builder


def _write_pack(assets_dir: Path) -> None:
    for relative_path in audition.required_pack_paths():
        destination = assets_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"asset:{relative_path.as_posix()}".encode())


def _fake_render(sample: audition.SonicSample, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(f"baseline:{sample.key}".encode())
    return output_path


def test_required_pack_paths_covers_manifest_core_surfaces_and_sfx() -> None:
    required = {path.as_posix() for path in audition.required_pack_paths()}

    assert "manifest.json" in required
    assert "station_id.mp3" in required
    assert "bumpers/ad_break.mp3" in required
    assert "stingers/music_to_speech.mp3" in required
    assert "stingers/speech_to_music.mp3" in required
    assert "beds/casa_notte.mp3" in required
    assert {f"sfx/{name}.mp3" for name in audition.AVAILABLE_SFX_TYPES}.issubset(required)


def test_main_uses_deterministic_timestamp_and_writes_manifest_and_html(tmp_path, monkeypatch) -> None:
    assets_dir = tmp_path / "pack"
    _write_pack(assets_dir)
    monkeypatch.setattr(audition, "PACKAGED_ASSETS_DIR", assets_dir)
    monkeypatch.setattr(audition, "render_baseline_sample", _fake_render)

    output_dir = tmp_path / "auditions"
    timestamp = "20260710T120000Z"
    assert audition.main(["--output-dir", str(output_dir), "--timestamp", timestamp, "--no-recipe-previews"]) == 0

    run_dir = output_dir / f"audition-{timestamp}"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    page = (run_dir / "index.html").read_text()

    assert manifest["generated_at"] == timestamp
    assert manifest["mode"] == "local-only"
    assert manifest["pack"] == "Recorded Night Drive"
    assert manifest["scene_recipes"] == []
    assert len(manifest["samples"]) == len(audition.SAMPLES)
    assert page.count("<audio controls") == len(audition.SAMPLES) * 2
    assert "Night Drive packaged asset" in page
    assert "https://" not in page

    for result in manifest["samples"]:
        baseline = run_dir / result["baseline"]
        night_drive = run_dir / result["night_drive"]
        assert baseline.read_bytes() == f"baseline:{result['key']}".encode()
        assert night_drive.read_bytes() == (assets_dir / result["source_asset"]).read_bytes()


def test_main_reports_invalid_timestamp_without_creating_output(tmp_path, monkeypatch, capsys) -> None:
    assets_dir = tmp_path / "pack"
    _write_pack(assets_dir)
    monkeypatch.setattr(audition, "PACKAGED_ASSETS_DIR", assets_dir)

    output_dir = tmp_path / "auditions"
    assert (
        audition.main(["--output-dir", str(output_dir), "--timestamp", "not-a-timestamp", "--no-recipe-previews"]) == 2
    )

    assert "timestamp must use YYYYMMDDTHHMMSSZ format" in capsys.readouterr().err
    assert not output_dir.exists()


def test_main_fails_clearly_when_the_night_drive_pack_is_incomplete(tmp_path, monkeypatch, capsys) -> None:
    assets_dir = tmp_path / "pack"
    assets_dir.mkdir()
    monkeypatch.setattr(audition, "PACKAGED_ASSETS_DIR", assets_dir)

    output_dir = tmp_path / "auditions"
    assert audition.main(["--output-dir", str(output_dir), "--timestamp", "20260710T120000Z"]) == 2

    error = capsys.readouterr().err
    assert "Night Drive pack is incomplete" in error
    assert "manifest.json" in error
    assert "station_id.mp3" in error
    assert "sfx/chime.mp3" in error
    assert not output_dir.exists()


def test_render_audition_refuses_to_overwrite_a_prior_deterministic_run(tmp_path, monkeypatch) -> None:
    assets_dir = tmp_path / "pack"
    _write_pack(assets_dir)
    monkeypatch.setattr(audition, "render_baseline_sample", _fake_render)
    run_dir = tmp_path / "audition-20260710T120000Z"

    audition.render_audition(run_dir, assets_dir=assets_dir)

    try:
        audition.render_audition(run_dir, assets_dir=assets_dir)
    except FileExistsError as exc:
        assert "Refusing to overwrite" in str(exc)
    else:
        raise AssertionError("expected deterministic run collision to be rejected")


def test_motif_prototypes_retire_the_old_motif_and_exclude_rejected_instruments() -> None:
    candidates = pack_builder.MOTIF_PROTOTYPES
    assert [candidate.id for candidate in candidates] == [
        "midnight_signal",
        "city_pulse",
        "warm_resolve",
    ]
    assert all(0.8 <= candidate.duration_sec <= 1.2 for candidate in candidates)

    pitch_sequences = [
        [layer.pitch_semitones for layer in candidate.layers if layer.role == "foreground"] for candidate in candidates
    ]
    assert [0, 4, 7, 12] not in pitch_sequences
    assert pitch_sequences == [[7, 3, 0], [0, 5, 0], [0, 5, 9, 4]]

    declared_text = json.dumps(
        {
            "sources": [source.__dict__ for source in pack_builder.MOTIF_PROTOTYPE_SOURCES],
            "candidates": [candidate.__dict__ for candidate in candidates],
        },
        default=str,
    ).lower()
    assert "trumpet" not in declared_text
    assert "mandolin" not in declared_text


def test_motif_source_verification_fails_closed_on_any_hash_mismatch(tmp_path, monkeypatch) -> None:
    source = pack_builder.MOTIF_PROTOTYPE_SOURCES[0]
    altered = replace(source, filename="source.mp3", source_sha256=hashlib.sha256(b"expected").hexdigest())
    monkeypatch.setattr(pack_builder, "MOTIF_PROTOTYPE_SOURCES", (altered,))
    (tmp_path / altered.filename).write_bytes(b"altered")

    with pytest.raises(ValueError, match=r"SHA-256 mismatch for source\.mp3"):
        pack_builder.verify_motif_prototype_sources(tmp_path)


def test_motif_manifest_records_exact_layers_digest_and_pending_receipt(tmp_path) -> None:
    for candidate in pack_builder.MOTIF_PROTOTYPES:
        (tmp_path / candidate.path).write_bytes(f"candidate:{candidate.id}".encode())

    manifest = pack_builder._motif_prototype_manifest(tmp_path, generated_at="20260813T171500Z")

    assert manifest["stage"] == "motif-selection"
    assert manifest["release_ready"] is False
    assert manifest["listening_receipt"] == {
        "status": "pending",
        "pack_digest": None,
        "approved_candidate_id": None,
        "surfaces": [],
        "device_classes": [],
        "reviewed_at": None,
    }
    assert len(manifest["pack_digest"]) == 64
    assert all(source["license"] == "CC0-1.0" and source["tags"] for source in manifest["sources"])
    for candidate in manifest["candidates"]:
        assert candidate["layers"]
        for layer in candidate["layers"]:
            assert set(layer) == {
                "source_id",
                "source_sha256",
                "source_start_sec",
                "duration_sec",
                "output_offset_sec",
                "gain_db",
                "dsp",
                "license",
                "role",
            }
            assert layer["role"] in {"foreground", "texture"}


def test_motif_gate_writes_durable_board_and_exact_listening_handoff(tmp_path, monkeypatch) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()

    def fake_render(_source_dir: Path, run_dir: Path, *, generated_at: str) -> dict[str, Any]:
        run_dir.mkdir(parents=True)
        candidates = []
        for candidate in pack_builder.MOTIF_PROTOTYPES:
            (run_dir / candidate.path).write_bytes(f"audio:{candidate.id}".encode())
            candidates.append(
                {
                    "id": candidate.id,
                    "label": candidate.label,
                    "brief": candidate.brief,
                    "path": candidate.path,
                    "sha256": hashlib.sha256(f"audio:{candidate.id}".encode()).hexdigest(),
                }
            )
        manifest: dict[str, Any] = {
            "generated_at": generated_at,
            "candidates": candidates,
            "sources": [
                {
                    "title": "Verified source",
                    "source_url": "https://example.test/source",
                    "creator": "Recorder",
                    "source_sha256": "a" * 64,
                }
            ],
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    monkeypatch.setattr(pack_builder, "render_motif_prototypes", fake_render)
    output_dir = tmp_path / "auditions"
    timestamp = "20260813T171500Z"

    assert (
        audition.main(
            [
                "--output-dir",
                str(output_dir),
                "--timestamp",
                timestamp,
                "--motif-source-dir",
                str(source_dir),
            ]
        )
        == 0
    )

    run_dir = output_dir / f"motif-gate-{timestamp}"
    page = (run_dir / "index.html").read_text()
    handoff = (run_dir / "README.md").read_text()
    assert page.count("<audio id=") == 3
    assert page.count('onclick="repeatThree') == 3
    assert "Prototype only" in page
    assert "Wohnzimmer Sonos Arc" in page
    assert "[Open the listening board](./index.html)" in handoff
    assert "[Midnight Signal](./midnight_signal.mp3)" in handoff
    assert "[City Pulse](./city_pulse.mp3)" in handoff
    assert "[Warm Resolve](./warm_resolve.mp3)" in handoff
    assert "https://example.test/source" in handoff
    assert audition.MOTIF_LISTENING_PROMPT in handoff


@pytest.mark.requires_ffmpeg
def test_scene_recipe_previews_cover_the_shipped_recorded_recipe_inventory(tmp_path) -> None:
    results = audition.render_recipe_previews(tmp_path, assets_dir=audition.PACKAGED_ASSETS_DIR)

    assert {result.id for result in results} == {
        "bureaucracy_stamp",
        "cafe_testimonial",
        "home_reveal",
        "late_night_hotline",
        "motorway_pass",
        "pharmacy_whisper",
        "showroom_reveal",
        "stadium_win",
        "supermarket_dash",
    }
    assert all((tmp_path / result.audio).is_file() for result in results)
    assert all(len(result.cues) <= 2 for result in results)
