"""Track acquisition helpers: local files, yt-dlp, and unavailable-source markers."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from functools import partial
from itertools import islice
from pathlib import Path
from types import ModuleType
from typing import Literal

from mammamiradio.audio.admission import ffmpeg_slot
from mammamiradio.core.models import Track
from mammamiradio.core.path_safety import safe_path_within

logger = logging.getLogger(__name__)

# Files that must never be evicted from the cache directory
_CACHE_PROTECTED = {
    "mammamiradio.db",
    "playlist_source.json",
    "session_stopped.flag",
    "evening_ledger.json",
    "release_campaign_ledger.json",
    "moments.json",
}
_TRUTHY = ("true", "1", "yes")
_EXTERNAL_MEDIA_SOURCES = frozenset({"youtube", "classic"})

# Per-socket-operation timeout for yt-dlp network reads. Python's urllib has no
# default socket timeout — without this a stalled YouTube socket blocks a
# download thread forever, leaking a slot from the shared run_in_executor pool
# the audio pipeline also uses. Pairs with the `throttled_rate` opt, which
# already bounds slow-but-alive transfers.
_YTDLP_SOCKET_TIMEOUT_SEC = 30

# Canonical YouTube video-id shape (11 chars, base64url alphabet). Single source
# of truth for both the search-result filter (below) and the add-external payload
# validator in web/streamer.py — keep them importing this, not re-spelling it.
YOUTUBE_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

# Per-session denylist of track cache keys rejected by validate_download or the
# quality gate. Keeps a poisoned cache file or a structurally-bad track from
# looping through the quality gate forever — once rejected, the track stays
# rejected for the lifetime of the process. Cleared explicitly by tests and at
# startup via `clear_rejected_cache_keys()`.
_REJECTED_CACHE_KEYS: set[str] = set()


def _rejected_cache_artifacts(cache_dir: Path, cache_key: str) -> list[Path]:
    raw_name = f"{cache_key}.mp3"
    norm_prefix = f"norm_{cache_key}_"
    fm_prefix = f"fm_norm_{cache_key}_"
    try:
        entries = list(cache_dir.iterdir())
    except OSError:
        return [cache_dir / raw_name]
    artifacts: list[Path] = []
    for path in entries:
        name = path.name
        if (
            name == raw_name
            or name == f"{raw_name}.json"
            or (name.startswith(norm_prefix) and (name.endswith(".mp3") or name.endswith(".mp3.json")))
            or (name.startswith(fm_prefix) and (name.endswith(".mp3") or ".staging_" in name))
        ):
            artifacts.append(path)
    return artifacts


def reject_cached_download(cache_dir: Path, cache_key: str, reason: str) -> bool:
    """Purge a rejected download from cache and denylist the key for the session.

    Called by the producer when validate_download or the audio-quality gate
    rejects a track. Without this, the file would remain at
    ``cache_dir/{cache_key}.mp3`` and become a cache hit on the next selection
    of the same track, endlessly re-rejected with no progress.

    Returns True if a cache file was actually removed.
    """
    if not cache_key:
        return False
    _REJECTED_CACHE_KEYS.add(cache_key)
    removed = False
    for path in _rejected_cache_artifacts(cache_dir, cache_key):
        try:
            if path.exists():
                path.unlink()
                removed = True
                logger.warning("Purged rejected cache artifact %s: %s", path.name, reason)
        except OSError as exc:
            logger.warning("Failed to purge rejected cache artifact %s: %s", path, exc)
    failed_path = cache_dir / f"_failed_{cache_key}.mp3"
    try:
        # A marker also gives a rejected local/demo fallback a timestamped
        # recovery boundary. If it later fails admission, refresh the marker so
        # the same corrupt file cannot restart a retry loop until replaced.
        failed_path.write_text(reason)
    except OSError as exc:
        logger.warning("Failed to update rejected-download marker %s: %s", failed_path, exc)
    return removed


def accept_recovered_download(cache_dir: Path, cache_key: str) -> None:
    """Clear a transient rejection after an external retry is admitted.

    Call this only once a later explicit download has passed the caller's
    admission checks.  A failed acquisition is intentionally session-denied to
    stop a retry loop, but a newly admitted replacement for that same key must
    be selectable and must not leave its stale unavailable marker behind.
    """
    if not cache_key:
        return
    was_rejected = cache_key in _REJECTED_CACHE_KEYS
    _REJECTED_CACHE_KEYS.discard(cache_key)
    failed_path = cache_dir / f"_failed_{cache_key}.mp3"
    had_marker = failed_path.exists()
    try:
        failed_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clear recovered-download marker %s: %s", failed_path, exc)
    else:
        if was_rejected or had_marker:
            logger.info("Cleared recovered download rejection: %s", cache_key)


def is_rejected_cache_key(cache_key: str) -> bool:
    """Return True if the cache key was rejected earlier in this session."""
    return bool(cache_key) and cache_key in _REJECTED_CACHE_KEYS


def clear_rejected_cache_keys() -> None:
    """Reset the session-level rejection set (startup and tests)."""
    _REJECTED_CACHE_KEYS.clear()


def validate_download(filepath: Path, *, background: bool = False) -> tuple[bool, str]:
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
        with ffmpeg_slot(background=background):
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

    A failed acquisition can leave a partial file or an unavailable-source
    marker. Purging those on startup permits a fresh transient-error retry.

    Also purges legacy ``_silence_*.mp3`` and current ``_failed_*.mp3`` files
    unconditionally. Neither is eligible music and neither should survive a
    restart.
    """
    if not cache_dir.is_dir():
        return 0
    purged = 0
    for f in cache_dir.glob("*.mp3"):
        if f.name in _CACHE_PROTECTED:
            continue
        # Legacy silence placeholders always get purged regardless of size.
        if f.name.startswith("_silence_"):
            logger.info("Purging silence placeholder: %s", f.name)
            f.unlink(missing_ok=True)
            purged += 1
            continue
        if f.name.startswith("_failed_"):
            logger.info("Purging failed-download marker: %s", f.name)
            f.unlink(missing_ok=True)
            purged += 1
            continue
        if f.name.startswith("synth_"):
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


