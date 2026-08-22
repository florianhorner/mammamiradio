"""Keepsakes: the operator saying "not this one".

Clips expire after 24 hours and the provenance ledger prunes after two weeks, so
a segment worth keeping is deleted twice over by default. These tests hold the
three properties that make a keepsake different: it outlives every retention
window in the system, nothing containing a third-party master can ever become
one, and the operator can take one back off the shelf.

These are route-level tests. The playback loop's half of the provenance
boundary — that a segment's chunk count and its identity are written together,
so a keepsake cannot be cut across a segment edge — is exercised against the
real loop in ``tests/web/test_streamer_routes.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from shutil import rmtree
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = Path(__file__).resolve().parents[2] / "radio.toml"
# One playback chunk, sized the way the loop sizes them: an eighth of a second.
_BITRATE_BYTES_PER_SEC = 192 * 1000 // 8
_CHUNK_BYTES = _BITRATE_BYTES_PER_SEC // 8
# Just past KEEPSAKE_MIN_SECONDS (3.0s) worth of chunks.
_ENOUGH_CHUNKS = 30


def _make_app(tmp_path: Path, *, admin_token: str = "tok") -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    config = load_config(str(TOML_PATH))
    config.admin_token = admin_token
    config.admin_password = ""
    config.cache_dir = tmp_path
    app.state.config = config
    app.state.station_state = StationState(playlist=[])
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.stream_hub = LiveStreamHub()
    app.state.start_time = time.time()
    app.state.clip_ring_buffer = None
    app.state.last_shareworthy_clip = None
    app.state.clip_segment = None
    return app


def _ring(chunks: int = 400, *, fill: bytes = b"\xa1") -> deque[bytes]:
    buf: deque[bytes] = deque(maxlen=1600)
    for _ in range(chunks):
        buf.append(fill * _CHUNK_BYTES)
    return buf


def _on_air(
    app: FastAPI,
    seg_type: str,
    *,
    title: str = "Marco on the weather",
    chunks: int = _ENOUGH_CHUNKS,
    has_music_tail: bool = False,
) -> None:
    """Put a segment on air the way the playback loop does.

    ``clip_segment`` is the loop's record of what is airing and how many of its
    own chunks are in the share ring; ``now_streaming`` is the public projection
    the console renders. Both, because the route reads the first for the audio
    and the second for the refusal it speaks.
    """
    app.state.clip_segment = {
        "type": seg_type,
        "chunks": chunks,
        "title": title,
        "has_music_tail": has_music_tail,
    }
    app.state.station_state.now_streaming = {
        "type": seg_type,
        "label": title,
        "started": time.time(),
        "metadata": {"title": title, "has_music_tail": has_music_tail},
    }


async def _keep(
    app: FastAPI,
    *,
    token: str | None = "tok",
    client_ip: str = "127.0.0.1",
) -> httpx.Response:
    """POST the keep action. Defaults to loopback, which admin auth fully trusts."""
    transport = httpx.ASGITransport(app=app, client=(client_ip, 12345))
    headers = {"X-Radio-Admin-Token": token} if token else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/api/clip/keep", headers=headers)


async def _call(app: FastAPI, method: str, url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, url, headers={"X-Radio-Admin-Token": "tok"})


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_banter_on_air_writes_a_durable_file(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    resp = await _keep(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["segment_type"] == "banter"
    assert body["title"] == "Marco on the weather"

    keepsake = tmp_path / "keepsakes" / f"{body['keepsake_id']}.mp3"
    assert keepsake.exists() and keepsake.stat().st_size > 0
    # It must land OUTSIDE clips/, which is the only reason it survives.
    assert not (tmp_path / "clips" / f"{body['keepsake_id']}.mp3").exists()

    sidecar = json.loads((tmp_path / "keepsakes" / f"{body['keepsake_id']}.json").read_text())
    assert sidecar["segment_type"] == "banter"
    assert sidecar["source"] == "live"
    # `clip_landing` reads track_title, so a keepsake renders on the share page
    # with no template change.
    assert sidecar["track_title"] == "Marco on the weather"


@pytest.mark.asyncio
@pytest.mark.parametrize("seg_type", ["banter", "ad", "news_flash", "station_id", "sweeper", "time_check"])
async def test_every_voice_segment_type_is_keepable(tmp_path, seg_type):
    """The whole voice taxonomy is the station's own work and may be kept."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, seg_type)

    resp = await _keep(app)
    assert resp.status_code == 200, seg_type
    assert resp.json()["segment_type"] == seg_type


