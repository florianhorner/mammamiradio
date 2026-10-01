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
from mammamiradio.web.mp3_frames import mpeg1_l3_bitrate_kbps
from mammamiradio.web.streamer import LiveStreamHub, StreamPacer, router, run_playback_loop
from tests.home_fixtures import SYNTHETIC_BINDINGS

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


class RecordingPacer(StreamPacer):
    def __init__(self, rate: float) -> None:
        super().__init__(rate, target_lead_seconds=0.0)
        self.sent: list[int] = []

    def after_send(self, chunk_bytes: int):
        self.sent.append(chunk_bytes)
        return super().after_send(chunk_bytes)


def _frames(count: int, marker: bytes) -> bytes:
    header = bytes((0xFF, 0xFB, (11 << 4) | (1 << 2), 0x00))
    bitrate = mpeg1_l3_bitrate_kbps(header)
    assert bitrate is not None
    frame_length = 144 * bitrate * 1000 // 48000
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
    boundary_glue.warn_unusable.cache_clear()
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
@pytest.mark.parametrize("generation_changed", [False, True])
@pytest.mark.parametrize(
    "previous_kind,previous_meta,next_meta,reason",
    [
        (SegmentType.SWEEPER, {}, {}, "prev_imaging"),
        (SegmentType.MUSIC, {"rescue": True}, {}, "prev_rescue"),
        (SegmentType.MUSIC, {}, {"rescue": True}, "next_rescue"),
        (SegmentType.MUSIC, {}, {"error": "failed"}, "next_error"),
        (SegmentType.MUSIC, {}, {"has_music_tail": True}, "next_music_tail"),
    ],
)
async def test_skip_rules_keep_the_cut_dry(
    tmp_path: Path, previous_kind, previous_meta, next_meta, reason, generation_changed
):
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
    hub = app.state.stream_hub
    original = hub.broadcast

    async def _change_room(chunk):
        accepted = await original(chunk)
        if generation_changed and b"PREV" in chunk:
            hub._delivery_generation += 1
        return accepted

    hub.broadcast = _change_room
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
    assert app.state.station_state.boundary_imaging_skips.get(reason) == 1
    assert app.state.station_state.boundary_imaging_skips.get("generation_changed", 0) == 0


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
    header = bytes((0xFF, 0xFB, (11 << 4) | (1 << 2), 0x00))
    bitrate = mpeg1_l3_bitrate_kbps(header)
    assert bitrate is not None
    frame_length = 144 * bitrate * 1000 // 48000
    payload = b"\x00\x80" + b"CART"
    padding = frame_length - 4 - len(payload)
    return (header + payload + b"\0" * padding) * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["skip", "epoch", "stop_resume", "privacy", "stop"])
