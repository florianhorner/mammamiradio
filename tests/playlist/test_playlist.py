"""Tests for playlist module: demo tracks and classic Italian source."""

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


# ── Classic Italian source ──────────────────────────────────────────────────


def test_copy_tracks_with_source_classic():
    from mammamiradio.playlist.playlist import _copy_tracks_with_source

    tracks = [Track(title="Azzurro", artist="Adriano Celentano", duration_ms=180000)]
    result = _copy_tracks_with_source(tracks, "classic")
    assert result[0].source == "classic"


def test_parse_classic_artist_title_hyphen():
    from mammamiradio.playlist.playlist import _parse_classic_artist_title

    parsed = _parse_classic_artist_title("Lucio Battisti - Acqua Azzurra")
    assert parsed == ("Lucio Battisti", "Acqua Azzurra")


def test_parse_classic_artist_title_em_dash():
    from mammamiradio.playlist.playlist import _parse_classic_artist_title

    parsed = _parse_classic_artist_title("Vasco Rossi – Vita Spericolata")
    assert parsed == ("Vasco Rossi", "Vita Spericolata")


def test_parse_classic_artist_title_no_separator():
    from mammamiradio.playlist.playlist import _parse_classic_artist_title

    assert _parse_classic_artist_title("RomaRecordsVEVO") is None


def test_classic_source_empty_url_preserves_source_id():
    from mammamiradio.playlist.playlist import _classic_era_from_source

    source = PlaylistSource(kind="classic", source_id="70s", url="")

    assert _classic_era_from_source(source) == "70s"


def test_classic_source_year_stamp(config, monkeypatch):
    from mammamiradio.playlist.playlist import _load_classic_italian_tracks

    fake_results = [
        {
            "title": "Lucio Battisti - Acqua Azzurra",
            "youtube_id": "abc123",
            "duration_ms": 210000,
            "artist": "LucioBattistiVEVO",
            "album_art": "",
        }
    ]
    monkeypatch.setattr("mammamiradio.playlist.downloader._ytdlp_enabled", lambda: True)
    monkeypatch.setattr("mammamiradio.playlist.downloader.search_ytdlp_metadata", lambda *_a, **_k: fake_results)

    tracks = _load_classic_italian_tracks("80s")
    assert tracks
    assert tracks[0].year == 1985
    assert tracks[0].source == "classic"
    assert tracks[0].artist == "Lucio Battisti"


def test_classic_source_ytdlp_disabled_raises(config, monkeypatch):
    from mammamiradio.playlist.playlist import ExplicitSourceError, load_explicit_source

    monkeypatch.setattr("mammamiradio.playlist.downloader._ytdlp_enabled", lambda: False)

    with pytest.raises(ExplicitSourceError, match="temporarily unavailable"):
        load_explicit_source(config, PlaylistSource(kind="classic", url="classic://italian/80s"))


def test_classic_source_post_restart(config, tmp_path, monkeypatch):
    from mammamiradio.playlist.playlist import (
        load_explicit_source,
        read_persisted_source,
        write_persisted_source,
    )

    source = PlaylistSource(kind="classic", url="classic://italian/70s", source_id="70s", label="Classici anni '70")
    write_persisted_source(tmp_path, source)

    restored = read_persisted_source(tmp_path)
    assert restored is not None
    assert restored.kind == "classic"
    assert "70s" in restored.url

    fake_results = [
        {
            "title": "Fabrizio De André - La Guerra di Piero",
            "youtube_id": "yt1",
            "duration_ms": 240000,
            "artist": "",
            "album_art": "",
        }
    ]
    monkeypatch.setattr("mammamiradio.playlist.downloader._ytdlp_enabled", lambda: True)
    monkeypatch.setattr("mammamiradio.playlist.downloader.search_ytdlp_metadata", lambda *_a, **_k: fake_results)

    tracks, resolved = load_explicit_source(config, restored)
    assert resolved.kind == "classic"
    assert tracks
    assert all(t.year == 1975 for t in tracks)
