"""Track acquisition helpers: local files, yt-dlp, and placeholder fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

from mammamiradio.models import Track
from mammamiradio.normalizer import _run_ffmpeg

logger = logging.getLogger(__name__)

# Files that must never be evicted from the cache directory
_CACHE_PROTECTED = {"mammamiradio.db", "playlist_source.json", "session_stopped.flag"}
_TRUTHY = ("true", "1", "yes")


def validate_download(filepath: Path) -> tuple[bool, str]:
    """Quickly reject partial/corrupt downloads before expensive normalization."""
    min_size_bytes = 500 * 1024
    try:
        size = filepath.stat().st_size
    except OSError as exc:
        return False, f"stat failed: {exc}"

    if size < min_size_bytes:
        return False, f"file too small ({size} bytes < {min_size_bytes})"

    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(filepath)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "ffprobe timed out"
    except OSError as exc:
        return False, f"ffprobe failed to start: {exc}"
    if result.returncode != 0:
        return False, "ffprobe failed"

    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, "ffprobe returned invalid JSON"

    duration_raw = (info.get("format") or {}).get("duration")
    if duration_raw is None:
        return False, "missing duration"
    try:
        duration_s = float(duration_raw)
    except (TypeError, ValueError):
        return False, f"invalid duration: {duration_raw!r}"
    if duration_s < 30:
        return False, f"duration too short ({duration_s:.1f}s)"

    return True, "ok"


def purge_suspect_cache_files(cache_dir: Path, min_size_bytes: int = 10240) -> int:
    """Delete cached files smaller than *min_size_bytes* (likely failed downloads).

    A failed yt-dlp run can cache a silence placeholder that's only a few KB.
    Subsequent boots serve silence from cache without re-downloading.  Purging
    these on startup forces a fresh download.
    """
    if not cache_dir.is_dir():
        return 0
    purged = 0
    for f in cache_dir.glob("*.mp3"):
        if f.name in _CACHE_PROTECTED:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size < min_size_bytes:
            logger.warning("Purging suspect cache file (too small): %s (%d bytes)", f.name, size)
            f.unlink(missing_ok=True)
            purged += 1
    return purged


def evict_cache_lru(cache_dir: Path, max_size_mb: int) -> None:
    """Delete oldest MP3s from cache_dir until total size is under max_size_mb.

    Only .mp3 files are evicted. The SQLite database, playlist source JSON, and
    session flag are always preserved.
    """
    if max_size_mb <= 0:
        return

    # Evict regular cache files first, then norm_ files if still over budget.
    regular = []
    norm = []
    for f in cache_dir.glob("*.mp3"):
        if f.name in _CACHE_PROTECTED:
            continue
        if f.name.startswith("norm_"):
            norm.append(f)
        else:
            regular.append(f)
    regular.sort(key=lambda f: f.stat().st_atime)
    norm.sort(key=lambda f: f.stat().st_atime)
    mp3_files = regular + norm

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

# Cached directory listings to avoid repeated glob() on every track lookup.
# Demo assets never change at runtime; local music rarely does.
_demo_files_cache: tuple[str, list[Path]] | None = None
_local_files_cache: dict[str, tuple[float, list[Path]]] = {}
_LOCAL_FILES_TTL = 60.0  # seconds


def _find_demo_asset(track: Track) -> Path | None:
    """Check bundled demo_assets/music/ for a matching MP3."""
    global _demo_files_cache
    cache_key = str(_DEMO_ASSETS_DIR)
    if _demo_files_cache is None or _demo_files_cache[0] != cache_key:
        if not _DEMO_ASSETS_DIR.exists():
            _demo_files_cache = (cache_key, [])
        else:
            _demo_files_cache = (cache_key, list(_DEMO_ASSETS_DIR.glob("*.mp3")))
    for f in _demo_files_cache[1]:
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
    import time as _time

    key = str(music_dir)
    cached = _local_files_cache.get(key)
    if cached and (_time.time() - cached[0]) < _LOCAL_FILES_TTL:
        files = cached[1]
    else:
        files = list(music_dir.glob("*.mp3"))
        _local_files_cache[key] = (_time.time(), files)
    for f in files:
        name = f.stem.lower()
        if track.cache_key in name or track.title.lower() in name:
            return f
    return None


def _download_ytdlp(track: Track, cache_dir: Path) -> Path:
    """Download the best-effort public audio match for a track via yt-dlp."""
    import yt_dlp

    # Use the exact video ID when available to download the chosen upload,
    # not a fresh text-search result that might return a different version.
    if track.youtube_id:
        query = f"https://www.youtube.com/watch?v={track.youtube_id}"
    else:
        query = f"ytsearch1:{track.artist} {track.title} official audio"
    ytdlp_tmp = cache_dir / ".ytdlp_tmp" / track.cache_key
    ytdlp_tmp.mkdir(parents=True, exist_ok=True)
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
        "abort_on_unavailable_fragments": True,
        "throttled_rate": 100_000,  # re-extract URLs if speed drops below 100 KB/s
        "check_formats": True,  # verify formats are downloadable before selecting
        "concurrent_fragment_downloads": 2,  # parallel fragment downloads
        "paths": {"temp": str(ytdlp_tmp)},  # atomic: fragments in temp, move on completion
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([query])

    out_path = cache_dir / f"{track.cache_key}.mp3"
    if not out_path.exists():
        raise FileNotFoundError(f"Download failed for {track.display}")
    return out_path


def _ytdlp_enabled() -> bool:
    """Return whether yt-dlp downloads are enabled for this runtime."""
    return os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in _TRUTHY


def _resolve_cached_or_local(track: Track, cache_dir: Path, music_dir: Path) -> Path | None:
    """Return an existing cache/local/demo asset path when one is already available."""
    out_path = cache_dir / f"{track.cache_key}.mp3"
    if out_path.exists():
        logger.info("Cache hit: %s", track.display)
        return out_path

    demo = _find_demo_asset(track)
    if demo:
        logger.info("Demo asset: %s -> %s", track.display, demo)
        return demo

    local = _find_local(track, music_dir)
    if local:
        logger.info("Local file: %s -> %s", track.display, local)
        return local

    return None


def _download_sync(track: Track, cache_dir: Path, music_dir: Path) -> Path:
    """Resolve a track from cache, local files, yt-dlp, or a placeholder tone."""
    out_path = cache_dir / f"{track.cache_key}.mp3"
    existing = _resolve_cached_or_local(track, cache_dir, music_dir)
    if existing is not None:
        return existing

    # 3. Try yt-dlp (opt-in only, disabled by default for copyright safety)
    if _ytdlp_enabled():
        try:
            return _download_ytdlp(track, cache_dir)
        except Exception as e:
            logger.warning("yt-dlp failed for %s: %s — using silence", track.display, e)
    else:
        logger.info("yt-dlp disabled for %s (set MAMMAMIRADIO_ALLOW_YTDLP=true to enable)", track.display)

    # 4. Fallback: brief silence (never a sine wave tone)
    return _generate_silence(track, out_path)


def _download_external_sync(track: Track, cache_dir: Path, music_dir: Path) -> Path:
    """Resolve an explicit external request without silently falling back to silence."""
    out_path = cache_dir / f"{track.cache_key}.mp3"
    if out_path.exists():
        logger.info("Cache hit: %s", track.display)
        return out_path

    local = _find_local(track, music_dir)
    if local:
        logger.info("Local file: %s -> %s", track.display, local)
        return local

    if not _ytdlp_enabled():
        raise RuntimeError("yt-dlp is disabled")

    return _download_ytdlp(track, cache_dir)


def search_ytdlp_metadata(query: str, max_results: int = 5) -> list[dict]:
    """Search yt-dlp for tracks matching query, returning metadata without downloading.

    Uses extract_flat so only lightweight playlist-level info is fetched.
    Returns a list of dicts with youtube_id, title, artist, duration_ms, display.
    Returns [] if yt-dlp is unavailable or the search fails.
    """
    if not _ytdlp_enabled():
        return []
    try:
        import yt_dlp
    except ImportError:
        return []
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        entries = info.get("entries", []) if info else []
        results = []
        for e in entries or []:
            if not e or not e.get("id"):
                continue
            title = e.get("title") or ""
            artist = e.get("uploader") or e.get("channel") or ""
            duration_s = e.get("duration") or 0
            display = f"{artist} \u2013 {title}" if artist else title
            results.append(
                {
                    "youtube_id": e["id"],
                    "title": title,
                    "artist": artist,
                    "duration_ms": int(duration_s * 1000),
                    "display": display,
                }
            )
        return results
    except Exception:
        logger.debug("yt-dlp metadata search failed", exc_info=True)
        return []


async def download_track(track: Track, cache_dir: Path, music_dir: Path | None = None) -> Path:
    """Run the synchronous download fallback chain off the event loop."""
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path("music")
    return await loop.run_in_executor(None, _download_sync, track, cache_dir, _music_dir)


async def download_external_track(track: Track, cache_dir: Path, music_dir: Path | None = None) -> Path:
    """Download an explicit external request, raising on failure instead of returning silence."""
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path("music")
    return await loop.run_in_executor(None, _download_external_sync, track, cache_dir, _music_dir)
