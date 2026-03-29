from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakeitaliradio.config import (
    AdsSection,
    AudioSection,
    PacingSection,
    PlaylistSection,
    StationConfig,
    StationSection,
)
from fakeitaliradio.models import HostPersonality, Track
from fakeitaliradio.spotify_player import SpotifyPlayer


def _test_config(tmp_path: Path) -> StationConfig:
    tmp_dir = tmp_path / "tmp"
    cache_dir = tmp_path / "cache"
    tmp_dir.mkdir()
    cache_dir.mkdir()
    return StationConfig(
        station=StationSection(name="Test Radio", language="it", theme="test"),
        playlist=PlaylistSection(),
        pacing=PacingSection(),
        hosts=[HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="warm")],
        ads=AdsSection(brands=[], voices=[], sfx_dir="sfx"),
        audio=AudioSection(),
        cache_dir=cache_dir,
        tmp_dir=tmp_dir,
    )


def test_start_closes_log_file_when_popen_fails(tmp_path, monkeypatch):
    config = _test_config(tmp_path)
    player = SpotifyPlayer(config)

    monkeypatch.setattr(SpotifyPlayer, "_ensure_fifo", lambda self: None)
    monkeypatch.setattr(SpotifyPlayer, "_drain_fifo", lambda self: None)
    monkeypatch.setattr(SpotifyPlayer, "_is_golibrespot_running", lambda self: False)

    def _fail_popen(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("fakeitaliradio.spotify_player.subprocess.Popen", _fail_popen)

    with pytest.raises(RuntimeError, match="boom"):
        player.start()

    assert player._log_file is None
    player.stop()


def test_capture_closes_stdin_before_wait_and_clears_sink(tmp_path, monkeypatch):
    config = _test_config(tmp_path)
    player = SpotifyPlayer(config)
    output_path = tmp_path / "captured.mp3"

    class FakeStdin:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeStderr:
        def read(self):
            return b""

    class FakeProc:
        def __init__(self, path: Path):
            self.stdin = FakeStdin()
            self.stderr = FakeStderr()
            self._path = path
            self.wait_saw_closed_stdin = False
            self._killed = False

        def wait(self, timeout=None):
            self.wait_saw_closed_stdin = self.stdin.closed
            self._path.write_bytes(b"1" * 2048)
            return 0

        def kill(self):
            self._killed = True

        def poll(self):
            if self._killed:
                return -9
            return 0 if self.wait_saw_closed_stdin else None

    holder: dict[str, FakeProc] = {}

    def _fake_popen(cmd, stdin=None, stdout=None, stderr=None):
        proc = FakeProc(Path(cmd[-1]))
        holder["proc"] = proc
        return proc

    async def _fake_play_track(self, track: Track) -> None:
        return None

    monkeypatch.setattr("fakeitaliradio.spotify_player.subprocess.Popen", _fake_popen)
    monkeypatch.setattr(SpotifyPlayer, "play_track", _fake_play_track)

    track = Track(
        title="Test",
        artist="Artist",
        duration_ms=10,
        spotify_id="spotify123",
    )

    result = asyncio.run(
        player.capture_track_audio(track, output_path, max_duration_sec=1)
    )

    assert result == output_path
    assert holder["proc"].wait_saw_closed_stdin is True
    assert player._capture_sink is None
