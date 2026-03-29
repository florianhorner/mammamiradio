"""
Spotify audio backend using go-librespot.

go-librespot writes raw PCM (44100Hz, S16LE, stereo) to a named pipe (FIFO).
Playback is controlled via its HTTP API on port 3678.

Flow:
1. Create FIFO at /tmp/fakeitaliradio.pcm
2. Start go-librespot subprocess (reads config with pipe_path)
3. User selects "fakeitaliradio" in Spotify app (Zeroconf)
4. We call API to play tracks, read PCM from FIFO, encode to MP3
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

import httpx

from fakeitaliradio.config import StationConfig
from fakeitaliradio.models import Track

logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_SIZE = 2  # 16-bit
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_SIZE


class SpotifyPlayer:
    """Manages go-librespot subprocess and HTTP API for playback control."""

    def __init__(self, config: StationConfig):
        self.config = config
        self._process: subprocess.Popen | None = None
        self._authenticated = False
        self._config_dir = Path("go-librespot")
        self._fifo_fd: int | None = None
        self._log_file = None
        self._fifo_path = Path(config.audio.fifo_path)
        self._api_base = f"http://127.0.0.1:{config.audio.go_librespot_port}"

    def _ensure_fifo(self) -> None:
        """Create the named pipe if it doesn't exist."""
        if self._fifo_path.exists():
            if not self._fifo_path.is_fifo():
                self._fifo_path.unlink()
                os.mkfifo(str(self._fifo_path))
        else:
            os.mkfifo(str(self._fifo_path))
        logger.info("FIFO ready: %s", self._fifo_path)

    def start(self) -> None:
        """Start go-librespot with pipe backend."""
        if self._process and self._process.poll() is None:
            return

        self._ensure_fifo()

        cmd = [
            self.config.audio.go_librespot_bin,
            "--config_dir", str(self._config_dir),
        ]

        logger.info("Starting go-librespot: %s", " ".join(cmd))
        self._log_file = open(self.config.tmp_dir / "go-librespot.log", "w")
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
        )
        logger.info("go-librespot started (PID %d)", self._process.pid)

    def stop(self) -> None:
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    async def wait_for_auth(self, timeout: float = 120.0) -> bool:
        """Wait for a user to connect to our device via Spotify Connect."""
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await client.get(f"{self._api_base}/status")
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("username"):
                            self._authenticated = True
                            logger.info("Authenticated as: %s", data["username"])
                            return True
                except Exception:
                    pass
                await asyncio.sleep(2.0)

        logger.warning("No user connected within %.0fs", timeout)
        return False

    async def play_track(self, track: Track) -> None:
        """Start playing a track via go-librespot API."""
        uri = f"spotify:track:{track.spotify_id}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._api_base}/player/play",
                json={"uri": uri},
            )
            if resp.status_code in (200, 204):
                logger.info("Playing via Spotify: %s", track.display)
            else:
                logger.error("Play failed (%d): %s", resp.status_code, resp.text)
                raise RuntimeError(f"go-librespot play failed: {resp.status_code}")

    async def pause(self) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(f"{self._api_base}/player/pause")

    async def capture_track_audio(
        self, track: Track, output_path: Path, max_duration_sec: float = 300
    ) -> Path:
        """
        Play a track via Spotify, read PCM from the FIFO, encode to MP3.

        IMPORTANT: Start ffmpeg reader BEFORE play_track() — go-librespot
        needs a reader on the FIFO or it gets ENXIO on macOS.
        """
        track_duration_sec = min(track.duration_ms / 1000.0, max_duration_sec)

        loop = asyncio.get_running_loop()

        # Step 1: Start ffmpeg reader (blocks on FIFO open until writer connects)
        def _start_reader():
            cmd = [
                "ffmpeg", "-y",
                "-f", "s16le",
                "-ar", str(SAMPLE_RATE),
                "-ac", str(CHANNELS),
                "-t", str(track_duration_sec),
                "-i", str(self._fifo_path),
                "-filter:a", "loudnorm=I=-16:LRA=11:TP=-1.5",
                "-ar", str(self.config.audio.sample_rate),
                "-ac", str(self.config.audio.channels),
                "-b:a", f"{self.config.audio.bitrate}k",
                "-f", "mp3", str(output_path),
            ]
            return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        ffmpeg_proc = await loop.run_in_executor(None, _start_reader)
        await asyncio.sleep(0.3)

        # Step 2: Tell Spotify to play — go-librespot writes PCM to FIFO
        await self.play_track(track)

        # Step 3: Wait for ffmpeg to finish encoding the full track
        def _wait_for_encode():
            try:
                ffmpeg_proc.wait(timeout=track_duration_sec + 30)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait()

            if not output_path.exists() or output_path.stat().st_size < 1000:
                stderr = ffmpeg_proc.stderr.read().decode(errors="replace")[-300:]
                raise RuntimeError(f"Capture failed: {stderr}")

            size = output_path.stat().st_size
            logger.info("Captured: %s (%.0fs, %.1fMB)", track.display, track_duration_sec, size / 1e6)

        await loop.run_in_executor(None, _wait_for_encode)
        return output_path


async def download_track_spotify(
    player: SpotifyPlayer, track: Track, output_path: Path
) -> Path:
    """High-level: capture a track's audio from Spotify via go-librespot."""
    return await player.capture_track_audio(track, output_path)
