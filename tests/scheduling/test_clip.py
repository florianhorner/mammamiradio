"""Tests for the clip extraction and sharing module."""

from collections import deque

from mammamiradio.scheduling.clip import cleanup_old_clips, extract_clip, save_clip


def test_extract_clip_empty_buffer():
    buf: deque[bytes] = deque(maxlen=100)
    assert extract_clip(buf) is None


def test_extract_clip_returns_tail():
    buf: deque[bytes] = deque(maxlen=1000)
    # Each chunk = 1 second at 192kbps = 24000 bytes
    chunk_size = 192 * 1000 // 8  # 24000
    for i in range(60):
        buf.append(bytes([i % 256]) * chunk_size)

    clip = extract_clip(buf, duration_seconds=10, bitrate_kbps=192)
    assert clip is not None
    expected_size = chunk_size * 10
    assert len(clip) == expected_size
    # Should contain data from the last 10 chunks (indices 50-59)
    assert clip[-chunk_size:] == bytes([59 % 256]) * chunk_size


def test_extract_clip_short_buffer():
    """When buffer has less data than requested, return all of it."""
    buf: deque[bytes] = deque(maxlen=100)
    buf.append(b"\xff" * 1000)
    clip = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    assert clip is not None
    assert len(clip) == 1000


def test_save_clip(tmp_path):
    clip_data = b"\x00" * 5000
    clip_id = save_clip(clip_data, tmp_path / "clips")
    assert len(clip_id) == 12
    clip_path = tmp_path / "clips" / f"{clip_id}.mp3"
    assert clip_path.exists()
    assert clip_path.read_bytes() == clip_data


def test_cleanup_old_clips(tmp_path):
    import os
    import time

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    # Create a "fresh" clip
    fresh = clips_dir / "fresh.mp3"
    fresh.write_bytes(b"\x00" * 100)

    # Create an "old" clip with mtime 48 hours ago
    old = clips_dir / "old.mp3"
    old.write_bytes(b"\x00" * 100)
    old_time = time.time() - 48 * 3600
    os.utime(old, (old_time, old_time))

    removed = cleanup_old_clips(clips_dir, max_age_hours=24)
    assert removed == 1
    assert fresh.exists()
    assert not old.exists()


def test_cleanup_old_clips_returns_zero_when_dir_missing(tmp_path):
    """cleanup_old_clips returns 0 immediately when the clips directory doesn't exist."""
    removed = cleanup_old_clips(tmp_path / "nonexistent")
    assert removed == 0


def test_cleanup_prunes_json_sidecar(tmp_path):
    """Expired MP3 + matching .json sidecar are both deleted by cleanup_old_clips."""
    import os
    import time

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    mp3 = clips_dir / "old.mp3"
    mp3.write_bytes(b"\x00" * 100)
    sidecar = clips_dir / "old.json"
    sidecar.write_text('{"station_name": "test"}')

    old_time = time.time() - 48 * 3600
    os.utime(mp3, (old_time, old_time))
    os.utime(sidecar, (old_time, old_time))

    removed = cleanup_old_clips(clips_dir, max_age_hours=24)
    assert removed == 1
    assert not mp3.exists()
    assert not sidecar.exists()


def test_cleanup_keeps_sidecar_when_mp3_fresh(tmp_path):
    """Fresh MP3 + sidecar pair is left intact by cleanup_old_clips."""
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    mp3 = clips_dir / "fresh.mp3"
    mp3.write_bytes(b"\x00" * 100)
    sidecar = clips_dir / "fresh.json"
    sidecar.write_text('{"station_name": "test"}')

    removed = cleanup_old_clips(clips_dir, max_age_hours=24)
    assert removed == 0
    assert mp3.exists()
    assert sidecar.exists()


