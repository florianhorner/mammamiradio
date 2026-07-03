"""Contract tests for the Home Assistant LLM scene namer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState
from mammamiradio.home.ha_context import HomeContext, ScoredEntity

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture
def config(tmp_path):
    cfg = load_config(TOML_PATH)
    cfg.cache_dir = tmp_path
    cfg.anthropic_api_key = "sk-ant-test"
    cfg.homeassistant.mood_llm_enabled = True
    cfg.homeassistant.mood_ttl_seconds = 60.0
    return cfg


@pytest.fixture
def state():
    return StationState()


@pytest.fixture(autouse=True)
def reset_scene_cache():
    from mammamiradio.home.scene_namer import reset_scene_namer_cache

    reset_scene_namer_cache()
    yield
    reset_scene_namer_cache()


def _entity(entity_id: str = "light.kitchen") -> ScoredEntity:
    return ScoredEntity(
        entity_id=entity_id,
        area="Kitchen",
        domain=entity_id.split(".", 1)[0],
        score=1.7,
        raw_state={"state": "on", "attributes": {"friendly_name": "Kitchen light"}},
        label_it="Luce cucina",
        label_en="Kitchen light",
        label_tier="catalog",
        summary_line="Luce cucina: accesa",
    )


def _home_context(*, mood: str = "Musica in casa", scored: list[ScoredEntity] | None = None) -> HomeContext:
    return HomeContext(
        raw_states={},
        mood=mood,
        mood_en="Music at home" if mood else "",
        scored=scored if scored is not None else [_entity()],
        summary="Luce cucina: accesa",
        timestamp=123.0,
    )


def _response(payload: str, *, input_tokens: int = 13, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=payload)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


@pytest.mark.asyncio
async def test_resolve_returns_ladder_immediately_then_cached_generated_scene(config, state, monkeypatch):
    from mammamiradio.home import scene_namer

    calls: list[dict] = []

    async def _call(_config, scored, *, local_hour, model):
        calls.append({"entities": [entity.entity_id for entity in scored], "local_hour": local_hour, "model": model})
        return _response('{"mood_it":"La cucina si accende","mood_en":"Kitchen waking up"}')

    monkeypatch.setattr(scene_namer, "_call_anthropic_scene", _call)

    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Musica in casa")) == (
        "Musica in casa",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == [
        {
            "entities": ["light.kitchen"],
            "local_hour": calls[0]["local_hour"],
            "model": "claude-haiku-4-5-20251001",
        }
    ]
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Lavatrice in funzione")) == (
        "La cucina si accende",
        "Kitchen waking up",
    )
    assert state.api_calls_by_category["script_home_mood"] == 1
    assert state.api_tokens_by_category_model["script_home_mood"]["claude-haiku-4-5-20251001"] == {
        "input": 13,
        "output": 5,
    }


@pytest.mark.asyncio
async def test_fresh_generated_cache_does_not_call_until_ttl_expires(config, state, monkeypatch):
    from mammamiradio.home import scene_namer

    now = 1_000.0
    calls = 0

    async def _call(_config, _scored, *, local_hour, model):
        nonlocal calls
        calls += 1
        return _response(
            f'{{"mood_it":"Scena generata {calls}","mood_en":"Generated scene {calls}"}}',
            input_tokens=1,
            output_tokens=1,
        )

    monkeypatch.setattr(scene_namer.time, "time", lambda: now)
    monkeypatch.setattr(scene_namer, "_call_anthropic_scene", _call)

    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Musica in casa")) == (
        "Musica in casa",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 1

    now += 30.0
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Lavatrice in funzione")) == (
        "Scena generata 1",
        "Generated scene 1",
    )
    await asyncio.sleep(0)
    assert calls == 1

    now += 31.0
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Serata cinema")) == (
        "Serata cinema",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 2
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Serata cinema")) == (
        "Scena generata 2",
        "Generated scene 2",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "api_key", "scored"),
    [
        (False, "sk-ant-test", [_entity()]),
        (True, "", [_entity()]),
        (True, "sk-ant-test", []),
    ],
)
async def test_no_background_call_when_disabled_missing_key_or_empty_scored_set(
    config, state, monkeypatch, enabled, api_key, scored
):
    from mammamiradio.home import scene_namer

    async def _call(*_args, **_kwargs):
        raise AssertionError("LLM home mood generation should not be scheduled")

    config.homeassistant.mood_llm_enabled = enabled
    config.anthropic_api_key = api_key
    monkeypatch.setattr(scene_namer, "_call_anthropic_scene", _call)

    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Musica in casa", scored=scored)) == (
        "Musica in casa",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_generation_failure_gates_immediate_retry(config, state, monkeypatch):
    from mammamiradio.home import scene_namer

    now = 1_000.0
    calls = 0

    async def _call(_config, _scored, *, local_hour, model):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("anthropic down")
        return _response('{"mood_it":"Seconda scena","mood_en":"Second scene"}', input_tokens=2, output_tokens=3)

    monkeypatch.setattr(scene_namer.time, "time", lambda: now)
    monkeypatch.setattr(scene_namer, "_call_anthropic_scene", _call)

    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Musica in casa")) == (
        "Musica in casa",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 1

    now += 1.0
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Lavatrice in funzione")) == (
        "Lavatrice in funzione",
        "Music at home",
    )
    await asyncio.sleep(0)
    assert calls == 1

    now += 60.0
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Serata cinema")) == (
        "Serata cinema",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 2
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Musica in casa")) == (
        "Seconda scena",
        "Second scene",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"mood_it":"light.kitchen","mood_en":"Kitchen"}',
        '{"mood_it":"Serata {debug}","mood_en":"Debug night"}',
    ],
)
async def test_invalid_llm_payload_keeps_ladder(config, state, monkeypatch, payload):
    from mammamiradio.home import scene_namer

    calls = 0

    async def _call(_config, _scored, *, local_hour, model):
        nonlocal calls
        calls += 1
        return _response(payload)

    monkeypatch.setattr(scene_namer, "_call_anthropic_scene", _call)

    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Musica in casa")) == (
        "Musica in casa",
        "Music at home",
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == 1
    assert scene_namer.resolve_home_mood(config, state, _home_context(mood="Lavatrice in funzione")) == (
        "Lavatrice in funzione",
        "Music at home",
    )
    assert state.api_calls_by_category["script_home_mood"] == 1
    assert state.api_tokens_by_category_model["script_home_mood"]["claude-haiku-4-5-20251001"] == {
        "input": 13,
        "output": 5,
    }
