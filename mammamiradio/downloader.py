"""Track acquisition helpers: local files, yt-dlp, and placeholder fallback."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from mammamiradio.models import Track
from mammamiradio.normalizer import _run_ffmpeg

logger = logging.getLogger(__name__)

# Files that must never be evicted from the cache directory
_CACHE_PROTECTED = {"mammamiradio.db", "playlist_source.json", "session_stopped.flag"}


def evict_cache_lru(cache_dir: Path, max_size_mb: int) -> None:
    """Delete oldest MP3s from cache_dir until total size is under max_size_mb.

    Only .mp3 files are evicted. The SQLite database, playlist source JSON, and
    session flag are always preserved.
    """
    if max_size_mb <= 0:
        return

    mp3_files = sorted(
        [f for f in cache_dir.glob("*.mp3") if f.name not in _CACHE_PROTECTED],
        key=lambda f: f.stat().st_atime,  # oldest access time first
    )

    total_bytes = sum(f.stat().st_size for f in mp3_files)
    max_bytes = max_size_mb * 1024 * 1024
    evicted = 0

    for f in mp3_files:
        if total_bytes <= max_bytes:
            break
        size = f.stat().st_size
        try:
            f.unlink()
            total_bytes -= size
            evicted += 1
        except OSError as exc:
            logger.warning("Cache eviction failed for %s: %s", f.name, exc)

    if evicted:
        logger.info(
            "Cache eviction: removed %d file(s), %.1f MB remaining",
            evicted,
            total_bytes / (1024 * 1024),
        )


_DEMO_ASSETS_DIR = Path(__file__).parent / "demo_assets" / "music"


def _find_demo_asset(track: Track) -> Path | None:
    """Check bundled demo_assets/music/ for a matching MP3."""
    if not _DEMO_ASSETS_DIR.exists():
        return None
    for f in _DEMO_ASSETS_DIR.glob("*.mp3"):
        name = f.stem.lower()
        if track.cache_key in name or track.title.lower() in name:
            return f
    return None


def _generate_silence(track: Track, out_path: Path) -> Path:
    """Generate silence as last-resort fallback (no sine wave tone)."""
    # Keep fallback audio above the producer's minimum music-duration gate so
    # demo/degraded boot can still fill the queue instead of looping forever.
    duration_s = max(35, int((track.duration_ms or 0) / 1000) if track.duration_ms else 0)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        str(duration_s),
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(out_path),
    ]
    _run_ffmpeg(cmd, f"silence for {track.display}")
    logger.info("Generated silence placeholder for: %s", track.display)
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
    """Download the best-effort public audio match for a track via yt-dlp."""
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
        "noprogress": True,
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([query])

    out_path = cache_dir / f"{track.cache_key}.mp3"
    if not out_path.exists():
        raise FileNotFoundError(f"Download failed for {track.display}")
    return out_path


def _download_sync(track: Track, cache_dir: Path, music_dir: Path) -> Path:
    """Resolve a track from cache, local files, yt-dlp, or a placeholder tone."""
    out_path = cache_dir / f"{track.cache_key}.mp3"
    if out_path.exists():
        logger.info("Cache hit: %s", track.display)
        return out_path

    # 1. Check bundled demo assets
    demo = _find_demo_asset(track)
    if demo:
        logger.info("Demo asset: %s -> %s", track.display, demo)
        return demo

    # 2. Check local music/ directory
    local = _find_local(track, music_dir)
    if local:
        logger.info("Local file: %s -> %s", track.display, local)
        return local

    # 3. Try yt-dlp (opt-in only, disabled by default for copyright safety)
    if os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes"):
        try:
            return _download_ytdlp(track, cache_dir)
        except Exception as e:
            logger.warning("yt-dlp failed for %s: %s — using silence", track.display, e)
    else:
        logger.info("yt-dlp disabled for %s (set MAMMAMIRADIO_ALLOW_YTDLP=true to enable)", track.display)

    # 4. Fallback: brief silence (never a sine wave tone)
    return _generate_silence(track, out_path)


async def download_track(track: Track, cache_dir: Path, music_dir: Path | None = None) -> Path:
    """Run the synchronous download fallback chain off the event loop."""
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path("music")
    return await loop.run_in_executor(None, _download_sync, track, cache_dir, _music_dir)