def test_cleanup_old_clips_skips_file_on_stat_oserror(tmp_path):
    """OSError during stat is silently skipped; function returns 0 removed."""
    from pathlib import Path
    from unittest.mock import patch

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    clip_a = clips_dir / "a.mp3"
    clip_a.write_bytes(b"\x00" * 100)

    _orig_stat = Path.stat

    def _raise_for_mp3(self, *, follow_symlinks=True):
        if self.suffix == ".mp3":
            raise OSError("permission denied")
        return _orig_stat(self, follow_symlinks=follow_symlinks)

    with patch.object(Path, "stat", _raise_for_mp3):
        removed = cleanup_old_clips(clips_dir, max_age_hours=0)

    assert removed == 0  # file skipped because stat raised OSError
    assert clip_a.exists()  # file was not deleted


# ---------------------------------------------------------------------------
# extract_clip: removed `if not chunks: return None` guard
# ---------------------------------------------------------------------------


def test_extract_clip_returns_bytes_not_none_for_partial_buffer():
    """extract_clip must return bytes (never None) when ring_buffer is non-empty.

    The `if not chunks: return None` guard was removed. For a non-empty ring_buffer,
    the for loop always produces at least one chunk, so the function must return
    bytes even when the buffer contains less data than the requested duration.

    This is a regression guard against reintroducing a redundant None-return.
    """
    buf: deque[bytes] = deque(maxlen=100)
    # Add a single small chunk — far less than 30s at 192kbps
    buf.append(b"\xaa" * 500)

    result = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    # Must return bytes, not None
    assert result is not None
    assert isinstance(result, bytes)
    assert len(result) == 500


def test_extract_clip_single_chunk_returns_that_chunk():
    """When the ring_buffer contains exactly one chunk, extract_clip returns it exactly."""
    buf: deque[bytes] = deque(maxlen=10)
    data = b"\xbb" * 1024
    buf.append(data)

    result = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    assert result == data


def test_extract_clip_returns_empty_bytes_for_empty_ring_buffer():
    """An empty ring_buffer returns None (guard at top of function is preserved)."""
    buf: deque[bytes] = deque(maxlen=100)
    result = extract_clip(buf)
    assert result is None


def test_save_keepsake_without_a_sidecar_writes_only_the_audio(tmp_path):
    from mammamiradio.scheduling.clip import save_keepsake

    keepsake_id = save_keepsake(b"\xff\xfbaudio", tmp_path / "keepsakes")
    assert (tmp_path / "keepsakes" / f"{keepsake_id}.mp3").read_bytes() == b"\xff\xfbaudio"
    assert not (tmp_path / "keepsakes" / f"{keepsake_id}.json").exists()


def test_a_sidecar_failure_still_returns_the_saved_audio(tmp_path):
    """Losing the metadata is survivable. Losing the audio is the failure this
    whole feature exists to prevent, so the id must still come back and the mp3
    must still be on disk."""
    from mammamiradio.scheduling.clip import save_keepsake

    class Unserializable:
        pass

    keepsake_id = save_keepsake(b"\xff\xfbaudio", tmp_path / "keepsakes", sidecar={"bad": Unserializable()})
    assert (tmp_path / "keepsakes" / f"{keepsake_id}.mp3").read_bytes() == b"\xff\xfbaudio"
    assert not (tmp_path / "keepsakes" / f"{keepsake_id}.json").exists()


def test_cache_eviction_never_reaches_into_keepsakes(tmp_path):
    """Durability rests on four other modules globbing cache_dir non-recursively.
    That is a property of their call sites, not of this data, so it needs a guard
    here: changing one of them to rglob would otherwise delete every keepsake
    and no test would notice.

    The budget has to be one eviction actually runs under. ``max_size_mb=0``
    returns before the walk, so the keepsake survived a function that never
    looked at anything — the assertion would have held just as well against a
    version that deletes every keepsake it finds.
    """
    import os

    from mammamiradio.playlist.downloader import evict_cache_lru
    from mammamiradio.scheduling.clip import KEEPSAKES_DIRNAME, save_keepsake

    doomed = tmp_path / "norm_song.mp3"
    doomed.write_bytes(b"\xff\xfb" + b"x" * (2 * 1024 * 1024))
    keepsake_id = save_keepsake(b"\xff\xfbkept", tmp_path / KEEPSAKES_DIRNAME)
    kept = tmp_path / KEEPSAKES_DIRNAME / f"{keepsake_id}.mp3"
    # Oldest by atime, so an evictor that could see it would take it first.
    ancient = 1_000_000.0
    os.utime(kept, (ancient, ancient))

    evict_cache_lru(tmp_path, max_size_mb=1)

    assert not doomed.exists(), "eviction did not run, so the guard proved nothing"
    assert kept.exists(), "the cache evictor reached into keepsakes"


