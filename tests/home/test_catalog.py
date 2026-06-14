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


@pytest.mark.parametrize(
    "entity_id,friendly_name",
    [
        # HA's default friendly_name for an unnamed entity is the snake_case
        # object_id — must never reach the host (anti-illusion guard).
        ("fan.kuche_lufter_shelly", "kuche_lufter_shelly"),
        # A full dotted entity_id leaking through friendly_name.
        ("light.office", "light.office"),
    ],
)
def test_resolve_label_fallback_drops_raw_object_ids(tmp_path, entity_id, friendly_name):
    state = {"state": "on", "attributes": {"friendly_name": friendly_name}}
    # validate_label rejects these strings; the fallback tier must honor that
    # and drop the entity rather than airing a raw id.
    assert not validate_label(friendly_name, entity_id)
    assert resolve_label(entity_id, state, cache_dir=tmp_path) is None


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
    states = {f"sensor.entity_{idx}": _state(f"Entity {idx}", area="A" * 240) for idx in range(60)}
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
    assert (
        json.loads((tmp_path / CATALOG_FILENAME).read_text(encoding="utf-8"))["entries"][entity_id]["label_it"]
        == "Vecchia luce"
    )


@pytest.mark.asyncio
async def test_generate_label_catalog_persist_failure_preserves_old_catalog(tmp_path):
    """Labels were accepted but the disk write failed: the refresh must not be
    reported as success — keep the old catalog so the next poll retries."""
    config = _config()
    entity_id = "light.counter"
    state = _state("Counter light")
    save_catalog(
        tmp_path,
        {
            "entries": {
                entity_id: {"hash": compute_hash(entity_id, state), "label_it": "Vecchia luce", "label_en": "Old light"}
            }
        },
    )

    fresh_labels = AsyncMock(return_value=[{"entity_id": entity_id, "label_it": "Luce nuova", "label_en": "New light"}])
    with (
        patch("mammamiradio.home.catalog._call_anthropic_labels", new=fresh_labels),
        patch("mammamiradio.home.catalog.save_catalog", return_value=False) as save,
    ):
        result = await generate_label_catalog({entity_id: state}, cache_dir=tmp_path, config=config, force=True)

    save.assert_called_once()
    # Returned catalog and on-disk file both keep the old label, not the new one.
    assert result["entries"][entity_id]["label_it"] == "Vecchia luce"
    assert (
        json.loads((tmp_path / CATALOG_FILENAME).read_text(encoding="utf-8"))["entries"][entity_id]["label_it"]
        == "Vecchia luce"
    )


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


@pytest.mark.asyncio
async def test_schedule_label_generation_resets_flag_after_failure(tmp_path):
    """A failed background refresh must clear the scheduled flag, not strand it
    True forever (which would silently starve all future generation)."""
    config = _config()
    states = {"light.counter": _state("Counter light")}

    with patch(
        "mammamiradio.home.catalog.generate_label_catalog",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        assert schedule_label_generation(states, cache_dir=tmp_path, config=config)
        # Let the background task run to completion (and hit its finally block).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Flag was reset despite the failure: a new refresh can be scheduled.
        assert schedule_label_generation(states, cache_dir=tmp_path, config=config)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


def test_response_text_joins_text_blocks_and_tolerates_shapes():
    from mammamiradio.home.catalog import _response_text

    resp = SimpleNamespace(content=[SimpleNamespace(text="ciao"), {"text": "mondo"}, SimpleNamespace(text=None)])
    assert _response_text(resp) == "ciao\nmondo"
    assert _response_text(SimpleNamespace(content=None)) == ""
    assert _response_text(object()) == ""


def test_resolve_anthropic_fast_model_walks_profile_then_catalog():
    from mammamiradio.home.catalog import _resolve_anthropic_fast_model

    models = SimpleNamespace(
        active_profile="economy",
        default_profile="balanced",
        profiles={"balanced": {"anthropic": {"fast": "haiku"}}},
        catalog={"anthropic": {"haiku": "claude-haiku-test"}},
    )
    config = SimpleNamespace(models=models)
    # active profile has no fast key; falls through to the default profile.
    assert _resolve_anthropic_fast_model(config) == "claude-haiku-test"


@pytest.mark.asyncio
async def test_call_anthropic_labels_builds_client_and_parses_labels():
    from mammamiradio.home.catalog import LabelCandidate, _call_anthropic_labels

    candidate = LabelCandidate(
        entity_id="light.counter",
        score=0.6,
        entity_hash="h",
        metadata={"entity_id": "light.counter", "friendly_name": "Counter"},
    )
    models = SimpleNamespace(
        active_profile="balanced",
        default_profile="balanced",
        profiles={"balanced": {"anthropic": {"fast": "haiku"}}},
        catalog={"anthropic": {"haiku": "claude-haiku-test"}},
    )
    config = SimpleNamespace(anthropic_api_key="sk-ant-test", models=models)

    fake_response = SimpleNamespace(
        content=[
            SimpleNamespace(
                text='{"labels": [{"entity_id": "light.counter", "label_it": "Luce", "label_en": "Light"}]}'
            )
        ]
    )
    create = AsyncMock(return_value=fake_response)
    scoped_client = SimpleNamespace(messages=SimpleNamespace(create=create))
    seen_options: dict[str, object] = {}

    def with_options(**kwargs):
        seen_options.update(kwargs)
        return scoped_client

    client = SimpleNamespace(with_options=with_options)

    with patch("anthropic.AsyncAnthropic", return_value=client):
        labels = await _call_anthropic_labels([candidate], config, role="fast")

    assert labels == [{"entity_id": "light.counter", "label_it": "Luce", "label_en": "Light"}]
    assert create.await_args.kwargs["model"] == "claude-haiku-test"
    # The 45s timeout must be applied (it guards _CATALOG_LOCK from a 10-min stall).
    assert seen_options == {"timeout": 45.0}