def evict_cache_lru(
    cache_dir: Path,
    max_size_mb: int,
    protected_paths: set[Path] | None = None,
) -> None:
    """Delete oldest MP3s from cache_dir until total size is under max_size_mb.

    Only .mp3 files are evicted. The SQLite database, playlist source JSON, and
    session flag are always preserved. Paths in ``protected_paths`` (typically
    files currently queued for playback) are skipped — evicting a queued norm
    file would break audio delivery mid-stream.
    """
    if max_size_mb <= 0:
        return

    protected_names = {p.name for p in (protected_paths or set())}

    # Evict transient/regular cache files first; processed audio that may be queued or
    # currently airing — norm_ originals and fm_ broadcast-chain bakes — is evicted last.
    # Within each group, oldest-by-atime first, so a cold or stale-chain-version bake goes
    # before a hot one and a file that is about to play is the least likely to be removed
    # mid-stream (matching the long-standing norm_ safety baseline).
    regular = []
    processed = []
    for f in cache_dir.glob("*.mp3"):
        if f.name in _CACHE_PROTECTED:
            continue
        if f.name in protected_names:
            continue
        if f.name.startswith(("norm_", "fm_")):
            processed.append(f)
        else:
            regular.append(f)
    regular.sort(key=lambda f: f.stat().st_atime)
    processed.sort(key=lambda f: f.stat().st_atime)
    mp3_files = regular + processed

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


