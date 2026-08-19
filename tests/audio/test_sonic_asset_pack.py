"""Contract tests for Mamma Mi Radio's Modern Night Drive imaging pack."""

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
EXPECTED_RUNTIME_MANIFEST_FIELDS = {
    "schema_version",
    "pack",
    "provenance",
    "generated_at",
    "design_direction",
    "production_contract",
    "sources",
    "assets",
    "recipes",
    "quality_guard_allowlist",
    "pack_digest",
    "inventory",
}
EXPECTED_ASSET_PATHS = {
    "station_id.mp3",
    "sweeper.mp3",
    "time_check.mp3",
    "bumpers/ad_in.mp3",
    "bumpers/ad_mid.mp3",
    "bumpers/ad_out.mp3",
    "stingers/music_to_speech.mp3",
    "stingers/speech_to_music.mp3",
    "beds/casa_notte.mp3",
    "sfx/chime.mp3",
    "sfx/ding.mp3",
    "sfx/cash_register.mp3",
    "sfx/register_hit.mp3",
    "sfx/sweep.mp3",
    "sfx/whoosh.mp3",
    "sfx/tape_stop.mp3",
    "sfx/hotline_beep.mp3",
    "sfx/mandolin_sting.mp3",
    "sfx/ice_clink.mp3",
    "sfx/startup_synth.mp3",
    "ads/beds/cafe_testimonial.mp3",
    "ads/beds/stadium_win.mp3",
    "ads/beds/showroom_reveal.mp3",
    "ads/beds/bureaucracy_stamp.mp3",
    "ads/beds/motorway_pass.mp3",
    "ads/beds/late_night_hotline.mp3",
    "ads/beds/supermarket_dash.mp3",
    "ads/beds/pharmacy_whisper.mp3",
    "ads/beds/home_reveal.mp3",
    "ads/cues/espresso_glint.mp3",
    "ads/cues/room_smile.mp3",
    "ads/cues/crowd_lift.mp3",
    "ads/cues/score_flash.mp3",
    "ads/cues/glass_open.mp3",
    "ads/cues/velvet_drop.mp3",
    "ads/cues/paper_tick.mp3",
    "ads/cues/seal_close.mp3",
    "ads/cues/lane_rise.mp3",
    "ads/cues/tail_light.mp3",
    "ads/cues/line_open.mp3",
    "ads/cues/line_release.mp3",
    "ads/cues/scanner_ping.mp3",
    "ads/cues/checkout_glide.mp3",
    "ads/cues/soft_dose.mp3",
    "ads/cues/clean_release.mp3",
    "ads/cues/door_glow.mp3",
    "ads/cues/lamp_resolve.mp3",
}


def test_runtime_manifest_covers_default_assets_and_official_scene_recipes() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assets = manifest["assets"]
    assert isinstance(assets, list)
    asset_paths = {entry["path"] for entry in assets}
    asset_ids = {entry["id"] for entry in assets}
    required_sfx = {f"sfx/{name}.mp3" for name in AVAILABLE_SFX_TYPES}

    assert manifest["schema_version"] == 2
    assert set(manifest) == EXPECTED_RUNTIME_MANIFEST_FIELDS
    assert manifest["pack"] == "Mamma Mi Radio — Modern Night Drive"
    assert asset_paths == EXPECTED_ASSET_PATHS
    assert required_sfx <= asset_paths
    assert "bumpers/ad_break.mp3" not in asset_paths
    assert len(assets) == 47
    assert all(entry["kind"] and entry["tags"] and entry["source_ids"] for entry in assets)
    for entry in assets:
        path = ASSETS_DIR / entry["path"]
        assert path.is_file(), f"manifest asset missing from the package: {entry['path']}"
        assert path.stat().st_size > 1_024, f"manifest asset is unexpectedly small: {entry['path']}"

    recipes = manifest["recipes"]
    assert len(recipes) == 9
    assert {recipe["id"] for recipe in recipes} == OFFICIAL_SONIC_RECIPE_IDS
    for recipe in recipes:
        assert recipe["bed"]["asset_id"] in asset_ids
        assert len(recipe["cues"]) <= 2
        assert all(cue["asset_id"] in asset_ids for cue in recipe["cues"])
        assert all(cue["anchor"] in RECIPE_CUE_ANCHORS for cue in recipe["cues"])


