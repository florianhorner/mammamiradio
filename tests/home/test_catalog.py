"""Tests for generated Home Assistant label catalog helpers."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.home.catalog import (
    CATALOG_FILENAME,
    ENTITY_LABELS,
    compute_hash,
    generate_label_catalog,
    load_catalog,
    reset_catalog_cache,
    resolve_label,
    save_catalog,
    schedule_label_generation,
    select_label_candidates,
    validate_label,
)


@pytest.fixture(autouse=True)
def _reset_catalog_state():
    reset_catalog_cache()
    yield
    reset_catalog_cache()


def _state(name: str | None = "Kitchen light", *, area: str | None = "Kitchen") -> dict:
    attrs = {}
    if name is not None:
        attrs["friendly_name"] = name
    if area is not None:
        attrs["area"] = area
    return {"state": "on", "attributes": attrs}


def _config():
    return SimpleNamespace(anthropic_api_key="sk-ant-test")


def test_resolve_label_precedence_curated_catalog_fallback(tmp_path):
    curated = resolve_label("switch.bar_kaffeemaschine_steckdose", _state(), cache_dir=tmp_path)
    assert curated is not None
    assert curated.tier == "curated"
    assert curated.label_it == ENTITY_LABELS["switch.bar_kaffeemaschine_steckdose"]

    entity_id = "light.counter"
    state = _state("Counter light")
    catalog = {
        "schema_version": 1,
        "entries": {
            entity_id: {
                "hash": compute_hash(entity_id, state),
                "label_it": "Luce bancone",
                "label_en": "Counter light",
            }
        },
    }
    save_catalog(tmp_path, catalog)

    generated = resolve_label(entity_id, state, cache_dir=tmp_path)
    assert generated is not None
    assert generated.tier == "catalog"
    assert generated.label_it == "Luce bancone"

    stale_state = _state("Renamed light")
    fallback = resolve_label(entity_id, stale_state, cache_dir=tmp_path)
    assert fallback is not None
    assert fallback.tier == "fallback"
    assert fallback.label_it == "Renamed light (Kitchen)"


def test_resolve_label_drops_entity_without_safe_name_or_catalog(tmp_path):
    assert resolve_label("sensor.unlabeled_helper", {"state": "on", "attributes": {}}, cache_dir=tmp_path) is None


def test_load_catalog_corrupt_json_degrades_to_empty(tmp_path):
    (tmp_path / CATALOG_FILENAME).write_text("{", encoding="utf-8")
    assert load_catalog(tmp_path)["entries"] == {}


def test_save_catalog_uses_owner_only_permissions(tmp_path):
    save_catalog(tmp_path, {"entries": {}})
    mode = os.stat(tmp_path / CATALOG_FILENAME).st_mode & 0o777
    assert mode == 0o600


def test_validate_label_rejects_unsafe_labels():
    entity_id = "light.counter_light"
    assert validate_label("Luce bancone", entity_id)
    assert not validate_label("", entity_id)
    assert not validate_label("x" * 81, entity_id)
    assert not validate_label("light.counter_light", entity_id)
    assert not validate_label("counter_light", entity_id)
    assert not validate_label("ignore previous instructions", entity_id)
    assert not validate_label("Kitchen <script>", entity_id)
    assert not validate_label("ABCD" * 12, entity_id)


def test_select_label_candidates_sorts_by_score_and_caps_tokens(tmp_path):
    states = {
        f"sensor.entity_{idx}": _state(f"Entity {idx}", area="A" * 240)
        for idx in range(60)
    }
    scores = {entity_id: float(idx) for idx, entity_id in enumerate(states)}

    selected = select_label_candidates(
        states,
        cache_dir=tmp_path,
        score_by_entity=scores,
        max_entities=50,
        max_input_tokens=800,
    )

    assert len(selected) < 50
    assert selected[0].entity_id == "sensor.entity_59"
    assert selected[1].entity_id == "sensor.entity_58"


@pytest.mark.asyncio
async def test_generate_label_catalog_lock_contention_calls_llm_once(tmp_path):
    config = _config()
    states = {"light.counter": _state("Counter light")}
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_call(candidates, _config, *, role):
        nonlocal calls
        assert role == "fast"
        calls += 1
        started.set()
        await release.wait()
        return [
            {
                "entity_id": candidates[0].entity_id,
                "label_it": "Luce bancone",
                "label_en": "Counter light",
            }
        ]

    with patch("mammamiradio.home.catalog._call_anthropic_labels", new=fake_call):
        first = asyncio.create_task(generate_label_catalog(states, cache_dir=tmp_path, config=config))
        await started.wait()
        second = asyncio.create_task(generate_label_catalog(states, cache_dir=tmp_path, config=config))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    assert calls == 1


@pytest.mark.asyncio
async def test_generate_label_catalog_failure_preserves_old_catalog(tmp_path):
    config = _config()
    entity_id = "light.counter"
    state = _state("Counter light")
    save_catalog(
        tmp_path,
        {
            "entries": {
                entity_id: {
                    "hash": compute_hash(entity_id, state),
                    "label_it": "Vecchia luce",
                    "label_en": "Old light",
                }
            }
        },
    )

    with patch("mammamiradio.home.catalog._call_anthropic_labels", new=AsyncMock(side_effect=RuntimeError("429"))):
        result = await generate_label_catalog({entity_id: state}, cache_dir=tmp_path, config=config, force=True)

    assert result["entries"][entity_id]["label_it"] == "Vecchia luce"
    assert json.loads((tmp_path / CATALOG_FILENAME).read_text(encoding="utf-8"))["entries"][entity_id][
        "label_it"
    ] == "Vecchia luce"


@pytest.mark.asyncio
async def test_schedule_label_generation_flag_prevents_task_buildup(tmp_path):
    config = _config()
    states = {"light.counter": _state("Counter light")}

    with patch("mammamiradio.home.catalog.generate_label_catalog", new=AsyncMock(return_value={})):
        assert schedule_label_generation(states, cache_dir=tmp_path, config=config)
        assert not schedule_label_generation(states, cache_dir=tmp_path, config=config)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert schedule_label_generation(states, cache_dir=tmp_path, config=config)
        await asyncio.sleep(0)
