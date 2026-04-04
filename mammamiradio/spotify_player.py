"""
Spotify audio backend using go-librespot.

go-librespot writes raw PCM (44100Hz, S16LE, stereo) to a named pipe (FIFO).

The key insight: go-librespot needs a reader on the FIFO AT ALL TIMES or
it gets ENXIO ("device not configured") on macOS and skips every track.

Solution: a persistent background thread drains the FIFO continuously.
When we want to capture a track, we redirect the drain to ffmpeg's stdin.
When we don't, we discard the data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import httpx

from mammamiradio.config import StationConfig
from mammamiradio.go_librespot_config import load_go_librespot_device_name
from mammamiradio.go_librespot_runtime import build_go_librespot_runtime, read_owned_pid
from mammamiradio.models import Track

GO_LIBRESPOT_CANDIDATES = (
    "/opt/homebrew/bin/go-librespot",
    "/usr/local/bin/go-librespot",
    "/usr/bin/go-librespot",
    "/bin/go-librespot",
)


def resolve_go_librespot_bin(configured: str) -> str | None:
    """Find a working go-librespot binary even when PATH is sparse."""
    if os.path.isabs(configured) and os.access(configured, os.X_OK):
        return configured

    discovered = shutil.which(configured)
    if discovered:
        return discovered

    for candidate in GO_LIBRESPOT_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate

    return None


logger = logging.getLogger(__name__)

SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_SIZE = 2  # 16-bit
BYTES_PER_SEC = SAMPLE_RATE * CHANNELS * SAMPLE_SIZE


class SpotifyPlayer:
    """Manage go-librespot, persistent FIFO draining, and track capture."""

    def __init__(self, config: StationConfig):
        self.config = config
        self._process: subprocess.Popen | None = None
        self._authenticated = False
        self._config_dir = Path(config.audio.go_librespot_config_dir)
        self._device_name = load_go_librespot_device_name(self._config_dir)
        self._log_file = None
        self._fifo_path = Path(config.audio.fifo_path)
        self._drain_pid_file = config.tmp_dir / "fifo-drain.pid"
        self._api_base = f"http://127.0.0.1:{config.audio.go_librespot_port}"
        self._go_librespot_runtime = build_go_librespot_runtime(
            go_librespot_bin=config.audio.go_librespot_bin,
            config_dir=self._config_dir,
            fifo_path=self._fifo_path,
            port=config.audio.go_librespot_port,
            tmp_dir=config.tmp_dir,
        )

        # Persistent FIFO drain
        self._drain_thread: threading.Thread | None = None
        self._drain_running = False
        self._capture_sink: subprocess.Popen | None = None  # ffmpeg stdin
        self._capture_lock = threading.Lock()
        self._transfer_counter = 0

    @property
    def device_name(self) -> str:
        return self._device_name

    def _ensure_fifo(self) -> None:
        """Create or repair the PCM FIFO used by go-librespot output."""
        if self._fifo_path.exists():
            if not self._fifo_path.is_fifo():
                self._fifo_path.unlink()
                os.mkfifo(str(self._fifo_path))
        else:
            os.mkfifo(str(self._fifo_path))
        logger.info("FIFO ready: %s", self._fifo_path)

    def _drain_fifo(self) -> None:
        """Background thread: always read from FIFO so go-librespot never gets ENXIO."""
        logger.info("FIFO drain thread started")
        while self._drain_running:
            try:
                # Open FIFO for reading (O_RDONLY | O_NONBLOCK avoids blocking
                # when no writer is connected yet)
                fd = os.open(str(self._fifo_path), os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                logger.error("FIFO open failed: %s", e)
                if not self._drain_running:
                    break
                import time

                time.sleep(1)
                continue

            try:
                while self._drain_running:
                    # Wait for data (1s timeout so we can check _drain_running)
                    readable, _, _ = select.select([fd], [], [], 1.0)
                    if not readable:
                        continue

                    data = os.read(fd, 65536)
                    if not data:
                        # Writer closed (track ended or go-librespot restarted)
                        break

                    # Forward to capture sink if active
                    with self._capture_lock:
                        if self._capture_sink and self._capture_sink.stdin:
                            try:
                                self._capture_sink.stdin.write(data)
                            except (BrokenPipeError, OSError):
                                # ffmpeg finished, stop forwarding
                                self._capture_sink = None
            except OSError as e:
                if self._drain_running:
                    logger.warning("FIFO read error: %s (reopening)", e)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

        logger.info("FIFO drain thread stopped")

    def _is_golibrespot_running(self) -> bool:
        """Check whether start.sh already owns a compatible go-librespot process."""
        pid = read_owned_pid(
            self._go_librespot_runtime.state_file,
            self._go_librespot_runtime.fingerprint,
        )
        if pid is None:
            return False
        self._external_pid = pid
        return True

    def _read_fallback_drain_pid(self) -> int | None:
        try:
            return int(self._drain_pid_file.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _find_fallback_drain_pids(self) -> list[int]:
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"cat .*{self._fifo_path}"],
                capture_output=True,
                text=True,
            )
        except Exception:
            return []

        if result.returncode != 0:
            return []

        pids = []
        for line in result.stdout.splitlines():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                continue
        return pids

    def _is_fallback_drain_pid(self, pid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
            )
        except Exception:
            return False

        if result.returncode != 0:
            return False

        cmd = result.stdout.strip()
        return "cat" in cmd and str(self._fifo_path) in cmd

    def _start_fallback_drain(self) -> None:
        pid = self._read_fallback_drain_pid()
        if pid and self._is_fallback_drain_pid(pid):
            return

        for legacy_pid in self._find_fallback_drain_pids():
            if self._is_fallback_drain_pid(legacy_pid):
                self._drain_pid_file.write_text(str(legacy_pid))
                return

        proc = subprocess.Popen(
            ["cat", str(self._fifo_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._drain_pid_file.write_text(str(proc.pid))
        logger.info("Started fallback FIFO drain (PID %d)", proc.pid)

    def _stop_fallback_drain(self) -> None:
        pids = []
        pid = self._read_fallback_drain_pid()
        if pid:
            pids.append(pid)
        pids.extend(self._find_fallback_drain_pids())

        seen = set()
        stopped = []
        for pid in pids:
            if pid in seen or not self._is_fallback_drain_pid(pid):
                seen.add(pid)
                continue

            seen.add(pid)
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 3
                while time.time() < deadline:
                    if not self._is_fallback_drain_pid(pid):
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stopped.append(pid)

        self._drain_pid_file.unlink(missing_ok=True)
        for pid in stopped:
            logger.info("Stopped fallback FIFO drain (PID %d)", pid)

    def start(self) -> None:
        """Start FIFO drainage and attach to or launch go-librespot."""
        if self._process and self._process.poll() is None:
            return

        self._ensure_fifo()

        # Start the persistent FIFO drain BEFORE go-librespot
        self._drain_running = True
        self._drain_thread = threading.Thread(target=self._drain_fifo, daemon=True)
        self._drain_thread.start()
        self._stop_fallback_drain()

        # Check if go-librespot is already running (started by start.sh)
        if self._is_golibrespot_running():
            logger.info("go-librespot already running externally — attaching")
            self._external = True
            return

        self._external = False
        cmd = [
            self._resolve_go_librespot_bin(),
            "--config_dir",
            str(self._config_dir),
        ]

        logger.info("Starting go-librespot: %s", " ".join(cmd))
        self._log_file = open(self.config.tmp_dir / "go-librespot.log", "w")  # type: ignore[assignment]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
        )
        logger.info("go-librespot started (PID %d)", self._process.pid)

    def _resolve_go_librespot_bin(self) -> str:
        """Find a working go-librespot binary even when PATH is stripped down."""
        configured = self.config.audio.go_librespot_bin
        resolved = resolve_go_librespot_bin(configured)
        if resolved:
            if resolved != configured:
                logger.info("Resolved go-librespot outside configured PATH: %s", resolved)
            return resolved
        raise FileNotFoundError(f"go-librespot binary not found: {configured}")

    def stop(self) -> None:
        """Stop capture helpers and terminate go-librespot if we launched it."""
        if getattr(self, "_external", False):
            # Keep a single reader alive across uvicorn reloads, then let the
            # next app process reclaim ownership on startup.
            self._start_fallback_drain()
        self._drain_running = False
        if self._drain_thread:
            self._drain_thread.join(timeout=3)
            self._drain_thread = None
        # Only kill go-librespot if WE started it (not if external)
        if self._process and not getattr(self, "_external", False):
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    async def check_auth(self) -> bool:
        """Quick single check if Spotify user is connected."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._api_base}/status", timeout=2.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("username"):
                        if not self._authenticated:
                            logger.info("Spotify connected: %s", data["username"])
                            self._authenticated = True
                        return True
        except Exception:
            pass

        if self._authenticated:
            logger.info("Spotify disconnected from mammamiradio")
        self._authenticated = False

        # Periodically try auto-transfer (every ~30 checks = ~15s)
        self._transfer_counter += 1
        if self._transfer_counter % 30 == 1:
            await self._try_transfer_playback()

        return False

    async def _try_transfer_playback(self) -> None:
        """Use Spotify Web API to transfer playback to our device."""
        if not self.config.spotify_client_id:
            return  # Web API needs client_id; skip silently
        try:
            from mammamiradio.spotify_auth import get_spotify_client

            sp = get_spotify_client(self.config)

            devices = sp.devices()
            our_device = None
            device_names = []
            for d in devices.get("devices", []):
                device_names.append(d.get("name", "?"))
                if d.get("name") == self._device_name:
                    our_device = d
                    break

            if our_device:
                sp.transfer_playback(our_device["id"], force_play=False)
                logger.info("Auto-transferred playback to %s (device %s)", self._device_name, our_device["id"])
                # Wait a moment for go-librespot to register the connection
                await asyncio.sleep(2)
                self._authenticated = True
            else:
                logger.info(
                    "%s not in Spotify devices yet (visible: %s). Select it manually in Spotify app.",
                    self._device_name,
                    ", ".join(device_names) or "none",
                )

        except Exception as e:
            logger.warning("Auto-transfer failed: %s", e)

    async def wait_for_auth(self, timeout: float = 120.0) -> bool:
        """Poll until a Spotify user connects or the timeout expires."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self.check_auth():
                return True
            await asyncio.sleep(2.0)

        logger.warning("No user connected within %.0fs", timeout)
        return False

    async def get_current_track(self) -> Track | None:
        """Read what go-librespot is currently playing, if anything."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._api_base}/status", timeout=2.0)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                t = data.get("track")
                if not t or not t.get("uri"):
                    return None
                # Extract Spotify ID from URI (spotify:track:XXXX)
                uri = t["uri"]
                spotify_id = uri.split(":")[-1] if uri.startswith("spotify:track:") else None
                if not spotify_id:
                    return None
                artist = t.get("artist_names", ["Unknown"])[0] if t.get("artist_names") else "Unknown"
                track = Track(
                    title=t.get("name", "Unknown"),
                    artist=artist,
                    duration_ms=t.get("duration", 0),
                    spotify_id=spotify_id,
                )
                track.position_ms = t.get("position", 0)
                return track
        except Exception:
            return None

    async def capture_current_audio(self, track: Track, output_path: Path) -> Path:
        """Capture audio already playing on go-librespot (no play_track call).

        Used for autoplay: the user's current song continues, we just capture it.
        """
        remaining_ms = track.duration_ms - track.position_ms
        remaining_sec = max(remaining_ms / 1000.0, 5.0)  # at least 5s
        remaining_sec = min(remaining_sec, 300.0)

        loop = asyncio.get_running_loop()

        def _start_ffmpeg():
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "-t",
                str(remaining_sec),
                "-i",
                "pipe:0",
                "-filter:a",
                "afade=t=in:d=0.3,loudnorm=I=-16:LRA=11:TP=-1.5",
                "-ar",
                str(self.config.audio.sample_rate),
                "-ac",
                str(self.config.audio.channels),
                "-b:a",
                f"{self.config.audio.bitrate}k",
                "-f",
                "mp3",
                str(output_path),
            ]
            return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        ffmpeg_proc = await loop.run_in_executor(None, _start_ffmpeg)

        with self._capture_lock:
            self._capture_sink = ffmpeg_proc

        # Do NOT call play_track — song is already playing

        def _wait_for_encode():
            try:
                ffmpeg_proc.wait(timeout=remaining_sec + 30)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait()
            with self._capture_lock:
                self._capture_sink = None
            if ffmpeg_proc.stdin:
                try:
                    ffmpeg_proc.stdin.close()
                except OSError:
                    pass
            if not output_path.exists() or output_path.stat().st_size < 1000:
                stderr_text = ""
                if ffmpeg_proc.stderr:
                    stderr_text = ffmpeg_proc.stderr.read().decode(errors="replace")[-300:]
                raise RuntimeError(f"Autoplay capture failed: {stderr_text}")
            logger.info("Autoplay captured: %s (%.0fs remaining)", track.display, remaining_sec)

        await loop.run_in_executor(None, _wait_for_encode)
        return output_path

    async def play_track(self, track: Track) -> None:
        """Ask go-librespot to start playback for one Spotify track URI."""
        uri = f"spotify:track:{track.spotify_id}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._api_base}/player/play",
                json={"uri": uri},
                timeout=10.0,
            )
            if resp.status_code in (200, 204):
                logger.info("Playing via Spotify: %s", track.display)
            else:
                logger.error("Play failed (%d): %s", resp.status_code, resp.text)
                raise RuntimeError(f"go-librespot play failed: {resp.status_code}")

    async def pause(self) -> None:
        """Pause the currently playing Spotify track, if any."""
        async with httpx.AsyncClient() as client:
            await client.post(f"{self._api_base}/player/pause")

    async def capture_track_audio(self, track: Track, output_path: Path, max_duration_sec: float = 300) -> Path:
        """
        Play a track via Spotify, capture PCM from the drain thread, encode to MP3.

        The drain thread is always reading from the FIFO. We just redirect its
        output to ffmpeg's stdin for the duration of the track.
        """
        track_duration_sec = min(track.duration_ms / 1000.0, max_duration_sec)
        loop = asyncio.get_running_loop()

        # Start ffmpeg reading from stdin (pipe)
        def _start_ffmpeg():
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "s16le",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                str(CHANNELS),
                "-t",
                str(track_duration_sec),
                "-i",
                "pipe:0",
                "-filter:a",
                "loudnorm=I=-16:LRA=11:TP=-1.5",
                "-ar",
                str(self.config.audio.sample_rate),
                "-ac",
                str(self.config.audio.channels),
                "-b:a",
                f"{self.config.audio.bitrate}k",
                "-f",
                "mp3",
                str(output_path),
            ]
            return subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        ffmpeg_proc = await loop.run_in_executor(None, _start_ffmpeg)

        # Redirect drain thread output to ffmpeg
        with self._capture_lock:
            self._capture_sink = ffmpeg_proc

        # Tell Spotify to play
        await self.play_track(track)

        # Wait for ffmpeg to finish (it has a -t duration limit)
        def _wait_for_encode():
            try:
                ffmpeg_proc.wait(timeout=track_duration_sec + 30)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
                ffmpeg_proc.wait()

            # Stop redirecting
            with self._capture_lock:
                self._capture_sink = None

            # Close ffmpeg stdin if still open
            if ffmpeg_proc.stdin:
                try:
                    ffmpeg_proc.stdin.close()
                except OSError:
                    pass

            if not output_path.exists() or output_path.stat().st_size < 1000:
                stderr_text = ""
                if ffmpeg_proc.stderr:
                    stderr_text = ffmpeg_proc.stderr.read().decode(errors="replace")[-300:]
                raise RuntimeError(f"Capture failed: {stderr_text}")

            size = output_path.stat().st_size
            logger.info(
                "Captured: %s (%.0fs, %.1fMB)",
                track.display,
                track_duration_sec,
                size / 1e6,
            )

        await loop.run_in_executor(None, _wait_for_encode)
        return output_path


async def download_track_spotify(player: SpotifyPlayer, track: Track, output_path: Path) -> Path:
    """Capture a Spotify-backed track into a normalized MP3 segment."""
    return await player.capture_track_audio(track, output_path)
