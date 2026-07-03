"""Background LLM scene names for Home Assistant home mood.

The existing heuristic mood ladder remains the synchronous fallback. This module
only reads a fresh cached LLM scene, or schedules a background refresh and
returns the ladder result for the current segment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mammamiradio.core.config import resolve_model
from mammamiradio.home.catalog import _response_text

if TYPE_CHECKING:
    from mammamiradio.core.config import StationConfig
    from mammamiradio.core.models import StationState
    from mammamiradio.home.ha_context import HomeContext, ScoredEntity

logger = logging.getLogger(__name__)

MAX_SCENE_LENGTH = 60
MAX_SCENE_ENTITIES = 12

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_HEX_TOKEN_RE = re.compile(r"^[a-fA-F0-9]{32,}$")
_PROMPT_INJECTION_RE = re.compile(r"(ignore previous|disregard|system override|forget your|</?home_state_data)", re.I)


@dataclass(frozen=True)
class _SceneCache:
    mood_it: str
    mood_en: str
    generated_at: float


_scene_cache: _SceneCache | None = None
_last_attempt: float = 0.0
_generation_task: asyncio.Task | None = None


def reset_scene_namer_cache() -> None:
    """Clear in-memory scene state for tests and hot-reload style workflows."""
    global _scene_cache, _last_attempt, _generation_task
    _scene_cache = None
    _last_attempt = 0.0
    _generation_task = None


def resolve_home_mood(config: StationConfig, state: StationState, ha_cache: HomeContext) -> tuple[str, str]:
    """Return the active home mood, scheduling an LLM refresh when safe."""
    ladder = (ha_cache.mood, ha_cache.mood_en)
    ha_cfg = config.homeassistant
    if not getattr(ha_cfg, "mood_llm_enabled", False):
        return ladder
    if not config.anthropic_api_key:
        return ladder
    ttl = float(getattr(ha_cfg, "mood_ttl_seconds", 90.0) or 90.0)
    now = time.time()
    if _scene_cache is not None and now - _scene_cache.generated_at < ttl:
        return _scene_cache.mood_it, _scene_cache.mood_en
    scored = [entity for entity in getattr(ha_cache, "scored", []) if str(entity.summary_line or "").strip()]
    if not scored:
        return ladder
    _schedule_generation(config, state, scored[:MAX_SCENE_ENTITIES], ttl=ttl, now=now)
    return ladder


def _schedule_generation(
    config: StationConfig,
    state: StationState,
    scored: list[ScoredEntity],
    *,
    ttl: float,
    now: float,
) -> bool:
    global _generation_task, _last_attempt
    if _generation_task is not None and not _generation_task.done():
        return False
    if _last_attempt and now - _last_attempt < ttl:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _last_attempt = now
    snapshot = tuple(scored)
    local_hour = time.localtime(now).tm_hour
    _generation_task = loop.create_task(_generate_scene(config, state, snapshot, local_hour=local_hour))
    return True


async def _generate_scene(
    config: StationConfig,
    state: StationState,
    scored: tuple[ScoredEntity, ...],
    *,
    local_hour: int,
) -> None:
    global _scene_cache
    try:
        model = resolve_model(config.models, "home_mood", "anthropic")
        response = await _call_anthropic_scene(config, scored, local_hour=local_hour, model=model)
        usage = getattr(response, "usage", None)
        if usage is not None:
            state.record_llm_usage(
                "script_home_mood",
                model,
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
            )
        scene = _parse_scene_payload(_response_text(response), scored)
        if scene is None:
            return
        _scene_cache = _SceneCache(scene[0], scene[1], time.time())
    except Exception as exc:
        logger.warning("HA scene naming failed; keeping heuristic mood: %s", exc)


async def _call_anthropic_scene(
    config: StationConfig,
    scored: tuple[ScoredEntity, ...],
    *,
    local_hour: int,
    model: str,
) -> object:
    from anthropic import AsyncAnthropic

    lines = [entity.summary_line for entity in scored if entity.summary_line]
    prompt = (
        "Name the current home scene for an Italian radio host. "
        "Return only JSON with mood_it and mood_en. Each value must be a short "
        "scene name, 2-6 words, not a sentence, and must not include entity IDs.\n\n"
        + json.dumps({"local_hour": local_hour, "home_state": lines}, ensure_ascii=False, sort_keys=True)
    )
    client = AsyncAnthropic(api_key=config.anthropic_api_key)
    return await client.with_options(timeout=45.0).messages.create(
        model=model,
        max_tokens=200,
        system="You create safe, concise scene names from home-state summaries.",
        messages=[{"role": "user", "content": prompt}],
    )


def _parse_scene_payload(text: str, scored: tuple[ScoredEntity, ...]) -> tuple[str, str] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("HA scene naming returned unparseable JSON; keeping heuristic mood")
        return None
    if not isinstance(data, dict):
        return None
    mood_it = _clean_scene(data.get("mood_it"))
    mood_en = _clean_scene(data.get("mood_en"))
    if not (_validate_scene(mood_it, scored) and _validate_scene(mood_en, scored)):
        logger.warning("HA scene naming returned unsafe scene; keeping heuristic mood")
        return None
    return mood_it, mood_en


def _clean_scene(value: object) -> str:
    text = str(value or "").strip()
    text = _CONTROL_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _validate_scene(scene: str, scored: tuple[ScoredEntity, ...]) -> bool:
    if not scene:
        return False
    if len(scene) > MAX_SCENE_LENGTH:
        return False
    if "\n" in scene or "\r" in scene or _CONTROL_RE.search(scene):
        return False
    if "{" in scene or "}" in scene or "<" in scene or ">" in scene:
        return False
    if _PROMPT_INJECTION_RE.search(scene):
        return False
    if _IP_RE.search(scene) or _EMAIL_RE.search(scene) or _TOKEN_RE.fullmatch(scene) or _HEX_TOKEN_RE.fullmatch(scene):
        return False
    lowered = scene.lower()
    for entity in scored:
        entity_id = entity.entity_id.lower()
        object_id = entity_id.split(".", 1)[-1]
        if entity_id in lowered or ("_" in object_id and object_id in lowered):
            return False
    return True