def prune_stale_tmp_files(tmp_dir: Path, max_age_hours: float = 6) -> int:
    """Delete completed and atomic render scratch older than *max_age_hours*.

    ``tmp_dir`` holds only ephemeral ``{prefix}_{uuid}.mp3`` segment renders that
    the producer consumes within seconds. A crash or restart can orphan them, and
    on the HA add-on they pile up unbounded in ``/data/tmp``. Startup runs before
    the producer/playback loop, so nothing in tmp is in-flight — but the age gate
    keeps the prune conservative regardless. ``.mmr-atomic-*.part`` is the exact
    private staging contract used by frame-safe artifact publication; unrelated
    partial files remain untouched. Best-effort: never raises into startup.
    """
    if not tmp_dir.is_dir():
        return 0
    if tmp_dir.is_symlink():
        # reject_symlinks=True below only rejects a symlinked *leaf* (f); it
        # can't catch tmp_dir itself being a symlink, since a leaf under a
        # symlinked root still resolves "contained" relative to that same
        # root. Unlike a symlinked leaf (unlink() never dereferences), a
        # symlinked root means every glob/stat/unlink here targets real files
        # in the redirected directory — a genuine delete-outside-tmp_dir path.
        logger.warning("Skipping tmp scratch cleanup: tmp_dir is a symlink: %s", tmp_dir)
        return 0
    cutoff = time.time() - max_age_hours * 3600
    pruned = 0
    for pattern in ("*.mp3", ".mmr-atomic-*.part"):
        for f in tmp_dir.glob(pattern):
            try:
                if safe_path_within(f, tmp_dir, reject_symlinks=True) is None:
                    continue
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    pruned += 1
            except OSError:
                continue
    return pruned


# Cached directory listings avoid repeated glob() on every local-track lookup.
_local_files_cache: dict[str, tuple[float, list[Path]]] = {}
_LOCAL_FILES_TTL = 60.0  # seconds
_LOCAL_FILES_LIMIT = 200
_LOCAL_DIRECTORY_ENTRY_LIMIT = 10_000


def _safe_operator_local_path(path: Path, music_dir: Path) -> Path | None:
    """Resolve one real file without escaping the configured music root."""
    try:
        if music_dir.is_symlink():
            return None
        resolved = safe_path_within(path, music_dir, reject_symlinks=True)
        return resolved if resolved is not None and resolved.is_file() else None
    except OSError:
        return None


def _is_scanned_library_track(track: Track) -> bool:
    return track.source == "local" and track.spotify_id.startswith("local_")


def _find_local(track: Track, music_dir: Path) -> Path | None:
    """Check if a local MP3 exists in the music/ directory."""
    if not music_dir.is_dir() or music_dir.is_symlink():
        return None
    import time as _time

    key = str(music_dir)
    cached = _local_files_cache.get(key)
    if cached and (_time.time() - cached[0]) < _LOCAL_FILES_TTL:
        files = cached[1]
    else:
        # Bound raw directory entries before filtering. A glob limited after
        # the ``*.mp3`` filter can still scan an arbitrarily large directory
        # containing no MP3s.
        files = []
        try:
            with os.scandir(music_dir) as entries:
                for entry in islice(entries, _LOCAL_DIRECTORY_ENTRY_LIMIT):
                    if not entry.name.endswith(".mp3"):
                        continue
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    files.append(Path(entry.path))
                    if len(files) >= _LOCAL_FILES_LIMIT:
                        break
        except OSError:
            files = []
        _local_files_cache[key] = (_time.time(), files)
    for f in files:
        name = f.stem.lower()
        if track.cache_key in name or track.title.lower() in name:
            admitted = _safe_operator_local_path(f, music_dir)
            if admitted is not None:
                return admitted
    return None


def _download_ytdlp(track: Track, cache_dir: Path) -> Path:
    """Download the best-effort public audio match for a track via yt-dlp."""
    yt_dlp = _load_external_media_module()

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
        "socket_timeout": _YTDLP_SOCKET_TIMEOUT_SEC,  # fail a stalled socket, never hang
        "throttled_rate": 100_000,  # re-extract URLs if speed drops below 100 KB/s
        "check_formats": True,  # verify formats are downloadable before selecting
        "concurrent_fragment_downloads": 2,  # parallel fragment downloads
        "paths": {"temp": str(ytdlp_tmp)},  # atomic: fragments in temp, move on completion
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([query])
    finally:
        shutil.rmtree(ytdlp_tmp, ignore_errors=True)

    out_path = cache_dir / f"{track.cache_key}.mp3"
    if not out_path.exists():
        raise FileNotFoundError(f"Download failed for {track.display}")
    return out_path


