"""Keepsakes: the operator saying "not this one".

Clips expire after 24 hours and the provenance ledger prunes after two weeks, so
a segment worth keeping is deleted twice over by default. These tests hold the
two properties that make a keepsake different: it outlives every retention
window in the system, and nothing containing a third-party master can ever
become one.

Covers the three mandatory audio scenarios:
  normal        - banter on air, keep succeeds
  empty fallback - no ring buffer, no snapshot, warm refusal instead of a crash
  post-restart   - clips directory wiped, keepsake still served
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
_BITRATE_BYTES_PER_SEC = 192 * 1000 // 8


def _make_app(tmp_path: Path, *, admin_token: str = "tok") -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    config = load_config(TOML_PATH)
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
    return app


def _ring(seconds: int = 40) -> deque[bytes]:
    buf: deque[bytes] = deque(maxlen=600)
    for i in range(seconds):
        buf.append(bytes([i % 256]) * _BITRATE_BYTES_PER_SEC)
    return buf


# The route reads its own clock, so a fixture that subtracts from a live one is
# racing it. Every timing test pins both ends to this value instead.
FROZEN_NOW = 1_760_000_000.0


def _airing(seg_type: str, *, title: str = "Marco on the weather", elapsed: float = 12.0) -> dict:
    return {
        "type": seg_type,
        "started": FROZEN_NOW - elapsed,
        "duration_sec": 45.0,
        "metadata": {"title": title},
    }


def _frozen_clock():
    """Pin the route's `time.time()` so elapsed is exactly what the test set."""
    return patch("mammamiradio.web.streamer.time.time", return_value=FROZEN_NOW)


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


