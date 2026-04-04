"""Track acquisition helpers used when Spotify capture is unavailable."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mammamiradio.models import Track
from mammamiradio.normalizer import _run_ffmpeg

logger = logging.getLogger(__name__)


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
    """Generate brief silence as last-resort fallback (no sine wave tone)."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        "5",
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

    # 3. Try yt-dlp
    try:
        return _download_ytdlp(track, cache_dir)
    except Exception as e:
        logger.warning("yt-dlp failed for %s: %s — using silence", track.display, e)

    # 4. Fallback: brief silence (never a sine wave tone)
    return _generate_silence(track, out_path)


async def download_track(track: Track, cache_dir: Path, music_dir: Path | None = None) -> Path:
    """Run the synchronous download fallback chain off the event loop."""
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path("music")
    return await loop.run_in_executor(None, _download_sync, track, cache_dir, _music_dir)
