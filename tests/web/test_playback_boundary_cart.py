"""Playback-loop cart: paced prelude, accounting exclusion, and the three air paths."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState, Track
from mammamiradio.scheduling import boundary_glue
from mammamiradio.scheduling.boundary_glue import MUSIC_TO_SPEECH, SPEECH_TO_MUSIC
from mammamiradio.web.streamer import LiveStreamHub, StreamPacer, router, run_playback_loop

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)


class RecordingPacer(StreamPacer):
    def __init__(self, rate: float) -> None:
        super().__init__(rate, target_lead_seconds=0.0)
        self.sent: list[int] = []

    def after_send(self, chunk_bytes: int):
        self.sent.append(chunk_bytes)
        return super().after_send(chunk_bytes)


def _frames(count: int, marker: bytes) -> bytes:
    bitrate = _BITRATES[11]
    frame_length = 144 * bitrate * 1000 // 48000
    header = bytes((0xFF, 0xFB, (11 << 4) | (1 << 2), 0x00))
    payload = b"\0\0" + marker
    padding = frame_length - 4 - len(payload)
    return (header + payload + b"\0" * padding) * count


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = ""
    config.admin_token = ""
    config.is_addon = False
    config.imaging.assets_dir = str(tmp_path / "imaging")
    config.audio.boundary_imaging = True
    state = StationState(playlist=[Track(title="S", artist="A", duration_ms=180_000)])
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    app.state.clip_ring_buffer = []
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    app.state.stream_pacer_factory = RecordingPacer
    return app


def _install_carts(root: Path, *, music_to_speech: bytes | None, speech_to_music: bytes | None = None) -> None:
    if music_to_speech is not None:
        path = root / "imaging" / MUSIC_TO_SPEECH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(music_to_speech)
    if speech_to_music is not None:
        path = root / "imaging" / SPEECH_TO_MUSIC
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(speech_to_music)


def _segment(path: Path, kind: SegmentType, **metadata) -> Segment:
    return Segment(type=kind, path=path, metadata=metadata, ephemeral=False)


async def _play(app: FastAPI, segments: list[Segment]) -> bytes:
    listener_id, listener_queue = app.state.stream_hub.subscribe()
    for segment in segments:
        app.state.queue.put_nowait(segment)
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=3)
        chunks: list[bytes] = []
        while not listener_queue.empty():
            chunks.append(listener_queue.get_nowait())
        return b"".join(chunks)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        app.state.stream_hub.unsubscribe(listener_id)


@pytest.fixture(autouse=True)
def _clear_cache():
    boundary_glue._VALIDATION_CACHE.clear()
    yield
    boundary_glue._VALIDATION_CACHE.clear()


@pytest.mark.asyncio
async def test_music_then_banter_airs_one_cart_excluded_from_accounting(tmp_path: Path):
    cart = _frames(2, b"CART")
    music = _frames(2, b"SONG")
    talk = _frames(2, b"TALK")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    music_path = tmp_path / "song.mp3"
    talk_path = tmp_path / "talk.mp3"
    music_path.write_bytes(music)
    talk_path.write_bytes(talk)

    def _forbid(*_args, **_kwargs):
        raise AssertionError("boundary cart must not call ffmpeg")

    with (
        patch("subprocess.run", _forbid),
        patch("mammamiradio.audio.imaging.ImagingLibrary.pick_stinger", _forbid),
    ):
        heard = await _play(
            app,
            [_segment(music_path, SegmentType.MUSIC), _segment(talk_path, SegmentType.BANTER, title="Talk")],
        )

    assert heard == music + cart + talk
    assert app.state.station_state.boundary_carts_aired == 1
    outcome = app.state.station_state.stream_outcome_history[-1]
    assert outcome["bytes_sent"] == len(talk)
    assert outcome["segment_type"] == "banter"
    assert app.state.clip_segment["chunks"] == 1
    assert app.state.clip_ring_buffer == [talk]
    assert b"CART" not in b"".join(app.state.clip_ring_buffer)
    pacer = app.state.stream_pacer
    assert len(cart) in pacer.sent
    assert sum(pacer.sent) == len(music) + len(cart) + len(talk)


@pytest.mark.asyncio
async def test_switch_off_matches_programme_accounting_and_skips_the_cart(tmp_path: Path):
    cart = _frames(2, b"CART")
    music = _frames(2, b"SONG")
    talk = _frames(2, b"TALK")
    _install_carts(tmp_path, music_to_speech=cart)

    async def _run(enabled: bool) -> tuple[int, int, bytes]:
        boundary_glue._VALIDATION_CACHE.clear()
        app = _app(tmp_path)
        app.state.config.audio.boundary_imaging = enabled
        music_path = tmp_path / f"song-{enabled}.mp3"
        talk_path = tmp_path / f"talk-{enabled}.mp3"
        music_path.write_bytes(music)
        talk_path.write_bytes(talk)
        heard = await _play(
            app,
            [_segment(music_path, SegmentType.MUSIC), _segment(talk_path, SegmentType.BANTER)],
        )
        outcome = app.state.station_state.stream_outcome_history[-1]
        return outcome["bytes_sent"], app.state.clip_segment["chunks"], heard

    on_bytes, on_chunks, on_heard = await _run(True)
    off_bytes, off_chunks, off_heard = await _run(False)
    assert on_bytes == off_bytes == len(talk)
    assert on_chunks == off_chunks == 1
    assert on_heard == music + cart + talk
    assert off_heard == music + talk


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous_kind,previous_meta,next_meta",
    [
        (SegmentType.SWEEPER, {}, {}),
        (SegmentType.MUSIC, {"rescue": True}, {}),
        (SegmentType.MUSIC, {}, {"boundary_sting_merged": True}),
        (SegmentType.MUSIC, {}, {"rescue": True}),
        (SegmentType.MUSIC, {}, {"error": "failed"}),
        (SegmentType.MUSIC, {}, {"has_music_tail": True}),
    ],
)
async def test_skip_rules_keep_the_cut_dry(tmp_path: Path, previous_kind, previous_meta, next_meta):
    cart = _frames(2, b"CART")
    _install_carts(tmp_path, music_to_speech=cart, speech_to_music=cart)
    app = _app(tmp_path)
    previous = tmp_path / "previous.mp3"
    nxt = tmp_path / "next.mp3"
    previous.write_bytes(_frames(2, b"PREV"))
    nxt.write_bytes(_frames(2, b"NEXT"))
    next_kind = SegmentType.MUSIC if previous_kind is not SegmentType.MUSIC else SegmentType.BANTER
    if previous_kind is SegmentType.SWEEPER:
        next_kind = SegmentType.MUSIC
    heard = await _play(
        app,
        [
            _segment(previous, previous_kind, **previous_meta),
            _segment(nxt, next_kind, **next_meta),
        ],
    )
    assert b"CART" not in heard
    assert b"NEXT" in heard
    assert app.state.station_state.boundary_carts_aired == 0


@pytest.mark.asyncio
async def test_first_segment_and_empty_imaging_dir_stay_programme(tmp_path: Path):
    app = _app(tmp_path)
    (tmp_path / "imaging").mkdir()
    talk = tmp_path / "talk.mp3"
    payload = _frames(2, b"TALK")
    talk.write_bytes(payload)
    heard = await _play(app, [_segment(talk, SegmentType.BANTER)])
    assert heard == payload
    assert app.state.station_state.boundary_carts_aired == 0
    assert app.state.station_state.boundary_imaging_skips.get("prev_none") == 1


@pytest.mark.asyncio
async def test_missing_empty_and_header_only_programmes_send_no_cart(tmp_path: Path):
    cart = _frames(2, b"CART")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    music = tmp_path / "song.mp3"
    music.write_bytes(_frames(2, b"SONG"))
    missing = tmp_path / "gone.mp3"
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    header_only = tmp_path / "header.mp3"
    header_only.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")
    heard = await _play(
        app,
        [
            _segment(music, SegmentType.MUSIC),
            _segment(missing, SegmentType.BANTER),
            _segment(empty, SegmentType.BANTER),
            _segment(header_only, SegmentType.BANTER),
        ],
    )
    assert b"CART" not in heard
    assert b"SONG" in heard
    assert app.state.station_state.boundary_carts_aired == 0


@pytest.mark.asyncio
async def test_stop_and_skip_during_the_cart_keep_the_programme_off_air(tmp_path: Path):
    cart = _frames(8, b"CART")
    music = _frames(2, b"SONG")
    talk = _frames(2, b"TALK")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    music_path = tmp_path / "song.mp3"
    talk_path = tmp_path / "talk.mp3"
    music_path.write_bytes(music)
    talk_path.write_bytes(talk)
    hub = app.state.stream_hub
    original = hub.broadcast
    mode = {"stop": False}

    async def _wrapped(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"CART" in chunk and mode["stop"]:
            state = app.state.station_state
            state.session_stopped = True
            state.session_stop_revision += 1
            state.continuity_epoch += 1
        elif b"CART" in chunk:
            app.state.skip_event.set()
        return accepted

    hub.broadcast = _wrapped
    heard = await _play(app, [_segment(music_path, SegmentType.MUSIC), _segment(talk_path, SegmentType.BANTER)])
    assert b"TALK" not in heard
    assert b"SONG" in heard

    mode["stop"] = True
    app.state.station_state.session_stopped = False
    music_path.write_bytes(music)
    talk_path.write_bytes(talk)
    heard = await _play(app, [_segment(music_path, SegmentType.MUSIC), _segment(talk_path, SegmentType.BANTER)])
    assert b"TALK" not in heard


@pytest.mark.asyncio
async def test_generation_change_before_and_during_the_cart(tmp_path: Path):
    cart = _frames(8, b"CART")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    music = tmp_path / "song.mp3"
    talk = tmp_path / "talk.mp3"
    music.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    hub = app.state.stream_hub
    original = hub.broadcast

    async def _bump_after_music(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"SONG" in chunk:
            hub._delivery_generation += 1
        return accepted

    hub.broadcast = _bump_after_music
    heard = await _play(app, [_segment(music, SegmentType.MUSIC), _segment(talk, SegmentType.BANTER)])
    assert b"CART" not in heard
    assert b"TALK" in heard
    assert app.state.station_state.boundary_imaging_skips.get("generation_changed") == 1

    boundary_glue._VALIDATION_CACHE.clear()
    app = _app(tmp_path)
    music.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    hub = app.state.stream_hub
    original = hub.broadcast
    bumped = {"done": False}

    async def _bump_mid_cart(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"CART" in chunk and not bumped["done"]:
            bumped["done"] = True
            hub._delivery_generation += 1
        return accepted

    hub.broadcast = _bump_mid_cart
    heard = await _play(app, [_segment(music, SegmentType.MUSIC), _segment(talk, SegmentType.BANTER)])
    assert b"TALK" in heard
    assert heard.count(b"CART") < cart.count(b"CART")
    assert app.state.station_state.boundary_carts_aired == 0


@pytest.mark.asyncio
async def test_privacy_revocation_during_the_cart_drops_the_programme(tmp_path: Path):
    cart = _frames(2, b"CART")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    app.state.config.homeassistant.context_enabled = True
    app.state.station_state.home_context_policy_generation = 1
    music = tmp_path / "song.mp3"
    talk = tmp_path / "talk.mp3"
    music.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    hub = app.state.stream_hub
    original = hub.broadcast

    async def _revoke(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"CART" in chunk:
            app.state.station_state.home_context_policy_generation = 2
        return accepted

    hub.broadcast = _revoke
    heard = await _play(
        app,
        [
            _segment(music, SegmentType.MUSIC),
            _segment(talk, SegmentType.BANTER, home_context_generation=1),
        ],
    )
    assert b"CART" in heard
    assert b"TALK" not in heard
    assert app.state.station_state.discard_by_reason[GenerationWasteReason.OPERATOR_PURGE] == 1


@pytest.mark.asyncio
async def test_session_stop_resets_history_so_resume_starts_with_programme(tmp_path: Path):
    cart = _frames(2, b"CART")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    music = tmp_path / "song.mp3"
    talk = tmp_path / "talk.mp3"
    music.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    hub = app.state.stream_hub
    original = hub.broadcast

    async def _stop_after_song(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"SONG" in chunk:
            app.state.station_state.session_stopped = True
        return accepted

    hub.broadcast = _stop_after_song
    _listener, listener_queue = hub.subscribe()
    app.state.queue.put_nowait(_segment(music, SegmentType.MUSIC))
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=3)
        await asyncio.sleep(0.05)
        app.state.queue.put_nowait(_segment(talk, SegmentType.BANTER))
        app.state.station_state.session_stopped = False
        app.state.station_state.resume_event.set()
        await asyncio.wait_for(app.state.queue.join(), timeout=3)
        chunks: list[bytes] = []
        while not listener_queue.empty():
            chunks.append(listener_queue.get_nowait())
        heard = b"".join(chunks)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert b"SONG" in heard
    assert b"TALK" in heard
    assert b"CART" not in heard


@pytest.mark.asyncio
async def test_empty_room_forgets_the_previous_segment(tmp_path: Path):
    cart = _frames(2, b"CART")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    music = tmp_path / "song.mp3"
    talk = tmp_path / "talk.mp3"
    music.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    hub = app.state.stream_hub
    listener_id, _first_queue = hub.subscribe()
    original = hub.broadcast

    async def _leave(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"SONG" in chunk:
            hub.unsubscribe(listener_id)
        return accepted

    hub.broadcast = _leave
    app.state.queue.put_nowait(_segment(music, SegmentType.MUSIC))
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(app.state.queue.join(), timeout=3)
        await asyncio.sleep(0.05)
        _second_id, second_queue = hub.subscribe()
        app.state.queue.put_nowait(_segment(talk, SegmentType.BANTER))
        await asyncio.wait_for(app.state.queue.join(), timeout=3)
        chunks: list[bytes] = []
        while not second_queue.empty():
            chunks.append(second_queue.get_nowait())
        heard = b"".join(chunks)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert b"TALK" in heard
    assert b"CART" not in heard
    skips = app.state.station_state.boundary_imaging_skips
    assert skips.get("prev_none") == 1
    assert skips.get("generation_changed", 0) == 0


@pytest.mark.asyncio
async def test_dial_off_mid_cart_finishes_this_seam_and_dries_the_next(tmp_path: Path):
    entry = _frames(8, b"CART")
    exit_cart = _frames(2, b"EXIT")
    _install_carts(tmp_path, music_to_speech=entry, speech_to_music=exit_cart)
    app = _app(tmp_path)
    song = tmp_path / "song.mp3"
    talk = tmp_path / "talk.mp3"
    song2 = tmp_path / "song2.mp3"
    song.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    song2.write_bytes(_frames(2, b"NEXT"))
    hub = app.state.stream_hub
    original = hub.broadcast

    async def _disable(chunk: bytes) -> int:
        accepted = await original(chunk)
        if b"CART" in chunk:
            app.state.config.audio.boundary_imaging = False
        return accepted

    hub.broadcast = _disable
    heard = await _play(
        app,
        [
            _segment(song, SegmentType.MUSIC),
            _segment(talk, SegmentType.BANTER),
            _segment(song2, SegmentType.MUSIC),
        ],
    )
    assert entry in heard
    assert b"TALK" in heard
    assert b"NEXT" in heard
    assert b"EXIT" not in heard
    assert app.state.station_state.boundary_imaging_skips.get("switch_off") == 1


@pytest.mark.asyncio
async def test_invalid_asset_warns_once_and_the_station_still_airs(tmp_path: Path, caplog):
    _install_carts(tmp_path, music_to_speech=_frames_bad())
    app = _app(tmp_path)
    song = tmp_path / "song.mp3"
    talk = tmp_path / "talk.mp3"
    song.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    with caplog.at_level("WARNING"):
        heard = await _play(app, [_segment(song, SegmentType.MUSIC), _segment(talk, SegmentType.BANTER)])
    assert b"TALK" in heard
    assert b"CART" not in heard
    warnings = [record for record in caplog.records if "Boundary imaging asset unusable" in record.message]
    assert len(warnings) == 1


def _frames_bad() -> bytes:
    """192 kbps frames whose first frame points at the previous file's reservoir."""
    bitrate = _BITRATES[11]
    frame_length = 144 * bitrate * 1000 // 48000
    header = bytes((0xFF, 0xFB, (11 << 4) | (1 << 2), 0x00))
    payload = b"\x00\x80" + b"CART"
    padding = frame_length - 4 - len(payload)
    return (header + payload + b"\0" * padding) * 4