def test_extract_segment_audio_returns_only_the_requested_chunks():
    from mammamiradio.scheduling.clip import extract_segment_audio

    buf: deque[bytes] = deque(maxlen=10)
    for marker in (b"\x11", b"\x22", b"\x33"):
        buf.append(marker * 100)

    assert extract_segment_audio(buf, 2) == b"\x22" * 100 + b"\x33" * 100
    assert extract_segment_audio(buf, 0) is None
    assert extract_segment_audio(buf, -5) is None
    assert extract_segment_audio(deque(), 4) is None


def test_extract_segment_audio_clamps_to_what_the_ring_still_holds():
    """A segment longer than the buffer has aged out of its own head. What is
    left is still wholly its own audio, so the count is clamped rather than
    refused."""
    from mammamiradio.scheduling.clip import extract_segment_audio

    buf: deque[bytes] = deque(maxlen=3)
    for _ in range(3):
        buf.append(b"\x44" * 100)

    assert extract_segment_audio(buf, 9_000) == b"\x44" * 300


def test_stale_keepsake_scratch_is_pruned_but_a_write_in_flight_is_not(tmp_path):
    """save_keepsake publishes atomically, so a kill between mkstemp and replace
    leaves scratch rather than a half-published keepsake. Nothing else collects
    it: every cache pruner globs one level, and both API routes glob *.mp3."""
    import os
    import time

    from mammamiradio.scheduling.clip import prune_stale_keepsake_tmp_files

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    orphan = keepsakes / ".keepsake-abc.tmp"
    orphan.write_bytes(b"\xff\xfbhalf")
    ancient = time.time() - 90 * 24 * 3600
    os.utime(orphan, (ancient, ancient))
    in_flight = keepsakes / ".keepsake-xyz.tmp"
    in_flight.write_bytes(b"\xff\xfbwriting")
    kept = keepsakes / "abc123def456.mp3"
    kept.write_bytes(b"\xff\xfbkept")
    os.utime(kept, (ancient, ancient))

    assert prune_stale_keepsake_tmp_files(keepsakes) == 1
    assert not orphan.exists()
    assert in_flight.exists(), "a write in flight was pruned"
    assert kept.exists(), "the pruner reached a published keepsake"


def test_keepsake_scratch_pruning_is_a_no_op_without_a_positive_age(tmp_path):
    """A zero or negative age would prune a write in flight along with the
    orphans. Prune nothing rather than everything."""
    from mammamiradio.scheduling.clip import prune_stale_keepsake_tmp_files

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    (keepsakes / ".keepsake-abc.tmp").write_bytes(b"x")

    assert prune_stale_keepsake_tmp_files(keepsakes, max_age_hours=0) == 0
    assert prune_stale_keepsake_tmp_files(tmp_path / "nope") == 0
    assert (keepsakes / ".keepsake-abc.tmp").exists()


def test_keepsake_scratch_pruning_survives_an_unremovable_file(tmp_path):
    """Best-effort: a read-only mount or a permission problem leaves the scratch
    in place and is logged, rather than raising into startup."""
    import os
    import time
    from unittest.mock import patch

    from mammamiradio.scheduling.clip import prune_stale_keepsake_tmp_files

    keepsakes = tmp_path / "keepsakes"
    keepsakes.mkdir()
    stubborn = keepsakes / ".keepsake-abc.tmp"
    stubborn.write_bytes(b"x")
    ancient = time.time() - 90 * 24 * 3600
    os.utime(stubborn, (ancient, ancient))

    with patch("pathlib.Path.unlink", side_effect=OSError("EROFS")):
        assert prune_stale_keepsake_tmp_files(keepsakes) == 0
    assert stubborn.exists()
