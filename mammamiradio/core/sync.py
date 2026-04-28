"""Playlist sync via yt-dlp — download audio to cache, extract metadata to SQLite."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from pathlib import Path

from mammamiradio.audio.normalizer import normalize
from mammamiradio.core.models import Track

logger = logging.getLogger(__name__)

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    youtube_id TEXT UNIQUE,
    title TEXT NOT NULL,
    artist TEXT,
    duration_s REAL,
    file_path TEXT NOT NULL,
    downloaded_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS play_history (
    id INTEGER PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id),
    played_at TEXT DEFAULT (datetime('now')),
    session_id TEXT,
    host_script TEXT,
    persona_updates TEXT
);

CREATE TABLE IF NOT EXISTS listener_persona (
    id INTEGER PRIMARY KEY DEFAULT 1,
    motifs TEXT DEFAULT '[]',
    theories TEXT DEFAULT '[]',
    running_jokes TEXT DEFAULT '[]',
    callbacks TEXT DEFAULT '[]',
    personality_guesses TEXT DEFAULT '[]',
    session_count INTEGER DEFAULT 0,
    last_session TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Seed the default persona row so UPDATE-based methods never no-op
INSERT OR IGNORE INTO listener_persona (id) VALUES (1);

CREATE TABLE IF NOT EXISTS track_rules (
    id INTEGER PRIMARY KEY,
    youtube_id TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS song_cues (
    id INTEGER PRIMARY KEY,
    youtube_id TEXT NOT NULL,
    cue_type TEXT NOT NULL,
    cue_text TEXT NOT NULL,
    source_session INTEGER,
    times_used INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_song_cues_yt ON song_cues(youtube_id);
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add new columns to existing tables. Idempotent — safe to run on every startup."""
    migrations = [
        "ALTER TABLE listener_persona ADD COLUMN arc_metadata TEXT DEFAULT '{}'",
        "ALTER TABLE play_history ADD COLUMN skipped INTEGER DEFAULT 0",
        "ALTER TABLE play_history ADD COLUMN listen_duration_s REAL",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column name" in msg or "already exists" in msg:
                continue
            raise


def init_db(db_path: Path) -> None:
    """Create the SQLite database and tables if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(DB_SCHEMA)
    _migrate_schema(conn)
    conn.close()
    logger.info("Database initialized: %s", db_path)


def _resolve_cookies_arg() -> list[str]:
    """Try Chrome, Firefox, Safari in order for cookie extraction."""
    for browser in ("chrome", "firefox", "safari"):
        try:
            import yt_dlp

            with yt_dlp.YoutubeDL({"cookiesfrombrowser": (browser,), "quiet": True}) as ydl:
                ydl.cookiejar  # noqa: B018 — just test access
            return ["--cookies-from-browser", browser]
        except Exception:
            continue
    logger.warning("No browser cookies available — yt-dlp will use public access")
    return []


def _sync_playlist_blocking(
    playlist_url: str,
    cache_dir: Path,
    db_path: Path,
    config=None,
) -> list[Track]:
    """Download playlist tracks to cache and write metadata to SQLite."""
    import yt_dlp

    cache_dir.mkdir(parents=True, exist_ok=True)

    # Extract playlist info without downloading
    extract_opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }

    # Try cookies
    cookie_browser = None
    for browser in ("chrome", "firefox", "safari"):
        try:
            test_opts = {**extract_opts, "cookiesfrombrowser": (browser,)}
            with yt_dlp.YoutubeDL(test_opts) as ydl:
                ydl.cookiejar  # noqa: B018
            cookie_browser = browser
            extract_opts["cookiesfrombrowser"] = (browser,)
            break
        except Exception:
            continue

    logger.info("Extracting playlist metadata from: %s", playlist_url)
    with yt_dlp.YoutubeDL(extract_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    if not info or "entries" not in info:
        logger.error("Failed to extract playlist — no entries found")
        return []

    entries = list(info["entries"] or [])
    logger.info("Playlist has %d tracks", len(entries))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    tracks: list[Track] = []

    for entry in entries:
        if not entry:
            continue

        video_id = entry.get("id", "")
        title = entry.get("title", "Unknown")
        artist = entry.get("uploader", entry.get("channel", "Unknown"))
        duration = entry.get("duration", 0) or 0

        # Parse "Artist - Title" format common in music videos
        if " - " in title and artist in ("Unknown", title.split(" - ")[0].strip()):
            parts = title.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        # Check if already cached
        existing = conn.execute("SELECT file_path FROM tracks WHERE youtube_id = ?", (video_id,)).fetchone()

        if existing and Path(existing["file_path"]).exists():
            logger.debug("Already cached: %s – %s", artist, title)
            tracks.append(
                Track(
                    title=title,
                    artist=artist,
                    duration_ms=int(duration * 1000),
                    youtube_id=video_id,
                    local_path=Path(existing["file_path"]),
                )
            )
            continue

        # Download audio
        part_path = cache_dir / f"{video_id}.part.mp3"
        final_path = cache_dir / f"{video_id}.mp3"

        dl_opts: dict = {
            "format": "bestaudio/best",
            "outtmpl": str(cache_dir / f"{video_id}.part.%(ext)s"),
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
        if cookie_browser:
            dl_opts["cookiesfrombrowser"] = (cookie_browser,)

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

            # Atomic rename after download
            if part_path.exists():
                # Normalize to station loudness
                norm_path = cache_dir / f"{video_id}.norm.mp3"
                try:
                    normalize(part_path, norm_path, config)
                    part_path.unlink(missing_ok=True)
                    os.rename(str(norm_path), str(final_path))
                except Exception as e:
                    logger.warning("Normalize failed for %s, using raw: %s", title, e)
                    os.rename(str(part_path), str(final_path))
            else:
                # yt-dlp may have used a different extension before postprocessing
                for candidate in cache_dir.glob(f"{video_id}.part.*"):
                    os.rename(str(candidate), str(final_path))
                    break

            if not final_path.exists():
                logger.warning("Download produced no file for: %s – %s", artist, title)
                continue

            # Write to DB
            conn.execute(
                "INSERT OR REPLACE INTO tracks (youtube_id, title, artist, duration_s, file_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (video_id, title, artist, duration, str(final_path)),
            )
            conn.commit()

            tracks.append(
                Track(
                    title=title,
                    artist=artist,
                    duration_ms=int(duration * 1000),
                    youtube_id=video_id,
                    local_path=final_path,
                )
            )
            logger.info("Downloaded: %s – %s", artist, title)

        except Exception as e:
            logger.warning("Failed to download %s – %s: %s — skipping", artist, title, e)
            continue

    conn.close()

    logger.info("Sync complete: %d tracks available", len(tracks))
    return tracks


def load_cached_tracks(db_path: Path) -> list[Track]:
    """Load previously synced tracks from SQLite without downloading."""
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM tracks ORDER BY id").fetchall()
    conn.close()

    tracks = []
    for row in rows:
        path = Path(row["file_path"])
        if path.exists():
            tracks.append(
                Track(
                    title=row["title"],
                    artist=row["artist"] or "Unknown",
                    duration_ms=int((row["duration_s"] or 0) * 1000),
                    youtube_id=row["youtube_id"],
                    local_path=path,
                )
            )
    return tracks


async def sync_playlist(
    playlist_url: str,
    cache_dir: Path,
    db_path: Path,
    config=None,
) -> list[Track]:
    """Async wrapper — runs the blocking sync off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_playlist_blocking, playlist_url, cache_dir, db_path, config)