def test_runtime_manifest_maps_each_asset_to_one_retained_master() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assets = manifest["assets"]
    sources = manifest["sources"]

    assert len(sources) == 47
    assert all(source["license"] == "CC0-1.0" for source in sources)
    assert all(source["creator"] == "Mammami Radio project" for source in sources)
    assert all(source["provenance_type"] == "project-authored-deterministic-master" for source in sources)
    assert all(len(asset["source_ids"]) == 1 and len(asset["layers"]) == 1 for asset in assets)
    assert {asset["source_ids"][0] for asset in assets} == {source["id"] for source in sources}
    assert len({asset["source_ids"][0] for asset in assets}) == 47
    assert {source["original_path"] for source in sources} == {
        f"provenance/source-masters/{asset['id']}.mp3" for asset in assets
    }


def test_runtime_manifest_inventory_excludes_external_receipt() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assets = manifest["assets"]
    sources = manifest["sources"]
    inventory = manifest["inventory"]
    files = inventory["files"]
    declared_paths = [entry["path"] for entry in files]
    expected_paths = {
        "README.md",
        "ATTRIBUTION.md",
        *(asset["path"] for asset in assets),
        *(source["original_path"] for source in sources),
    }

    assert "listening_receipt" not in manifest
    assert "release_ready" not in manifest
    assert inventory["schema_version"] == 1
    assert set(inventory) == {"schema_version", "files", "digest"}
    assert declared_paths == sorted(expected_paths)
    assert len(declared_paths) == 96
    assert all(set(entry) == {"path", "sha256"} for entry in files)

    pack_entries = tuple(ASSETS_DIR.rglob("*"))
    assert not [path for path in pack_entries if path.is_symlink()]
    actual_files = {path.relative_to(ASSETS_DIR).as_posix() for path in pack_entries if path.is_file()}
    assert actual_files == expected_paths | {"manifest.json"}


def test_runtime_manifest_declares_identity_and_bumper_roles() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["design_direction"] == {
        "signature": "neon_relay",
        "atmospheric_character": "velvet_horizon",
        "signature_roles": ["identity.station-id", "identity.sweeper"],
        "foreground_policy": "one unique project-authored source per advertising cue",
        "small_speaker_policy": "recognition remains above 170 Hz",
    }
    assert manifest["production_contract"]["station_and_sweeper_are_runtime_underlays"] is True
    assert manifest["production_contract"]["ad_bumper_roles"] == ["in", "mid", "out"]


def test_public_pack_provenance_and_attribution_ledger_are_valid() -> None:
    report = validate_audio_asset_pack.validate_audio_asset_pack(ASSETS_DIR)
    validate_audio_asset_pack.check_attribution(report)
    assert report.recipes == len(OFFICIAL_SONIC_RECIPE_IDS)


def test_default_imaging_library_uses_the_pack_for_all_audible_default_surfaces(tmp_path: Path) -> None:
    library = ImagingLibrary([523, 659, 784, 1047], tmp_path)

    station_id = tmp_path / "station_id.mp3"
    sweeper = tmp_path / "sweeper.mp3"
    time_check = tmp_path / "time_check.mp3"
    ad_in = tmp_path / "ad_in.mp3"
    ad_mid = tmp_path / "ad_mid.mp3"
    ad_out = tmp_path / "ad_out.mp3"
    assert library.pick_station_id_bed(station_id) == station_id
    assert library.pick_sweeper_sting(sweeper) == sweeper
    assert library.pick_time_check_sting(time_check) == time_check
    assert library.pick_ad_bumper(ad_in) == ad_in
    assert library.pick_ad_bumper(ad_mid, role="mid") == ad_mid
    assert library.pick_ad_bumper(ad_out, role="out") == ad_out

    assert station_id.read_bytes() == (ASSETS_DIR / "station_id.mp3").read_bytes()
    assert sweeper.read_bytes() == (ASSETS_DIR / "sweeper.mp3").read_bytes()
    assert time_check.read_bytes() == (ASSETS_DIR / "time_check.mp3").read_bytes()
    assert ad_in.read_bytes() == (ASSETS_DIR / "bumpers" / "ad_in.mp3").read_bytes()
    assert ad_mid.read_bytes() == (ASSETS_DIR / "bumpers" / "ad_mid.mp3").read_bytes()
    assert ad_out.read_bytes() == (ASSETS_DIR / "bumpers" / "ad_out.mp3").read_bytes()
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

    library.pick_ad_bumper(mid_bumper, 0.8, role="mid")

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
    assert "Audio asset pack OK: 47 sources, 47 assets, 9 recipes" in completed.stdout