# --------------------------------------------------------------------------
# The legal guarantee
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_can_never_be_kept(tmp_path):
    """A keepsake has no expiry and is meant to be shared, so it may never hold
    somebody else's master."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "music", title="Some Song")

    resp = await _keep(app)
    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    # Names the actual situation, not a catch-all: the operator is told why THIS
    # press failed and when to press again.
    assert body["reason"] == "music"
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_unknown_segment_type_fails_closed(tmp_path):
    """A segment type nobody has reviewed does not inherit publish rights."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "some_future_type")

    resp = await _keep(app)
    assert resp.status_code == 409
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_refusal_speaks_human_and_offers_a_way_out(tmp_path):
    """Leadership principle #5: name the problem AND the next step, no lingo."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "music")

    message = (await _keep(app)).json()["message"]
    assert "tap Keep" in message  # a concrete next action
    for lingo in ("copyright", "403", "409", "segment_type", "buffer", "invalid"):
        assert lingo not in message.lower()


@pytest.mark.asyncio
async def test_a_banter_that_opens_over_a_song_can_never_be_kept(tmp_path):
    """The music-to-speech handoff crossfades the outgoing song's real master
    under the opening seconds of the next break and marks it has_music_tail.
    That segment is still `banter` by type and now contains someone else's
    recording, so a type allowlist alone would durably publish it."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", has_music_tail=True)

    resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "music_tail"
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_a_tailed_lookback_snapshot_can_never_be_kept(tmp_path):
    """The snapshot carries the flag too, so the lookback path cannot smuggle in
    what the live path refuses."""
    app = _make_app(tmp_path)
    _on_air(app, "music")
    app.state.last_shareworthy_clip = {
        "bytes": b"\xff\xfb" + b"tailed" * 400,
        "ended_monotonic": 5_000.0,
        "type": "banter",
        "title": "opened over the outro",
        "has_music_tail": True,
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_002.0):
        assert (await _keep(app)).status_code == 409
    assert not (tmp_path / "keepsakes").exists()


def test_eligibility_needs_both_gates():
    from mammamiradio.scheduling.clip import is_keepsake_eligible

    assert is_keepsake_eligible("banter", {}) is True
    assert is_keepsake_eligible("banter", {"has_music_tail": True}) is False
    assert is_keepsake_eligible("music", {}) is False
    # A non-dict metadata must not crash or accidentally pass.
    assert is_keepsake_eligible("banter", None) is True
    assert is_keepsake_eligible("banter", "not-a-dict") is True


# --------------------------------------------------------------------------
# The cut stops at the segment edge
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cut_never_reaches_into_the_previous_segment(tmp_path):
    """The ring holds every voice segment back to back, because music never
    enters it. A window derived from wall-clock elapsed and rounded up to whole
    seconds therefore prepends the tail of whatever aired before — audio this
    segment's rights check never looked at, wearing this segment's title.

    Marker bytes, so the assertion is about provenance and not about length.
    """
    app = _make_app(tmp_path)
    ring: deque[bytes] = deque(maxlen=1600)
    for _ in range(200):
        ring.append(b"\x11" * _CHUNK_BYTES)  # the segment before: someone else's
    for _ in range(_ENOUGH_CHUNKS):
        ring.append(b"\x22" * _CHUNK_BYTES)  # this segment: ours
    app.state.clip_ring_buffer = ring
    _on_air(app, "banter", chunks=_ENOUGH_CHUNKS)

    body = (await _keep(app)).json()
    kept = (tmp_path / "keepsakes" / f"{body['keepsake_id']}.mp3").read_bytes()
    assert b"\x11" not in kept, "the cut reached back past the segment boundary"
    assert kept == b"\x22" * (_CHUNK_BYTES * _ENOUGH_CHUNKS)


