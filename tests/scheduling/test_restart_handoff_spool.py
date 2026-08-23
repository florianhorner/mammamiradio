from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.config import PacingSection
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.scheduling.producer import _enqueue_with_egress, _schedule_restart_handoff_spool


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
        metadata={
            "artist": "Artist",
            "title_only": "Song",
            "audio_source": "download",
            "source_kind": "local",
        },
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
        metadata={
            "artist": "Artist",
            "title_only": "Song",
            "audio_source": "download",
            "source_kind": "local",
        },
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
async def test_listener_handoff_music_clears_older_restart_manifest(tmp_path):
    queue: asyncio.Queue = asyncio.Queue()
    track = Track(
        title="Promised Song",
        artist="Listener Artist",
        duration_ms=120_000,
        youtube_id="promised-song",
    )
    state = StationState(playlist=[track], pinned_track=track)
    request = {"request_id": "listener-request"}
    assert state.arm_listener_request_handoff(request, track)
    # A promised song is always an external search result: the listener-request
    # route only starts a lookup when external media resolution is available
    # (otherwise the request lands on "downloads_disabled" with no track at all),
    # and that download builds its Track with the default source.
    assert track.source == "youtube"
    config = SimpleNamespace(
        cache_dir=tmp_path,
        tmp_dir=tmp_path / "tmp",
        allow_ytdlp=True,
        pacing=PacingSection(),
    )
    music = tmp_path / "norm_listener_song_192k.mp3"
    music.write_bytes(b"audio")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=music,
        duration_sec=120.0,
        metadata={
            "artist": track.artist,
            "title_only": track.title,
            "youtube_id": track.youtube_id,
            "duration_ms": track.duration_ms,
            "audio_source": "download",
            # The producer stamps the rendered track's own origin alongside the
            # dedication metadata, so a live promised segment always carries it.
            "source_kind": track.source,
            **state.listener_request_handoff_metadata(track),
        },
        ephemeral=False,
    )

    with (
        patch("mammamiradio.scheduling.producer.try_write_restart_handoff_spool", return_value=True) as m_write,
        # Stands in for the standalone-only extractor module being installed,
        # which is what made this listener song request resolvable in the first
        # place. Without it the test host's missing optional dependency, not the
        # code under test, would decide the outcome.
        patch("mammamiradio.scheduling.producer.external_media_enabled", return_value=True),
    ):
        assert await _enqueue_with_egress(
            queue,
            state,
            config,
            segment,
            admission_callback=state.admit_listener_request_handoff,
        )
        tasks = list(state._restart_handoff_tasks)
        assert len(tasks) == 1
        await tasks[0]

    _, kwargs = m_write.call_args
    assert kwargs["clear_when_empty"] is True


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


@pytest.mark.asyncio
async def test_post_restart_spool_keeps_full_music_instead_of_private_handoff_head(tmp_path):
    """Audio-delivery Scenario 3: a private handoff head is never persisted.

    The ordinary full song may seed restart recovery, but a later exact-once
    queue commit must not replace that safe candidate with a truncated head
    whose tail-bearing successor exists only in the live process.
    """

    state = StationState()
    config = SimpleNamespace(cache_dir=tmp_path, tmp_dir=tmp_path / "tmp")
    original = tmp_path / "norm_artist_song_192k.mp3"
    original.write_bytes(b"full-song")
    head = tmp_path / "handoff_head.mp3"
    head.write_bytes(b"shortened-head")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=original,
        duration_sec=120.0,
        metadata={
            "artist": "Artist",
            "title_only": "Song",
            "audio_source": "download",
            "source_kind": "local",
        },
        ephemeral=False,
    )

    with patch("mammamiradio.scheduling.producer.try_write_restart_handoff_spool", return_value=True) as m_write:
        _schedule_restart_handoff_spool(state, config, segment)
        tasks = list(state._restart_handoff_tasks)
        assert len(tasks) == 1
        await tasks[0]

        segment.path = head
        segment.duration_sec = 112.0
        segment.ephemeral = True
        segment.handoff_id = "private-handoff"
        _schedule_restart_handoff_spool(state, config, segment)
        await asyncio.sleep(0)

    m_write.assert_called_once()
    candidate = m_write.call_args.args[1][0]
    assert candidate.path == original
    assert candidate.duration_sec == 120.0
    assert not state.handoff_reservations


@pytest.mark.asyncio
async def test_enqueue_starter_music_never_writes_restart_handoff(tmp_path):
    queue: asyncio.Queue = asyncio.Queue()
    state = StationState()
    config = SimpleNamespace(cache_dir=tmp_path, tmp_dir=tmp_path / "tmp")
    starter = tmp_path / "starter-carefree.mp3"
    starter.write_bytes(b"canonical starter bytes")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=starter,
        duration_sec=180.0,
        metadata={
            "artist": "Kevin MacLeod",
            "title_only": "Carefree",
            "source_kind": "starter",
        },
        ephemeral=False,
    )

    with patch("mammamiradio.scheduling.producer.try_write_restart_handoff_spool") as m_write:
        assert await _enqueue_with_egress(queue, state, config, segment) is True
        await asyncio.sleep(0)

    m_write.assert_not_called()
    assert state._restart_handoff_tasks == set()
