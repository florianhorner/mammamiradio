"""Per-track personality rules — flagged reactions accumulate over time."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def get_rules(db_path: Path, youtube_id: str) -> list[str]:
    """Return all rules flagged for a track."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT rule_text FROM track_rules WHERE youtube_id = ? ORDER BY created_at",
            (youtube_id,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning("track_rules.get_rules failed: %s", e)
        return []


def add_rule(db_path: Path, youtube_id: str, rule_text: str) -> None:
    """Persist a new rule for a track."""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO track_rules (youtube_id, rule_text) VALUES (?, ?)",
            (youtube_id, rule_text.strip()[:200]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("track_rules.add_rule failed: %s", e)
