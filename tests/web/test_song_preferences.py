"""Contract tests for operator song preferences.

This is the bounded UI/API slice for thumbs-up/down preferences. The tests pin
the accepted operator-only contract so route, selection, and admin wiring stay
non-destructive.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web import streamer as streamer_module
from mammamiradio.web.streamer import LiveStreamHub, router

ROOT = Path(__file__).resolve().parents[2]
TOML_PATH = str(ROOT / "radio.toml")
WEB_ROOT = ROOT / "mammamiradio" / "web"
ADMIN_HTML = WEB_ROOT / "templates" / "admin.html"
LISTENER_HTML = WEB_ROOT / "templates" / "listener.html"
LISTENER_JS = WEB_ROOT / "static" / "listener.js"


def _track(title: str, artist: str, spotify_id: str = "") -> Track:
    return Track(title=title, artist=artist, duration_ms=180_000, spotify_id=spotify_id)


def _make_app(tmp_path: Path, tracks: list[Track] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path(tmp_path)
    state = StationState(
        playlist=list(
            tracks
            if tracks is not None
            else [
                _track("Volare", "Modugno", "t1"),
                _track("Felicita", "Al Bano", "t2"),
                _track("Tintarella di Luna", "Mina", "t3"),
            ]
        )
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.source_switch_lock = asyncio.Lock()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
        base_url="http://testserver",
    )


def _music_segment(label: str, artist: str, title: str, queue_id: str) -> Segment:
    return Segment(
        type=SegmentType.MUSIC,
        path=Path(f"/tmp/{queue_id}.mp3"),
        metadata={"artist": artist, "title_only": title, "title": label, "queue_id": queue_id},
    )


def _seed_non_destructive_boundary(app: FastAPI) -> dict[str, Any]:
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Modugno - Volare",
        "started": time.time(),
        "metadata": {"artist": "Modugno", "title_only": "Volare", "album_art": "https://img.example/volare.jpg"},
    }
    state.blocklist = {
        ("existing", "ban"): {
            "display": "Existing - Ban",
            "banned_by": "operator",
            "banned_at": 1.0,
        }
    }
    state.queued_segments = [
        {"id": "q-next", "type": "music", "label": "Al Bano - Felicita", "metadata": {"queue_id": "q-next"}},
        {"id": "q-third", "type": "music", "label": "Mina - Tintarella", "metadata": {"queue_id": "q-third"}},
    ]
    app.state.queue.put_nowait(_music_segment("Al Bano - Felicita", "Al Bano", "Felicita", "q-next"))
    app.state.queue.put_nowait(_music_segment("Mina - Tintarella", "Mina", "Tintarella di Luna", "q-third"))
    return _playback_snapshot(app)


def _playback_snapshot(app: FastAPI) -> dict[str, Any]:
    state = app.state.station_state
    return {
        "skip_event": app.state.skip_event.is_set(),
        "queued_segments": copy.deepcopy(state.queued_segments),
        "queue": [(seg.type, seg.path, copy.deepcopy(seg.metadata)) for seg in list(app.state.queue._queue)],
        "blocklist": copy.deepcopy(state.blocklist),
    }


def _assert_preference_kept_playback_intact(app: FastAPI, before: dict[str, Any]) -> None:
    after = _playback_snapshot(app)
    assert after["skip_event"] is False
    assert after["queued_segments"] == before["queued_segments"]
    assert after["queue"] == before["queue"]
    assert after["blocklist"] == before["blocklist"]


async def _post_preference(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/api/track/preference", json=payload)
    assert response.status_code == 200
    return response.json()


def _assert_preference_response(
    body: dict[str, Any],
    *,
    target: str,
    score: int,
    key: list[str],
) -> None:
    assert body["ok"] is True
    assert body["target"] == target
    assert body["score"] == score
    assert body["key"] == key
    assert body["updated_by"] == "operator"
    assert isinstance(body["updated_at"], int | float)
    assert body["updated_at"] > 0


@pytest.mark.asyncio
async def test_post_preference_targets_current_song_without_interrupting_playback(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        body = await _post_preference(client, {"now_playing": True, "vote": "up"})

    _assert_preference_response(body, target="now_playing", score=1, key=["modugno", "volare"])
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_current_song_preference_uses_title_when_title_only_is_missing(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Colapesce - Musica Leggera",
        "started": time.time(),
        "metadata": {"artist": "Colapesce", "title": "Musica Leggera"},
    }

    async with _client(app) as client:
        body = await _post_preference(client, {"now_playing": True, "vote": "up"})

    _assert_preference_response(body, target="now_playing", score=1, key=["colapesce", "musica leggera"])
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_current_song_preference_parses_combined_metadata_title(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Mina - Se telefonando",
        "started": time.time(),
        "metadata": {"artist": "Mina", "title": "Mina - Se telefonando"},
    }

    async with _client(app) as client:
        body = await _post_preference(client, {"now_playing": True, "vote": "up"})

    _assert_preference_response(body, target="now_playing", score=1, key=["mina", "se telefonando"])
    assert ("mina", "mina - se telefonando") not in app.state.station_state.song_preferences
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_current_song_preference_rejects_one_sided_song_identity(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Colapesce",
        "started": time.time(),
        "metadata": {"artist": "Colapesce"},
    }

    async with _client(app) as client:
        body = await _post_preference(client, {"now_playing": True, "vote": "up"})

    assert body["ok"] is False
    assert "music" in body["error"].lower() or "song" in body["error"].lower()
    assert app.state.station_state.song_preferences == {}
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_post_preference_targets_playlist_index_without_interrupting_playback(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        body = await _post_preference(client, {"index": 1, "vote": "down"})

    _assert_preference_response(body, target="index", score=-1, key=["al bano", "felicita"])
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [-1, 99])
async def test_post_preference_rejects_out_of_range_index(tmp_path: Path, index: int) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        response = await client.post("/api/track/preference", json={"index": index, "vote": "up"})

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "Invalid song index."
    assert app.state.station_state.song_preferences == {}
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"vote": "up"},
        {"now_playing": True, "index": 0, "vote": "up"},
    ],
)
async def test_post_preference_rejects_missing_or_ambiguous_target(tmp_path: Path, payload: dict[str, Any]) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        response = await client.post("/api/track/preference", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "Choose exactly one preference target."
    assert app.state.station_state.song_preferences == {}
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_post_preference_rejects_invalid_vote_without_side_effects(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        response = await client.post("/api/track/preference", json={"index": 1, "vote": "maybe"})

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "Preference vote must be up, down, or clear."
    assert app.state.station_state.song_preferences == {}
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_repeated_identical_vote_does_not_bump_revision(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        first = await _post_preference(client, {"index": 1, "vote": "down"})
        second = await _post_preference(client, {"index": 1, "vote": "down"})

    assert first["preference_revision"] == 1
    assert second["preference_revision"] == 1
    assert second["score"] == -1
    assert app.state.station_state.song_preferences[("al bano", "felicita")]["score"] == -1
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_repeated_identical_vote_retries_persistence_after_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)
    attempts: list[dict[tuple[str, str], dict[str, Any]]] = []

    def _flaky_save(_cache_dir: Path, preferences: dict[tuple[str, str], dict[str, Any]]) -> bool:
        attempts.append(copy.deepcopy(preferences))
        return len(attempts) > 1

    monkeypatch.setattr(streamer_module, "save_preferences", _flaky_save)

    async with _client(app) as client:
        first = await _post_preference(client, {"index": 1, "vote": "down"})
        second = await _post_preference(client, {"index": 1, "vote": "down"})

    assert first["persisted"] is False
    assert second["persisted"] is True
    assert first["preference_revision"] == 1
    assert second["preference_revision"] == 1
    assert attempts == [
        {("al bano", "felicita"): app.state.station_state.song_preferences[("al bano", "felicita")]},
        {("al bano", "felicita"): app.state.station_state.song_preferences[("al bano", "felicita")]},
    ]
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_post_preference_targets_explicit_key_without_interrupting_playback(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        body = await _post_preference(
            client,
            {"key": ["Mina", "Tintarella di Luna"], "vote": "up"},
        )

    _assert_preference_response(body, target="key", score=1, key=["mina", "tintarella di luna"])
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_explicit_key_preference_rejects_one_sided_identity(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        response = await client.post("/api/track/preference", json={"key": ["Mina", ""], "vote": "up"})

    assert response.status_code == 422
    body = response.json()
    assert body["ok"] is False
    assert "key" in body["error"].lower()
    assert app.state.station_state.song_preferences == {}
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_current_song_preference_rejects_non_music_without_side_effects(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    before = _seed_non_destructive_boundary(app)
    app.state.station_state.now_streaming = {
        "type": "banter",
        "label": "Hosts talking",
        "started": time.time(),
        "metadata": {"host": "Sofia"},
    }

    async with _client(app) as client:
        body = await _post_preference(client, {"now_playing": True, "vote": "down"})

    assert body["ok"] is False
    assert "music" in body["error"].lower() or "song" in body["error"].lower()
    assert app.state.station_state.song_preferences == {}
    _assert_preference_kept_playback_intact(app, before)


@pytest.mark.asyncio
async def test_get_track_preferences_shape_after_operator_preferences(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        await _post_preference(client, {"now_playing": True, "vote": "up"})
        await _post_preference(client, {"index": 1, "vote": "down"})
        response = await client.get("/api/track/preferences")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["count"] == len(body["preferences"]) == 2
    assert body["revision"] == 2
    rows = {(row["artist"], row["title"]): row for row in body["preferences"]}
    assert rows[("modugno", "volare")]["score"] == 1
    assert rows[("al bano", "felicita")]["score"] == -1
    for row in rows.values():
        assert set(row) >= {"artist", "title", "display", "score", "updated_at", "updated_by"}
        assert row["updated_by"] == "operator"
        assert isinstance(row["updated_at"], int | float)


@pytest.mark.asyncio
async def test_admin_status_and_playlist_include_preference_state(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    _seed_non_destructive_boundary(app)

    async with _client(app) as client:
        await _post_preference(client, {"now_playing": True, "vote": "up"})
        await _post_preference(client, {"index": 1, "vote": "down"})
        playlist = (await client.get("/api/playlist")).json()
        status = (await client.get("/status")).json()
        preferences = (await client.get("/api/track/preferences")).json()

    assert playlist["tracks"][0]["preference"] == 1
    assert playlist["tracks"][1]["preference"] == -1
    assert status["playlist"][0]["preference"] == 1
    assert status["current_track_preference"] == 1
    assert status["song_preferences"]["count"] == 2
    assert status["song_preferences"]["revision"] == 2
    assert "preferences" not in status["song_preferences"]
    assert preferences["count"] == 2
    assert preferences["revision"] == 2
    assert len(preferences["preferences"]) == 2


def _admin_function_block(name: str) -> str:
    html = ADMIN_HTML.read_text()
    start = html.find(f"function {name}")
    assert start != -1, f"could not locate {name}() in admin.html"
    next_function = re.search(r"\n(?:async\s+)?function\s+", html[start + 1 :])
    end = start + 1 + next_function.start() if next_function is not None else len(html)
    return html[start:end]


def test_admin_html_has_on_air_thumb_preference_buttons_and_handler() -> None:
    html = ADMIN_HTML.read_text()
    update_now = _admin_function_block("updateNow")
    assert "/api/track/preference" in html
    assert 'data-preference-target="now"' in html
    assert re.search(r'aria-label="[^"]*(?:like|thumbs up|thumb up)[^"]*(?:song|track)', html, re.I)
    assert re.search(r'aria-label="[^"]*(?:dislike|thumbs down|thumb down)[^"]*(?:song|track)', html, re.I)
    assert re.search(r"(?:api|fetch)\([^)]*['\"]POST['\"][^)]*['\"]/api/track/preference", html, re.S)
    assert '[data-preference-target="now"]' in update_now


def test_admin_playlist_rows_have_index_targeted_thumb_buttons() -> None:
    html = ADMIN_HTML.read_text()
    update_pl = _admin_function_block("updatePl")
    renderer = _admin_function_block("renderPreferenceButton")
    assert "/api/track/preference" in html
    assert "renderPreferenceControls" in update_pl
    assert 'data-preference-target="index"' in html
    assert "Like this song" in renderer
    assert "Dislike this song" in renderer


def test_admin_preference_handler_updates_cached_playlist_rows() -> None:
    handler = _admin_function_block("setTrackPreference")
    cache_updater = _admin_function_block("applyPreferenceToCachedRows")
    payload_resolver = _admin_function_block("preferencePayloadFromButton")

    assert "applyPreferenceToCachedRows(r.key,r.score)" in handler
    assert "preference_revision" in handler
    assert "_plRows=_plRows.map(apply)" in cache_updater
    assert "_st.playlist=_st.playlist.map(apply)" in cache_updater
    assert "updatePl(_plRows,_plPage,true)" in cache_updater
    assert "now_playing:true" in payload_resolver
    assert "index:Number(el.dataset.preferenceIndex)" in payload_resolver


def test_admin_playlist_poll_skips_unchanged_renders() -> None:
    html = ADMIN_HTML.read_text()
    update_pl = _admin_function_block("updatePl")

    assert "let _plRenderSig=''" in html
    assert "function playlistRenderSignature" in html
    assert "function playlistPreferenceRevision" in html
    assert "playlistPreferenceRevision()" in html
    assert "if(!force&&renderSig===_plRenderSig)return" in update_pl


@pytest.mark.parametrize("path", [LISTENER_HTML, LISTENER_JS])
def test_listener_surface_does_not_call_operator_preference_api(path: Path) -> None:
    assert "/api/track/preference" not in path.read_text()
