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
