"""Persistent operator song blocklist.

A banned ``(artist, title)`` never re-enters the rotation pool — across HA addon
restarts and across every music source (charts, Jamendo, yt-dlp, local). Stored as
``blocklist.json`` under the cache dir, so it inherits the addon (``/data/cache``)
vs standalone (``./cache``) resolution like every other persisted file.

File I/O is best-effort and corrupt-tolerant: a missing or malformed file yields an
empty blocklist and never raises into the audio path (mirrors the norm-sidecar and
ledger tolerance rules). Writes are atomic (tmp + ``os.replace``).

Identity model (the single key definition lives in ``playlist.normalized_track_key``;
this module only ever stores/loads already-computed tuple keys):

    key = (artist.strip().lower(), title.strip().lower())

JSON object keys cannot be tuples, so each row serializes its key as
``"<artist>\\x1f<title>"`` (ASCII unit separator — never appears in song text).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BlockKey = tuple[str, str]

_KEY_SEP = "\x1f"


def blocklist_path(cache_dir: Path | str) -> Path:
    """Canonical on-disk location for the persisted blocklist."""
    return Path(cache_dir) / "blocklist.json"


def _encode_key(key: BlockKey) -> str:
    return f"{key[0]}{_KEY_SEP}{key[1]}"


def _decode_key(raw: str) -> BlockKey | None:
    if _KEY_SEP not in raw:
        return None
    artist, title = raw.split(_KEY_SEP, 1)
    return (artist, title)


def _coerce_banned_at(value: object) -> float:
    """Best-effort timestamp coercion. A corrupt/hand-edited blocklist.json with a
    non-numeric ``banned_at`` (string, list, ...) must NOT crash the load — it would
    take the whole startup down with it (dead air). A bad value falls back to ``0.0``
    so the row simply sorts oldest in the banlist rather than raising."""
    if value is None:
        return time.time()
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def block_meta(display: str = "", *, banned_by: str = "operator", banned_at: float | None = None) -> dict:
    """Build a blocklist row's metadata (display + provenance) for the unban view."""
    return {
        "display": str(display or ""),
        "banned_by": str(banned_by or "operator"),
        "banned_at": _coerce_banned_at(banned_at),
    }


def load_blocklist(cache_dir: Path | str) -> dict[BlockKey, dict]:
    """Load the persisted blocklist as ``{key: meta}``.

    Returns ``{}`` for a missing or corrupt file — a parse failure must never
    silently un-ban every song by raising, and must never reach the audio path.
    """
    path = blocklist_path(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("blocklist.json is unreadable — ignoring it (songs stay un-banned)")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[BlockKey, dict] = {}
    for raw_key, meta in data.items():
        key = _decode_key(raw_key) if isinstance(raw_key, str) else None
        if key is None:
            continue
        meta = meta if isinstance(meta, dict) else {}
        out[key] = block_meta(
            meta.get("display", ""),
            banned_by=meta.get("banned_by", "operator"),
            banned_at=meta.get("banned_at", 0.0) or 0.0,
        )
    return out


def save_blocklist(cache_dir: Path | str, blocklist: dict[BlockKey, dict]) -> bool:
    """Persist the blocklist atomically (tmp + ``os.replace``). Best-effort.

    Returns ``True`` on success. A failed write logs and returns ``False`` — the
    in-memory ban still holds for the session; only durability across restart is
    lost, and the caller stays on the air either way.
    """
    path = blocklist_path(cache_dir)
    payload = {_encode_key(key): meta for key, meta in blocklist.items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".blocklist-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception as exc:
        # Best-effort + never-raises is the whole contract: a full disk (OSError),
        # an un-encodable title (UnicodeEncodeError), or any other write failure
        # must leave the in-memory ban holding for the session and the caller on
        # the air — only durability across restart is lost.
        logger.warning("Failed to persist blocklist.json: %s", exc)
        return False
