"""Persistent operator song preferences.

Operator likes/dislikes are stored under the configured cache directory as
``song_preferences.json``. This module is intentionally data-only: callers own
when preferences influence scheduling, routes, or UI.

The on-disk key format mirrors ``playlist.blocklist`` so song identity stays
consistent across the playlist data layer:

    ``"<artist>\\x1f<title>"``
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path

logger = logging.getLogger(__name__)

PreferenceKey = tuple[str, str]
PreferenceMeta = dict[str, object]

PREFERENCE_UP_SCORE = 1
PREFERENCE_DOWN_SCORE = -1
PREFERENCE_NEUTRAL_SCORE = 0
PREFERENCE_UP_WEIGHT = 2.5
PREFERENCE_DOWN_WEIGHT = 0.15
PREFERENCE_NEUTRAL_WEIGHT = 1.0
VALID_SCORES = {-1, 1}
_KEY_SEP = "\x1f"


def preferences_path(cache_dir: Path | str) -> Path:
    """Canonical on-disk location for persisted song preferences."""
    return Path(cache_dir) / "song_preferences.json"


def _encode_key(key: PreferenceKey) -> str:
    return f"{key[0]}{_KEY_SEP}{key[1]}"


def _decode_key(raw: str) -> PreferenceKey | None:
    if _KEY_SEP not in raw:
        return None
    artist, title = raw.split(_KEY_SEP, 1)
    return (artist, title)


def _coerce_score(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value in VALID_SCORES:
        return value
    if isinstance(value, float) and value in VALID_SCORES:
        return int(value)
    return None


def preference_score(preferences: object, key: PreferenceKey) -> int:
    """Return a sign-clamped preference score for runtime weighting."""
    if not isinstance(preferences, Mapping):
        return PREFERENCE_NEUTRAL_SCORE
    meta = preferences.get(key)
    if not isinstance(meta, Mapping):
        return PREFERENCE_NEUTRAL_SCORE
    value = meta.get("score")
    if isinstance(value, bool):
        return PREFERENCE_NEUTRAL_SCORE
    try:
        score = int(value) if value is not None else PREFERENCE_NEUTRAL_SCORE
    except (TypeError, ValueError):
        return PREFERENCE_NEUTRAL_SCORE
    if score > 0:
        return PREFERENCE_UP_SCORE
    if score < 0:
        return PREFERENCE_DOWN_SCORE
    return PREFERENCE_NEUTRAL_SCORE


def preference_score_map(preferences: object) -> dict[PreferenceKey, int]:
    """Return normalized runtime scores keyed by canonical song identity."""
    if not isinstance(preferences, Mapping):
        return {}
    scores: dict[PreferenceKey, int] = {}
    for key in preferences:
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        score = preference_score(preferences, key)
        if score:
            scores[(str(key[0]), str(key[1]))] = score
    return scores


def preference_weight(score: int) -> float:
    """Map a normalized score to its soft selection multiplier."""
    if score > 0:
        return PREFERENCE_UP_WEIGHT
    if score < 0:
        return PREFERENCE_DOWN_WEIGHT
    return PREFERENCE_NEUTRAL_WEIGHT


def _coerce_updated_at(value: object) -> float:
    if value is None:
        return time.time()
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def preference_meta(
    score: int,
    display: str = "",
    *,
    updated_at: float | None = None,
    updated_by: str = "operator",
) -> PreferenceMeta:
    """Build normalized preference metadata.

    ``score`` is deliberately constrained to ``-1`` or ``1`` so the persistence
    layer cannot grow ambiguous states before the scheduler/UI contract exists.
    """
    coerced_score = _coerce_score(score)
    if coerced_score is None:
        raise ValueError("song preference score must be -1 or 1")
    return {
        "score": coerced_score,
        "display": str(display or ""),
        "updated_at": _coerce_updated_at(updated_at),
        "updated_by": str(updated_by or "operator"),
    }


def set_preference(
    preferences: MutableMapping[PreferenceKey, PreferenceMeta],
    key: PreferenceKey,
    score: int,
    display: str = "",
    *,
    updated_at: float | None = None,
    updated_by: str = "operator",
) -> PreferenceMeta:
    """Set one in-memory song preference and return its normalized metadata."""
    meta = preference_meta(score, display, updated_at=updated_at, updated_by=updated_by)
    preferences[key] = meta
    return meta


def clear_preference(preferences: MutableMapping[PreferenceKey, PreferenceMeta], key: PreferenceKey) -> bool:
    """Remove one in-memory preference.

    Returns ``True`` when a row was removed and ``False`` when the key was already
    absent, so route code can report honest no-op behavior later.
    """
    if key not in preferences:
        return False
    del preferences[key]
    return True


def _meta_from_raw(meta: object) -> PreferenceMeta | None:
    if not isinstance(meta, Mapping):
        return None
    score = _coerce_score(meta.get("score"))
    if score is None:
        return None
    return preference_meta(
        score,
        str(meta.get("display", "")),
        updated_at=_coerce_updated_at(meta.get("updated_at", 0.0) or 0.0),
        updated_by=str(meta.get("updated_by", "operator") or "operator"),
    )


def load_preferences(cache_dir: Path | str) -> dict[PreferenceKey, PreferenceMeta]:
    """Load persisted song preferences as ``{(artist, title): meta}``.

    Missing, corrupt, or non-object JSON returns ``{}``. Invalid rows are skipped
    so one hand-edited preference cannot disable the whole store.
    """
    path = preferences_path(cache_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("song_preferences.json is unreadable; ignoring it")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[PreferenceKey, PreferenceMeta] = {}
    for raw_key, raw_meta in data.items():
        key = _decode_key(raw_key) if isinstance(raw_key, str) else None
        meta = _meta_from_raw(raw_meta)
        if key is None or meta is None:
            continue
        out[key] = meta
    return out


def _serialize_preferences(preferences: Mapping[PreferenceKey, Mapping[str, object]]) -> dict[str, PreferenceMeta]:
    payload: dict[str, PreferenceMeta] = {}
    for key, meta in preferences.items():
        score = _coerce_score(meta.get("score"))
        if score is None:
            raise ValueError("song preference score must be -1 or 1")
        payload[_encode_key(key)] = preference_meta(
            score,
            str(meta.get("display", "")),
            updated_at=_coerce_updated_at(meta.get("updated_at", 0.0) or 0.0),
            updated_by=str(meta.get("updated_by", "operator") or "operator"),
        )
    return payload


def save_preferences(cache_dir: Path | str, preferences: Mapping[PreferenceKey, Mapping[str, object]]) -> bool:
    """Persist preferences atomically with best-effort failure handling.

    Returns ``True`` on success. Any write or validation failure logs and returns
    ``False``; callers can keep using their in-memory preferences for the session.
    """
    path = preferences_path(cache_dir)
    try:
        payload = _serialize_preferences(preferences)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".song-preferences-", suffix=".tmp")
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
        logger.warning("Failed to persist song_preferences.json: %s", exc)
        return False