@pytest.mark.asyncio
async def test_keeping_too_early_refuses_instead_of_padding(tmp_path):
    """Two seconds into a break, only two seconds exist. Padding the window out
    to a 30s floor would have saved 28 seconds of the preceding ad and labelled
    it with this segment's title."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", chunks=8)  # one second

    resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "too_early"


@pytest.mark.asyncio
async def test_a_segment_longer_than_the_ring_still_keeps_only_its_own_audio(tmp_path):
    """The count outlives the bytes: on a segment long enough to have aged out
    of the buffer, what remains is still wholly that segment's."""
    app = _make_app(tmp_path)
    ring: deque[bytes] = deque(maxlen=100)
    for _ in range(100):
        ring.append(b"\x33" * _CHUNK_BYTES)
    app.state.clip_ring_buffer = ring
    _on_air(app, "banter", chunks=4000)

    body = (await _keep(app)).json()
    kept = (tmp_path / "keepsakes" / f"{body['keepsake_id']}.mp3").read_bytes()
    assert kept == b"\x33" * (_CHUNK_BYTES * 100)


@pytest.mark.asyncio
async def test_no_chunk_count_means_nothing_of_this_segment_has_aired(tmp_path):
    """A full ring plus a segment that has put nothing into it is a station that
    just started this break, not a licence to keep the one before."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", chunks=0)

    assert (await _keep(app)).status_code == 409
    assert not (tmp_path / "keepsakes").exists()


# --------------------------------------------------------------------------
# Lookback - reaching for the button a second late
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_uses_the_lookback_snapshot_after_the_segment_ends(tmp_path):
    """The payoff lands, the segment ends, music starts, THEN you reach for it."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "music")
    app.state.last_shareworthy_clip = {
        "bytes": b"\xff\xfb" + b"payoff" * 400,
        "ended_monotonic": 5_000.0,
        "type": "banter",
        "title": "the callback payoff",
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_003.0):
        resp = await _keep(app)
    assert resp.status_code == 200
    body = resp.json()
    assert body["segment_type"] == "banter"
    assert body["title"] == "the callback payoff"

    sidecar = json.loads((tmp_path / "keepsakes" / f"{body['keepsake_id']}.json").read_text())
    assert sidecar["source"] == "lookback"
    # The saved bytes must BE the snapshot, not merely non-empty: st_size > 0
    # passes even when the wrong audio was written.
    kept = (tmp_path / "keepsakes" / f"{body['keepsake_id']}.mp3").read_bytes()
    assert kept == app.state.last_shareworthy_clip["bytes"]


@pytest.mark.asyncio
async def test_a_stale_lookback_snapshot_is_not_kept(tmp_path):
    app = _make_app(tmp_path)
    _on_air(app, "music")
    app.state.last_shareworthy_clip = {
        "bytes": b"old",
        "ended_monotonic": 5_000.0,
        "type": "banter",
        "title": "long gone",
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_600.0):
        assert (await _keep(app)).status_code == 409