def _load_external_media_module() -> ModuleType:
    """Load the optional extractor behind the single lazy import boundary.

    The default distribution and both current Home Assistant add-ons omit this
    dependency. Keeping the import here lets the rest of the application load
    and serve local/bundled music when the optional extra is absent.
    """
    try:
        return importlib.import_module("yt_dlp")
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "external media is unavailable; install mammamiradio[external-media] before enabling it"
        ) from exc


def _external_media_opted_in(configured: bool | None = None) -> bool:
    """Return the operator opt-in half of the gate, without probing the module.

    ``external_media_enabled`` is the effective gate and stays the answer to
    "may this process resolve external media". This is only its first half, so
    a caller that reports *why* the gate is shut can tell an operator setting
    apart from a missing optional install instead of blaming the setting.
    """
    if configured is None:
        return os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in _TRUTHY
    return bool(configured)


def external_media_enabled(configured: bool | None = None) -> bool:
    """Return the effective extractor gate for this process.

    A caller-supplied value represents the already-parsed station setting;
    otherwise the environment is read directly. Opt-in is necessary but not
    sufficient: the standalone-only optional module must also be installed.
    """
    if not _external_media_opted_in(configured):
        return False
    try:
        _load_external_media_module()
    except RuntimeError:
        return False
    return True


def _ytdlp_enabled() -> bool:
    """Compatibility alias for the canonical external-media gate."""
    return external_media_enabled()


def _track_requires_external_media(track: Track) -> bool:
    """Return whether cached bytes came from an extractor-owned source."""
    return track.source in _EXTERNAL_MEDIA_SOURCES or bool(track.youtube_id)


def _failed_download_path(track: Track, cache_dir: Path, reason: str) -> Path:
    """Return a tiny failure marker that makes the producer skip this track."""
    failure_path = cache_dir / f"_failed_{track.cache_key}.mp3"
    failure_path.write_text(reason)
    return failure_path


def _resolve_cached_or_local(track: Track, cache_dir: Path, music_dir: Path) -> Path | None:
    """Return an existing admitted cache or operator-local path when available."""
    if track.source == "jamendo":
        # Only JamendoStreamProvider may own Jamendo bytes. Legacy cache and
        # direct-URL tracks are intentionally invisible to the normal pipeline.
        return None
    external_cache_allowed = not _track_requires_external_media(track) or _ytdlp_enabled()
    out_path = cache_dir / f"{track.cache_key}.mp3"
    if out_path.exists() and external_cache_allowed:
        logger.info("Cache hit: %s", track.display)
        return out_path
    if out_path.exists():
        logger.info("Ignoring external-media cache while extraction is disabled: %s", track.display)
    if track.source == "youtube" and external_cache_allowed:
        legacy_path = cache_dir / f"{track.legacy_cache_key}.mp3"
        if legacy_path.exists():
            logger.info("Legacy YouTube cache hit: %s", track.display)
            return legacy_path
    if track.local_path is not None:
        attached = (
            _safe_operator_local_path(track.local_path, music_dir)
            if _is_scanned_library_track(track)
            else track.local_path
            if track.local_path.is_file()
            else None
        )
        if attached is not None:
            logger.info("Track file: %s -> %s", track.display, attached)
            return attached

    local = _find_local(track, music_dir)
    if local:
        logger.info("Local file: %s -> %s", track.display, local)
        return local

    failed_path = cache_dir / f"_failed_{track.cache_key}.mp3"
    if failed_path.exists():
        logger.info("Failed-download marker hit, skipping: %s", track.display)
        return failed_path

    return None


