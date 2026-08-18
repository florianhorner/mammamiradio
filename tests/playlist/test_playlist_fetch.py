"""Tests for base-playlist loading and the retired durable Jamendo path."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import PlaylistSource, StationState, Track
from mammamiradio.media.starter import StarterCatalogError
from mammamiradio.playlist.playlist import (
    DEMO_TRACKS,
    ExplicitSourceError,
    ExternalMediaUnavailableError,
    LegacyJamendoSourceRetiredError,
    _load_local_music_tracks,
    fetch_chart_refresh,
    fetch_startup_playlist,
    load_explicit_source,
    read_persisted_source,
    write_persisted_source,
)


@pytest.fixture()
def config():
    return load_config()


def _track(
    title: str,
    artist: str = "Artista",
    *,
    source: Literal["youtube", "jamendo", "local", "demo", "classic", "starter"] = "youtube",
) -> Track:
    return Track(
        title=title,
        artist=artist,
        duration_ms=210_000,
        spotify_id=f"test_{title.lower().replace(' ', '_')}",
        source=source,
    )


def _chart_response(results: list[dict[str, str]]) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps({"feed": {"results": results}}).encode()
    response.__enter__ = lambda value: value
    response.__exit__ = MagicMock(return_value=False)
    return response


# ---------------------------------------------------------------------------
# Startup base selection
# ---------------------------------------------------------------------------


def test_unlicensed_demo_metadata_catalog_is_empty():
    assert DEMO_TRACKS == []


def test_startup_prefers_operator_local_music(config):
    local = _track("Emozioni", "Lucio Battisti")

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[local]),
        patch("mammamiradio.media.starter.load_starter_rotation_tracks") as starter_loader,
    ):
        tracks, source, error = fetch_startup_playlist(config)

    assert [(track.title, track.source) for track in tracks] == [("Emozioni", "local")]
    assert source.kind == "local"
    assert source.track_count == 1
    assert error == ""
    starter_loader.assert_not_called()


def test_startup_uses_strict_starter_when_local_music_is_empty(config):
    starter_tracks = [_track("Carefree", source="starter"), _track("Cipher", source="starter")]

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.media.starter.load_starter_rotation_tracks", return_value=starter_tracks),
    ):
        tracks, source, error = fetch_startup_playlist(config)

    assert tracks == starter_tracks
    assert source.kind == "starter"
    assert source.track_count == 2
    assert error == ""


def test_startup_pending_starter_catalog_fails_closed(config):
    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch(
            "mammamiradio.media.starter.load_starter_rotation_tracks",
            side_effect=StarterCatalogError("catalog has 0 approved derivatives; release requires 12"),
        ),
    ):
        tracks, source, error = fetch_startup_playlist(config)

    assert tracks == []
    assert source.kind == "starter"
    assert source.track_count == 0
    assert "not release-ready" in error
    assert "requires 12" in error


def test_startup_does_not_consult_charts_even_when_external_media_is_enabled(config):
    config.allow_ytdlp = True
    starter_tracks = [_track("Carefree", source="starter")]

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.media.starter.load_starter_rotation_tracks", return_value=starter_tracks),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts") as chart_fetch,
    ):
        tracks, source, _error = fetch_startup_playlist(config)

    assert tracks == starter_tracks
    assert source.kind == "starter"
    chart_fetch.assert_not_called()


def test_persisted_legacy_jamendo_migrates_to_local_base(config, tmp_path):
    config.cache_dir = tmp_path
    legacy = PlaylistSource(
        kind="jamendo",
        source_id="pop",
        label="Jamendo CC Music",
        url="jamendo://playlist?tags=pop",
    )

    with patch(
        "mammamiradio.playlist.playlist._load_local_music_tracks",
        return_value=[_track("Casa", "Mina")],
    ):
        tracks, source, error = fetch_startup_playlist(config, legacy)

    assert [track.source for track in tracks] == ["local"]
    assert source.kind == "local"
    assert "retired" in error.lower()
    persisted = read_persisted_source(tmp_path)
    assert persisted is not None
    assert persisted.kind == "local"


def test_persisted_legacy_jamendo_migrates_to_starter_base(config, tmp_path):
    config.cache_dir = tmp_path
    legacy = PlaylistSource(
        kind="url",
        source_id="pop",
        label="Old Jamendo URL",
        url="jamendo://playlist?tags=pop",
    )
    starter_tracks = [_track("Wallpaper", source="starter")]

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.media.starter.load_starter_rotation_tracks", return_value=starter_tracks),
    ):
        tracks, source, error = fetch_startup_playlist(config, legacy)

    assert tracks == starter_tracks
    assert source.kind == "starter"
    assert "retired" in error.lower()
    persisted = read_persisted_source(tmp_path)
    assert persisted is not None
    assert persisted.kind == "starter"


def test_persisted_legacy_jamendo_migrates_when_starter_is_pending(config, tmp_path):
    config.cache_dir = tmp_path
    legacy = PlaylistSource(
        kind="jamendo",
        source_id="pop",
        label="Old Jamendo",
        url="jamendo://playlist?tags=pop",
    )

    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch(
            "mammamiradio.media.starter.load_starter_rotation_tracks",
            side_effect=StarterCatalogError("catalog approval pending"),
        ),
    ):
        tracks, source, error = fetch_startup_playlist(config, legacy)

    assert tracks == []
    assert source.kind == "starter"
    assert source.track_count == 0
    assert "retired" in error.lower()
    assert "not release-ready" in error
    persisted = read_persisted_source(tmp_path)
    assert persisted is not None
    assert persisted.kind == "starter"
    assert persisted.track_count == 0


# ---------------------------------------------------------------------------
# Explicit sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        PlaylistSource(kind="jamendo", source_id="pop", label="Old Jamendo"),
        PlaylistSource(kind="url", source_id="", label="Old Jamendo URL", url="jamendo://playlist?tags=pop"),
    ],
)
def test_explicit_legacy_jamendo_source_is_retired_without_network(config, source):
    with (
        patch("mammamiradio.playlist.playlist.urlopen") as network,
        pytest.raises(LegacyJamendoSourceRetiredError, match="transient Jamendo"),
    ):
        load_explicit_source(config, source)

    network.assert_not_called()


@pytest.mark.parametrize("kind", ["demo", "starter"])
def test_explicit_demo_alias_and_starter_use_canonical_starter_catalog(config, kind):
    starter_tracks = [_track("Carefree", source="starter")]

    with patch("mammamiradio.media.starter.load_starter_rotation_tracks", return_value=starter_tracks):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind=kind, source_id=kind, label=kind.title()),
        )

    assert tracks == starter_tracks
    assert source.kind == "starter"


def test_explicit_starter_rejects_pending_catalog(config):
    with (
        patch(
            "mammamiradio.media.starter.load_starter_rotation_tracks",
            side_effect=StarterCatalogError("human audition pending"),
        ),
        pytest.raises(ExplicitSourceError, match="not release-ready"),
    ):
        load_explicit_source(config, PlaylistSource(kind="starter", source_id="starter", label="Starter"))


def test_explicit_charts_require_external_media_capability(config):
    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=False),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts") as chart_fetch,
        pytest.raises(ExternalMediaUnavailableError, match="external-media extra"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Current Italian charts"),
        )

    chart_fetch.assert_not_called()


def test_explicit_charts_blend_local_tracks_and_dedupe(config):
    config.playlist.shuffle = False
    chart_tracks = [
        _track("Emozioni", "Lucio Battisti", source="youtube"),
        _track("Chart Three", "Artist Three", source="youtube"),
    ]
    local_tracks = [
        _track("Emozioni", "lucio battisti"),
        _track("Grande Grande Grande", "Mina"),
    ]

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=chart_tracks),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=local_tracks),
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Current Italian charts"),
        )

    assert source.kind == "charts"
    assert {(track.artist.casefold(), track.title.casefold()) for track in tracks} == {
        ("lucio battisti", "emozioni"),
        ("artist three", "chart three"),
        ("mina", "grande grande grande"),
    }
    assert [track.source for track in tracks if track.title == "Grande Grande Grande"] == ["local"]


def test_explicit_charts_raise_when_feed_is_empty(config):
    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[]),
        pytest.raises(ExplicitSourceError, match="temporarily unavailable"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Current Italian charts"),
        )


def test_explicit_local_source(config):
    with patch(
        "mammamiradio.playlist.playlist._load_local_music_tracks",
        return_value=[_track("Local One")],
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="local", source_id="local_music_dir", label="Local music/ files"),
        )

    assert [track.source for track in tracks] == ["local"]
    assert source.kind == "local"


def test_explicit_local_source_rejects_empty_directory(config):
    with (
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        pytest.raises(ExplicitSourceError, match="No MP3 files"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="local", source_id="local_music_dir", label="Local music/ files"),
        )


def test_unsupported_explicit_source_raises(config):
    with pytest.raises(ExplicitSourceError, match="Unsupported source kind"):
        load_explicit_source(config, PlaylistSource(kind="unsupported", source_id="", label="Bad"))


# ---------------------------------------------------------------------------
# Apple Music chart parsing
# ---------------------------------------------------------------------------


def test_fetch_current_italy_charts_parses_tracks_and_artwork():
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    response = _chart_response(
        [
            {
                "name": "Song One",
                "artistName": "Artist A",
                "id": "1",
                "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/100x100bb.jpg",
            },
            {"name": "Song Two", "artistName": "Artist B", "id": "2"},
            {"name": "", "artistName": "Artist C", "id": "3"},
        ]
    )

    with patch("mammamiradio.playlist.playlist.urlopen", return_value=response):
        tracks = _fetch_current_italy_charts()

    assert [(track.title, track.spotify_id) for track in tracks] == [
        ("Song One", "chart_1"),
        ("Song Two", "chart_2"),
    ]
    assert tracks[0].album_art.endswith("/600x600bb.jpg")
    assert tracks[1].album_art == ""


def test_fetch_current_italy_charts_caps_each_artist():
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    results = [{"name": f"Shiva Track {index}", "artistName": "Shiva", "id": str(index)} for index in range(1, 6)] + [
        {"name": "Other Song", "artistName": "Geolier", "id": "6"},
        {"name": "Another Song", "artistName": "Tiziano Ferro", "id": "7"},
    ]

    with patch("mammamiradio.playlist.playlist.urlopen", return_value=_chart_response(results)):
        tracks = _fetch_current_italy_charts()

    assert len([track for track in tracks if track.artist == "Shiva"]) == 2
    assert len(tracks) == 4


def test_fetch_current_italy_charts_respects_limit():
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    results = [{"name": f"Song {index}", "artistName": f"Artist {index}", "id": str(index)} for index in range(10)]
    with patch("mammamiradio.playlist.playlist.urlopen", return_value=_chart_response(results)):
        tracks = _fetch_current_italy_charts(limit=3)

    assert len(tracks) == 3


@pytest.mark.parametrize("failure", [URLError("network down"), TimeoutError(), OSError("socket")])
def test_fetch_current_italy_charts_returns_empty_on_network_failure(failure):
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    with patch("mammamiradio.playlist.playlist.urlopen", side_effect=failure):
        assert _fetch_current_italy_charts() == []


def test_fetch_current_italy_charts_returns_empty_on_invalid_json():
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    response = MagicMock()
    response.read.return_value = b"not json"
    response.__enter__ = lambda value: value
    response.__exit__ = MagicMock(return_value=False)
    with patch("mammamiradio.playlist.playlist.urlopen", return_value=response):
        assert _fetch_current_italy_charts() == []


def test_fetch_current_italy_charts_filters_obvious_non_music():
    from mammamiradio.playlist.playlist import _fetch_current_italy_charts

    results = [
        {"name": "Real Song", "artistName": "Real Artist", "id": "1"},
        {"name": "Do You Speak English? - Big Train", "artistName": "BBC Studios", "id": "2"},
        {"name": "Morning News Briefing", "artistName": "Some Outlet", "id": "3"},
        {"name": "Harry Potter Audiobook Ch. 1", "artistName": "Narrator X", "id": "4"},
    ]
    with patch("mammamiradio.playlist.playlist.urlopen", return_value=_chart_response(results)):
        tracks = _fetch_current_italy_charts()

    assert [track.title for track in tracks] == ["Real Song"]


def test_music_title_plausibility_is_conservative():
    from mammamiradio.playlist.playlist import _is_plausible_music_title

    assert _is_plausible_music_title("Volare", "Domenico Modugno")
    assert _is_plausible_music_title("News For You", "Some Band")
    assert not _is_plausible_music_title("", "Artist")
    assert not _is_plausible_music_title("Title", "")
    assert not _is_plausible_music_title("x" * 151, "Artist")
    assert not _is_plausible_music_title("Title", "x" * 101)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_read_persisted_source_returns_none_for_bad_payloads(tmp_path):
    path = tmp_path / "playlist_source.json"

    path.write_bytes(b"\x00\x00")
    assert read_persisted_source(tmp_path) is None

    path.write_text(json.dumps({"source_id": "missing-kind"}))
    assert read_persisted_source(tmp_path) is None

    path.write_text(json.dumps({"kind": "local", "track_count": "bad"}))
    assert read_persisted_source(tmp_path) is None


def test_persisted_source_round_trip(tmp_path):
    source = PlaylistSource(
        kind="local",
        source_id="local_music_dir",
        url="",
        label="Local music/ files",
        track_count=42,
        selected_at=1_234_567_890.0,
    )
    write_persisted_source(tmp_path, source)

    assert read_persisted_source(tmp_path) == source


def test_persisted_chart_source_id_migrates_to_top_100(tmp_path):
    (tmp_path / "playlist_source.json").write_text(
        json.dumps(
            {
                "kind": "charts",
                "source_id": "apple_music_it_top_50",
                "label": "Current Italian charts",
                "track_count": 50,
                "selected_at": 1.0,
            }
        )
    )

    source = read_persisted_source(tmp_path)
    assert source is not None
    assert source.source_id == "apple_music_it_top_100"


# ---------------------------------------------------------------------------
# Local files and chart refresh
# ---------------------------------------------------------------------------


def test_load_local_music_tracks_parses_names_and_paths(tmp_path):
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    named = tmp_path / "Lucio Battisti - Emozioni.mp3"
    unnamed = tmp_path / "NoHyphen.mp3"
    named.write_bytes(b"")
    unnamed.write_bytes(b"")

    tracks = {track.title: track for track in _load_local_music_tracks(tmp_path)}

    assert tracks["Emozioni"].artist == "Lucio Battisti"
    assert tracks["Emozioni"].local_path == named
    assert tracks["Emozioni"].source == "local"
    assert tracks["NoHyphen"].artist == "Unknown"
    assert tracks["NoHyphen"].local_path == unnamed


def test_load_operator_local_tracks_tags_shuffles_and_drops_blocklisted(tmp_path):
    from mammamiradio.playlist.playlist import load_operator_local_tracks

    (tmp_path / "Banned Artist - Banned Song.mp3").write_bytes(b"id3")
    (tmp_path / "Kevin MacLeod - Carefree.mp3").write_bytes(b"id3")
    config = load_config()
    config.music_dir = tmp_path
    config.playlist.shuffle = False

    tracks = load_operator_local_tracks(
        config,
        blocklist={("banned artist", "banned song"): {"display": "Banned Artist - Banned Song"}},
    )

    assert [track.title for track in tracks] == ["Carefree"]
    assert tracks[0].source == "local"
    assert tracks[0].artist == "Kevin MacLeod"


def test_load_local_music_tracks_missing_directory_is_empty(tmp_path):
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    assert _load_local_music_tracks(tmp_path / "missing") == []


def test_load_local_music_tracks_caps_at_200(tmp_path):
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    for index in range(205):
        (tmp_path / f"Artist {index:03d} - Song {index:03d}.mp3").write_bytes(b"")

    assert len(_load_local_music_tracks(tmp_path)) == 200


def test_chart_refresh_filters_existing_ids():
    tracks = [_track("A"), _track("B"), _track("C")]
    tracks[0].spotify_id = "id_a"
    tracks[1].spotify_id = "id_b"
    tracks[2].spotify_id = "id_c"

    with patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=tracks):
        result = fetch_chart_refresh({"id_a", "id_c"})

    assert [track.spotify_id for track in result] == ["id_b"]


def test_chart_refresh_propagates_empty_fetch_as_empty():
    with patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[]):
        assert fetch_chart_refresh(set()) == []


def test_local_loader_ignores_non_mp3_files(tmp_path):
    from mammamiradio.playlist.playlist import _load_local_music_tracks

    (tmp_path / "Artist - Song.wav").write_bytes(b"")
    (tmp_path / "README.md").write_text("not music")

    assert _load_local_music_tracks(Path(tmp_path)) == []


# ---------------------------------------------------------------------------
# Local-loader bounds and source-readiness evidence
# ---------------------------------------------------------------------------


def test_load_local_music_tracks_unreadable_dir_returns_empty(tmp_path):
    with patch("mammamiradio.playlist.playlist.os.scandir", side_effect=PermissionError("denied")):
        result = _load_local_music_tracks(tmp_path)

    assert result == []


def test_local_directory_enumeration_bounds_raw_entries_before_mp3_filtering(tmp_path):
    yielded = 0

    class CountingScandir:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            nonlocal yielded
            for index in range(10_000):
                yielded += 1
                yield SimpleNamespace(
                    name=f"not-music-{index}.txt",
                    path=str(tmp_path / f"not-music-{index}.txt"),
                    is_file=lambda: True,
                )

    with (
        patch("mammamiradio.playlist.playlist.os.scandir", return_value=CountingScandir()),
        patch("mammamiradio.playlist.playlist._MAX_LOCAL_DIRECTORY_ENTRIES", 8),
    ):
        tracks = _load_local_music_tracks(tmp_path)

    assert tracks == []
    assert yielded == 9


def test_music_dir_override_drives_chart_local_blend(config, tmp_path):
    music_dir = tmp_path / "operator-music"
    music_dir.mkdir()
    (music_dir / "Local Artist - Local Song.mp3").touch()
    config.music_dir = music_dir
    config.allow_ytdlp = True
    chart = _track("Chart Song", "Chart Artist")

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[chart]),
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Current Italian charts"),
        )

    assert {track.source for track in tracks} == {"youtube", "local"}
    evidence = source.readiness_evidence
    assert evidence is not None
    assert evidence.entries["local"].candidates == 1
    assert evidence.entries["charts"].candidates == 1


def test_source_evidence_records_chart_local_blend_and_recovery(config, tmp_path):
    config.allow_ytdlp = True
    config.music_dir = tmp_path / "music"
    config.music_dir.mkdir()
    (config.music_dir / "Local Artist - Local Song.mp3").touch()
    chart = _track("Chart Song", "Chart Artist")

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[chart]),
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Current Italian charts"),
        )

    assert len(tracks) == 2
    evidence = source.readiness_evidence
    assert evidence is not None
    assert evidence.entries["charts"].candidates == 1
    assert evidence.entries["local"].candidates == 1
    assert evidence.entries["recovery"].bundled is True
    state = StationState(playlist=tracks, playlist_source=source)
    assert state.source_readiness.entries["charts"].candidates == 1
    assert state.source_readiness.entries["local"].candidates == 1


def test_source_evidence_keeps_failed_chart_restore_separate_from_starter(config):
    starter_tracks = [_track("Carefree", source="starter"), _track("Cipher", source="starter")]
    persisted = PlaylistSource(kind="charts", source_id="apple_music_it_top_100", label="Current Italian charts")
    config.allow_ytdlp = True

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._fetch_current_italy_charts", return_value=[]),
        patch("mammamiradio.playlist.playlist._load_local_music_tracks", return_value=[]),
        patch("mammamiradio.media.starter.load_starter_rotation_tracks", return_value=starter_tracks),
    ):
        _tracks, source, error = fetch_startup_playlist(config, persisted)

    evidence = source.readiness_evidence
    assert evidence is not None
    assert evidence.entries["charts"].attempted is True
    assert evidence.entries["charts"].failure == "The saved source could not be restored"
    assert evidence.entries["demo"].failure == ""
    assert evidence.entries["demo"].bundled is True
    assert evidence.entries["demo"].candidates == len(starter_tracks)
    assert error
