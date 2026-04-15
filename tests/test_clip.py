"""Tests for the clip extraction and sharing module."""

from collections import deque

from mammamiradio.clip import cleanup_old_clips, extract_clip, save_clip


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
# Behavioral change: removed `if not chunks: return None` guard after the loop
# ---------------------------------------------------------------------------


def test_extract_clip_single_chunk_returns_bytes_not_none():
    """A non-empty ring buffer always returns bytes, never None.

    After removing the `if not chunks: return None` guard that followed the
    collection loop, any non-empty buffer must return bytes.  The first guard
    (`if not ring_buffer: return None`) still covers the empty-buffer case.
    """
    buf: deque[bytes] = deque(maxlen=10)
    buf.append(b"\xAA" * 500)
    result = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    # Must be bytes, not None — the only None path left is the empty-buffer check.
    assert isinstance(result, bytes)
    assert result == b"\xAA" * 500


def test_extract_clip_multiple_small_chunks_below_requested_duration():
    """When total buffered bytes < bytes_needed, all chunks are returned as bytes.

    Verifies that the absence of the removed `if not chunks` guard does not
    affect the "short buffer" path: the loop runs to completion and all chunks
    are joined correctly.
    """
    buf: deque[bytes] = deque(maxlen=100)
    # Two small chunks, well below 30s @ 192kbps (720000 bytes)
    buf.append(b"\x01" * 1000)
    buf.append(b"\x02" * 2000)
    result = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    assert result is not None
    assert isinstance(result, bytes)
    # Should contain both chunks in order (oldest first after reverse)
    assert result == b"\x01" * 1000 + b"\x02" * 2000


def test_extract_clip_exact_bytes_needed():
    """When the buffer holds exactly bytes_needed, only those chunks are returned."""
    bitrate_kbps = 192
    duration_seconds = 5
    bytes_needed = (bitrate_kbps * 1000 // 8) * duration_seconds  # 120000

    buf: deque[bytes] = deque(maxlen=100)
    # Three equal chunks that together equal exactly bytes_needed
    chunk_size = bytes_needed // 3  # 40000
    buf.append(b"\x0A" * chunk_size)
    buf.append(b"\x0B" * chunk_size)
    buf.append(b"\x0C" * chunk_size)

    result = extract_clip(buf, duration_seconds=duration_seconds, bitrate_kbps=bitrate_kbps)
    assert result is not None
    # All three chunks returned in original order (oldest first)
    assert result == b"\x0A" * chunk_size + b"\x0B" * chunk_size + b"\x0C" * chunk_size