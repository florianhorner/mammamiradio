"""Admin heading endpoint tests for Seed Station Phase 2A."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Heading, PlaylistSource, StationState, Track
from mammamiradio.playlist.direction import DirectionExpansion, DirectionTarget
from mammamiradio.playlist.playlist import read_persisted_heading, write_persisted_heading
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _track(title: str, artist: str = "Artist", spotify_id: str = "", youtube_id: str = "") -> Track:
    return Track(
        title=title,
        artist=artist,
        duration_ms=180_000,
        spotify_id=spotify_id or title,
        youtube_id=youtube_id,
    )


def _source(seed: str = "classic://italian/80s") -> PlaylistSource:
    return PlaylistSource(kind="classic", source_id=seed.rsplit("/", 1)[-1], url=seed, label="Classici")


def _make_app(tmp_path, tracks: list[Track] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path(tmp_path)
    state = StationState(playlist=list(tracks if tracks is not None else [_track("Base", "Base Artist", "base")]))
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    )


@pytest.mark.asyncio
async def test_heading_set_adds_tags_and_persists(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Blue Jeans", "Franco", "h1"), _track("Notte", "Gianna", "h2")]
    start_revision = app.state.station_state.playlist_revision

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            status = await client.get("/status")

    body = resp.json()
    state = app.state.station_state
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["added"] == 2
    assert state.playlist_revision == start_revision + 1
    assert state.heading is not None
    assert state.playlist[0].title == "Base"
    assert {track.heading_id for track in state.playlist if track.title in {"Blue Jeans", "Notte"}} == {
        state.heading.id
    }
    assert read_persisted_heading(tmp_path) == state.heading
    assert status.json()["heading"]["active"] is True
    assert status.json()["heading"]["label"] == "Anni '80"
    assert status.json()["heading"]["phase"] == "steering"


@pytest.mark.asyncio
async def test_heading_empty_fallback_arms_nothing(tmp_path):
    app = _make_app(tmp_path)
    start_revision = app.state.station_state.playlist_revision

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=([], _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    body = resp.json()
    assert body["ok"] is False
    assert "give it a moment" in body["message"]
    assert app.state.station_state.heading is None
    assert app.state.station_state.heading_pending_announcement == ""
    assert app.state.station_state.playlist_revision == start_revision
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_heading_repeat_same_seed_is_idempotent(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Estate", "Bruno", "h1")]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())) as load:
        async with _client(app) as client:
            first = await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            revision = app.state.station_state.playlist_revision
            second = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    assert first.json()["ok"] is True
    assert second.json()["ok"] is True
    assert second.json()["idempotent"] is True
    assert load.call_count == 1
    assert app.state.station_state.playlist_revision == revision
    assert len([track for track in app.state.station_state.playlist if track.heading_id]) == 1


@pytest.mark.asyncio
async def test_heading_clear_keeps_blended_tracks_and_deletes_persistence(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Splendido", "Donatella", "h1")]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())):
        async with _client(app) as client:
            await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            resp = await client.post("/api/heading/clear")

    assert resp.json()["ok"] is True
    assert app.state.station_state.heading is None
    assert app.state.station_state.heading_pending_announcement == ""
    assert [track.title for track in app.state.station_state.playlist if track.spotify_id == "h1"] == ["Splendido"]
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_heading_same_era_after_clear_retags_existing_tracks(tmp_path):
    app = _make_app(tmp_path)
    heading_tracks = [_track("Splendido", "Donatella", "h1")]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(heading_tracks, _source())):
        async with _client(app) as client:
            first = await client.post("/api/heading", json={"seed": "classic://italian/80s"})
            old_heading_id = app.state.station_state.heading.id
            await client.post("/api/heading/clear")
            second = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    state = app.state.station_state
    assert first.json()["ok"] is True
    assert second.json()["ok"] is True
    assert second.json()["added"] == 0
    assert second.json()["retagged_existing"] == 1
    assert state.heading is not None
    assert state.heading.id != old_heading_id
    assert [track.heading_id for track in state.playlist if track.title == "Splendido"] == [state.heading.id]
    assert state.heading_pending_announcement == "Anni '80"
    assert state.heading_pending_narration_kind == "first_found"
    assert read_persisted_heading(tmp_path) == state.heading


@pytest.mark.asyncio
async def test_heading_excludes_banned_tracks(tmp_path):
    app = _make_app(tmp_path)
    app.state.station_state.blocklist = {("banned artist", "banned song"): {"display": "Banned Artist - Banned Song"}}
    fetched = [
        _track("Banned Song", "Banned Artist", "bad"),
        _track("Good Song", "Good Artist", "good"),
    ]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(fetched, _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    assert resp.json()["ok"] is True
    assert resp.json()["added"] == 1
    assert "Banned Song" not in [track.title for track in app.state.station_state.playlist]
    assert "Good Song" in [track.title for track in app.state.station_state.playlist]


@pytest.mark.asyncio
async def test_heading_skips_existing_track_with_different_youtube_id(tmp_path):
    existing = _track("Estate", "Bruno", "base", youtube_id="yt-old")
    app = _make_app(tmp_path, tracks=[existing])
    fetched = [
        _track("Estate", "Bruno", "dupe", youtube_id="yt-new"),
        _track("Notte", "Gianna", "new", youtube_id="yt-other"),
    ]

    with patch("mammamiradio.web.streamer.load_explicit_source", return_value=(fetched, _source())):
        async with _client(app) as client:
            resp = await client.post("/api/heading", json={"seed": "classic://italian/80s"})

    body = resp.json()
    assert body["ok"] is True
    assert body["added"] == 1
    assert body["skipped_existing"] == 1
    assert [(track.artist, track.title) for track in app.state.station_state.playlist].count(("Bruno", "Estate")) == 1
    assert [track.title for track in app.state.station_state.playlist if track.heading_id] == ["Notte"]


@pytest.mark.asyncio
async def test_playlist_load_clears_active_heading(tmp_path):
    app = _make_app(tmp_path)
    heading = Heading("h-old", "classic://italian/80s", "Anni '80", 1.0, "operator")
    app.state.station_state.heading = heading
    app.state.station_state.heading_pending_announcement = "Anni '80"
    write_persisted_heading(tmp_path, heading)
    new_tracks = [_track("New Base", "New Artist", "new")]

    with (
        patch(
            "mammamiradio.web.streamer.load_explicit_source",
            return_value=(new_tracks, _source("classic://italian/70s")),
        ),
        patch("mammamiradio.web.streamer.write_persisted_source"),
    ):
        async with _client(app) as client:
            resp = await client.post("/api/playlist/load", json={"url": "classic://italian/70s"})

    assert resp.json()["ok"] is True
    assert app.state.station_state.heading is None
    assert app.state.station_state.heading_pending_announcement == ""
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_heading_malformed_body_returns_422_not_500(tmp_path):
    """An empty or non-JSON POST body degrades to a warm 422, never a raw 500.

    The admin UI always sends ``{seed: ...}``, but a malformed request must still
    get a human, way-out message (leadership principle #5) rather than a 500.
    """
    app = _make_app(tmp_path)
    async with _client(app) as client:
        empty = await client.post("/api/heading")  # no body at all
        garbage = await client.post(
            "/api/heading",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    for resp in (empty, garbage):
        assert resp.status_code == 422
        body = resp.json()
        assert body["ok"] is False
        assert body["error"]  # a human message with a way out
    # a malformed request must not arm a heading
    assert app.state.station_state.heading is None


@pytest.mark.asyncio
async def test_direction_sets_existing_track_heading_and_persists(tmp_path):
    existing = _track("Toxic", "Britney Spears", "base")
    app = _make_app(tmp_path, tracks=[existing])
    app.state.config.allow_ytdlp = False
    expansion = DirectionExpansion(
        label="2000s female vocals",
        targets=[DirectionTarget("Britney Spears", "Toxic")],
        source="llm",
    )

    with patch("mammamiradio.web.streamer.expand_direction", return_value=expansion):
        async with _client(app) as client:
            resp = await client.post("/api/direction", json={"text": "2000s female vocals"})
            status = await client.get("/status")

    body = resp.json()
    state = app.state.station_state
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["retagged_existing"] == 1
    assert body["queued_downloads"] == 0
    assert state.heading is not None
    assert existing.heading_id == state.heading.id
    assert state.heading.selection_budget == 1
    assert state.heading.targets == [{"artist": "Britney Spears", "title": "Toxic"}]
    assert read_persisted_heading(tmp_path) == state.heading
    status_body = status.json()
    assert status_body["heading"]["label"] == "2000s female vocals"
    assert status_body["heading"]["tagged_count"] == 1
    assert status_body["heading"]["selection_remaining"] == 1
    assert status_body["playlist"][0]["heading_id"] == state.heading.id


@pytest.mark.asyncio
async def test_direction_queues_resolved_targets_without_pin(tmp_path):
    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    expansion = DirectionExpansion(
        label="Sunday morning Italian",
        targets=[DirectionTarget("Lucio Battisti", "Il mio canto libero")],
        source="curated",
    )
    meta = {
        "title": "Il mio canto libero",
        "artist": "Lucio Battisti",
        "duration_ms": 180_000,
        "youtube_id": "abc12345678",
        "album_art": "https://img.example/battisti.jpg",
    }

    with (
        patch("mammamiradio.web.streamer.expand_direction", return_value=expansion),
        patch("mammamiradio.playlist.downloader.search_ytdlp_metadata", return_value=[meta]),
        patch(
            "mammamiradio.playlist.downloader.download_external_track",
            new_callable=AsyncMock,
            return_value=tmp_path / "song.mp3",
        ),
    ):
        async with _client(app) as client:
            resp = await client.post("/api/direction", json={"text": "sunday morning italian"})
        tasks = list(getattr(app.state, "background_tasks", set()))
        if tasks:
            await asyncio.gather(*tasks)

    body = resp.json()
    state = app.state.station_state
    assert body["ok"] is True
    assert body["queued_downloads"] == 1
    assert state.pinned_track is None
    assert state.force_next is None
    assert state.heading is not None
    assert state.heading.selection_budget == 1
    direction_tracks = [track for track in state.playlist if track.heading_id == state.heading.id]
    assert [(track.artist, track.title) for track in direction_tracks] == [("Lucio Battisti", "Il mio canto libero")]


@pytest.mark.asyncio
async def test_direction_download_failure_leaves_no_active_heading_and_can_retry(tmp_path):
    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    expansion = DirectionExpansion(
        label="Sunday morning Italian",
        targets=[DirectionTarget("Lucio Battisti", "Il mio canto libero")],
        source="curated",
    )
    meta = {
        "title": "Il mio canto libero",
        "artist": "Lucio Battisti",
        "duration_ms": 180_000,
        "youtube_id": "abc12345678",
    }

    with (
        patch("mammamiradio.web.streamer.expand_direction", return_value=expansion) as expand_direction,
        patch("mammamiradio.playlist.downloader.search_ytdlp_metadata", return_value=[meta]),
        patch(
            "mammamiradio.playlist.downloader.download_external_track",
            new_callable=AsyncMock,
            side_effect=RuntimeError("yt-dlp failed"),
        ),
    ):
        async with _client(app) as client:
            first = await client.post("/api/direction", json={"text": "sunday morning italian"})
            second = await client.post("/api/direction", json={"text": "sunday morning italian"})

    assert first.json()["ok"] is False
    assert second.json()["ok"] is False
    assert first.json().get("idempotent") is None
    assert second.json().get("idempotent") is None
    assert expand_direction.call_count == 2
    assert app.state.station_state.heading is None
    assert [track.title for track in app.state.station_state.playlist] == ["Base"]
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_slow_direction_does_not_override_later_back_to_auto(tmp_path):
    app = _make_app(tmp_path, tracks=[_track("Toxic", "Britney Spears", "base")])
    app.state.config.allow_ytdlp = False
    app.state.station_state.heading = Heading("old", "classic://italian/80s", "Anni '80", 1.0, "operator")
    expansion = DirectionExpansion(
        label="2000s female vocals",
        targets=[DirectionTarget("Britney Spears", "Toxic")],
        source="llm",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_expand_direction(*_args, **_kwargs):
        started.set()
        await release.wait()
        return expansion

    with patch("mammamiradio.web.streamer.expand_direction", side_effect=slow_expand_direction):
        async with _client(app) as client:
            pending = asyncio.create_task(client.post("/api/direction", json={"text": "2000s female vocals"}))
            await started.wait()
            clear = await client.post("/api/heading/clear")
            release.set()
            direction = await pending

    assert clear.json()["ok"] is True
    assert direction.json()["ok"] is False
    assert direction.json()["stale"] is True
    assert app.state.station_state.heading is None
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_direction_download_drops_when_heading_changes_before_commit(tmp_path):
    from mammamiradio.web.streamer import _download_direction_track

    app = _make_app(tmp_path)
    old_heading = Heading("h-old", "direction://old", "Old", 1.0, "operator")
    app.state.station_state.heading = old_heading
    revision = app.state.station_state.source_revision
    track = _track("Toxic", "Britney Spears", "yt", youtube_id="abc12345678")
    app.state.station_state.heading = Heading("h-new", "direction://new", "New", 2.0, "operator")

    with patch("mammamiradio.playlist.downloader.download_external_track", new_callable=AsyncMock):
        status = await _download_direction_track(track, app.state, revision, old_heading.id)

    assert status == "dropped"
    assert track not in app.state.station_state.playlist


@pytest.mark.asyncio
async def test_direction_download_drops_when_source_switches_before_commit(tmp_path):
    from mammamiradio.web.streamer import _download_direction_track

    app = _make_app(tmp_path)
    heading = Heading("h-old", "direction://old", "Old", 1.0, "operator")
    app.state.station_state.heading = heading
    revision = app.state.station_state.source_revision
    app.state.station_state.source_revision += 1
    track = _track("Toxic", "Britney Spears", "yt", youtube_id="abc12345678")

    with patch("mammamiradio.playlist.downloader.download_external_track", new_callable=AsyncMock):
        status = await _download_direction_track(track, app.state, revision, heading.id)

    assert status == "dropped"
    assert track not in app.state.station_state.playlist


@pytest.mark.asyncio
async def test_direction_download_refuses_track_banned_after_submit(tmp_path):
    from mammamiradio.web.streamer import _download_direction_track

    app = _make_app(tmp_path)
    heading = Heading("h-old", "direction://old", "Old", 1.0, "operator")
    app.state.station_state.heading = heading
    app.state.station_state.blocklist = {("britney spears", "toxic"): {"display": "Britney Spears - Toxic"}}
    revision = app.state.station_state.source_revision
    track = _track("Toxic", "Britney Spears", "yt", youtube_id="abc12345678")

    with patch("mammamiradio.playlist.downloader.download_external_track", new_callable=AsyncMock):
        status = await _download_direction_track(track, app.state, revision, heading.id)

    assert status == "banned"
    assert track not in app.state.station_state.playlist


@pytest.mark.asyncio
async def test_direction_empty_text_returns_422_without_state(tmp_path):
    app = _make_app(tmp_path)

    async with _client(app) as client:
        resp = await client.post("/api/direction", json={"text": "   "})

    assert resp.status_code == 422
    assert resp.json()["ok"] is False
    assert app.state.station_state.heading is None
    assert read_persisted_heading(tmp_path) is None


@pytest.mark.asyncio
async def test_direction_mixed_case_confirmed_count_and_failure_notice(tmp_path, caplog):
    """Existing match keeps the course live; a failed new download surfaces a notice
    and is NOT counted as an aired song (added = confirmed only)."""
    existing = _track("Toxic", "Britney Spears", "base")
    app = _make_app(tmp_path, tracks=[existing])
    app.state.config.allow_ytdlp = True
    expansion = DirectionExpansion(
        label="2000s pop",
        targets=[DirectionTarget("Britney Spears", "Toxic"), DirectionTarget("Fergie", "Glamorous")],
        source="llm",
    )
    new_track = _track("Glamorous", "Fergie", "yt", youtube_id="ferg1234567")

    with (
        caplog.at_level(logging.WARNING, logger="mammamiradio.web.streamer"),
        patch("mammamiradio.web.streamer.expand_direction", return_value=expansion),
        patch(
            "mammamiradio.web.streamer._resolve_direction_tracks_for_route",
            new_callable=AsyncMock,
            return_value=[new_track],
        ),
        patch(
            "mammamiradio.playlist.downloader.download_external_track",
            new_callable=AsyncMock,
            side_effect=RuntimeError("yt-dlp failed"),
        ),
    ):
        async with _client(app) as client:
            resp = await client.post("/api/direction", json={"text": "2000s pop"})
        tasks = list(getattr(app.state, "background_tasks", set()))
        if tasks:
            await asyncio.gather(*tasks)

    body = resp.json()
    state = app.state.station_state
    assert body["ok"] is True
    assert body["retagged_existing"] == 1
    assert body["added"] == 1  # only the confirmed existing track
    assert body["committed_downloads"] == 0
    assert body["queued_downloads"] == 1
    assert body["pending_downloads"] == 1
    assert state.heading is not None  # course stays live on its existing track
    assert existing.heading_id == state.heading.id
    reasons = [n.get("reason") for n in state.external_add_notices]
    assert "download_failed" in reasons
    assert "Direction download failed for Fergie – Glamorous" in caplog.text
    assert "RuntimeError: yt-dlp failed" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_direction_resolver_skips_longform_first_hit(tmp_path):
    from mammamiradio.web.streamer import _resolve_direction_tracks_for_route

    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    target = DirectionTarget("FKJ", "Tadow")
    longform = {
        "title": "FKJ - Tadow DJ Set Full Album",
        "artist": "FKJ",
        "duration_ms": 7_200_000,
        "youtube_id": "set00000001",
        "album_art": "",
    }
    single = {
        "title": "FKJ - Tadow official audio",
        "artist": "FKJ",
        "duration_ms": 240_000,
        "youtube_id": "song0000001",
        "album_art": "",
    }

    with patch("mammamiradio.playlist.downloader.search_ytdlp_metadata", return_value=[longform, single]):
        tracks = await _resolve_direction_tracks_for_route([target], app.state.station_state, app.state.config)

    assert len(tracks) == 1
    assert tracks[0].youtube_id == "song0000001"
    assert list(app.state.station_state.external_add_notices) == []


@pytest.mark.asyncio
async def test_direction_resolver_notices_only_when_all_candidates_are_longform(tmp_path):
    from mammamiradio.web.streamer import _resolve_direction_tracks_for_route

    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    target = DirectionTarget("FKJ", "Tadow")
    longform = {
        "title": "FKJ - Tadow DJ Set Full Album",
        "artist": "FKJ",
        "duration_ms": 7_200_000,
        "youtube_id": "set00000001",
        "album_art": "",
    }

    with patch("mammamiradio.playlist.downloader.search_ytdlp_metadata", return_value=[longform]):
        tracks = await _resolve_direction_tracks_for_route([target], app.state.station_state, app.state.config)

    assert tracks == []
    assert [n.get("reason") for n in app.state.station_state.external_add_notices] == ["longform_audio"]


@pytest.mark.asyncio
async def test_direction_resolver_notices_non_music_rejection(tmp_path):
    from mammamiradio.web.streamer import _resolve_direction_tracks_for_route

    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    target = DirectionTarget("Talk", "Episode")
    non_music = {
        "title": "Talk - Podcast Episode",
        "artist": "Talk",
        "duration_ms": 180_000,
        "youtube_id": "episode0001",
        "album_art": "",
    }

    with patch("mammamiradio.playlist.downloader.search_ytdlp_metadata", return_value=[non_music]):
        tracks = await _resolve_direction_tracks_for_route([target], app.state.station_state, app.state.config)

    assert tracks == []
    assert [n.get("reason") for n in app.state.station_state.external_add_notices] == ["non_music_audio"]


@pytest.mark.asyncio
async def test_direction_submit_idempotent_even_before_tracks_land(tmp_path):
    """A duplicate submit while the first course's downloads are still in flight
    (zero tracks tagged yet) is a no-op, never a second competing course."""
    app = _make_app(tmp_path, tracks=[_track("Base", "Base Artist", "base")])
    seed = "direction://2000s female vocals"
    inflight = Heading(
        "h-inflight",
        seed,
        "2000s female vocals",
        1.0,
        "operator",
        targets=[{"artist": "Britney Spears", "title": "Toxic"}],
    )
    app.state.station_state.heading = inflight  # active, but nothing tagged yet
    expansion = DirectionExpansion(
        label="2000s female vocals",
        targets=[DirectionTarget("Britney Spears", "Toxic")],
        source="llm",
    )

    with patch("mammamiradio.web.streamer.expand_direction", return_value=expansion) as mock_expand:
        async with _client(app) as client:
            resp = await client.post("/api/direction", json={"text": "2000s female vocals"})

    body = resp.json()
    state = app.state.station_state
    assert body["ok"] is True
    assert body["idempotent"] is True
    assert state.heading is inflight  # unchanged — no competing heading created
    assert state.heading.id == "h-inflight"
    mock_expand.assert_not_called()  # short-circuits before the expensive expansion


def test_serialize_heading_resolving_before_tracks_tagged(tmp_path):
    """A restored/in-flight text direction reads as `resolving` until a track lands."""
    from mammamiradio.web.streamer import _serialize_heading

    state = StationState(playlist=[_track("Base", "Base Artist", "base")])
    heading = Heading(
        "h1", "direction://x", "X", 1.0, "operator", selection_budget=2, targets=[{"artist": "A", "title": "B"}]
    )
    state.heading = heading

    data = _serialize_heading(heading, state)
    assert data["phase"] == "hunting"
    assert data["tagged_count"] == 0
    assert data["resolving"] is True

    state.playlist[0].heading_id = heading.id
    heading.selection_budget = 1
    heading.selection_spent = 1
    data2 = _serialize_heading(heading, state)
    assert data2["phase"] == "steering"
    assert data2["tagged_count"] == 1
    assert data2["resolving"] is False
    assert data2["selection_remaining"] == 0

    # No state passed -> no resolving/tagged_count fields (back-compat callers).
    assert "resolving" not in _serialize_heading(heading)


@pytest.mark.asyncio
async def test_direction_all_new_timeout_keeps_course_and_still_downloading(tmp_path, monkeypatch):
    """When the first-commit wait times out, the course stays live and the response
    reports it's still downloading (never blocks, never rolls back — leadership #2/#5)."""
    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    expansion = DirectionExpansion(
        label="Sunday morning Italian",
        targets=[DirectionTarget("Lucio Battisti", "Il mio canto libero")],
        source="curated",
    )
    new_track = _track("Il mio canto libero", "Lucio Battisti", "yt", youtube_id="btti1234567")

    async def _slow_first_commit(_tasks):
        await asyncio.sleep(10)
        return 0, []

    monkeypatch.setattr("mammamiradio.web.streamer.DIRECTION_COMMIT_WAIT_SECONDS", 0.05)

    with (
        patch("mammamiradio.web.streamer.expand_direction", return_value=expansion),
        patch(
            "mammamiradio.web.streamer._resolve_direction_tracks_for_route",
            new_callable=AsyncMock,
            return_value=[new_track],
        ),
        patch("mammamiradio.web.streamer._await_first_direction_commit", side_effect=_slow_first_commit),
        patch(
            "mammamiradio.playlist.downloader.download_external_track",
            new_callable=AsyncMock,
            return_value=tmp_path / "song.mp3",
        ),
    ):
        async with _client(app) as client:
            resp = await client.post("/api/direction", json={"text": "sunday morning italian"})
        tasks = list(getattr(app.state, "background_tasks", set()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    body = resp.json()
    state = app.state.station_state
    assert body["ok"] is True
    assert body["still_downloading"] is True
    assert body["committed_downloads"] == 0
    assert state.heading is not None  # course stays live, not rolled back on timeout


@pytest.mark.asyncio
async def test_direction_all_new_timeout_clears_when_background_downloads_all_fail(tmp_path, monkeypatch):
    """A timed-out all-new hunt must not stay stuck resolving after its batch fails."""
    app = _make_app(tmp_path)
    app.state.config.allow_ytdlp = True
    expansion = DirectionExpansion(
        label="Sunday morning Italian",
        targets=[DirectionTarget("Lucio Battisti", "Il mio canto libero")],
        source="curated",
    )
    new_track = _track("Il mio canto libero", "Lucio Battisti", "yt", youtube_id="btti1234567")

    async def _slow_first_commit(_tasks):
        await asyncio.sleep(10)
        return 0, []

    monkeypatch.setattr("mammamiradio.web.streamer.DIRECTION_COMMIT_WAIT_SECONDS", 0.05)

    with (
        patch("mammamiradio.web.streamer.expand_direction", return_value=expansion),
        patch(
            "mammamiradio.web.streamer._resolve_direction_tracks_for_route",
            new_callable=AsyncMock,
            return_value=[new_track],
        ),
        patch("mammamiradio.web.streamer._await_first_direction_commit", side_effect=_slow_first_commit),
        patch(
            "mammamiradio.playlist.downloader.download_external_track",
            new_callable=AsyncMock,
            side_effect=RuntimeError("yt-dlp failed"),
        ),
    ):
        async with _client(app) as client:
            resp = await client.post("/api/direction", json={"text": "sunday morning italian"})
            tasks = list(getattr(app.state, "background_tasks", set()))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            status = await client.get("/status")

    body = resp.json()
    state = app.state.station_state
    assert body["ok"] is True
    assert body["still_downloading"] is True
    assert state.heading is None
    assert read_persisted_heading(tmp_path) is None
    assert status.json()["heading"]["active"] is False
    reasons = [n.get("reason") for n in state.external_add_notices]
    assert "download_failed" in reasons


@pytest.mark.asyncio
async def test_download_direction_track_grows_budget_and_persists(tmp_path):
    """A landed direction download grows the course's selection budget to cover it
    and re-persists, so the downloaded songs actually get selection bias."""
    from mammamiradio.web.streamer import _download_direction_track

    app = _make_app(tmp_path, tracks=[])
    heading = Heading(
        "h-grow",
        "direction://x",
        "X",
        1.0,
        "operator",
        selection_budget=0,
        targets=[{"artist": "Britney Spears", "title": "Toxic"}],
    )
    app.state.station_state.heading = heading
    write_persisted_heading(tmp_path, heading)  # persisted at budget 0
    revision = app.state.station_state.source_revision
    track = _track("Toxic", "Britney Spears", "yt", youtube_id="txc12345678")
    track.heading_id = heading.id  # the caller tags download tracks before dispatch

    with patch("mammamiradio.playlist.downloader.download_external_track", new_callable=AsyncMock):
        status = await _download_direction_track(track, app.state, revision, heading.id)

    assert status == "queued"
    assert track in app.state.station_state.playlist
    assert heading.selection_budget == 1  # grew from 0 to cover the landed track
    assert heading.phase == "steering"
    restored = read_persisted_heading(tmp_path)
    assert restored is not None
    assert restored.selection_budget == 1
    assert restored.phase == "steering"