# --------------------------------------------------------------------------
# Scenario 1 - normal
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_banter_on_air_writes_a_durable_file(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter")

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
    app.state.station_state.now_streaming = _airing(seg_type)

    resp = await _keep(app)
    assert resp.status_code == 200, seg_type
    assert resp.json()["segment_type"] == seg_type


# --------------------------------------------------------------------------
# The legal guarantee
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_can_never_be_kept(tmp_path):
    """A keepsake has no expiry and is meant to be shared, so it may never hold
    somebody else's master. This is the single refusal the feature exists to make."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("music", title="Some Song")

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
    app.state.station_state.now_streaming = _airing("some_future_type")

    resp = await _keep(app)
    assert resp.status_code == 409
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_refusal_speaks_human_and_offers_a_way_out(tmp_path):
    """Leadership principle #5: name the problem AND the next step, no lingo."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("music")

    message = (await _keep(app)).json()["message"]
    assert "tap Keep" in message  # a concrete next action
    for lingo in ("copyright", "403", "409", "segment_type", "buffer", "invalid"):
        assert lingo not in message.lower()


# --------------------------------------------------------------------------
# Lookback - reaching for the button a second late
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_uses_the_lookback_snapshot_after_the_segment_ends(tmp_path):
    """The payoff lands, the segment ends, music starts, THEN you reach for it."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("music")
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
    app.state.station_state.now_streaming = _airing("music")
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
    app.state.station_state.now_streaming = _airing("music")
    app.state.last_shareworthy_clip = {
        "bytes": b"\xff\xfb" + b"song" * 400,
        "ended_monotonic": 5_000.0,
        "type": "music",
        "title": "Some Song",
    }

    with patch("mammamiradio.web.streamer.time.monotonic", return_value=5_001.0):
        assert (await _keep(app)).status_code == 409
    assert not (tmp_path / "keepsakes").exists()


# --------------------------------------------------------------------------
# Scenario 2 - empty fallback
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
    app.state.station_state.now_streaming = _airing("banter")

    assert (await _keep(app)).status_code == 409


# --------------------------------------------------------------------------
# Scenario 3 - post-restart durability, the whole point
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_keepsake_outlives_the_clip_expiry_that_lost_the_original(tmp_path):
    """Wipe clips/ the way 24h expiry and a restart would. The keepsake stays."""
    from mammamiradio.scheduling.clip import cleanup_old_clips

    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter")

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


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_caller_without_a_token_cannot_keep(tmp_path):
    """Keeping is an operator action. Loopback is trusted by the admin model
    (same machine), so the auth path is only exercised off-box."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter")

    resp = await _keep(app, token=None, client_ip="192.0.2.10")
    assert resp.status_code in (401, 403)
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_remote_caller_with_the_admin_token_can_keep(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter")

    resp = await _keep(app, token="tok", client_ip="192.0.2.10")
    assert resp.status_code == 200
    assert (tmp_path / "keepsakes" / f"{resp.json()['keepsake_id']}.mp3").exists()


@pytest.mark.asyncio
async def test_remote_caller_with_a_wrong_token_cannot_keep(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter")

    resp = await _keep(app, token="not-the-token", client_ip="192.0.2.10")
    assert resp.status_code in (401, 403)
    assert not (tmp_path / "keepsakes").exists()


# --------------------------------------------------------------------------
# The music-tail gate: type is not proof of provenance
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_banter_that_opens_over_a_song_can_never_be_kept(tmp_path):
    """The music-to-speech handoff crossfades the outgoing song's real master
    under the opening seconds of the next break and marks it has_music_tail.
    That segment is still `banter` by type and now contains someone else's
    recording, so a type allowlist alone would durably publish it."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    airing = _airing("banter")
    airing["metadata"]["has_music_tail"] = True
    app.state.station_state.now_streaming = airing

    resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "music_tail"
    assert not (tmp_path / "keepsakes").exists()


@pytest.mark.asyncio
async def test_a_tailed_lookback_snapshot_can_never_be_kept(tmp_path):
    """The snapshot carries the flag too, so the lookback path cannot smuggle in
    what the live path refuses."""
    app = _make_app(tmp_path)
    app.state.station_state.now_streaming = _airing("music")
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
# The window starts at the segment boundary
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keeping_early_never_reaches_into_the_previous_segment(tmp_path):
    """Two seconds into a break, only two seconds exist. Padding the window out
    to a 30s floor would have saved 28 seconds of the preceding ad and labelled
    it with this segment's title."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter", elapsed=2.0)

    with _frozen_clock():
        resp = await _keep(app)
    assert resp.status_code == 409
    assert resp.json()["reason"] == "too_early"


@pytest.mark.asyncio
async def test_the_kept_window_matches_what_has_aired(tmp_path):
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring(seconds=120)
    airing = _airing("banter", elapsed=10.0)
    airing["duration_sec"] = 90.0
    app.state.station_state.now_streaming = airing

    with _frozen_clock():
        body = (await _keep(app)).json()
    size = (tmp_path / "keepsakes" / f"{body['keepsake_id']}.mp3").stat().st_size
    # ~10s aired, so well under the 30s the old floor would have taken.
    assert size <= _BITRATE_BYTES_PER_SEC * 14, "window reached past the segment start"
    assert size >= _BITRATE_BYTES_PER_SEC * 8


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
    app.state.station_state.now_streaming = _airing("banter")

    resp = await _keep(app)
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "archive_full"
    assert "Delete" in body["message"]  # a concrete next step


@pytest.mark.asyncio
async def test_a_failed_write_leaves_no_half_file_behind(tmp_path):
    """In the one directory with no TTL, no cap and no evictor, a truncated file
    would survive forever, look like a keepsake and play as garbage."""
    from mammamiradio.scheduling.clip import save_keepsake

    keepsakes = tmp_path / "keepsakes"

    class Exploding(bytes):
        pass

    with patch("mammamiradio.scheduling.clip.os.replace", side_effect=OSError("ENOSPC")), pytest.raises(OSError):
        save_keepsake(b"\xff\xfb" + b"x" * 1000, keepsakes)

    assert list(keepsakes.glob("*.mp3")) == []
    assert list(keepsakes.glob(".keepsake-*")) == [], "scratch file left behind"


@pytest.mark.asyncio
async def test_the_ring_buffer_is_copied_before_it_leaves_the_loop(tmp_path):
    """extract_clip iterates the deque the playback loop appends to. Handing the
    live deque to a worker thread makes `deque mutated during iteration`
    reachable; a shallow copy on the loop does not."""
    from mammamiradio.web.streamer import _snapshot_ring

    live = _ring(seconds=40)
    seen = {}

    def fake_extract(buf, **kwargs):
        seen["is_same_object"] = buf is live
        return b"\xff\xfbdata"

    with patch("mammamiradio.scheduling.clip.extract_clip", fake_extract):
        out = await _snapshot_ring(live, 10, 192)

    assert out == b"\xff\xfbdata"
    assert seen["is_same_object"] is False


# --------------------------------------------------------------------------
# The share link, the ledger row, and a genuinely restarted station
# --------------------------------------------------------------------------


class _FakeLedger:
    """Mirrors ProvenanceLedger.record()'s surface."""

    def __init__(self) -> None:
        self.enabled = True
        self.rows: list[dict] = []

    def record(self, row: dict) -> None:
        self.rows.append(row)


@pytest.mark.asyncio
async def test_the_share_page_renders_a_keepsake_after_clips_is_wiped(tmp_path):
    """share_url is what the route returns and what the admin copies to the
    clipboard. Deleting the keepsake fallback in clip_landing left every test
    green while that link rendered "this moment has passed" for a file that by
    definition never passes."""
    app = _make_app(tmp_path)
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter", title="the one worth keeping")

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


@pytest.mark.asyncio
async def test_keeping_writes_an_operator_action_row(tmp_path):
    """Without this, deleting the ledger call left every test green and the one
    durable trace of an operator decision disappeared silently."""
    app = _make_app(tmp_path)
    ledger = _FakeLedger()
    app.state.ledger = ledger
    app.state.clip_ring_buffer = _ring()
    app.state.station_state.now_streaming = _airing("banter")

    body = (await _keep(app)).json()
    rows = [r for r in ledger.rows if r.get("record") == "operator_action"]
    assert len(rows) == 1
    assert rows[0]["action"] == "keep_this"
    assert body["keepsake_id"] in str(rows[0])


@pytest.mark.asyncio
async def test_a_keepsake_survives_an_actual_restart(tmp_path):
    """Scenario 3 properly: a SECOND app over the same cache_dir with the
    session stopped and the ring buffer empty, which is what a container that
    has just come back up looks like. Wiping a directory inside one live app
    does not test that."""
    first = _make_app(tmp_path)
    first.state.clip_ring_buffer = _ring()
    first.state.station_state.now_streaming = _airing("banter", title="before the restart")
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