@pytest.mark.parametrize("cart_frames", [2, 8])
async def test_orphan_cart_is_not_repeated_and_discard_releases_home_fact(tmp_path, mode, cart_frames):
    from mammamiradio.home.context_director import DirectorObservation, HomeContextDirector

    cart = _frames(cart_frames, b"CART")
    _install_carts(tmp_path, music_to_speech=cart)
    app = _app(tmp_path)
    state = app.state.station_state
    app.state.config.homeassistant.context_enabled = True
    state.home_context_policy_generation = 1
    director = HomeContextDirector(bindings=SYNTHETIC_BINDINGS)
    director.observe(
        [
            DirectorObservation(
                entity_id="weather.example", domain="weather", state="sunny", temperature_c=24, score=9.0
            )
        ],
        policy_revision=0,
    )
    fact = director.select()
    assert fact is not None and director.reserve("talk", fact)
    state.home_context_director = director
    segments = []
    for name, kind in [("SONG", SegmentType.MUSIC), ("TALK", SegmentType.BANTER), ("NEXT", SegmentType.BANTER)]:
        path = tmp_path / f"{name}.mp3"
        path.write_bytes(_frames(2, name.encode()))
        segments.append(_segment(path, kind, title=name))
    segments[1].metadata.update(queue_id="talk", home_fact_id=fact.fact_id, home_context_generation=1)
    original = app.state.stream_hub.broadcast
    fired = False

    async def _abort(chunk):
        nonlocal fired
        accepted = await original(chunk)
        if b"CART" in chunk and not fired:
            fired = True
            if mode == "skip":
                app.state.skip_event.set()
            elif mode == "privacy":
                state.home_context_policy_generation = 2
            else:
                state.continuity_epoch += 1
                if mode in {"stop", "stop_resume"}:
                    state.session_stop_revision += 1
                    state.session_stopped = mode == "stop"
        return accepted

    app.state.stream_hub.broadcast = _abort
    heard = await _play(app, segments[:2] if mode == "stop" else segments)
    assert b"SONG" in heard and b"TALK" not in heard
    assert 0 < heard.count(b"CART") <= cart_frames
    completed = mode == "privacy" or cart_frames == 2
    if completed:
        assert heard.count(b"CART") == cart_frames
    else:
        assert heard.count(b"CART") < cart_frames
    assert state.boundary_carts_aired == int(completed)
    abort_reason = {
        "skip": "skip",
        "privacy": GenerationWasteReason.OPERATOR_PURGE,
        "stop": GenerationWasteReason.SESSION_STOPPED,
    }.get(mode, GenerationWasteReason.STALE_CONTINUITY)
    assert state.boundary_imaging_skips.get(abort_reason, 0) == int(not completed)
    if mode != "stop":
        assert b"NEXT" in heard
    assert app.state.queue._unfinished_tasks == 0
    if mode != "skip":
        reason = {"privacy": GenerationWasteReason.OPERATOR_PURGE, "stop": GenerationWasteReason.SESSION_STOPPED}.get(
            mode, GenerationWasteReason.STALE_CONTINUITY
        )
        assert state.discard_by_reason[reason] == 1
        assert all(row["bytes_sent"] > 0 for row in state.stream_outcome_history)
        assert len(state.stream_outcome_history) == (1 if mode == "stop" else 2)
        status = director.admin_status()
        assert status["reserved_count"] == 0
        assert status["session_counters"]["released"] == 1
        assert status["session_counters"]["activated"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("packaged", [False, True])
async def test_ad_boundary_carts_follow_packaged_provenance(tmp_path, packaged):
    app = _app(tmp_path)
    cart = _frames(2, b"CART")
    for relative in (boundary_glue.AD_IN, boundary_glue.AD_OUT):
        path = tmp_path / "imaging" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cart)
    segments = []
    expected = b""
    for name, kind in [("SONG", SegmentType.MUSIC), ("AD", SegmentType.AD), ("NEXT", SegmentType.MUSIC)]:
        path = tmp_path / f"{name}.mp3"
        payload = _frames(2, name.encode())
        path.write_bytes(payload)
        segments.append(_segment(path, kind, packaged=packaged))
        expected += (cart if packaged and name != "SONG" else b"") + payload
    assert await _play(app, segments) == expected
    assert app.state.station_state.boundary_carts_aired == (2 if packaged else 0)


@pytest.mark.asyncio
async def test_urgent_interrupt_slot_gets_no_cart_on_either_side(tmp_path):
    app = _app(tmp_path)
    _install_carts(tmp_path, music_to_speech=_frames(2, b"CART"), speech_to_music=_frames(2, b"EXIT"))
    song, tone, nxt = (tmp_path / f"{name}.mp3" for name in ("song", "tone", "next"))
    song.write_bytes(_frames(2, b"SONG"))
    tone.write_bytes(_frames(2, b"TONE"))
    nxt.write_bytes(_frames(2, b"NEXT"))
    original = app.state.stream_hub.broadcast

    async def _interrupt(chunk):
        accepted = await original(chunk)
        if b"SONG" in chunk:
            app.state.station_state.interrupt_slot = tone
            app.state.station_state.interrupt_slot_ephemeral = False
        return accepted

    app.state.stream_hub.broadcast = _interrupt
    heard = await _play(app, [_segment(song, SegmentType.MUSIC), _segment(nxt, SegmentType.MUSIC)])
    assert heard == song.read_bytes() + tone.read_bytes() + nxt.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "lookup", "validator"])
