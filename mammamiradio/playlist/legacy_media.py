"""One-time retirement of exact persistent external-media artifacts.

This is deliberately not a quarantine system. It recognizes only cache names,
SQLite rows, and restart-handoff metadata written by released YouTube/Jamendo
paths. Unknown cache bytes and operator-owned ``music/`` files are preserved;
the runtime admission gates keep unknown bytes ineligible.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mammamiradio.core.path_safety import safe_path_within

logger = logging.getLogger(__name__)

RECONCILIATION_VERSION = 1
RECONCILIATION_MARKER = ".legacy_external_media_reconciled_v1.json"

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SLUGGED_YOUTUBE_ID = r"youtube_[a-z0-9_-]{11}"
_SOURCE_SUFFIX_YOUTUBE = r"[a-z0-9_]+_youtube"
_JAMENDO_ID = r"jamendo_jamendo_[0-9]+"
_EXTERNAL_CACHE_KEY = rf"(?:{_SLUGGED_YOUTUBE_ID}|{_SOURCE_SUFFIX_YOUTUBE}|{_JAMENDO_ID})"
_RECOGNIZED_CACHE_ARTIFACTS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"^{_EXTERNAL_CACHE_KEY}\.(?:m4a|mp3|part|tmp|webm)$",
        rf"^{_EXTERNAL_CACHE_KEY}\.mp3\.json$",
        rf"^{_EXTERNAL_CACHE_KEY}\.[0-9a-f]{{32}}\.tmp$",
        rf"^norm_{_EXTERNAL_CACHE_KEY}_[1-9][0-9]*k\.mp3(?:\.json)?$",
        rf"^fm_norm_{_EXTERNAL_CACHE_KEY}_.+\.mp3$",
        rf"^_failed_{_EXTERNAL_CACHE_KEY}\.mp3$",
    )
)
_EXTERNAL_HANDOFF_SOURCES = frozenset({"charts", "classic", "jamendo", "youtube", "yt-dlp", "ytdlp"})


@dataclass(frozen=True)
class LegacyMediaReconciliation:
    already_complete: bool = False
    removed_files: int = 0
    removed_handoff_entries: int = 0
    removed_db_rows: int = 0


def reconciliation_marker_path(cache_dir: Path) -> Path:
    return cache_dir / "state" / RECONCILIATION_MARKER


def reconcile_legacy_external_media(cache_dir: Path, db_path: Path) -> LegacyMediaReconciliation:
    """Remove exact released persistent external-media state once.

    The marker is published last. Any exception therefore leaves the migration
    resumable on the next boot; every deletion and database statement is
    idempotent.
    """
    cache_dir = Path(cache_dir)
    marker = reconciliation_marker_path(cache_dir)
    if _valid_marker(marker):
        return LegacyMediaReconciliation(already_complete=True)

    removed_files = _remove_recognized_cache_files(cache_dir)
    handoff_entries, handoff_files = _remove_external_handoff_entries(cache_dir)
    removed_files += handoff_files
    db_rows, db_files = _remove_external_db_rows(cache_dir, db_path)
    removed_files += db_files

    _publish_marker(marker)
    result = LegacyMediaReconciliation(
        removed_files=removed_files,
        removed_handoff_entries=handoff_entries,
        removed_db_rows=db_rows,
    )
    if removed_files or handoff_entries or db_rows:
        logger.info(
            "Legacy external-media reconciliation removed %d file(s), %d handoff entry/entries, and %d database row(s)",
            removed_files,
            handoff_entries,
            db_rows,
        )
    return result


def _valid_marker(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("schema_version") == RECONCILIATION_VERSION


def _recognized_cache_artifact(name: str) -> bool:
    return any(pattern.fullmatch(name) for pattern in _RECOGNIZED_CACHE_ARTIFACTS)


def _remove_recognized_cache_files(cache_dir: Path) -> int:
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for path in cache_dir.iterdir():
        if path.is_file() and _recognized_cache_artifact(path.name):
            removed += _remove_file(path, cache_dir)

    ytdlp_tmp = cache_dir / ".ytdlp_tmp"
    if ytdlp_tmp.is_dir() and not ytdlp_tmp.is_symlink():
        resolved = safe_path_within(ytdlp_tmp, cache_dir, reject_symlinks=True)
        if resolved is not None:
            shutil.rmtree(resolved)
            removed += 1
    return removed


def _handoff_entry_is_external(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        return False
    source = (
        str(metadata.get("source") or metadata.get("source_kind") or metadata.get("audio_source") or "")
        .strip()
        .casefold()
    )
    youtube_id = str(metadata.get("youtube_id") or "").strip()
    provider = str(metadata.get("provider") or "").strip().casefold()
    return source in _EXTERNAL_HANDOFF_SOURCES or provider == "jamendo" or bool(_YOUTUBE_ID_RE.fullmatch(youtube_id))


def _remove_external_handoff_entries(cache_dir: Path) -> tuple[int, int]:
    handoff_root = cache_dir / "restart_handoff"
    manifest_path = handoff_root / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0, 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Legacy external-media reconciliation left an unreadable handoff manifest untouched")
        return 0, 0
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return 0, 0
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return 0, 0

    retained: list[object] = []
    removed_entries: list[dict[str, Any]] = []
    for raw in entries:
        if _handoff_entry_is_external(raw):
            assert isinstance(raw, dict)
            removed_entries.append(raw)
        else:
            retained.append(raw)
    if not removed_entries:
        return 0, 0

    removed_files = 0
    segments_root = handoff_root / "segments"
    for entry in removed_entries:
        relative_path = entry.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path:
            continue
        candidate = handoff_root / relative_path
        resolved = safe_path_within(candidate, segments_root, reject_symlinks=True)
        if resolved is not None:
            removed_files += _remove_file(resolved, segments_root)

    updated = dict(payload)
    updated["entries"] = retained
    _atomic_write_json(manifest_path, updated)
    return len(removed_entries), removed_files


def _remove_external_db_rows(cache_dir: Path, db_path: Path) -> tuple[int, int]:
    db_path = Path(db_path)
    if not db_path.exists():
        return 0, 0

    removed_files = 0
    removed_rows = 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "tracks"):
            return 0, 0
        rows = conn.execute("SELECT id, youtube_id, file_path FROM tracks").fetchall()
        external_rows = [row for row in rows if _YOUTUBE_ID_RE.fullmatch(str(row["youtube_id"] or ""))]

        # Delete bytes before references. A crash in this phase leaves a missing
        # path that the same idempotent pass can safely finish next boot.
        for row in external_rows:
            resolved = _resolve_db_cache_path(cache_dir, str(row["file_path"] or ""))
            if resolved is not None:
                removed_files += _remove_file(resolved, cache_dir)

        track_ids = [int(row["id"]) for row in external_rows]
        youtube_ids = [str(row["youtube_id"]) for row in external_rows]
        with conn:
            if track_ids and _table_exists(conn, "play_history"):
                removed_rows += _delete_where_in(conn, "play_history", "track_id", track_ids)
            if youtube_ids and _table_exists(conn, "track_rules"):
                removed_rows += _delete_where_in(conn, "track_rules", "youtube_id", youtube_ids)
            if youtube_ids and _table_exists(conn, "song_cues"):
                removed_rows += _delete_where_in(conn, "song_cues", "youtube_id", youtube_ids)
            if track_ids:
                removed_rows += _delete_where_in(conn, "tracks", "id", track_ids)
    finally:
        conn.close()
    return removed_rows, removed_files


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _delete_where_in(conn: sqlite3.Connection, table: str, column: str, values: list[int] | list[str]) -> int:
    # Table and column are internal literals from the calls above; values remain
    # bound parameters.
    placeholders = ",".join("?" for _ in values)
    cursor = conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", values)
    return max(0, int(cursor.rowcount or 0))


def _resolve_db_cache_path(cache_dir: Path, raw: str) -> Path | None:
    if not raw:
        return None
    value = Path(raw)
    candidates = [value] if value.is_absolute() else [cache_dir / value, value]
    for candidate in candidates:
        resolved = safe_path_within(candidate, cache_dir, reject_symlinks=True)
        if resolved is None or _is_operator_music_path(resolved, cache_dir):
            continue
        if resolved.exists():
            return resolved
    return None


def _is_operator_music_path(path: Path, cache_dir: Path) -> bool:
    try:
        relative = path.relative_to(cache_dir.resolve(strict=False))
    except (OSError, ValueError):
        return False
    return bool(relative.parts and relative.parts[0] == "music")


def _remove_file(path: Path, root: Path) -> int:
    resolved = safe_path_within(path, root, reject_symlinks=True)
    if resolved is None:
        return 0
    try:
        resolved.unlink()
    except FileNotFoundError:
        return 0
    return 1


def _publish_marker(path: Path) -> None:
    _atomic_write_json(path, {"schema_version": RECONCILIATION_VERSION})
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
