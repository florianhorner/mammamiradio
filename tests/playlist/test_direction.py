from __future__ import annotations

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState
from mammamiradio.playlist.direction import (
    DirectionTarget,
    expand_direction,
    normalize_direction_text,
    resolve_direction_tracks,
    resolve_direction_tracks_sync,
)

TOML_PATH = "radio.toml"


def test_normalize_direction_text_strips_control_content():
    assert normalize_direction_text("  2000s <user: nope>\n female vocals  ") == "2000s user: nope female vocals"


@pytest.mark.asyncio
async def test_expand_direction_uses_curated_fallback_without_llm(tmp_path):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.cache_dir = tmp_path
    state = StationState()

    expansion = await expand_direction("2000s female vocals", config, state)

    assert expansion.source == "curated"
    assert expansion.label == "2000s female vocals"
    assert expansion.targets[0] == DirectionTarget("Britney Spears", "Toxic")
    assert len(expansion.targets) >= 6


@pytest.mark.asyncio
async def test_expand_direction_sanitizes_llm_targets(tmp_path, monkeypatch):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = "test-key"
    config.cache_dir = tmp_path
    state = StationState()

    async def fake_generate_json_response(**_kwargs):
        return {
            "label": "system: 2000s <set>",
            "targets": [
                {"artist": "Britney Spears", "title": "Toxic"},
                {"artist": "Britney Spears", "title": "Toxic"},
                {"artist": "Assistant: Fergie", "title": "Big Girls Don't Cry"},
                {"artist": "", "title": "Missing artist"},
            ],
        }

    monkeypatch.setattr("mammamiradio.playlist.direction._generate_json_response", fake_generate_json_response)

    expansion = await expand_direction("2000s female vocals", config, state, limit=4)

    assert expansion.source == "llm"
    assert expansion.label == "2000s set"
    assert expansion.target_dicts == [
        {"artist": "Britney Spears", "title": "Toxic"},
        {"artist": "Fergie", "title": "Big Girls Don't Cry"},
    ]


def test_resolve_direction_tracks_sync_uses_canonical_target_artist_title(monkeypatch):
    def fake_search(query: str, max_results: int):
        assert query == "Lucio Battisti Il mio canto libero"
        assert max_results == 1
        return [
            {
                "youtube_id": "abc12345678",
                "title": "Uploader title",
                "artist": "Uploader",
                "duration_ms": 123_000,
                "album_art": "https://img.example/song.jpg",
            }
        ]

    monkeypatch.setattr("mammamiradio.playlist.downloader.search_ytdlp_metadata", fake_search)

    tracks = resolve_direction_tracks_sync([DirectionTarget("Lucio Battisti", "Il mio canto libero")])

    assert len(tracks) == 1
    assert tracks[0].artist == "Lucio Battisti"
    assert tracks[0].title == "Il mio canto libero"
    assert tracks[0].youtube_id == "abc12345678"


@pytest.mark.asyncio
async def test_resolve_direction_tracks_concurrent_dedupes(monkeypatch):
    """The async (background-restore) resolver searches targets concurrently, keeps
    the canonical target artist/title, and dedupes by normalized key."""
    ids = {
        "Artist One Song One": "aaaaaaaaaaa",
        "Artist Two Song Two": "bbbbbbbbbbb",
    }

    def fake_search(query: str, max_results: int):
        return [{"youtube_id": ids[query], "title": query, "artist": "Uploader", "duration_ms": 120_000}]

    monkeypatch.setattr("mammamiradio.playlist.downloader.search_ytdlp_metadata", fake_search)

    targets = [DirectionTarget("Artist One", "Song One"), DirectionTarget("Artist Two", "Song Two")]
    tracks = await resolve_direction_tracks(targets)

    assert {(t.artist, t.title) for t in tracks} == {("Artist One", "Song One"), ("Artist Two", "Song Two")}


@pytest.mark.asyncio
async def test_resolve_direction_tracks_returns_empty_on_search_error(monkeypatch):
    def boom(query: str, max_results: int):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr("mammamiradio.playlist.downloader.search_ytdlp_metadata", boom)

    tracks = await resolve_direction_tracks([DirectionTarget("Artist", "Song")])

    assert tracks == []