def has_fresh_concrete_track_source(track: Track, cache_dir: Path, music_dir: Path) -> bool:
    """Return whether newer concrete media can heal an unavailable-source marker.

    The producer uses this narrow escape hatch only for already-denied keys. A
    newly synced local, demo, legacy, or raw-cache file deserves one fresh
    admission attempt; after it fails, ``reject_cached_download`` refreshes the
    marker timestamp so the same corrupt file cannot restart a retry loop.
    """
    failed_path = cache_dir / f"_failed_{track.cache_key}.mp3"
    if not failed_path.exists():
        return False
    # A marker is the exceptional recovery path. Refresh the ordinarily cached
    # local directory listing so an operator-synced music/ file is visible now,
    # not after the normal 60-second lookup TTL.
    _local_files_cache.pop(str(music_dir), None)
    resolved = _resolve_cached_or_local(track, cache_dir, music_dir)
    if resolved is None or resolved == failed_path:
        return False
    try:
        return resolved.stat().st_mtime_ns > failed_path.stat().st_mtime_ns
    except OSError:
        return False


def _download_sync(track: Track, cache_dir: Path, music_dir: Path, *, background: bool = False) -> Path:
    """Resolve a track from cache, local files, yt-dlp, or an unavailable marker."""
    if track.source == "jamendo":
        raise RuntimeError("persistent Jamendo track acquisition is retired")
    existing = _resolve_cached_or_local(track, cache_dir, music_dir)
    if existing is not None:
        return existing

    # 3. Try yt-dlp (opt-in only, disabled by default for copyright safety)
    if _ytdlp_enabled():
        try:
            return _download_ytdlp(track, cache_dir)
        except Exception as e:
            reason = f"yt-dlp failed: {e}"
            logger.warning("yt-dlp failed for %s: %s — marking track unavailable", track.display, e)
            return _failed_download_path(track, cache_dir, reason)
    else:
        reason = "yt-dlp disabled"
        logger.info("yt-dlp disabled for %s — marking track unavailable", track.display)
        return _failed_download_path(track, cache_dir, reason)


def _download_external_sync(track: Track, cache_dir: Path, music_dir: Path) -> Path:
    """Resolve an explicit external request without a silent fallback."""
    if track.local_path is not None:
        attached = (
            _safe_operator_local_path(track.local_path, music_dir)
            if _is_scanned_library_track(track)
            else track.local_path
            if track.local_path.is_file()
            else None
        )
        if attached is not None:
            logger.info("Track file: %s -> %s", track.display, attached)
            return attached
    local = _find_local(track, music_dir)
    if local:
        logger.info("Local file: %s -> %s", track.display, local)
        return local

    if not _ytdlp_enabled():
        raise RuntimeError("yt-dlp is disabled")

    # Extractor-owned cache reuse is gated too: disabling external media must
    # not keep serving bytes acquired by an earlier enabled process.
    out_path = cache_dir / f"{track.cache_key}.mp3"
    if out_path.exists():
        logger.info("Cache hit: %s", track.display)
        return out_path

    return _download_ytdlp(track, cache_dir)


YtdlpSearchStatus = Literal["ok", "disabled", "unavailable", "failed"]


@dataclass(frozen=True)
class YtdlpSearchOutcome:
    """Strict yt-dlp metadata-search result for callers that need honest state."""

    status: YtdlpSearchStatus
    results: list[dict]

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"


