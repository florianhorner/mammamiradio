"""Endpoint + engine tests for the operator song blocklist (Phase 1).

Scenarios (CLAUDE.md audio-delivery rule):
  * Normal      — ban a song -> dropped from rotation + persisted + listed.
  * Empty/edge  — bulk ban that would starve the pool is refused with a warm message.
  * Post-restart — covered at the data layer in tests/playlist/test_blocklist.py.
Plus: durable ✕ removal, unban, the 4th ingest doorway (_commit_external_download),
on-air queue purge, and pinned-track clear.

The on-air console "Ban" button (``/api/track/ban-now-playing`` = ban + immediate skip)
carries its own three-scenario block at the foot of this file: Normal (ban+purge+cut),
Empty fallback (bridge so a queue emptied by the ban never goes dead), and Post-restart
(session stopped -> reject cleanly, no spurious skip).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web import streamer
from mammamiradio.web.streamer import LiveStreamHub, _admin_track_id, _apply_ban, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _track(title: str, artist: str, spotify_id: str = "") -> Track:
    return Track(title=title, artist=artist, duration_ms=180_000, spotify_id=spotify_id)


def _make_app(tmp_path, tracks=None) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.cache_dir = Path(tmp_path)
    state = StationState(playlist=list(tracks if tracks is not None else [_track("Volare", "Modugno", "t1")]))
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
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    )


@pytest.mark.asyncio
async def test_ban_by_keys_drops_and_persists(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    async with _client(app) as c:
        r = await c.post("/api/track/ban", json={"keys": [["Modugno", "Volare"]]})
        body = r.json()
        assert body["ok"] is True and body["removed"] == 1
        # Dropped from the live pool...
        assert [t.title for t in state.playlist] == ["Felicità"]
        # ...persisted to disk...
        assert ("modugno", "volare") in state.blocklist
        # ...and surfaced in the banlist view.
        bl = (await c.get("/api/track/banlist")).json()
        assert bl["count"] == 1 and bl["banned"][0]["title"] == "volare"


@pytest.mark.asyncio
async def test_ban_by_indices(tmp_path):
    app = _make_app(tmp_path, [_track("A", "X"), _track("B", "Y"), _track("C", "Z")])
    state = app.state.station_state
    async with _client(app) as c:
        r = await c.post("/api/track/ban", json={"indices": [0, 2]})
        assert r.json()["removed"] == 2
    assert [t.title for t in state.playlist] == ["B"]


@pytest.mark.asyncio
async def test_bulk_ban_starvation_rejected_with_warm_message(tmp_path):
    pool = [_track(f"S{i}", "A", f"id{i}") for i in range(6)]
    app = _make_app(tmp_path, pool)
    state = app.state.station_state
    async with _client(app) as c:
        # Banning 5 of 6 would leave 1, below the floor -> refuse, change nothing.
        r = await c.post("/api/track/ban", json={"indices": [0, 1, 2, 3, 4]})
        body = r.json()
        assert body["ok"] is False
        assert "too few songs" in body["error"].lower()
        assert "429" not in body["error"] and "starv" not in body["error"].lower()
    assert len(state.playlist) == 6
    assert state.blocklist == {}


@pytest.mark.asyncio
async def test_remove_endpoint_is_a_durable_ban(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    target = state.playlist[0]
    async with _client(app) as c:
        r = await c.post(
            "/api/playlist/remove",
            json={
                "revision": state.playlist_revision,
                "index": 0,
                "id": _admin_track_id(target),
            },
        )
        assert r.json()["banned"] is True
    assert ("modugno", "volare") in state.blocklist
    assert [t.title for t in state.playlist] == ["Felicità"]


@pytest.mark.asyncio
async def test_unban_lifts_the_ban(tmp_path):
    # Two-track pool so the bulk ban clears the starvation floor (banning the only
    # song in a 1-track pool is now refused — see the empty-pool guard test).
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    async with _client(app) as c:
        await c.post("/api/track/ban", json={"keys": [["Modugno", "Volare"]]})
        assert ("modugno", "volare") in state.blocklist
        r = await c.post("/api/track/unban", json={"keys": [["Modugno", "Volare"]]})
        assert r.json()["unbanned"] == 1
    assert ("modugno", "volare") not in state.blocklist


@pytest.mark.asyncio
async def test_ban_clears_matching_pin(tmp_path):
    pinned = _track("Volare", "Modugno")
    app = _make_app(tmp_path, [pinned, _track("Felicità", "Al Bano")])
    state = app.state.station_state
    state.pinned_track = pinned
    _apply_ban(state, app.state.config, [pinned], queue=app.state.queue)
    assert state.pinned_track is None


@pytest.mark.asyncio
async def test_ban_purges_not_yet_started_queued_music_segment(tmp_path):
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    q = app.state.queue
    # A pre-produced music segment of the banned song + an innocent one.
    seg_banned = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/volare.mp3"),
        ephemeral=False,
        metadata={"artist": "Modugno", "title_only": "Volare", "queue_id": "q-ban"},
    )
    seg_keep = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/keep.mp3"),
        ephemeral=False,
        metadata={"artist": "Al Bano", "title_only": "Felicità", "queue_id": "q-keep"},
    )
    q.put_nowait(seg_banned)
    q.put_nowait(seg_keep)
    state.queued_segments = [{"id": "q-ban", "label": "Volare"}, {"id": "q-keep", "label": "Felicità"}]

    result = _apply_ban(state, app.state.config, [_track("Volare", "Modugno")], queue=q)
    assert result["purged"] == 1
    # Shadow + real queue both keep only the innocent segment.
    assert [s["id"] for s in state.queued_segments] == ["q-keep"]
    survivors = []
    while not q.empty():
        survivors.append(q.get_nowait())
    assert [s.metadata["queue_id"] for s in survivors] == ["q-keep"]


@pytest.mark.asyncio
async def test_ban_purges_queued_segment_stamped_with_title_only_key(tmp_path):
    """Norm-cache bridge / rescue fills stamp `title` (not `title_only`); the ban
    purge must still drop such a queued segment for the banned song."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    q = app.state.queue
    # A rescue-shaped music segment: `title` set, no `title_only`.
    seg_banned = Segment(
        type=SegmentType.MUSIC,
        path=Path("/tmp/volare-rescue.mp3"),
        ephemeral=False,
        metadata={"artist": "Modugno", "title": "Volare", "queue_id": "q-ban"},
    )
    q.put_nowait(seg_banned)
    state.queued_segments = [{"id": "q-ban", "label": "Volare"}]

    result = _apply_ban(state, app.state.config, [_track("Volare", "Modugno")], queue=q)
    assert result["purged"] == 1
    assert state.queued_segments == []
    assert q.empty()


