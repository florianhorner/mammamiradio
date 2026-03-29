from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from fakeitaliradio.models import Track

logger = logging.getLogger(__name__)


def _run_ffmpeg(cmd: list[str], description: str) -> subprocess.CompletedProcess:
    """Run an ffmpeg command with stderr capture and logging on failure."""
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        logger.error("ffmpeg failed (%s): %s", description, stderr)
        result.check_returncode()  # raises CalledProcessError
    return result


def _generate_placeholder(track: Track, out_path: Path) -> Path:
    """Generate a short placeholder track using ffmpeg tone + TTS announcement."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:duration=30",
        "-af", "volume=0.3",
        "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-f", "mp3", str(out_path),
    ]
    _run_ffmpeg(cmd, f"placeholder for {track.display}")
    logger.info("Generated placeholder for: %s", track.display)
    return out_path


def _find_local(track: Track, music_dir: Path) -> Path | None:
    """Check if a local MP3 exists in the music/ directory."""
    if not music_dir.exists():
        return None
    # Try exact match first, then fuzzy
    for f in music_dir.glob("*.mp3"):
        name = f.stem.lower()
        if track.cache_key in name or track.title.lower() in name:
            return f
    return None


def _download_ytdlp(track: Track, cache_dir: Path) -> Path:
    import yt_dlp

    query = f"ytsearch1:{track.artist} {track.title} official audio"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(cache_dir / f"{track.cache_key}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([query])

    out_path = cache_dir / f"{track.cache_key}.mp3"
    if not out_path.exists():
        raise FileNotFoundError(f"Download failed for {track.display}")
    return out_path


def _download_sync(track: Track, cache_dir: Path, music_dir: Path) -> Path:
    out_path = cache_dir / f"{track.cache_key}.mp3"
    if out_path.exists():
        logger.info("Cache hit: %s", track.display)
        return out_path

    # 1. Check local music/ directory
    local = _find_local(track, music_dir)
    if local:
        logger.info("Local file: %s -> %s", track.display, local)
        return local

    # 2. Try yt-dlp
    try:
        return _download_ytdlp(track, cache_dir)
    except Exception as e:
        logger.warning("yt-dlp failed for %s: %s — using placeholder", track.display, e)

    # 3. Fallback: generate placeholder audio
    return _generate_placeholder(track, out_path)


async def download_track(track: Track, cache_dir: Path, music_dir: Path | None = None) -> Path:
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path("music")
    return await loop.run_in_executor(None, _download_sync, track, cache_dir, _music_dir)
