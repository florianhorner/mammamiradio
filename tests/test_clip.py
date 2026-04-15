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


# ── Regression: removed `if not chunks: return None` guard ──────────────────


def test_extract_clip_nonempty_buffer_always_returns_bytes():
    """A non-empty ring buffer must return bytes, never None.

    Regression test for the removed `if not chunks: return None` guard inside
    the loop body of extract_clip. That guard was dead code because
    `if not ring_buffer: return None` at the top already handles the empty case —
    any non-empty ring_buffer produces at least one chunk. This test locks down
    the contract: non-empty buffer → bytes.
    """
    buf: deque[bytes] = deque(maxlen=10)
    buf.append(b"\xaa" * 100)
    result = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    assert isinstance(result, bytes), "Expected bytes from non-empty ring buffer, got None"


def test_extract_clip_single_chunk_smaller_than_requested():
    """Buffer with one chunk smaller than bytes_needed returns that chunk as bytes."""
    buf: deque[bytes] = deque(maxlen=10)
    buf.append(b"\xbb" * 50)  # much smaller than 30s @ 192kbps
    result = extract_clip(buf, duration_seconds=30, bitrate_kbps=192)
    assert result == b"\xbb" * 50


def test_extract_clip_zero_duration_returns_first_chunk():
    """duration_seconds=0 means bytes_needed=0; the loop breaks after collecting
    the first reversed chunk (total=len(chunk) >= 0), so result is one chunk."""
    buf: deque[bytes] = deque(maxlen=10)
    buf.append(b"\x01" * 100)
    buf.append(b"\x02" * 100)
    result = extract_clip(buf, duration_seconds=0, bitrate_kbps=192)
    # bytes_needed=0, loop breaks on first iteration with total >= 0
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_extract_clip_exactly_bytes_needed():
    """When buffer contains exactly bytes_needed, all chunks are returned."""
    bitrate_kbps = 192
    duration_seconds = 5
    bytes_needed = (bitrate_kbps * 1000 // 8) * duration_seconds  # 120000
    buf: deque[bytes] = deque(maxlen=100)
    buf.append(b"\xcc" * bytes_needed)
    result = extract_clip(buf, duration_seconds=duration_seconds, bitrate_kbps=bitrate_kbps)
    assert result == b"\xcc" * bytes_needed


def test_cleanup_old_clips_removes_all_old_keeps_fresh(tmp_path):
    """Removes multiple old clips while keeping fresh ones."""
    import os
    import time

    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()

    fresh = clips_dir / "fresh.mp3"
    fresh.write_bytes(b"\x00" * 100)

    for name in ("old1.mp3", "old2.mp3"):
        old = clips_dir / name
        old.write_bytes(b"\x00" * 100)
        ts = time.time() - 48 * 3600
        os.utime(old, (ts, ts))

    removed = cleanup_old_clips(clips_dir, max_age_hours=24)
    assert removed == 2
    assert fresh.exists()
    assert not (clips_dir / "old1.mp3").exists()
    assert not (clips_dir / "old2.mp3").exists()