async def test_cart_load_failure_is_one_warning_and_programme_continues(tmp_path, caplog, failure):
    app = _app(tmp_path)
    if failure != "missing":
        _install_carts(tmp_path, music_to_speech=_frames(2, b"CART"))
    song, talk = tmp_path / "song.mp3", tmp_path / "talk.mp3"
    song.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    target = "_packaged_file" if failure == "lookup" else "validated_playable_bytes"
    from contextlib import nullcontext

    guard = (
        patch(f"mammamiradio.web.streamer.{target}", side_effect=RuntimeError("load failed"))
        if failure != "missing"
        else nullcontext()
    )
    with guard, caplog.at_level("WARNING"):
        heard = await _play(
            app,
            [
                segment
                for _ in range(2)
                for segment in (_segment(song, SegmentType.MUSIC), _segment(talk, SegmentType.BANTER))
            ],
        )
    assert heard == (song.read_bytes() + talk.read_bytes()) * 2
    # The opposite cart is also absent; count the warning for the entry asset.
    warnings = [
        record.message
        for record in caplog.records
        if "Boundary imaging asset unusable" in record.message and "music_to_speech" in record.message
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_true_queue_gap_forgets_the_previous_song(tmp_path):
    app = _app(tmp_path)
    app.state.config.cache_dir = tmp_path
    _install_carts(tmp_path, music_to_speech=_frames(2, b"CART"))
    song, talk = tmp_path / "song.mp3", tmp_path / "talk.mp3"
    song.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    gap = asyncio.Event()
    reset = StreamPacer.reset_timeline

    def _reset(pacer, reason):
        reset(pacer, reason)
        if reason == "queue_gap_fallback":
            gap.set()

    _, listener = app.state.stream_hub.subscribe()
    app.state.queue.put_nowait(_segment(song, SegmentType.MUSIC))
    with (
        patch("mammamiradio.scheduling.producer._pick_recovery_clip", return_value=None),
        patch("mammamiradio.web.streamer._select_norm_cache_rescue", return_value=None),
        patch("mammamiradio.web.streamer.FIRST_BYTE_GRACE_SECONDS", 0.01),
        patch.object(StreamPacer, "reset_timeline", _reset),
    ):
        task = asyncio.create_task(run_playback_loop(app))
        try:
            await asyncio.wait_for(gap.wait(), 2)
            app.state.queue.put_nowait(_segment(talk, SegmentType.BANTER))
            await asyncio.wait_for(app.state.queue.join(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    chunks = []
    while not listener.empty():
        chunks.append(listener.get_nowait())
    assert b"".join(chunks) == song.read_bytes() + talk.read_bytes()


@pytest.mark.asyncio
async def test_cancel_during_cart_settles_queue_without_claiming_programme_air(tmp_path):
    app = _app(tmp_path)
    _install_carts(tmp_path, music_to_speech=_frames(8, b"CART"))
    song, talk = tmp_path / "song.mp3", tmp_path / "talk.mp3"
    song.write_bytes(_frames(2, b"SONG"))
    talk.write_bytes(_frames(2, b"TALK"))
    app.state.queue.put_nowait(_segment(song, SegmentType.MUSIC))
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=talk, ephemeral=True))
    _, listener = app.state.stream_hub.subscribe()
    cart_started = asyncio.Event()
    original = app.state.stream_hub.broadcast

    async def _observe(chunk):
        accepted = await original(chunk)
        if b"CART" in chunk:
            cart_started.set()
        return accepted

    app.state.stream_hub.broadcast = _observe
    task = asyncio.create_task(run_playback_loop(app))
    try:
        await asyncio.wait_for(cart_started.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(app.state.queue.join(), 1)
    chunks = []
    while not listener.empty():
        chunks.append(listener.get_nowait())
    heard = b"".join(chunks)
    assert b"CART" in heard and b"TALK" not in heard
    assert app.state.station_state.boundary_carts_aired == 0
    assert app.state.station_state.active_playback_segment is None
    assert app.state.clip_ring_buffer == []
    assert not talk.exists()


@pytest.mark.asyncio
async def test_committed_music_tail_pair_airs_without_a_valid_cart(tmp_path):
    from tests.web.test_streamer_routes import _commit_playback_handoff

    app = _app(tmp_path)
    _install_carts(tmp_path, music_to_speech=_frames(2, b"CART"))
    song, talk = _frames(2, b"SONG"), _frames(2, b"TAIL-TALK")
    _music, successor, source = _commit_playback_handoff(app, tmp_path, head_payload=song)
    successor.path.write_bytes(talk)
    assert await _play(app, []) == song + talk
    assert app.state.station_state.boundary_imaging_skips["next_music_tail"] == 1
    assert not app.state.station_state.handoff_reservations
    assert source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["ban", "listener_request"])
async def test_music_policy_change_during_cart_wins_before_first_programme_byte(tmp_path, policy):
    from mammamiradio.web.streamer import _segment_blocklist_key

    app = _app(tmp_path)
    _install_carts(tmp_path, music_to_speech=None, speech_to_music=_frames(2, b"CART"))
    state = app.state.station_state
    segments = []
    for title, kind in [("TALK", SegmentType.BANTER), ("SONG", SegmentType.MUSIC), ("NEXT", SegmentType.MUSIC)]:
        path = tmp_path / f"{title}.mp3"
        path.write_bytes(_frames(2, title.encode()))
        segments.append(_segment(path, kind, artist="Artist", title_only=title))
    original = app.state.stream_hub.broadcast

    async def _change_policy(chunk):
        accepted = await original(chunk)
        if b"CART" in chunk:
            if policy == "ban":
                state.blocklist[_segment_blocklist_key(segments[1])] = {"display": "Artist - SONG"}
            else:
                state.pending_requests.append(
                    {
                        "request_id": "request-example",
                        "type": "song_request",
                        "song_found": True,
                        "song_track_obj": Track(title="SONG", artist="Artist", duration_ms=180000),
                    }
                )
        return accepted

    app.state.stream_hub.broadcast = _change_policy
    heard = await _play(app, segments)
    assert b"SONG" not in heard
    assert b"TALK" in heard and b"NEXT" in heard
    assert heard.count(b"CART") == 2
    reason = GenerationWasteReason.OPERATOR_BAN if policy == "ban" else GenerationWasteReason.LISTENER_REQUEST_RESERVED
    assert state.discard_by_reason[reason] == 1
    assert len(state.stream_outcome_history) == 2
    assert app.state.queue._unfinished_tasks == 0


@pytest.mark.asyncio
async def test_provider_admission_waits_until_cart_finishes(tmp_path):
    app = _app(tmp_path)
    _install_carts(tmp_path, music_to_speech=None, speech_to_music=_frames(2, b"CART"))
    talk, song = tmp_path / "talk.mp3", tmp_path / "song.mp3"
    talk.write_bytes(_frames(2, b"TALK"))
    song.write_bytes(_frames(2, b"SONG"))
    revoked = False
    admitted_before_revoke = []
    releases = []

    def _admit():
        admitted_before_revoke.append(not revoked)
        return not revoked

    segment = _segment(song, SegmentType.MUSIC)
    segment.playback_start_callback = _admit
    segment.release_callback = lambda: releases.append(True)
    original = app.state.stream_hub.broadcast

    async def _disable_provider(chunk):
        nonlocal revoked
        accepted = await original(chunk)
        if b"CART" in chunk:
            revoked = True
        return accepted

    app.state.stream_hub.broadcast = _disable_provider
    heard = await _play(app, [_segment(talk, SegmentType.BANTER), segment])
    assert b"SONG" not in heard
    assert admitted_before_revoke == [False]
    assert releases == [True]
    assert app.state.station_state.discard_by_reason[GenerationWasteReason.PLAYBACK_ADMISSION_DENIED] == 1
    assert len(app.state.station_state.stream_outcome_history) == 1
