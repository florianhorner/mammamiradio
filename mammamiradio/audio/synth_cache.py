"""Small persistent cache for generated synthetic MP3 layers."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from mammamiradio.audio.normalizer import _MP3_OUTPUT_ARGS

logger = logging.getLogger(__name__)

SYNTH_CACHE_VERSION = "2"
SYNTH_CACHE_PREFIX = "synth_"
SYNTH_VARIANT_POOL_SIZE = 3

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_variants_guard = threading.Lock()
_last_variants: dict[str, int] = {}


def duration_bucket_sec(duration_sec: float) -> int:
    """Round a requested duration up so cached beds are never too short."""
    return max(1, math.ceil(max(float(duration_sec), 0.0)))


def next_synth_variant(kind: str, params: Mapping[str, Any], *, pool_size: int = SYNTH_VARIANT_POOL_SIZE) -> int:
    """Return a bounded variant index without repeating the previous value."""
    pool = max(1, int(pool_size))
    if pool == 1:
        return 0

    key = _cache_key(kind, params, variant=None)
    with _variants_guard:
        last = _last_variants.get(key, -1)
        variant = (last + 1) % pool
        _last_variants[key] = variant
        return variant


def materialize_synth_mp3(
    cache_dir: Path | None,
    kind: str,
    output_path: Path,
    params: Mapping[str, Any],
    generator: Callable[[Path], Path | None],
    *,
    variant: int = 0,
) -> Path:
    """Materialize a generated synthetic MP3 via cache when possible.

    Cache infrastructure errors fall back to direct generation at ``output_path``.
    Generator errors still propagate so existing caller fallback behavior remains
    unchanged.
    """
    if cache_dir is None:
        generator(output_path)
        return output_path

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = _cache_key(kind, params, variant=variant)
        cache_path = cache_dir / f"{SYNTH_CACHE_PREFIX}{_safe_kind(kind)}_{key}.mp3"
        lock = _lock_for(key)
    except Exception as exc:
        logger.warning("Synthetic cache unavailable for %s, generating directly: %s", kind, exc)
        generator(output_path)
        return output_path

    with lock:
        try:
            if _valid_mp3(cache_path):
                shutil.copy2(cache_path, output_path)
                _touch_atime(cache_path)
                return output_path

            staging = cache_dir / f".{cache_path.stem}.{uuid4().hex}.tmp.mp3"
            try:
                generator(staging)
                if not _valid_mp3(staging):
                    staging.unlink(missing_ok=True)
                    return output_path
                os.replace(staging, cache_path)
            finally:
                staging.unlink(missing_ok=True)

            shutil.copy2(cache_path, output_path)
            _touch_atime(cache_path)
            return output_path
        except Exception as exc:
            logger.warning("Synthetic cache failed for %s, generating directly: %s", kind, exc)
            generator(output_path)
            return output_path


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _cache_key(kind: str, params: Mapping[str, Any], *, variant: int | None) -> str:
    payload = {
        "audio": _MP3_OUTPUT_ARGS,
        "kind": kind,
        "params": _normalize(params),
        "variant": variant,
        "version": SYNTH_CACHE_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:24]


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return repr(value)


def _safe_kind(kind: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in kind.strip().lower()).strip("_") or "audio"


def _valid_mp3(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _touch_atime(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass
