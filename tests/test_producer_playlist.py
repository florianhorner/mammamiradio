from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from fakeitaliradio.config import (
    AdsSection,
    AudioSection,
    PacingSection,
    PlaylistSection,
    StationConfig,
    StationSection,
)
from fakeitaliradio.models import HostPersonality, SegmentType, StationState, Track
from fakeitaliradio.producer import run_producer, select_next_track


def _track(name: str) -> Track:
    return Track(
        title=name,
        artist="Artist",
        duration_ms=180000,
        spotify_id=name,
    )


def _test_config(tmp_path: Path) -> StationConfig:
    tmp_dir = tmp_path / "tmp"
    cache_dir = tmp_path / "cache"
    tmp_dir.mkdir()
    cache_dir.mkdir()
    return StationConfig(
        station=StationSection(name="Test Radio", language="it", theme="test"),
        playlist=PlaylistSection(),
        pacing=PacingSection(lookahead_segments=1),
        hosts=[HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")],
        ads=AdsSection(brands=[], voices=[], sfx_dir="sfx"),
        audio=AudioSection(),
        cache_dir=cache_dir,
        tmp_dir=tmp_dir,
    )


def test_select_next_track_uses_live_playlist():
    a = _track("a")
    b = _track("b")
    c = _track("c")
    playlist = [a, b, c]

    assert select_next_track(playlist, None) == a
    assert select_next_track(playlist, a) == b

    # Simulate queue mutation: remove "b" while "a" is current.
    playlist.pop(1)
    assert select_next_track(playlist, a) == c

    # Current track missing from playlist => start from first item.
    assert select_next_track(playlist, b) == a

    # Simulate move operation and ensure selection follows new order.
    playlist.insert(0, playlist.pop(1))  # [c, a]
    assert select_next_track(playlist, a) == c


def test_run_producer_handles_empty_playlist_without_crashing(tmp_path, monkeypatch):
    def fake_generate_silence(path: Path, duration_sec: float) -> None:
        path.write_bytes(b"0" * 2048)

    monkeypatch.setattr("fakeitaliradio.producer.generate_silence", fake_generate_silence)

    async def _run() -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        state = StationState(playlist=[])
        config = _test_config(tmp_path)

        task = asyncio.create_task(run_producer(queue, state, config, spotify_player=None))
        segment = await asyncio.wait_for(queue.get(), timeout=2.0)

        assert segment.type == SegmentType.MUSIC
        assert "Playlist is empty" in segment.metadata.get("error", "")
        assert state.segments_produced == 1
        assert state.songs_since_banter == 1
        assert state.songs_since_ad == 1

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())
