"""Persistence tests for the heading overlay."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Heading, PlaylistSource, Track
from mammamiradio.playlist.playlist import read_persisted_heading, write_persisted_heading

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def test_persisted_heading_round_trip(tmp_path):
    heading = Heading(
        id="h-80s",
        seed="classic://italian/80s",
        label="Anni '80",
        set_at=123.4,
        set_by="operator",
    )

    write_persisted_heading(tmp_path, heading)

    restored = read_persisted_heading(tmp_path)
    assert restored == heading


def test_persisted_heading_round_trip_announced(tmp_path):
    heading = Heading(
        id="h-80s",
        seed="classic://italian/80s",
        label="Anni '80",
        set_at=123.4,
        set_by="operator",
        announced=True,
    )

    write_persisted_heading(tmp_path, heading)

    restored = read_persisted_heading(tmp_path)
    assert restored is not None
    assert restored == heading
    assert restored.announced is True


def test_persisted_heading_missing_returns_none(tmp_path):
    assert read_persisted_heading(tmp_path) is None


def test_persisted_heading_corrupt_returns_none(tmp_path):
    (tmp_path / "heading.json").write_text("{not json")

    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_startup_drops_persisted_heading_when_restore_adds_no_new_tracks(tmp_path):
    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path / "tmp"
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = False
    config.allow_ytdlp = False
    heading = Heading(
        id="h-80s",
        seed="classic://italian/80s",
        label="Anni '80",
        set_at=123.4,
        set_by="operator",
    )
    write_persisted_heading(tmp_path, heading)
    base_track = Track(title="Estate", artist="Bruno", duration_ms=180_000, spotify_id="base", youtube_id="yt-old")
    fetched_duplicate = Track(
        title="Estate",
        artist="Bruno",
        duration_ms=180_000,
        spotify_id="heading",
        youtube_id="yt-new",
    )
    source = PlaylistSource(kind="classic", source_id="80s", url=heading.seed, label="Classici")

    with (
        patch("mammamiradio.main.load_config", return_value=config),
        patch("mammamiradio.main.read_persisted_source", return_value=None),
        patch("mammamiradio.main.fetch_startup_playlist", return_value=([base_track], source, "")),
        patch("mammamiradio.main.load_explicit_source", return_value=([fetched_duplicate], source)),
        patch("mammamiradio.main.load_blocklist", return_value={}),
        patch("mammamiradio.main.init_db", return_value=None),
        patch("mammamiradio.main.prune_stale_tmp_files", return_value=0),
        patch("mammamiradio.main.purge_suspect_cache_files", return_value=0),
        patch("mammamiradio.main.evict_cache_lru", return_value=None),
        patch("mammamiradio.main.prewarm_first_segment", new=AsyncMock(return_value=False)),
        patch("mammamiradio.main.run_producer", new=AsyncMock(return_value=None)),
        patch("mammamiradio.main.run_playback_loop", new=AsyncMock(return_value=None)),
    ):
        import mammamiradio.main as main_module

        await main_module.startup()
        await main_module.app.state.prewarm_task
        await main_module.app.state.producer_task
        await main_module.app.state.playback_task

    state = main_module.app.state.station_state
    assert state.heading is None
    assert state.playlist == [base_track]
    assert state.playlist[0].heading_id == ""
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_startup_restores_announced_heading_without_rearming(tmp_path):
    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path / "tmp"
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = False
    config.allow_ytdlp = False
    heading = Heading(
        id="h-80s",
        seed="classic://italian/80s",
        label="Anni '80",
        set_at=123.4,
        set_by="operator",
        announced=True,
    )
    write_persisted_heading(tmp_path, heading)
    base_track = Track(title="Base", artist="Radio", duration_ms=180_000, spotify_id="base")
    heading_track = Track(title="Estate", artist="Bruno", duration_ms=180_000, spotify_id="heading")
    source = PlaylistSource(kind="classic", source_id="80s", url=heading.seed, label="Classici")

    with (
        patch("mammamiradio.main.load_config", return_value=config),
        patch("mammamiradio.main.read_persisted_source", return_value=None),
        patch("mammamiradio.main.fetch_startup_playlist", return_value=([base_track], source, "")),
        patch("mammamiradio.main.load_explicit_source", return_value=([heading_track], source)),
        patch("mammamiradio.main.load_blocklist", return_value={}),
        patch("mammamiradio.main.init_db", return_value=None),
        patch("mammamiradio.main.prune_stale_tmp_files", return_value=0),
        patch("mammamiradio.main.purge_suspect_cache_files", return_value=0),
        patch("mammamiradio.main.evict_cache_lru", return_value=None),
        patch("mammamiradio.main.prewarm_first_segment", new=AsyncMock(return_value=False)),
        patch("mammamiradio.main.run_producer", new=AsyncMock(return_value=None)),
        patch("mammamiradio.main.run_playback_loop", new=AsyncMock(return_value=None)),
    ):
        import mammamiradio.main as main_module

        await main_module.startup()
        await main_module.app.state.prewarm_task
        await main_module.app.state.producer_task
        await main_module.app.state.playback_task

    state = main_module.app.state.station_state
    restored_heading_track = next(track for track in state.playlist if track.title == "Estate")
    state._arm_heading_announcement_if_needed(restored_heading_track)
    assert state.heading == heading
    assert state.heading_announced_id == heading.id
    assert state.heading_pending_announcement == ""
