"""Packaged recovery-audio invariants."""

from __future__ import annotations

from importlib import resources


def test_packaged_recovery_clip_exists() -> None:
    """The app image must carry at least one real recovery MP3."""
    recovery_dir = resources.files("mammamiradio").joinpath("assets", "demo", "recovery")
    clips = [clip for clip in recovery_dir.iterdir() if clip.name.endswith(".mp3")]

    assert clips, "missing packaged recovery MP3 under mammamiradio/assets/demo/recovery/"
    assert any(len(clip.read_bytes()) > 1024 for clip in clips), "packaged recovery MP3 is empty or too small"
