"""Tests for playlist module: demo tracks."""

from __future__ import annotations

import pytest

from mammamiradio.playlist import DEMO_TRACKS


def test_demo_tracks_has_entries():
    assert len(DEMO_TRACKS) >= 5
    for t in DEMO_TRACKS:
        assert t.title
        assert t.artist
        assert t.duration_ms > 0
        assert t.spotify_id.startswith("demo")


def test_demo_tracks_match_bundled_assets():
    """Every demo track title should match at least one bundled asset filename."""
    from pathlib import Path

    from mammamiradio.downloader import _find_demo_asset

    assets_dir = Path(__file__).parent.parent / "mammamiradio" / "demo_assets" / "music"
    if not assets_dir.exists():
        pytest.skip("demo_assets/music/ not found")

    for track in DEMO_TRACKS:
        result = _find_demo_asset(track)
        assert result is not None, f"Demo track '{track.display}' has no matching asset in demo_assets/music/"
