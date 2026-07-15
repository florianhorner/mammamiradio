"""Extended tests for mammamiradio/web/streamer.py -- coverage sprint.

Covers: LiveStreamHub, golden path, ingress prefix sanitization, utility
        routes. (Auth-helper and CSRF unit tests moved to tests/web/test_auth.py
        with the web/auth.py cut.)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from mammamiradio.core.listener_session import ListenerSession
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState
from mammamiradio.web import status_payload as status_payload_mod
from mammamiradio.web.streamer import (
    LiveStreamHub,
    _golden_path_status,
    _has_any_mp3,
    _is_packaged_asset,
    _preview_tracks,
    _purge_queue_and_shadow,
    _purge_segment_queue,
    _serialize_source,
    _tail_log,
    _unlink_ephemeral_best_effort,
)

# ---------------------------------------------------------------------------
# LiveStreamHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_subscribe_unsubscribe():
    hub = LiveStreamHub()
    lid, _q = hub.subscribe()
    assert hub.has_listener(lid)
    hub.unsubscribe(lid)
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_hub_broadcast():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    await hub.broadcast(b"chunk1")
    assert q.get_nowait() == b"chunk1"
    hub.unsubscribe(lid)


@pytest.mark.asyncio
async def test_hub_drops_slow_listeners():
    hub = LiveStreamHub(listener_queue_size=1)
    lid, _q = hub.subscribe()
    await hub.broadcast(b"chunk1")
    await hub.broadcast(b"chunk2")  # should drop the listener
    assert not hub.has_listener(lid)


@pytest.mark.asyncio
async def test_hub_close():
    hub = LiveStreamHub()
    lid, q = hub.subscribe()
    hub.close()
    assert not hub.has_listener(lid)
    # None sentinel should be in queue
    assert q.get_nowait() is None


@pytest.mark.asyncio
async def test_hub_double_unsubscribe():
    hub = LiveStreamHub()
    lid, _q = hub.subscribe()
    hub.unsubscribe(lid)
    hub.unsubscribe(lid)  # should not raise


def test_hub_membership_is_the_single_listener_session_authority():
    clock = [0.0]
    state = StationState(listener_session=ListenerSession(monotonic=lambda: clock[0]))
    hub = LiveStreamHub()
    hub.bind_state(state)

    first, _ = hub.subscribe()
    second, _ = hub.subscribe()
    assert state.listeners_active == len(hub._listeners) == 2
    assert state.listeners_total == 2
    assert state.listener_session.epoch == 1

    hub.unsubscribe(first)
    assert state.listeners_active == len(hub._listeners) == 1
    assert state.listener_session.epoch == 1

    hub.unsubscribe(second)
    assert state.listeners_active == len(hub._listeners) == 0
    assert state.listener_session.snapshot(now=clock[0]).phase == "grace"

    clock[0] = 599.999
    resumed, _ = hub.subscribe()
    assert state.listeners_active == len(hub._listeners) == 1
    assert state.listener_session.epoch == 1
    hub.unsubscribe(resumed)


# ---------------------------------------------------------------------------
# _purge_segment_queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_segment_queue():
    q = asyncio.Queue()
    seg = MagicMock()
    seg.path = MagicMock()
    q.put_nowait(seg)
    purged = _purge_segment_queue(q)
    assert purged == 1
    assert q.empty()


@pytest.mark.asyncio
async def test_purge_empty_queue():
    q = asyncio.Queue()
    assert _purge_segment_queue(q) == 0


@pytest.mark.asyncio
async def test_purge_queue_and_shadow_drains_and_clears(tmp_path):
    """The single purge home drains the real queue AND clears the shadow, returning the count."""
    q = asyncio.Queue()
    f = tmp_path / "seg.mp3"
    f.write_bytes(b"x")
    q.put_nowait(Segment(type=SegmentType.MUSIC, path=f, ephemeral=False))
    state = StationState()
    state.queued_segments = [{"type": "music", "label": "A"}, {"type": "banter", "label": "B"}]

    purged = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_PURGE)

    assert purged == 1
    assert q.empty()
    assert state.queued_segments == []  # shadow cleared together with the real queue


@pytest.mark.asyncio
async def test_purge_queue_and_shadow_unlinks_ephemeral_keeps_durable(tmp_path):
    """Ephemeral segments are unlinked from disk on purge; non-ephemeral are kept."""
    q = asyncio.Queue()
    eph = tmp_path / "eph.mp3"
    eph.write_bytes(b"x")
    keep = tmp_path / "keep.mp3"
    keep.write_bytes(b"x")
    q.put_nowait(Segment(type=SegmentType.MUSIC, path=eph, ephemeral=True))
    q.put_nowait(Segment(type=SegmentType.MUSIC, path=keep, ephemeral=False))
    state = StationState()
    state.queued_segments = [{"label": "x"}]

    purged = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_PURGE)

    assert purged == 2
    assert state.queued_segments == []
    assert not eph.exists()  # ephemeral unlinked
    assert keep.exists()  # non-ephemeral kept


@pytest.mark.asyncio
async def test_purge_queue_and_shadow_keeps_packaged_asset_even_if_ephemeral(tmp_path, monkeypatch):
    """Package data must survive queue purges even with a bad ephemeral flag."""
    from mammamiradio.web import streamer

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    tmp_render = tmp_path / "tmp" / "render.mp3"
    tmp_render.parent.mkdir()
    tmp_render.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(streamer, "_DEMO_ASSETS_DIR", demo_root)

    q = asyncio.Queue()
    q.put_nowait(Segment(type=SegmentType.BANTER, path=packaged, ephemeral=True))
    q.put_nowait(Segment(type=SegmentType.BANTER, path=tmp_render, ephemeral=True))
    state = StationState()
    state.queued_segments = [{"label": "asset"}, {"label": "tmp"}]

    assert _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_PURGE) == 2
    assert packaged.exists()
    assert not tmp_render.exists()
    assert _is_packaged_asset(packaged) is True


@pytest.mark.asyncio
async def test_purge_queue_and_shadow_records_discard_reason(tmp_path):
    """Operator purges record each drained segment with the supplied reason."""
    q = asyncio.Queue()
    f = tmp_path / "banter.mp3"
    f.write_bytes(b"x")
    q.put_nowait(Segment(type=SegmentType.BANTER, path=f, duration_sec=30.0, ephemeral=True))
    state = StationState()

    purged = _purge_queue_and_shadow(q, state, reason=GenerationWasteReason.OPERATOR_STOP)

    assert purged == 1
    assert state.discarded_segments_total == 1
    assert state.discard_by_reason == {GenerationWasteReason.OPERATOR_STOP: 1}
    assert state.discard_by_type == {"banter": 1}
    assert not f.exists()


# ---------------------------------------------------------------------------
# _has_any_mp3
# ---------------------------------------------------------------------------


def test_has_any_mp3_true(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"data")
    assert _has_any_mp3(tmp_path) is True


def test_has_any_mp3_false(tmp_path):
    assert _has_any_mp3(tmp_path) is False


def test_has_any_mp3_nonexistent(tmp_path):
    assert _has_any_mp3(tmp_path / "nope") is False


# ---------------------------------------------------------------------------
# _serialize_source / _preview_tracks
# ---------------------------------------------------------------------------


def test_serialize_source_none():
    assert _serialize_source(None) is None


def test_serialize_source():
    from mammamiradio.core.models import PlaylistSource

    src = PlaylistSource(kind="url", url="https://open.spotify.com/playlist/abc", label="Test")
    result = _serialize_source(src)
    assert result["kind"] == "url"
    assert result["label"] == "Test"


def test_preview_tracks():
    tracks = [MagicMock(title=f"Song {i}", artist=f"Artist {i}") for i in range(5)]
    result = _preview_tracks(tracks, limit=2)
    assert result["track_count"] == 5
    assert len(result["tracks"]) == 2


# ---------------------------------------------------------------------------
# _golden_path_status
# ---------------------------------------------------------------------------


def test_golden_path_demo():
    config = MagicMock()
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    state = MagicMock()
    result = _golden_path_status(config, state)
    assert result["stage"] in ("music_available", "needs_music_source")
    assert "blocking" in result
    assert "headline" in result
    assert "fallback_sources" in result


# ---------------------------------------------------------------------------
# _tail_log
# ---------------------------------------------------------------------------


def test_tail_log_exists(tmp_path):
    log = tmp_path / "test.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)))
    lines = _tail_log(str(log), 5)
    assert len(lines) == 5
    assert "line 99" in lines[-1]


def test_tail_log_missing():
    assert _tail_log("/nonexistent/path.log") == []


@pytest.mark.asyncio
async def test_purge_segment_queue_ephemeral_unlinks(tmp_path):
    """Ephemeral segments have their file unlinked during purge."""

    audio = tmp_path / "seg.mp3"
    audio.write_bytes(b"\x00" * 64)
    q = asyncio.Queue()
    seg = Segment(type=SegmentType.BANTER, path=audio, metadata={}, ephemeral=True)
    q.put_nowait(seg)
    purged = _purge_segment_queue(q)
    assert purged == 1
    assert not audio.exists()


@pytest.mark.asyncio
async def test_purge_segment_queue_keeps_packaged_asset_even_if_ephemeral(tmp_path, monkeypatch):
    from mammamiradio.web import streamer

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    tmp_render = tmp_path / "tmp" / "render.mp3"
    tmp_render.parent.mkdir()
    tmp_render.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(streamer, "_DEMO_ASSETS_DIR", demo_root)

    q = asyncio.Queue()
    q.put_nowait(Segment(type=SegmentType.BANTER, path=packaged, metadata={}, ephemeral=True))
    q.put_nowait(Segment(type=SegmentType.BANTER, path=tmp_render, metadata={}, ephemeral=True))

    assert _purge_segment_queue(q) == 2
    assert packaged.exists()
    assert not tmp_render.exists()


def test_unlink_ephemeral_best_effort_keeps_packaged_asset(tmp_path, monkeypatch):
    from mammamiradio.web import streamer

    demo_root = tmp_path / "assets" / "demo"
    packaged = demo_root / "recovery" / "continuity_1.mp3"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(b"\x00" * 2048)
    tmp_render = tmp_path / "tmp" / "render.mp3"
    tmp_render.parent.mkdir()
    tmp_render.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(streamer, "_DEMO_ASSETS_DIR", demo_root)

    _unlink_ephemeral_best_effort(Segment(type=SegmentType.BANTER, path=packaged, metadata={}, ephemeral=True))
    _unlink_ephemeral_best_effort(Segment(type=SegmentType.BANTER, path=tmp_render, metadata={}, ephemeral=True))

    assert packaged.exists()
    assert not tmp_render.exists()


def test_golden_path_with_local_music(tmp_path, monkeypatch):
    """When local music/ directory contains MP3s, golden path shows music_available."""
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_key", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_ts", 0.0)

    config = MagicMock()
    config.anthropic_api_key = "key"
    config.openai_api_key = ""
    config.allow_ytdlp = False
    state = MagicMock()

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "song.mp3").write_bytes(b"data")

    with (
        patch("mammamiradio.web.status_payload._has_any_mp3", side_effect=lambda p: "music" in str(p)),
    ):
        result = _golden_path_status(config, state)

    assert result["stage"] == "music_available"


def test_golden_path_with_ytdlp(monkeypatch):
    """When yt-dlp is enabled in loaded config, it appears in fallback_sources."""
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_key", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_ts", 0.0)

    config = MagicMock()
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.allow_ytdlp = True
    state = MagicMock()
    state.playlist = []

    with patch("mammamiradio.web.status_payload._has_any_mp3", return_value=False):
        result = _golden_path_status(config, state)

    assert "yt-dlp downloads" in result["fallback_sources"]


def test_golden_path_loaded_playlist_counts_as_music_source(monkeypatch):
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_key", None)
    monkeypatch.setattr(status_payload_mod, "_golden_path_cache_ts", 0.0)
    config = MagicMock()
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.allow_ytdlp = False
    state = MagicMock()
    state.playlist = [object()]
    state.playlist_source = None

    with patch("mammamiradio.web.status_payload._has_any_mp3", return_value=False):
        result = _golden_path_status(config, state)

    assert result["blocking"] is False
    assert "loaded playlist" in result["fallback_sources"]


def test_source_options_reason():
    """_source_options_reason formats a readable error string."""
    from mammamiradio.web.streamer import _source_options_reason

    msg = _source_options_reason(None, ValueError("something broke"))
    assert "something broke" in msg


@pytest.mark.asyncio
async def test_hub_close_queue_full():
    """close() swallows QueueFull when a listener's queue is already full."""
    hub = LiveStreamHub()
    hub._listener_queue_size = 1
    _lid, q = hub.subscribe()
    # Fill the queue to capacity so put_nowait raises QueueFull
    q.put_nowait(b"chunk")
    # Should not raise even though QueueFull will be triggered
    hub.close()


def test_hub_unsubscribe_updates_state():
    """unsubscribe updates state.listeners_active when state is attached."""

    hub = LiveStreamHub()
    state = StationState()
    hub._state = state
    lid, _q = hub.subscribe()
    hub.unsubscribe(lid)
    assert state.listeners_active == 0
