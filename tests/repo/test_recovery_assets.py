"""Source/package-resource recovery-audio invariants."""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources

import pytest

from mammamiradio.core.packaged_assets import DEMO_ASSETS_DIR
from mammamiradio.core.spoken_assets import (
    is_approved_packaged_audio_asset,
    validate_spoken_asset_manifest,
)

REQUIRED_RECOVERY_ASSETS = ("continuity_1.mp3", "emergency_tone.mp3")


@pytest.mark.parametrize("asset_name", REQUIRED_RECOVERY_ASSETS)
def test_required_recovery_resource_is_package_reachable_and_nontrivial(asset_name: str) -> None:
    """Each required recovery rung is independently readable from the package."""
    recovery_dir = resources.files("mammamiradio").joinpath("assets", "demo", "recovery")
    clip = recovery_dir.joinpath(asset_name)

    assert clip.is_file(), f"missing packaged recovery resource: {asset_name}"
    assert len(clip.read_bytes()) > 1024, f"packaged recovery resource is too small: {asset_name}"


def test_recovery_manifest_accepts_required_set_and_allows_reviewed_extras() -> None:
    """The manifest/hash boundary validates the inventory without requiring exact equality."""
    assert validate_spoken_asset_manifest() == []

    recovery_dir = resources.files("mammamiradio").joinpath("assets", "demo", "recovery")
    packaged_names = {clip.name for clip in recovery_dir.iterdir() if clip.is_file() and clip.name.endswith(".mp3")}
    assert set(REQUIRED_RECOVERY_ASSETS) <= packaged_names


@pytest.mark.parametrize("asset_name", REQUIRED_RECOVERY_ASSETS)
def test_required_recovery_resource_is_manifest_hash_approved(asset_name: str) -> None:
    """A present filename alone is insufficient; each required asset must match its hash."""
    asset_path = DEMO_ASSETS_DIR / "recovery" / asset_name
    assert is_approved_packaged_audio_asset(asset_path, assets_root=DEMO_ASSETS_DIR)


@pytest.mark.parametrize("asset_name", REQUIRED_RECOVERY_ASSETS)
def test_required_recovery_resource_contains_ffprobe_audio(asset_name: str) -> None:
    """Every required asset—not merely the first valid clip—must contain audio."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe is not installed")

    asset_path = DEMO_ASSETS_DIR / "recovery" / asset_name
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(asset_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "audio" in result.stdout.lower(), f"{asset_name} has no ffprobe-readable audio stream"