@pytest.mark.asyncio
async def test_the_snapshots_own_type_is_rechecked_not_trusted(tmp_path):
    """A snapshot claiming to be music is refused even inside the window."""
    app = _make_app(tmp_path)
    _on_air(app, "music")
    app.state.last_shareworthy_clip = {
        "bytes": b"\xff\xfb" + b"song" * 400,
        "ended_monotonic": 5_000.0,
        "type": "music",
        "title": "Some Song",
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_001.0):
        assert (await _keep(app)).status_code == 409
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_a_too_early_press_never_falls_back_to_the_previous_segment(tmp_path):
    """ "Nothing to cut yet" and "that break is over" are different situations.
    Treating them alike returned 200 and permanently saved the segment BEFORE
    the one the operator was listening to, under a refusal they never saw."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", chunks=4)
    app.state.last_shareworthy_clip = {
        "bytes": b"\xff\xfb" + b"the previous break" * 400,
        "ended_monotonic": 5_000.0,
        "type": "ad",
        "title": "not what is on air",
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_001.0):
        resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "too_early"
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_a_tailed_segment_never_falls_back_to_the_previous_segment(tmp_path):
    """Same conflation, the other refusal: the operator is told the break opens
    over a song, and something else is not quietly saved instead."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", has_music_tail=True)
    app.state.last_shareworthy_clip = {
        "bytes": b"\xff\xfb" + b"the previous break" * 400,
        "ended_monotonic": 5_000.0,
        "type": "ad",
        "title": "not what is on air",
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_001.0):
        resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "music_tail"
    assert not (tmp_path / "keepsakes").exists()


# --------------------------------------------------------------------------
# Nothing on air
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_ring_buffer_and_no_snapshot_refuses_without_crashing(tmp_path):
    """Cold start: nothing buffered, nothing remembered, nothing on air."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = None
    app.state.last_shareworthy_clip = None
    app.state.station_state.now_streaming = {}

    resp = await _keep(app)
    assert resp.status_code == 409
    # A stopped station is not a copyright refusal, and "wait for the hosts to
    # come back" is wrong advice when nothing is on air.
    assert resp.json()["reason"] == "not_on_air"


@pytest.mark.asyncio
async def test_banter_on_air_but_empty_ring_buffer_refuses(tmp_path):
    """An eligible segment with no audio yet is still nothing to keep."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = deque(maxlen=240)
    _on_air(app, "banter")

    assert (await _keep(app)).status_code == 409