def search_ytdlp_metadata_outcome(query: str, max_results: int = 5) -> YtdlpSearchOutcome:
    """Search yt-dlp while preserving empty, unavailable, and failed outcomes.

    ``status='ok'`` with no results is a genuine empty search.  The other
    statuses let interactive callers avoid reporting infrastructure failures as
    catalogue misses.  ``search_ytdlp_metadata`` remains the compatibility API
    for best-effort callers that intentionally collapse every failure to ``[]``.
    """
    if not _ytdlp_enabled():
        # The effective gate is shut for two reasons that mean different things
        # to a listener: the operator never opted in (a settings choice), or the
        # opt-in is on and the standalone-only extractor cannot load (a broken
        # install). Collapsing the second into "disabled" would tell a listener
        # to change a setting that is already correct.
        if _external_media_opted_in():
            return YtdlpSearchOutcome(status="unavailable", results=[])
        return YtdlpSearchOutcome(status="disabled", results=[])
    try:
        yt_dlp = _load_external_media_module()
    except RuntimeError:
        # Gate passed, module vanished between that check and this load.
        return YtdlpSearchOutcome(status="unavailable", results=[])
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extract_flat": True,
        "socket_timeout": _YTDLP_SOCKET_TIMEOUT_SEC,  # fail a stalled socket, never hang
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        entries = info.get("entries", []) if info else []
        results = []
        for e in entries or []:
            if not e or not e.get("id"):
                continue
            # ytsearch mixes channel/playlist hits (e.g. a "UC..." channel id)
            # in with videos. Those aren't downloadable and would 400 at
            # /api/playlist/add-external, so keep only real 11-char video ids —
            # the same shape the queue endpoint validates against. str() guards
            # against a non-string id wiping the whole result set via TypeError.
            if not YOUTUBE_VIDEO_ID_RE.fullmatch(str(e["id"])):
                continue
            title = e.get("title") or ""
            artist = e.get("uploader") or e.get("channel") or ""
            raw_duration_s = e.get("duration")
            if raw_duration_s in (None, ""):
                duration_ms = 0
            else:
                try:
                    duration_ms = max(0, int(float(raw_duration_s) * 1000))
                except (TypeError, ValueError, OverflowError):
                    logger.debug("Skipping yt-dlp result with invalid duration: %r", raw_duration_s)
                    continue
            thumbnail = e.get("thumbnail") or ""
            if not thumbnail and e.get("thumbnails"):
                thumbnail = (e.get("thumbnails") or [{}])[-1].get("url") or ""
            display = f"{artist} \u2013 {title}" if artist else title
            results.append(
                {
                    "youtube_id": e["id"],
                    "title": title,
                    "artist": artist,
                    # Additive identity evidence for strict relevance callers.
                    # ``artist`` above intentionally retains its legacy
                    # uploader/channel meaning for existing API consumers.
                    "track_title": e.get("track") or "",
                    "track_artist": e.get("artist") or e.get("creator") or "",
                    "uploader": e.get("uploader") or "",
                    "channel": e.get("channel") or "",
                    "duration_ms": duration_ms,
                    "album_art": thumbnail,
                    "display": display,
                }
            )
        return YtdlpSearchOutcome(status="ok", results=results)
    except Exception:
        logger.debug("yt-dlp metadata search failed", exc_info=True)
        return YtdlpSearchOutcome(status="failed", results=[])


def search_ytdlp_metadata(query: str, max_results: int = 5) -> list[dict]:
    """Compatibility search API that collapses unavailable/failure to ``[]``."""
    return search_ytdlp_metadata_outcome(query, max_results).results


async def download_track(
    track: Track, cache_dir: Path, music_dir: Path | None = None, *, background: bool = False
) -> Path:
    """Run the synchronous download fallback chain off the event loop."""
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path(os.getenv("MAMMAMIRADIO_MUSIC_DIR", "music"))
    download_fn = partial(_download_sync, track, cache_dir, _music_dir, background=background)
    return await loop.run_in_executor(None, download_fn)


async def download_external_track(track: Track, cache_dir: Path, music_dir: Path | None = None) -> Path:
    """Download an explicit external request, raising on failure instead of returning silence."""
    loop = asyncio.get_running_loop()
    _music_dir = music_dir or Path(os.getenv("MAMMAMIRADIO_MUSIC_DIR", "music"))
    return await loop.run_in_executor(None, _download_external_sync, track, cache_dir, _music_dir)