@pytest.mark.asyncio
async def test_commit_external_download_drops_banned_song(tmp_path):
    """4th ingest doorway: an admin queue-from-search / listener request for a
    banned song must be refused, not committed to rotation. The status is the
    distinct "banned" (not "dropped") so each caller can surface an honest answer."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.blocklist = {
        ("modugno", "volare"): {"display": "Modugno - Volare", "banned_by": "operator", "banned_at": 0.0}
    }
    banned = _track("Volare", "Modugno", "yt1")

    async def _no_download(track, cache_dir, music_dir=None):
        return None

    with (
        patch("mammamiradio.playlist.downloader.download_external_track", _no_download),
        patch("mammamiradio.playlist.cover_art.needs_resolve", return_value=False),
    ):
        status = await streamer._commit_external_download(
            banned,
            app.state,
            state.source_revision,
            should_commit=lambda: True,
            should_pin=lambda: True,
        )
    assert status == "banned"
    assert banned not in state.playlist
    assert state.playlist == []


@pytest.mark.asyncio
async def test_bulk_ban_cannot_empty_an_already_small_pool(tmp_path):
    """The starvation guard must also fire below the floor: a bulk ban that would
    drop a small pool to zero is refused, otherwise the station starves onto rescue."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    async with _client(app) as c:
        # 2-track pool, ban both -> would leave 0 -> refuse, change nothing.
        r = await c.post("/api/track/ban", json={"indices": [0, 1]})
        body = r.json()
        assert body["ok"] is False
        assert "too few songs" in body["error"].lower()
    assert len(state.playlist) == 2
    assert state.blocklist == {}
    # Banning just one still leaves a song, so it is allowed.
    async with _client(app) as c:
        r = await c.post("/api/track/ban", json={"indices": [0]})
        assert r.json()["ok"] is True
    assert len(state.playlist) == 1