# --------------------------------------------------------------------------
# Durability
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_keepsake_outlives_the_clip_expiry_that_lost_the_original(tmp_path):
    """Wipe clips/ the way 24h expiry and a restart would. The keepsake stays."""
    from mammamiradio.scheduling.clip import cleanup_old_clips

    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    keepsake_id = (await _keep(app)).json()["keepsake_id"]
    keepsake = tmp_path / "keepsakes" / f"{keepsake_id}.mp3"

    # Age everything past every retention window in the system, then run the
    # expiry that deletes an ordinary clip.
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(exist_ok=True)
    (clips_dir / "doomed.mp3").write_bytes(b"\xff\xfbdoomed")
    ancient = time.time() - 90 * 24 * 3600
    import os

    for f in list(clips_dir.glob("*")) + list((tmp_path / "keepsakes").glob("*")):
        os.utime(f, (ancient, ancient))

    cleanup_old_clips(clips_dir)
    rmtree(clips_dir)  # and a restart on a container with an ephemeral clips dir

    assert keepsake.exists(), "a keepsake must outlive every retention window"

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        served = await client.get(f"/clips/{keepsake_id}.mp3")
    assert served.status_code == 200
    assert served.headers["content-type"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_cleanup_refuses_to_run_against_the_keepsakes_directory(tmp_path):
    """One wrong argument must be a no-op, not the deletion of the only copy."""
    from mammamiradio.scheduling.clip import cleanup_old_clips

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    kept = keepsakes / "abc123.mp3"
    kept.write_bytes(b"\xff\xfbkept")
    ancient = time.time() - 90 * 24 * 3600
    import os

    os.utime(kept, (ancient, ancient))

    assert cleanup_old_clips(keepsakes) == 0
    assert kept.exists()


@pytest.mark.asyncio
async def test_a_keepsake_survives_an_actual_restart(tmp_path):
    """A SECOND app over the same cache_dir with the session stopped and the ring
    buffer empty, which is what a container that has just come back up looks
    like. Wiping a directory inside one live app does not test that."""
    first = _make_app(tmp_path)
    first.state.clip_ring_buffer = _ring()
    _on_air(first, "banter", title="before the restart")
    keepsake_id = (await _keep(first)).json()["keepsake_id"]

    # Restart: new app, same disk, nothing in memory, station not started.
    second = _make_app(tmp_path)
    second.state.clip_ring_buffer = None
    second.state.last_shareworthy_clip = None
    second.state.station_state.now_streaming = {}
    second.state.station_state.session_stopped = True

    transport = httpx.ASGITransport(app=second, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        audio = await client.get(f"/clips/{keepsake_id}.mp3")
        page = await client.get(f"/clips/{keepsake_id}")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"
    assert page.status_code == 200
    assert "before the restart" in page.text


@pytest.mark.asyncio
async def test_the_share_page_renders_a_keepsake_after_clips_is_wiped(tmp_path):
    """share_url is what the route returns and what the admin copies to the
    clipboard. Deleting the keepsake fallback in clip_landing left every test
    green while that link rendered "this moment has passed" for a file that by
    definition never passes."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", title="the one worth keeping")

    body = (await _keep(app)).json()
    assert body["share_url"] == f"/clips/{body['keepsake_id']}"

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(exist_ok=True)
    rmtree(clips_dir)

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get(body["share_url"])
    assert page.status_code == 200
    assert "the one worth keeping" in page.text
    assert "passato" not in page.text, "a keepsake rendered as expired"


# --------------------------------------------------------------------------
# Taking one back off the shelf
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kept_moment_can_be_listed_and_removed(tmp_path):
    """The audio is served without a password and never expires, so an operator
    who keeps the wrong thing needs a supported way to take it back."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter", title="the regrettable one")

    keepsake_id = (await _keep(app)).json()["keepsake_id"]

    listed = (await _call(app, "GET", "/api/clip/keep")).json()
    assert [row["keepsake_id"] for row in listed["keepsakes"]] == [keepsake_id]
    assert listed["keepsakes"][0]["title"] == "the regrettable one"
    assert listed["keepsakes"][0]["segment_type"] == "banter"

    removed = await _call(app, "DELETE", f"/api/clip/keep/{keepsake_id}")
    assert removed.status_code == 200
    # Revocation is the file being gone: both public routes read from disk.
    assert not (tmp_path / "keepsakes" / f"{keepsake_id}.mp3").exists()
    assert not (tmp_path / "keepsakes" / f"{keepsake_id}.json").exists()
    assert (await _call(app, "GET", "/api/clip/keep")).json()["keepsakes"] == []

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        gone = await client.get(f"/clips/{keepsake_id}.mp3")
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_a_file_with_a_hand_written_name_is_not_listed(tmp_path):
    """Ids are ours, but the console renders them into an onclick attribute, so
    a name dropped into the directory by hand would read as markup."""
    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    (keepsakes / "ok123abc.mp3").write_bytes(b"\xff\xfb")
    (keepsakes / "'),alert(1),('.mp3").write_bytes(b"\xff\xfb")

    app = _make_app(tmp_path)
    listed = (await _call(app, "GET", "/api/clip/keep")).json()
    assert [row["keepsake_id"] for row in listed["keepsakes"]] == ["ok123abc"]


@pytest.mark.asyncio
async def test_listing_an_empty_shelf_is_an_empty_list(tmp_path):
    """Before the first keep the directory does not exist yet, and the Archivio
    panel asks for the list on arrival."""
    app = _make_app(tmp_path)
    resp = await _call(app, "GET", "/api/clip/keep")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "keepsakes": [], "max_saved": 200}


