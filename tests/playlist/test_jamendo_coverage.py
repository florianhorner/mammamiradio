"""Containment coverage for retired durable Jamendo and classic sources.

The transient provider has its protocol, streaming, race, and FFmpeg coverage in
``test_jamendo_transient.py``.  This module guards the inverse boundary: no old
playlist/direct-download/cache path may regain authority over Jamendo bytes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core import config as config_module
from mammamiradio.core.config import JAMENDO_ACK_REVISION, load_config
from mammamiradio.core.models import PlaylistSource, SourceReadinessEvidence, Track


@pytest.fixture()
def config():
    return load_config()


def _legacy_jamendo_track(tmp_path=None) -> Track:
    local_path = None
    if tmp_path is not None:
        local_path = tmp_path / "legacy-jamendo.mp3"
        local_path.write_bytes(b"legacy bytes")
    return Track(
        title="Retired Song",
        artist="Legacy Artist",
        duration_ms=180_000,
        spotify_id="jamendo_legacy_42",
        source="jamendo",
        direct_url="https://storage.jamendo.com/tracks/42.mp3",
        local_path=local_path,
    )


# ---------------------------------------------------------------------------
# Durable Jamendo authority is removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        PlaylistSource(kind="jamendo", source_id="pop", label="Old Jamendo"),
        PlaylistSource(kind="url", source_id="", label="Old Jamendo URL", url="jamendo://playlist?tags=pop"),
    ],
)
def test_legacy_jamendo_explicit_sources_are_retired(config, source):
    from mammamiradio.playlist.playlist import LegacyJamendoSourceRetiredError, load_explicit_source

    with pytest.raises(LegacyJamendoSourceRetiredError, match="transient Jamendo"):
        load_explicit_source(config, source)


@pytest.mark.parametrize(
    "module_name,symbol",
    [
        ("mammamiradio.playlist.playlist", "_fetch_jamendo_playlist"),
        ("mammamiradio.playlist.playlist", "_build_jamendo_url"),
        ("mammamiradio.playlist.playlist", "_jamendo_tags"),
        ("mammamiradio.playlist.playlist", "_jamendo_source"),
        ("mammamiradio.playlist.downloader", "_download_direct_url"),
        ("mammamiradio.playlist.downloader", "_validate_direct_url"),
        ("mammamiradio.playlist.downloader", "_BlockRedirectHandler"),
    ],
)
def test_legacy_jamendo_playlist_and_direct_download_symbols_are_absent(module_name, symbol):
    import importlib

    module = importlib.import_module(module_name)
    assert not hasattr(module, symbol)


def test_legacy_jamendo_cache_and_local_path_are_ineligible(tmp_path):
    from mammamiradio.playlist.downloader import _resolve_cached_or_local

    cache_dir = tmp_path / "cache"
    music_dir = tmp_path / "music"
    cache_dir.mkdir()
    music_dir.mkdir()
    track = _legacy_jamendo_track(tmp_path)
    (cache_dir / f"{track.cache_key}.mp3").write_bytes(b"cached legacy bytes")
    (music_dir / f"{track.artist} - {track.title}.mp3").write_bytes(b"operator collision")

    assert _resolve_cached_or_local(track, cache_dir, music_dir) is None


@pytest.mark.parametrize("external_media_enabled", [False, True])
def test_download_sync_rejects_jamendo_before_cache_or_extractor(tmp_path, external_media_enabled):
    from mammamiradio.playlist.downloader import _download_sync

    cache_dir = tmp_path / "cache"
    music_dir = tmp_path / "music"
    cache_dir.mkdir()
    music_dir.mkdir()
    track = _legacy_jamendo_track(tmp_path)
    cached = cache_dir / f"{track.cache_key}.mp3"
    cached.write_bytes(b"cached legacy bytes")

    with (
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=external_media_enabled),
        patch("mammamiradio.playlist.downloader._download_ytdlp") as extractor,
        pytest.raises(RuntimeError, match="persistent Jamendo track acquisition is retired"),
    ):
        _download_sync(track, cache_dir, music_dir)

    extractor.assert_not_called()
    assert cached.read_bytes() == b"cached legacy bytes"
    assert list(cache_dir.glob("_failed_*.mp3")) == []


@pytest.mark.asyncio
async def test_async_download_boundary_rejects_legacy_jamendo(tmp_path):
    from mammamiradio.playlist.downloader import download_track

    cache_dir = tmp_path / "cache"
    music_dir = tmp_path / "music"
    cache_dir.mkdir()
    music_dir.mkdir()

    with pytest.raises(RuntimeError, match="persistent Jamendo track acquisition is retired"):
        await download_track(_legacy_jamendo_track(), cache_dir, music_dir)


# ---------------------------------------------------------------------------
# Classic sources remain an optional external-media feature
# ---------------------------------------------------------------------------


def test_parse_classic_artist_title():
    from mammamiradio.playlist.playlist import _parse_classic_artist_title

    assert _parse_classic_artist_title("Vasco Rossi - Vita spericolata") == (
        "Vasco Rossi",
        "Vita spericolata",
    )
    assert _parse_classic_artist_title("Lucio Battisti – Emozioni") == ("Lucio Battisti", "Emozioni")
    assert _parse_classic_artist_title("Senza separatore") is None


def test_copy_tracks_with_source_supports_classic():
    from mammamiradio.playlist.playlist import _copy_tracks_with_source

    tracks = _copy_tracks_with_source(
        [Track(title="Azzurro", artist="Adriano Celentano", duration_ms=210_000)],
        "classic",
    )
    assert tracks[0].source == "classic"


def test_classic_metadata_search_stamps_era_year_and_parses_title():
    from mammamiradio.playlist.playlist import _load_classic_italian_tracks

    with (
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True),
        patch(
            "mammamiradio.playlist.downloader.search_ytdlp_metadata",
            return_value=[
                {
                    "youtube_id": "abc123class",
                    "title": "Vasco Rossi - Vita spericolata",
                    "artist": "VascoRossiVEVO",
                    "duration_ms": 240_000,
                    "album_art": "https://img.example/vasco.jpg",
                }
            ],
        ),
    ):
        tracks = _load_classic_italian_tracks("80s")

    assert len(tracks) == 1
    assert tracks[0].artist == "Vasco Rossi"
    assert tracks[0].title == "Vita spericolata"
    assert tracks[0].year == 1985
    assert tracks[0].source == "classic"
    assert tracks[0].youtube_id == "abc123class"


def test_classic_loader_is_empty_when_external_media_is_disabled():
    from mammamiradio.playlist.playlist import _load_classic_italian_tracks

    with (
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=False),
        patch("mammamiradio.playlist.downloader.search_ytdlp_metadata") as search,
    ):
        assert _load_classic_italian_tracks("80s") == []

    search.assert_not_called()


def test_explicit_classic_url_resolves_when_capability_is_enabled(config):
    """Normal: a resolved classic source retains explicit attempted evidence."""
    from mammamiradio.playlist.playlist import load_explicit_source

    config.playlist.shuffle = False
    readiness = SourceReadinessEvidence()
    classic_track = Track(
        title="Vita spericolata",
        artist="Vasco Rossi",
        duration_ms=240_000,
        youtube_id="abc123class",
        year=1985,
        source="classic",
    )

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._load_classic_italian_tracks", return_value=[classic_track]),
    ):
        tracks, source = load_explicit_source(
            config,
            PlaylistSource(kind="url", source_id="", label="Anni 80", url="classic://italian/80s"),
            readiness=readiness,
        )

    assert tracks == [classic_track]
    assert source.kind == "classic"
    assert source.source_id == "80s"
    assert source.url == "classic://italian/80s"
    assert source.readiness_evidence is readiness
    assert readiness.advanced is not None
    assert readiness.advanced.kind == "classic"
    assert readiness.advanced.attempted is True


def test_explicit_classic_is_actionably_blocked_without_capability(config):
    """Empty fallback: a blocked classic load retains explicit attempted evidence."""
    from mammamiradio.playlist.playlist import ExternalMediaUnavailableError, load_explicit_source

    readiness = SourceReadinessEvidence()
    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=False),
        patch("mammamiradio.playlist.playlist._load_classic_italian_tracks") as loader,
        pytest.raises(ExternalMediaUnavailableError, match="external-media extra"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="classic", source_id="80s", label="Anni 80", url="classic://italian/80s"),
            readiness=readiness,
        )

    loader.assert_not_called()
    assert readiness.advanced is not None
    assert readiness.advanced.kind == "classic"
    assert readiness.advanced.attempted is True
    assert readiness.advanced.failure == "External media support is not installed"


def test_explicit_classic_empty_candidates_retain_failure_evidence(config):
    """Empty fallback: a failed classic load retains explicit attempted evidence."""
    from mammamiradio.playlist.playlist import ExplicitSourceError, load_explicit_source

    readiness = SourceReadinessEvidence()
    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._load_classic_italian_tracks", return_value=[]),
        pytest.raises(ExplicitSourceError, match="temporarily unavailable"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="classic", source_id="80s", label="Anni 80", url="classic://italian/80s"),
            readiness=readiness,
        )

    assert readiness.advanced is not None
    assert readiness.advanced.kind == "classic"
    assert readiness.advanced.attempted is True
    assert readiness.advanced.failure == "Classic Italian returned no playable candidates"


def test_explicit_classic_rejects_unsupported_era(config):
    from mammamiradio.playlist.playlist import ExplicitSourceError, load_explicit_source

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        pytest.raises(ExplicitSourceError, match="Unsupported classic Italian era"),
    ):
        load_explicit_source(
            config,
            PlaylistSource(kind="classic", source_id="60s", label="Anni 60", url="classic://italian/60s"),
        )


def test_startup_restores_persisted_classic_source(config):
    from mammamiradio.playlist.playlist import fetch_startup_playlist

    config.playlist.shuffle = False
    classic_track = Track(
        title="Vita spericolata",
        artist="Vasco Rossi",
        duration_ms=240_000,
        youtube_id="abc123class",
        year=1985,
        source="classic",
    )

    with (
        patch("mammamiradio.playlist.downloader.external_media_enabled", return_value=True),
        patch("mammamiradio.playlist.playlist._load_classic_italian_tracks", return_value=[classic_track]),
    ):
        tracks, source, error = fetch_startup_playlist(
            config,
            PlaylistSource(kind="classic", source_id="80s", label="Anni 80", url="classic://italian/80s"),
        )

    assert tracks == [classic_track]
    assert source.kind == "classic"
    assert error == ""


# ---------------------------------------------------------------------------
# Transient Jamendo configuration remains validated and default-off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("country", ["Italian", "DE", "ita", "IT4"])
def test_validate_config_rejects_bad_jamendo_country(config, country):
    from mammamiradio.core.config import _validate

    config.playlist.jamendo_country = country
    with pytest.raises(ValueError, match="jamendo_country"):
        _validate(config)


@pytest.mark.parametrize("country", ["ITA", "DEU", "FRA", ""])
def test_validate_config_accepts_supported_jamendo_country_shape(config, country):
    from mammamiradio.core.config import _validate

    config.playlist.jamendo_country = country
    _validate(config)


def test_validate_config_rejects_unknown_jamendo_order(config):
    from mammamiradio.core.config import _validate

    config.playlist.jamendo_order = "random"
    with pytest.raises(ValueError, match="jamendo_order"):
        _validate(config)


@pytest.mark.parametrize(
    "order",
    ["popularity_total", "popularity_month", "popularity_week", "releasedate_desc", ""],
)
def test_validate_config_accepts_supported_jamendo_orders(config, order):
    from mammamiradio.core.config import _validate

    config.playlist.jamendo_order = order
    _validate(config)


@pytest.mark.parametrize("limit", [0, 201, "200", True])
def test_validate_config_rejects_bad_jamendo_limit(config, limit):
    from mammamiradio.core.config import _validate

    config.playlist.jamendo_limit = limit
    with pytest.raises(ValueError, match="jamendo_limit"):
        _validate(config)


@pytest.mark.parametrize("limit", [1, 50, 200])
def test_validate_config_accepts_bounded_jamendo_limit(config, limit):
    from mammamiradio.core.config import _validate

    config.playlist.jamendo_limit = limit
    _validate(config)


def test_load_config_jamendo_env_overrides_are_current_and_explicit(monkeypatch):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "valid-client-id")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ENABLED", "true")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED", "true")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ACK_REVISION", JAMENDO_ACK_REVISION)
    monkeypatch.setenv("JAMENDO_COUNTRY", "DEU")
    monkeypatch.setenv("JAMENDO_ORDER", "popularity_month")
    monkeypatch.setenv("JAMENDO_LIMIT", "123")

    config = load_config()

    assert config.playlist.jamendo_client_id == "valid-client-id"
    assert config.playlist.jamendo_enabled is True
    assert config.playlist.jamendo_noncommercial_acknowledged is True
    assert config.playlist.jamendo_ack_revision == JAMENDO_ACK_REVISION
    assert config.playlist.jamendo_country == "DEU"
    assert config.playlist.jamendo_order == "popularity_month"
    assert config.playlist.jamendo_limit == 123


@pytest.mark.parametrize(
    "env",
    [
        {
            "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED": "false",
            "MAMMAMIRADIO_JAMENDO_ACK_REVISION": JAMENDO_ACK_REVISION,
        },
        {
            "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED": "true",
            "MAMMAMIRADIO_JAMENDO_ACK_REVISION": "old-terms",
        },
    ],
)
def test_load_config_incomplete_acknowledgement_keeps_jamendo_disabled(monkeypatch, env):
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "valid-client-id")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ENABLED", "true")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    config = load_config()

    assert config.playlist.jamendo_enabled is False
    assert config.playlist.jamendo_noncommercial_acknowledged is False


def test_load_config_with_no_access_at_all_keeps_jamendo_disabled(monkeypatch):
    """Without operator or bundled access, enablement cannot stick."""
    monkeypatch.setattr(config_module, "BUNDLED_JAMENDO_CLIENT_ID", "")
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ENABLED", "true")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED", "true")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ACK_REVISION", JAMENDO_ACK_REVISION)

    assert load_config().playlist.jamendo_enabled is False


def test_load_config_without_an_operator_id_uses_the_bundled_station_access(monkeypatch):
    """Bundled access needs only the operator acknowledgement."""
    monkeypatch.setattr(config_module, "BUNDLED_JAMENDO_CLIENT_ID", "station-bundled-id")
    monkeypatch.setenv("JAMENDO_CLIENT_ID", "")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ENABLED", "true")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED", "true")
    monkeypatch.setenv("MAMMAMIRADIO_JAMENDO_ACK_REVISION", JAMENDO_ACK_REVISION)

    playlist = load_config().playlist
    assert playlist.jamendo_enabled is True
    assert playlist.jamendo_client_id == "station-bundled-id"
    assert playlist.jamendo_client_id_source == "bundled"


def test_load_config_invalid_jamendo_limit_env_var_raises(monkeypatch):
    monkeypatch.setenv("JAMENDO_LIMIT", "abc")
    with pytest.raises(ValueError, match="jamendo_limit"):
        load_config()


# ---------------------------------------------------------------------------
# Useful non-Jamendo downloader guards retained from the former coverage file
# ---------------------------------------------------------------------------


def test_download_external_sync_calls_extractor_when_enabled(tmp_path):
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    music_dir = tmp_path / "music"
    cache_dir.mkdir()
    music_dir.mkdir()
    track = Track(
        title="External Song",
        artist="Artist",
        duration_ms=180_000,
        spotify_id="external_1",
        source="youtube",
    )
    expected = cache_dir / f"{track.cache_key}.mp3"

    with (
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=True),
        patch("mammamiradio.playlist.downloader._download_ytdlp", return_value=expected) as extractor,
    ):
        assert _download_external_sync(track, cache_dir, music_dir) == expected

    extractor.assert_called_once_with(track, cache_dir)


def test_download_external_sync_rejects_when_extractor_is_disabled(tmp_path):
    from mammamiradio.playlist.downloader import _download_external_sync

    cache_dir = tmp_path / "cache"
    music_dir = tmp_path / "music"
    cache_dir.mkdir()
    music_dir.mkdir()
    track = Track(title="External Song", artist="Artist", duration_ms=180_000, source="youtube")

    with (
        patch("mammamiradio.playlist.downloader._ytdlp_enabled", return_value=False),
        pytest.raises(RuntimeError, match="yt-dlp is disabled"),
    ):
        _download_external_sync(track, cache_dir, music_dir)


def test_evict_cache_lru_preserves_protected_paths(tmp_path):
    from mammamiradio.playlist.downloader import evict_cache_lru

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    queued = cache_dir / "queued_track.mp3"
    evictable = cache_dir / "old_track.mp3"
    queued.write_bytes(b"x" * (700 * 1024))
    evictable.write_bytes(b"x" * (1200 * 1024))

    evict_cache_lru(cache_dir, max_size_mb=1, protected_paths={queued})

    assert queued.exists()
    assert not evictable.exists()


def test_validate_download_rejects_short_duration(tmp_path):
    from mammamiradio.playlist.downloader import validate_download

    path = tmp_path / "short.mp3"
    path.write_bytes(b"x" * (600 * 1024))
    result = MagicMock(returncode=0, stdout='{"format": {"duration": "15.3"}}')

    with patch("mammamiradio.playlist.downloader.subprocess.run", return_value=result):
        ok, reason = validate_download(path)

    assert ok is False
    assert "duration too short" in reason