@pytest.mark.asyncio
async def test_ban_reports_not_persisted_when_disk_write_fails(tmp_path):
    """A failed save must not let the API promise a durable ban: the ban holds for
    the session (in-memory) but the response says persisted=False so the UI is honest."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    with patch("mammamiradio.web.streamer.save_blocklist", return_value=False):
        async with _client(app) as c:
            r = await c.post("/api/track/ban", json={"keys": [["Modugno", "Volare"]]})
            body = r.json()
    assert body["ok"] is True
    assert body["persisted"] is False
    # In-memory ban still holds for the session.
    assert ("modugno", "volare") in state.blocklist


# --- Ban-now-playing: the on-air console "Ban" button (ban + immediate skip) --------
#
# Audio-delivery rule (CLAUDE.md): this path touches the streamer/skip/bridge, so all
# three scenarios are covered — Normal, Empty fallback (bridge, never dead air), and
# Post-restart (session stopped -> reject cleanly).


def _airing_music(state, *, artist="Modugno", title_only="Volare", label=None):
    """Stamp now_streaming as a music segment the way the playback loop does."""
    state.now_streaming = {
        "type": "music",
        "label": label if label is not None else f"{artist} — {title_only}",
        "started": time.time(),
        "metadata": {"artist": artist, "title_only": title_only},
    }


@pytest.mark.asyncio
async def test_ban_now_playing_bans_skips_and_purges(tmp_path):
    """Scenario 1 — Normal: the airing song is blocklisted, dropped from the pool,
    its queued copy purged, and the air segment cut (skip_event set, now -> skipping)."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno"), _track("Felicità", "Al Bano")])
    state = app.state.station_state
    q = app.state.queue
    _airing_music(state)
    # A queued copy of the same song + an innocent one, plus a third innocent so the
    # queue is non-empty after the purge (no bridge expected).
    q.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=Path("/tmp/v.mp3"),
            ephemeral=False,
            metadata={"artist": "Modugno", "title_only": "Volare", "queue_id": "q-ban"},
        )
    )
    safe_path = tmp_path / "f.mp3"
    safe_path.write_bytes(b"safe")
    q.put_nowait(
        Segment(
            type=SegmentType.MUSIC,
            path=safe_path,
            ephemeral=False,
            metadata={"artist": "Al Bano", "title_only": "Felicità", "queue_id": "q-keep"},
        )
    )
    state.queued_segments = [{"id": "q-ban", "label": "Volare"}, {"id": "q-keep", "label": "Felicità"}]

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is True and body["skipped"] is True and body["bridged"] is False
    assert body["purged"] == 1
    # Server returns the resolved key so the client's Undo can't target a stale song.
    assert body["key"] == ["modugno", "volare"]
    # Durable ban + dropped from the pool.
    assert ("modugno", "volare") in state.blocklist
    assert [t.title for t in state.playlist] == ["Felicità"]
    # Queued copy gone, innocent kept.
    assert [s["id"] for s in state.queued_segments] == ["q-keep"]
    # Air segment cut.
    assert app.state.skip_event.is_set()
    assert state.now_streaming["type"] == "skipping"
    # Shares the skip path, so the listener profile records the skip (no drift between
    # the two _request_skip callers).
    assert state.listener.songs_skipped >= 1