@pytest.mark.asyncio
async def test_removing_something_already_gone_says_so(tmp_path):
    app = _make_app(tmp_path)
    resp = await _call(app, "DELETE", "/api/clip/keep/deadbeef1234")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "not_found"
    assert "Refresh" in resp.json()["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["..%252Fnorm_song", "a%5Cb", "..%255Cnorm_song"])
async def test_a_traversal_id_cannot_delete_outside_the_shelf(tmp_path, bad_id):
    """A single-encoded `..%2F` never reaches the handler — the client normalizes
    it away and the router answers 404 or 405, so asserting `in (400, 404)` proved
    nothing about the guard. These forms do reach it, and must be refused there."""
    app = _make_app(tmp_path)
    victim = tmp_path / "norm_song.mp3"
    victim.write_bytes(b"\xff\xfbsong")

    resp = await _call(app, "DELETE", f"/api/clip/keep/{bad_id}")
    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert victim.exists()


@pytest.mark.asyncio
async def test_listing_and_removing_need_admin_access(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")
    keepsake_id = (await _keep(app)).json()["keepsake_id"]

    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        listed = await client.get("/api/clip/keep")
        removed = await client.delete(f"/api/clip/keep/{keepsake_id}")
    assert listed.status_code in (401, 403)
    assert removed.status_code in (401, 403)
    assert (tmp_path / "keepsakes" / f"{keepsake_id}.mp3").exists()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_caller_without_a_token_cannot_keep(tmp_path):
    """Keeping is an operator action. Loopback is trusted by the admin model
    (same machine), so the auth path is only exercised off-box."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    resp = await _keep(app, token=None, client_ip="192.0.2.10")
    assert resp.status_code in (401, 403)
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_remote_caller_with_the_admin_token_can_keep(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    resp = await _keep(app, token="tok", client_ip="192.0.2.10")
    assert resp.status_code == 200
    assert (tmp_path / "keepsakes" / f"{resp.json()['keepsake_id']}.mp3").exists()


@pytest.mark.asyncio
async def test_remote_caller_with_a_wrong_token_cannot_keep(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    resp = await _keep(app, token="not-the-token", client_ip="192.0.2.10")
    assert resp.status_code in (401, 403)
    assert not (tmp_path / "keepsakes").exists()


# --------------------------------------------------------------------------
# Bounded, atomic, and off the playback loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_archive_has_a_ceiling_with_a_way_out(tmp_path):
    """Nothing reclaims this directory, so the limit is explicit and honest."""
    from mammamiradio.web.streamer import KEEPSAKE_MAX_SAVED

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    for i in range(KEEPSAKE_MAX_SAVED):
        (keepsakes / f"full{i:04d}.mp3").write_bytes(b"\xff\xfb")

    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    resp = await _keep(app)
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "archive_full"
    # A next step the operator can actually take from the admin, not a folder
    # path inside a container they cannot open.
    assert "Kept moments" in body["message"]


@pytest.mark.asyncio
async def test_two_presses_at_the_ceiling_cannot_both_pass(tmp_path):
    """Counting the shelf, reserving the space and publishing the file are one
    decision. Two requests that each counted 199 would both pass and leave 201
    on a shelf nothing ever trims."""
    from mammamiradio.web.streamer import KEEPSAKE_MAX_SAVED

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    for i in range(KEEPSAKE_MAX_SAVED - 1):
        (keepsakes / f"full{i:04d}.mp3").write_bytes(b"\xff\xfb")

    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    first, second = await asyncio.gather(_keep(app), _keep(app))
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409]
    assert len(list(keepsakes.glob("*.mp3"))) == KEEPSAKE_MAX_SAVED


@pytest.mark.asyncio
async def test_a_failed_write_leaves_no_half_file_behind(tmp_path):
    """In the one directory with no TTL, no cap and no evictor, a truncated file
    would survive forever, look like a keepsake and play as garbage."""
    from mammamiradio.scheduling.clip import save_keepsake

    keepsakes = tmp_path / "keepsakes"

    with patch("mammamiradio.scheduling.clip.os.replace", side_effect=OSError("ENOSPC")), pytest.raises(OSError):
        save_keepsake(b"\xff\xfb" + b"x" * 1000, keepsakes)

    assert list(keepsakes.glob("*.mp3")) == []
    assert list(keepsakes.glob(".keepsake-*")) == [], "scratch file left behind"


@pytest.mark.asyncio
async def test_the_ring_buffer_is_copied_before_it_leaves_the_loop(tmp_path):
    """The extraction iterates the deque the playback loop appends to. Handing
    the live deque to a worker thread makes `deque mutated during iteration`
    reachable; a shallow copy on the loop does not."""
    from mammamiradio.web.streamer import _snapshot_segment

    live = _ring(40)
    seen = {}

    def fake_extract(buf, chunk_count):
        seen["is_same_object"] = buf is live
        return b"\xff\xfbdata"

    with patch("mammamiradio.scheduling.clip.extract_segment_audio", fake_extract):
        out = await _snapshot_segment(live, 10)

    assert out == b"\xff\xfbdata"
    assert seen["is_same_object"] is False


class _FakeLedger:
    """Mirrors ProvenanceLedger.record()'s surface."""

    def __init__(self) -> None:
        self.enabled = True
        self.rows: list[dict] = []

    def record(self, row: dict) -> None:
        self.rows.append(row)


@pytest.mark.asyncio
async def test_keeping_writes_an_operator_action_row(tmp_path):
    """Without this, deleting the ledger call left every test green and the one
    durable trace of an operator decision disappeared silently."""
    app = _make_app(tmp_path)
    ledger = _FakeLedger()
    app.state.ledger = ledger
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    body = (await _keep(app)).json()
    rows = [r for r in ledger.rows if r.get("record") == "operator_action"]
    assert len(rows) == 1
    assert rows[0]["action"] == "keep_this"
    assert body["keepsake_id"] in str(rows[0])


@pytest.mark.asyncio
async def test_removing_writes_an_operator_action_row(tmp_path):
    """Taking a moment back off the shelf is an operator decision too, and it is
    the one a later "where did that link go?" needs to be able to see."""
    app = _make_app(tmp_path)
    ledger = _FakeLedger()
    app.state.ledger = ledger
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    keepsake_id = (await _keep(app)).json()["keepsake_id"]
    await _call(app, "DELETE", f"/api/clip/keep/{keepsake_id}")

    rows = [r for r in ledger.rows if r.get("record") == "operator_action"]
    assert [r["action"] for r in rows] == ["keep_this", "unkeep_this"]
    # Pin the shape: a debrief reads old -> new, so keeping is "this segment type
    # became this file" and removing is "this file became nothing".
    assert rows[0]["old_value"] == "banter"
    assert rows[0]["new_value"] == keepsake_id
    assert rows[1]["old_value"] == keepsake_id
    assert rows[1]["new_value"] is None


# --------------------------------------------------------------------------
# The refusal envelopes that only fire when something is wrong
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_disk_refuses_before_writing(tmp_path):
    """Keeping must never be the write that fills the volume and takes the
    station off air with it (leadership principle #2)."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    from shutil import disk_usage as _real

    tiny = _real(tmp_path)._replace(free=8 * 1024 * 1024)
    with patch("mammamiradio.web.streamer.shutil.disk_usage", return_value=tiny):
        resp = await _keep(app)
    assert resp.status_code == 507
    assert resp.json()["reason"] == "no_room"
    assert "Free some space" in resp.json()["message"]
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_an_unreadable_mount_does_not_invent_a_refusal(tmp_path):
    """Fail open, deliberately: a mount whose free space cannot be read is not
    evidence that there is no room, and refusing on it would block keeping for a
    reason nobody could act on."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    with patch("mammamiradio.web.streamer.shutil.disk_usage", side_effect=OSError("no statfs")):
        resp = await _keep(app)
    assert resp.status_code == 200
    assert (tmp_path / "keepsakes" / f"{resp.json()['keepsake_id']}.mp3").exists()


@pytest.mark.asyncio
async def test_a_failed_write_answers_with_a_way_out(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")

    with patch("mammamiradio.scheduling.clip.os.replace", side_effect=OSError("ENOSPC")):
        resp = await _keep(app)
    assert resp.status_code == 500
    assert resp.json()["reason"] == "write_failed"
    assert "tap Keep again" in resp.json()["message"]


@pytest.mark.asyncio
async def test_a_failed_delete_answers_with_a_way_out(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")
    keepsake_id = (await _keep(app)).json()["keepsake_id"]

    with patch("mammamiradio.web.streamer.Path.unlink", side_effect=OSError("EROFS")):
        resp = await _call(app, "DELETE", f"/api/clip/keep/{keepsake_id}")
    assert resp.status_code == 500
    assert resp.json()["reason"] == "delete_failed"
    assert "try again" in resp.json()["message"]


@pytest.mark.asyncio
async def test_one_bad_sidecar_cannot_take_the_whole_shelf_out_of_reach(tmp_path):
    """The listing is the admin's only route to the delete, and the shelf-full
    refusal points at it. A hand-edited or older-format `created_at` must not be
    able to 500 it, or the one directory with no evictor becomes unreachable."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    _on_air(app, "banter")
    good = (await _keep(app)).json()["keepsake_id"]

    keepsakes = tmp_path / "keepsakes"
    (keepsakes / "bbbbbbbbbbbb.mp3").write_bytes(b"\xff\xfb")
    (keepsakes / "bbbbbbbbbbbb.json").write_text(json.dumps({"created_at": "2026-08-22"}))
    (keepsakes / "cccccccccccc.mp3").write_bytes(b"\xff\xfb")
    (keepsakes / "cccccccccccc.json").write_text(json.dumps(["not", "a", "dict"]))

    resp = await _call(app, "GET", "/api/clip/keep")
    assert resp.status_code == 200
    ids = [row["keepsake_id"] for row in resp.json()["keepsakes"]]
    assert set(ids) == {good, "bbbbbbbbbbbb", "cccccccccccc"}
    # The unreadable timestamps sort last rather than raising.
    assert ids[0] == good


@pytest.mark.asyncio
async def test_a_file_removed_mid_listing_is_skipped(tmp_path):
    """A concurrent remove, or the add-on operator's file manager, can delete a
    file between the glob and the stat."""
    from mammamiradio.web.streamer import _collect_keepsakes

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    (keepsakes / "aaaaaaaaaaaa.mp3").write_bytes(b"\xff\xfb")
    (keepsakes / "bbbbbbbbbbbb.mp3").write_bytes(b"\xff\xfb")

    real_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "aaaaaaaaaaaa.mp3":
            raise OSError("vanished")
        return real_stat(self, *args, **kwargs)

    with patch("mammamiradio.web.streamer.Path.stat", flaky_stat):
        rows = _collect_keepsakes(keepsakes)
    assert [row["keepsake_id"] for row in rows] == ["bbbbbbbbbbbb"]


@pytest.mark.asyncio
async def test_keep_after_a_stop_refuses_even_with_a_full_ring(tmp_path):
    """Stop clears the airing record, so the ring still holds the last break's
    bytes with nothing left that says they may be cut. The refusal has to be
    "nothing is on air", not a keepsake of whatever was playing when the
    operator pressed Stop."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.clip_segment = None
    app.state.station_state.now_streaming = {"type": "stopped", "label": "Session stopped", "metadata": {}}
    app.state.station_state.session_stopped = True

    resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "not_on_air"
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_a_non_dict_now_streaming_does_not_crash_the_refusal(tmp_path):
    app = _make_app(tmp_path)
    app.state.station_state.now_streaming = "not-a-dict"

    resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "not_on_air"


@pytest.mark.asyncio
async def test_an_empty_ring_under_a_live_break_says_wait_not_wrong_type(tmp_path):
    """The reason the operator sees is the whole point of the ladder, so pin it
    rather than only the status code."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = deque(maxlen=240)
    _on_air(app, "banter")

    resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "too_early"
