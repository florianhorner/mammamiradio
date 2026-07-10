"""Contract tests for Mamma Mi Radio's public recorded imaging pack."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.imaging_schema import RECIPE_CUE_ANCHORS
from mammamiradio.audio.normalizer import AVAILABLE_SFX_TYPES, loop_audio_bed
from mammamiradio.hosts.ad_creative import OFFICIAL_SONIC_RECIPE_IDS
from scripts import validate_audio_asset_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_sonic_brand_assets.py"


def test_recorded_manifest_covers_default_runtime_and_official_scene_recipes() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assets = manifest["assets"]
    assert isinstance(assets, list)
    asset_paths = {entry["path"] for entry in assets}
    asset_ids = {entry["id"] for entry in assets}
    required_core = {
        "station_id.mp3",
        "sweeper.mp3",
        "time_check.mp3",
        "bumpers/ad_break.mp3",
        "stingers/music_to_speech.mp3",
        "stingers/speech_to_music.mp3",
        "beds/casa_notte.mp3",
    }
    required_sfx = {f"sfx/{name}.mp3" for name in AVAILABLE_SFX_TYPES}

    assert manifest["schema_version"] == 2
    assert required_core | required_sfx <= asset_paths
    assert len(manifest["sources"]) >= 10
    assert all(source["license"] == "CC0-1.0" for source in manifest["sources"])
    assert all(entry["kind"] and entry["tags"] and entry["source_ids"] for entry in assets)
    for entry in assets:
        path = ASSETS_DIR / entry["path"]
        assert path.is_file(), f"manifest asset missing from the package: {entry['path']}"
        assert path.stat().st_size > 1_024, f"manifest asset is unexpectedly small: {entry['path']}"

    recipes = manifest["recipes"]
    assert {recipe["id"] for recipe in recipes} == OFFICIAL_SONIC_RECIPE_IDS
    for recipe in recipes:
        assert recipe["bed"]["asset_id"] in asset_ids
        assert len(recipe["cues"]) <= 2
        assert all(cue["asset_id"] in asset_ids for cue in recipe["cues"])
        assert all(cue["anchor"] in RECIPE_CUE_ANCHORS for cue in recipe["cues"])


def test_public_pack_provenance_and_attribution_ledger_are_valid() -> None:
    report = validate_audio_asset_pack.validate_audio_asset_pack(ASSETS_DIR)
    validate_audio_asset_pack.check_attribution(report)
    assert report.recipes == len(OFFICIAL_SONIC_RECIPE_IDS)


def test_default_imaging_library_uses_the_pack_for_all_audible_default_surfaces(tmp_path: Path) -> None:
    library = ImagingLibrary([523, 659, 784, 1047], tmp_path)

    station_id = tmp_path / "station_id.mp3"
    sweeper = tmp_path / "sweeper.mp3"
    time_check = tmp_path / "time_check.mp3"
    bumper = tmp_path / "bumper.mp3"
    assert library.pick_station_id_bed(station_id) == station_id
    assert library.pick_sweeper_sting(sweeper) == sweeper
    assert library.pick_time_check_sting(time_check) == time_check
    assert library.pick_ad_bumper(bumper) == bumper

    assert station_id.read_bytes() == (ASSETS_DIR / "station_id.mp3").read_bytes()
    assert sweeper.read_bytes() == (ASSETS_DIR / "sweeper.mp3").read_bytes()
    assert time_check.read_bytes() == (ASSETS_DIR / "time_check.mp3").read_bytes()
    assert bumper.read_bytes() == (ASSETS_DIR / "bumpers" / "ad_break.mp3").read_bytes()
    assert library.ad_sfx_dir() == ASSETS_DIR / "sfx"
    assert library.ad_beds_dir() == ASSETS_DIR / "beds"


def _duration_sec(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return float(completed.stdout)


def _mean_volume_db(path: Path, start_sec: float, duration_sec: float) -> float:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    matches = re.findall(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", completed.stderr)
    assert matches, completed.stderr
    return float(matches[-1])


@pytest.mark.requires_ffmpeg
def test_casa_notte_has_no_level_drop_across_a_runtime_loop_boundary(tmp_path: Path) -> None:
    """A long spoken segment must not fade down every time its room bed repeats."""
    source = ASSETS_DIR / "beds" / "casa_notte.mp3"
    looped = tmp_path / "looped_casa_notte.mp3"
    loop_audio_bed(source, looped, 33.0)

    boundary = _duration_sec(source)
    before_boundary = _mean_volume_db(looped, boundary - 0.22, 0.16)
    after_boundary = _mean_volume_db(looped, boundary + 0.03, 0.16)
    assert after_boundary >= before_boundary - 6.0


@pytest.mark.requires_ffmpeg
def test_mid_break_bumper_honors_the_requested_short_duration(tmp_path: Path) -> None:
    library = ImagingLibrary([523, 659, 784, 1047], tmp_path)
    mid_bumper = tmp_path / "mid_bumper.mp3"

    library.pick_ad_bumper(mid_bumper, 0.8)

    assert _duration_sec(mid_bumper) == pytest.approx(0.8, abs=0.08)


@pytest.mark.requires_ffmpeg
def test_every_recorded_asset_has_audible_programme_signal() -> None:
    """A broken trim must not silently ship as an almost-empty public MP3."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    quiet: list[str] = []
    for entry in manifest["assets"]:
        mean = _mean_volume_db(ASSETS_DIR / entry["path"], 0.0, min(1.0, entry["duration_target_sec"]))
        if mean < -48.0:
            quiet.append(f"{entry['path']} ({mean:.1f} dB)")
    assert not quiet, "public imaging assets are effectively silent: " + ", ".join(quiet)


@pytest.mark.requires_ffmpeg
def test_legacy_generator_entrypoint_validates_but_cannot_recreate_synthetic_audio() -> None:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "--validate-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Audio asset pack OK: 11 sources, 60 assets, 9 recipes" in completed.stdout
