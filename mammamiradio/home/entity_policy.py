"""Local operator policy for Home Assistant entity usage."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_FILENAME = "ha_entity_policy.json"
SCHEMA_VERSION = 1
_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_LOCK = threading.RLock()


def policy_path(cache_dir: Path) -> Path:
    """Return the durable entity-policy path below the runtime state dir."""
    return Path(cache_dir) / "state" / POLICY_FILENAME


def valid_entity_id(entity_id: str) -> bool:
    """Return True when ``entity_id`` has the Home Assistant domain.object shape."""
    return bool(_ENTITY_ID_RE.fullmatch(entity_id or ""))


def _clean_text(value: object, *, max_len: int = 80) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()[:max_len]


def _clean_entry(entity_id: str, entry: object) -> dict[str, Any] | None:
    if not valid_entity_id(entity_id) or not isinstance(entry, dict):
        return None
    muted_at = entry.get("muted_at")
    if not isinstance(muted_at, int | float):
        muted_at = time.time()
    domain = _clean_text(entry.get("domain"), max_len=32) or entity_id.split(".", 1)[0]
    return {
        "muted_at": float(muted_at),
        "label": _clean_text(entry.get("label"), max_len=120),
        "domain": domain,
        "area": _clean_text(entry.get("area"), max_len=80),
    }


def empty_policy() -> dict[str, Any]:
    """Return an empty policy payload."""
    return {"schema_version": SCHEMA_VERSION, "muted": {}}


def load_entity_policy(cache_dir: Path) -> dict[str, Any]:
    """Load entity policy; malformed/missing files degrade to an empty policy."""
    path = policy_path(cache_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_policy()
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Ignoring malformed HA entity policy %s: %s", path, exc)
        return empty_policy()
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed HA entity policy %s: root is not an object", path)
        return empty_policy()
    muted_raw = data.get("muted")
    if not isinstance(muted_raw, dict):
        return empty_policy()
    muted: dict[str, dict[str, Any]] = {}
    for entity_id, entry in muted_raw.items():
        if not isinstance(entity_id, str):
            continue
        clean = _clean_entry(entity_id, entry)
        if clean is not None:
            muted[entity_id] = clean
    return {"schema_version": SCHEMA_VERSION, "muted": muted}


def muted_entity_ids(cache_dir: Path | None) -> set[str]:
    """Return the ids explicitly muted by the local operator."""
    if cache_dir is None:
        return set()
    policy = load_entity_policy(Path(cache_dir))
    muted = policy.get("muted", {})
    return set(muted.keys()) if isinstance(muted, dict) else set()


def _write_policy(cache_dir: Path, policy: dict[str, Any]) -> None:
    path = policy_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def set_entity_muted(
    cache_dir: Path,
    entity_id: str,
    muted: bool,
    *,
    label: object = "",
    domain: object = "",
    area: object = "",
    now: float | None = None,
) -> dict[str, Any]:
    """Persist an idempotent mute/unmute update and return the saved policy."""
    if not valid_entity_id(entity_id):
        raise ValueError("invalid entity_id")
    with _LOCK:
        policy = load_entity_policy(cache_dir)
        muted_map = dict(policy.get("muted", {}) if isinstance(policy.get("muted"), dict) else {})
        if muted:
            muted_map[entity_id] = {
                "muted_at": float(time.time() if now is None else now),
                "label": _clean_text(label, max_len=120),
                "domain": _clean_text(domain, max_len=32) or entity_id.split(".", 1)[0],
                "area": _clean_text(area, max_len=80),
            }
        else:
            muted_map.pop(entity_id, None)
        next_policy = {"schema_version": SCHEMA_VERSION, "muted": muted_map}
        _write_policy(cache_dir, next_policy)
        return next_policy