@pytest.mark.asyncio
async def test_ban_now_playing_works_when_song_not_in_playlist(tmp_path):
    """The airing song came from the rescue cache / a one-off download and is NOT in
    state.playlist. Index-based row ban can't reach it; ban-now-playing bans by
    identity and still cuts the air. The big win over /api/playlist/remove."""
    app = _make_app(tmp_path, [_track("Felicità", "Al Bano")])
    state = app.state.station_state
    _airing_music(state, artist="OneOff", title_only="Ghost Track")

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is True and body["skipped"] is True
    assert ("oneoff", "ghost track") in state.blocklist
    # The unrelated pool song is untouched.
    assert [t.title for t in state.playlist] == ["Felicità"]
    assert app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_bridges_when_queue_empty(tmp_path):
    """Scenario 2 — Empty fallback: queue empty + no queued segments. Ban-now must
    force the next music (the queue-empty bridge directive) before cutting, so a queue
    the ban itself emptied never goes dead. This pins the directive (force_next=MUSIC);
    that the producer can satisfy it under an empty cache is the producer/rescue tests'
    job, not this one's."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    _airing_music(state)
    assert app.state.queue.empty() and not state.queued_segments

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is True and body["bridged"] is True
    # The bridge directive that prevents dead air.
    assert state.force_next is SegmentType.MUSIC
    assert app.state.skip_event.is_set()
    assert any(a.get("source") == "ban_now_playing" for a in state.pending_actions)


@pytest.mark.asyncio
async def test_ban_now_playing_is_starvation_exempt(tmp_path):
    """Contract pin: ban-now is EXEMPT from MIN_ROTATION_AFTER_BAN (like the per-row ✕
    Ban), unlike bulk /api/track/ban which refuses below the floor. Banning the only
    song in a 1-track pool must succeed and empty the pool — the operator asked for
    THIS song gone now. If someone routes ban-now through the floor check, this fails
    with a clear reason instead of the bridge test failing obscurely."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    _airing_music(state)
    async with _client(app) as c:
        # Same one-track pool the bulk endpoint would refuse to empty...
        bulk = (await c.post("/api/track/ban", json={"indices": [0]})).json()
        assert bulk["ok"] is False  # bulk refuses (would starve)
    # ...but ban-now bans it anyway.
    state.blocklist.clear()
    state.playlist = [_track("Volare", "Modugno")]
    _airing_music(state)
    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()
    assert body["ok"] is True
    assert ("modugno", "volare") in state.blocklist
    assert state.playlist == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,expected",
    [
        ("Mina — Tintarella di Luna", ("mina", "tintarella di luna")),  # em dash
        ("Mina – Tintarella", ("mina", "tintarella")),  # en dash
        ("Mina - Tintarella", ("mina", "tintarella")),  # hyphen
    ],
)
async def test_ban_now_playing_label_dash_variants(tmp_path, label, expected):
    """Identity-from-label must handle all three separators the UI renders."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": label, "started": time.time(), "metadata": {}}
    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()
    assert body["ok"] is True
    assert expected in state.blocklist
    assert body["key"] == list(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Mina —", "— Volare", "Mina"])
async def test_ban_now_playing_one_sided_label_is_refused(tmp_path, label):
    """A malformed one-sided label is not a song identity — refuse rather than ban a
    half-key like ('mina', '') that would match nothing real."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": label, "started": time.time(), "metadata": {}}
    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()
    assert body["ok"] is False
    assert state.blocklist == {}
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_rejected_after_restart_stop(tmp_path):
    """Scenario 3 — Post-restart: the session is stopped (now_streaming type 'stopped').
    Nothing musical is on air, so ban-now rejects with a warm message and never raises
    into the audio path or fires a spurious skip."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    state.now_streaming = {"type": "stopped", "label": "Session stopped", "started": time.time(), "metadata": {}}

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is False
    assert "on air" in body["error"].lower()
    assert state.blocklist == {}
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_rejected_during_banter(tmp_path):
    """Only music can be banned — a banter/ad segment on air is rejected, button stays
    a no-op, no skip fired."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    state.now_streaming = {"type": "banter", "label": "Hosts chatting", "started": time.time(), "metadata": {}}

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is False
    assert state.blocklist == {}
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_falls_back_to_label_when_metadata_missing(tmp_path):
    """No artist/title_only in metadata -> resolve identity from the 'Artist — Title'
    label the queue/programme rows already render."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.now_streaming = {
        "type": "music",
        "label": "Mina — Tintarella di Luna",
        "started": time.time(),
        "metadata": {},
    }

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is True
    assert ("mina", "tintarella di luna") in state.blocklist
    assert app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_unresolvable_identity_is_refused(tmp_path):
    """Music on air but no identity anywhere (no metadata, label is just the bare type)
    -> refuse with a way-out message rather than ban an empty key."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.now_streaming = {"type": "music", "label": "music", "started": time.time(), "metadata": {}}

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is False
    assert state.blocklist == {}
    assert not app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_idempotent_when_already_banned(tmp_path):
    """Already on the blocklist (e.g. banned from the row while it kept airing): the
    operator still wants it OFF NOW, so ban-now skips anyway."""
    app = _make_app(tmp_path, [])
    state = app.state.station_state
    state.blocklist = {
        ("modugno", "volare"): {"display": "Modugno - Volare", "banned_by": "operator", "banned_at": 0.0}
    }
    _airing_music(state)

    async with _client(app) as c:
        body = (await c.post("/api/track/ban-now-playing")).json()

    assert body["ok"] is True and body["skipped"] is True
    assert ("modugno", "volare") in state.blocklist
    assert app.state.skip_event.is_set()


@pytest.mark.asyncio
async def test_ban_now_playing_honest_when_disk_write_fails(tmp_path):
    """Best-effort persistence surfaced honestly: a failed save still bans for the
    session and cuts the air, but persisted=False so the toast doesn't over-promise."""
    app = _make_app(tmp_path, [_track("Volare", "Modugno")])
    state = app.state.station_state
    _airing_music(state)
    with patch("mammamiradio.web.streamer.save_blocklist", return_value=False):
        async with _client(app) as c:
            body = (await c.post("/api/track/ban-now-playing")).json()
    assert body["ok"] is True and body["persisted"] is False
    assert ("modugno", "volare") in state.blocklist
    assert app.state.skip_event.is_set()
