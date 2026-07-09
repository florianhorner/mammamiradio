"""Contract tests for the bundled Italian Night Drive station-imaging pack."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.normalizer import AVAILABLE_SFX_TYPES, loop_audio_bed

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"
GENERATOR_PATH = REPO_ROOT / "scripts" / "generate_sonic_brand_assets.py"


def test_night_drive_manifest_covers_every_runtime_asset() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    asset_paths = {entry["path"] for entry in manifest["assets"]}
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

    assert manifest["schema_version"] == 1
    assert manifest["format"] == {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}
    assert manifest["license"] == "Apache-2.0"
    assert required_core | required_sfx == asset_paths
    for entry in manifest["assets"]:
        path = ASSETS_DIR / entry["path"]
        assert path.is_file(), f"manifest asset missing from the package: {entry['path']}"
        assert path.stat().st_size > 1_024, f"manifest asset is unexpectedly small: {entry['path']}"
        assert entry["license"] == "Apache-2.0"
        assert "no downloaded or external samples" in entry["provenance"]


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
    """A long ad must not breathe at every repeated Casa Notte boundary."""
    source = ASSETS_DIR / "beds" / "casa_notte.mp3"
    looped = tmp_path / "looped_casa_notte.mp3"
    loop_audio_bed(source, looped, 33.0)

    boundary = _duration_sec(source)
    before_boundary = _mean_volume_db(looped, boundary - 0.22, 0.16)
    after_boundary = _mean_volume_db(looped, boundary + 0.03, 0.16)

    # The pre-refresh terminal fade made the tail more than 20 dB quieter. A
    # normal musical phrase can move slightly, but this protects against a
    # repeated, obvious fade-to-near-silence in the runtime loop.
    assert after_boundary >= before_boundary - 6.0


@pytest.mark.requires_ffmpeg
def test_night_drive_generator_validates_the_checked_in_pack() -> None:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "--validate-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("OK ") == len(AVAILABLE_SFX_TYPES) + 7
