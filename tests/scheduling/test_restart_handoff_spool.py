from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.models import Segment, SegmentType, StationState
from mammamiradio.scheduling.producer import _enqueue_with_egress


@pytest.mark.asyncio
async def test_enqueue_music_schedules_restart_handoff_after_queue_success(tmp_path):
    queue: asyncio.Queue = asyncio.Queue()
    state = StationState()
    config = SimpleNamespace(cache_dir=tmp_path, tmp_dir=tmp_path / "tmp")
    music = tmp_path / "norm_artist_song_192k.mp3"
    music.write_bytes(b"audio")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=music,
        duration_sec=120.0,
        metadata={"artist": "Artist", "title_only": "Song", "audio_source": "download"},
        ephemeral=False,
    )

    with patch("mammamiradio.scheduling.producer.try_write_restart_handoff_spool", return_value=True) as m_write:
        assert await _enqueue_with_egress(queue, state, config, segment) is True
        tasks = list(state._restart_handoff_tasks)
        assert len(tasks) == 1
        await tasks[0]

    m_write.assert_called_once()
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_enqueue_music_passes_admitted_paths_as_protected(tmp_path):
    """F2: the per-enqueue spool write must protect the still-queued startup
    handoff files (the prune would otherwise delete them out from under the
    live queue -> cold-open dead air)."""
    queue: asyncio.Queue = asyncio.Queue()
    state = StationState()
    admitted = (tmp_path / "restart_handoff" / "segments" / "admitted.mp3").resolve()
    state.restart_handoff_admitted_paths = {admitted}
    config = SimpleNamespace(cache_dir=tmp_path, tmp_dir=tmp_path / "tmp")
    music = tmp_path / "norm_artist_song_192k.mp3"
    music.write_bytes(b"audio")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=music,
        duration_sec=120.0,
        metadata={"artist": "Artist", "title_only": "Song", "audio_source": "download"},
        ephemeral=False,
    )

    with patch("mammamiradio.scheduling.producer.try_write_restart_handoff_spool", return_value=True) as m_write:
        assert await _enqueue_with_egress(queue, state, config, segment) is True
        tasks = list(state._restart_handoff_tasks)
        assert len(tasks) == 1
        await tasks[0]

    _, kwargs = m_write.call_args
    assert kwargs["protected_paths"] == frozenset({admitted})


@pytest.mark.asyncio
async def test_enqueue_front_insert_does_not_write_restart_handoff(tmp_path):
    queue: asyncio.Queue = asyncio.Queue()
    state = StationState()
    config = SimpleNamespace(cache_dir=tmp_path, tmp_dir=tmp_path / "tmp")
    music = tmp_path / "norm_artist_song_192k.mp3"
    music.write_bytes(b"audio")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=music,
        duration_sec=120.0,
        metadata={"artist": "Artist", "title_only": "Song"},
        ephemeral=False,
    )

    with patch("mammamiradio.scheduling.producer.try_write_restart_handoff_spool") as m_write:
        assert await _enqueue_with_egress(
            queue,
            state,
            config,
            segment,
            front_insert=True,
            shadow_entry={"id": "q1"},
        )

    m_write.assert_not_called()
