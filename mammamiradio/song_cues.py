"""Per-track machine-derived memory — anthems, skip bits, and LLM reactions."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from mammamiradio.persona import _sanitize

logger = logging.getLogger(__name__)


async def get_cues(db_path: Path, youtube_id: str, limit: int = 3) -> list[dict]:
    """Return structured song cues for a track, pinned first, most recent next.

    Also merges any legacy ``track_rules`` entries (tagged as cue_type='operator').
    """
    if not db_path.exists():
        return []
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            # Song cues (machine-derived)
            cursor = await db.execute(
                "SELECT cue_type, cue_text, source_session, times_used FROM song_cues "
                "WHERE youtube_id = ? ORDER BY pinned DESC, last_used_at DESC NULLS LAST, created_at DESC "
                "LIMIT ?",
                (youtube_id, limit),
            )
            rows = await cursor.fetchall()
            cues = [
                {
                    "type": r["cue_type"],
                    "text": r["cue_text"],
                    "session": r["source_session"],
                    "uses": r["times_used"],
                }
                for r in rows
            ]

            # Legacy operator rules (track_rules table)
            remaining = limit - len(cues)
            if remaining > 0:
                cursor = await db.execute(
                    "SELECT rule_text FROM track_rules WHERE youtube_id = ? ORDER BY created_at LIMIT ?",
                    (youtube_id, remaining),
                )
                for r in await cursor.fetchall():
                    cues.append({"type": "operator", "text": r["rule_text"], "session": None, "uses": 0})

            return cues
    except Exception:
        logger.warning("song_cues.get_cues failed for %s", youtube_id, exc_info=True)
        return []


async def add_cue(
    db_path: Path,
    youtube_id: str,
    cue_type: str,
    cue_text: str,
    source_session: int = 0,
) -> None:
    """Insert a song cue, deduplicating by youtube_id + cue_type."""
    if not youtube_id or not cue_text:
        return
    cue_text = _sanitize(cue_text)
    if cue_text == "(filtered)":
        return
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            # Dedupe: update if same youtube_id + cue_type exists
            cursor = await db.execute(
                "SELECT id FROM song_cues WHERE youtube_id = ? AND cue_type = ? LIMIT 1",
                (youtube_id, cue_type),
            )
            existing = await cursor.fetchone()
            if existing:
                await db.execute(
                    "UPDATE song_cues SET cue_text = ?, source_session = ?, "
                    "last_used_at = datetime('now') WHERE id = ?",
                    (cue_text, source_session, existing[0]),
                )
            else:
                await db.execute(
                    "INSERT INTO song_cues (youtube_id, cue_type, cue_text, source_session) VALUES (?, ?, ?, ?)",
                    (youtube_id, cue_type, cue_text, source_session),
                )
            await db.commit()
    except Exception:
        logger.warning("song_cues.add_cue failed", exc_info=True)


async def bump_usage(db_path: Path, youtube_id: str, cue_type: str) -> None:
    """Increment times_used and update last_used_at for a cue."""
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "UPDATE song_cues SET times_used = times_used + 1, "
                "last_used_at = datetime('now') WHERE youtube_id = ? AND cue_type = ?",
                (youtube_id, cue_type),
            )
            await db.commit()
    except Exception:
        logger.warning("song_cues.bump_usage failed", exc_info=True)


async def detect_anthem(db_path: Path, youtube_id: str, threshold: int = 3) -> bool:
    """Check if a track qualifies as an anthem (played N+ times, never skipped).

    Returns True if a new anthem cue was created.
    """
    if not youtube_id or not db_path.exists():
        return False
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            # Count plays and skips for this track
            cursor = await db.execute(
                "SELECT COUNT(*) as plays, SUM(COALESCE(skipped, 0)) as skips "
                "FROM play_history ph JOIN tracks t ON ph.track_id = t.id "
                "WHERE t.youtube_id = ?",
                (youtube_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return False
            plays, skips = row[0], row[1] or 0
            if plays >= threshold and skips == 0:
                # Check if anthem cue already exists
                cursor = await db.execute(
                    "SELECT id FROM song_cues WHERE youtube_id = ? AND cue_type = 'anthem'",
                    (youtube_id,),
                )
                if await cursor.fetchone():
                    # Update the play count in existing cue
                    await db.execute(
                        "UPDATE song_cues SET cue_text = ? WHERE youtube_id = ? AND cue_type = 'anthem'",
                        (f"Listener's anthem — {plays}th play, never skipped", youtube_id),
                    )
                    await db.commit()
                    return False  # Updated, not newly created
                await db.execute(
                    "INSERT INTO song_cues (youtube_id, cue_type, cue_text, pinned) VALUES (?, 'anthem', ?, 1)",
                    (youtube_id, f"Listener's anthem — {plays}th play, never skipped"),
                )
                await db.commit()
                logger.info("Anthem detected: %s (%d plays)", youtube_id, plays)
                return True
        return False
    except Exception:
        logger.warning("detect_anthem failed for %s", youtube_id, exc_info=True)
        return False


async def detect_skip_bit(db_path: Path, youtube_id: str, threshold: int = 2) -> bool:
    """Check if a track qualifies as a skip-bit (skipped N+ times).

    Returns True if a new skip_bit cue was created.
    """
    if not youtube_id or not db_path.exists():
        return False
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute(
                "SELECT SUM(COALESCE(skipped, 0)) as skips "
                "FROM play_history ph JOIN tracks t ON ph.track_id = t.id "
                "WHERE t.youtube_id = ?",
                (youtube_id,),
            )
            row = await cursor.fetchone()
            if not row or not row[0]:
                return False
            skips = row[0]
            if skips >= threshold:
                cursor = await db.execute(
                    "SELECT id FROM song_cues WHERE youtube_id = ? AND cue_type = 'skip_bit'",
                    (youtube_id,),
                )
                if await cursor.fetchone():
                    await db.execute(
                        "UPDATE song_cues SET cue_text = ? WHERE youtube_id = ? AND cue_type = 'skip_bit'",
                        (f"Always skips this — skipped {skips} times", youtube_id),
                    )
                    await db.commit()
                    return False
                await db.execute(
                    "INSERT INTO song_cues (youtube_id, cue_type, cue_text) VALUES (?, 'skip_bit', ?)",
                    (youtube_id, f"Always skips this — skipped {skips} times"),
                )
                await db.commit()
                logger.info("Skip-bit detected: %s (%d skips)", youtube_id, skips)
                return True
        return False
    except Exception:
        logger.warning("detect_skip_bit failed for %s", youtube_id, exc_info=True)
        return False
