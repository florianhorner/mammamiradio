from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import audition_sonic_brand as audition


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
