"""Tests for playlist module: demo tracks."""

from __future__ import annotations

import pytest

from mammamiradio.core.models import PlaylistSource, Track
from mammamiradio.playlist.playlist import DEMO_TRACKS


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

    from mammamiradio.playlist.downloader import _find_demo_asset

    assets_dir = Path(__file__).resolve().parents[2] / "mammamiradio" / "assets" / "demo" / "music"
    if not assets_dir.exists():
        pytest.skip("mammamiradio/assets/demo/music/ not found")

    for track in DEMO_TRACKS:
        result = _find_demo_asset(track)
        assert result is not None, (
            f"Demo track '{track.display}' has no matching asset in mammamiradio/assets/demo/music/"
        )


@pytest.fixture()
def config():
    from mammamiradio.core.config import load_config

    return load_config()


def test_load_explicit_source_sets_demo_track_source(config):
    from mammamiradio.playlist.playlist import load_explicit_source

    config.playlist.shuffle = False
    tracks, source = load_explicit_source(config, PlaylistSource(kind="demo", source_id="demo"))

    assert source.kind == "demo"
    assert tracks
    assert all(track.source == "demo" for track in tracks)


def test_load_explicit_source_sets_jamendo_track_source(config, monkeypatch):
    from mammamiradio.playlist.playlist import load_explicit_source

    config.playlist.jamendo_client_id = "jamendo-client"
    config.playlist.shuffle = False
    jamendo_tracks = [
        Track(
            title="CC Song",
            artist="CC Artist",
            duration_ms=180000,
            spotify_id="jamendo_1",
            direct_url="https://storage.jamendo.com/tracks/1.mp3",
        )
    ]

    monkeypatch.setattr(
        "mammamiradio.playlist.playlist._fetch_jamendo_playlist", lambda *_args, **_kwargs: jamendo_tracks
    )
    tracks, source = load_explicit_source(config, PlaylistSource(kind="jamendo", source_id="pop"))

    assert source.kind == "jamendo"
    assert tracks[0].source == "jamendo"


def test_load_explicit_source_sets_chart_and_local_track_sources(config, tmp_path, monkeypatch):
    from mammamiradio.playlist.playlist import _load_chart_source_tracks, load_explicit_source

    config.playlist.shuffle = False
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "Local Artist - Local Song.mp3").touch()
    chart_tracks = [Track(title="Chart Song", artist="Chart Artist", duration_ms=180000, spotify_id="chart_1")]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mammamiradio.playlist.playlist._fetch_current_italy_charts", lambda: chart_tracks)

    merged = _load_chart_source_tracks(config)
    tracks, source = load_explicit_source(config, PlaylistSource(kind="charts", source_id="apple_music_it_top_50"))

    assert source.kind == "charts"
    assert {track.source for track in merged} == {"local", "youtube"}
    assert {track.source for track in tracks} == {"local", "youtube"}
