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
    load_catalog_snapshot,
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


def test_load_catalog_snapshot_is_detached_from_module_cache(tmp_path):
    catalog = {
        "schema_version": 1,
        "entries": {
            "light.counter": {
                "hash": "abc",
                "label_it": "Luce bancone",
                "label_en": "Counter light",
            }
        },
    }
    save_catalog(tmp_path, catalog)

    cached = load_catalog(tmp_path)
    snapshot = load_catalog_snapshot(tmp_path)

    assert snapshot == cached
    assert snapshot is not cached
    assert snapshot["entries"] is not cached["entries"]
    snapshot["entries"]["light.counter"]["label_it"] = "Mutata"
    assert cached["entries"]["light.counter"]["label_it"] == "Luce bancone"


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

    async def fake_call(candidates, _config, *, role, policy_epoch):
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


@pytest.mark.asyncio
async def test_scheduled_label_generation_revoked_before_provider_call_sends_nothing(tmp_path):
    """A privacy cutover fences even a task that swallows cancellation before its provider call."""
    import mammamiradio.home.catalog as catalog

    config = _config()
    states = {"light.counter": _state("Counter light")}
    scheduled_entered = asyncio.Event()
    real_generate = catalog.generate_label_catalog

    async def cancellation_resistant_gate(*args, **kwargs):
        scheduled_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Model a dependency that consumes cancellation and continues. The
            # policy epoch, not cooperative cancellation, must be the final fence.
            pass
        return await real_generate(*args, **kwargs)

    provider = AsyncMock(return_value=[{"entity_id": "light.counter", "label_it": "Luce", "label_en": "Light"}])
    with (
        patch("mammamiradio.home.catalog.generate_label_catalog", new=cancellation_resistant_gate),
        patch("mammamiradio.home.catalog._call_anthropic_labels", new=provider),
        patch("mammamiradio.home.catalog.save_catalog") as save,
    ):
        assert schedule_label_generation(states, cache_dir=tmp_path, config=config)
        await asyncio.wait_for(scheduled_entered.wait(), timeout=1.0)
        await catalog.revoke_label_generation()
        await asyncio.sleep(0)

    provider.assert_not_awaited()
    save.assert_not_called()
    assert not catalog.generation_in_progress()
    assert load_catalog(tmp_path)["entries"] == {}
    assert not (tmp_path / CATALOG_FILENAME).exists()


@pytest.mark.asyncio
async def test_cancellation_resistant_label_completion_after_revoke_cannot_save_or_publish(tmp_path):
    """A late provider result from the old privacy era cannot replace the catalog."""
    import mammamiradio.home.catalog as catalog

    config = _config()
    entity_id = "light.counter"
    state = _state("Counter light")
    old_catalog = {
        "schema_version": 1,
        "entries": {
            entity_id: {
                "hash": compute_hash(entity_id, state),
                "label_it": "Vecchia luce",
                "label_en": "Old light",
            }
        },
    }
    save_catalog(tmp_path, old_catalog)
    provider_entered = asyncio.Event()

    async def cancellation_resistant_provider(candidates, _config, *, role, policy_epoch):
        assert role == "fast"
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return [
                {
                    "entity_id": candidates[0].entity_id,
                    "label_it": "Luce nuova",
                    "label_en": "New light",
                }
            ]
        raise AssertionError("provider gate unexpectedly completed without cancellation")

    provider = AsyncMock(side_effect=cancellation_resistant_provider)
    with (
        patch("mammamiradio.home.catalog._call_anthropic_labels", new=provider),
        patch("mammamiradio.home.catalog.save_catalog") as save,
    ):
        assert schedule_label_generation(states={entity_id: state}, cache_dir=tmp_path, config=config, force=True)
        await asyncio.wait_for(provider_entered.wait(), timeout=1.0)
        await catalog.revoke_label_generation()
        await asyncio.sleep(0)

    provider.assert_awaited_once()
    save.assert_not_called()
    assert not catalog.generation_in_progress()
    assert load_catalog(tmp_path)["entries"][entity_id]["label_it"] == "Vecchia luce"
    assert resolve_label(entity_id, state, cache_dir=tmp_path).label_it == "Vecchia luce"
    assert (
        json.loads((tmp_path / CATALOG_FILENAME).read_text(encoding="utf-8"))["entries"][entity_id]["label_it"]
        == "Vecchia luce"
    )


def test_response_text_joins_text_blocks_and_tolerates_shapes():
    from mammamiradio.home.catalog import _response_text

    resp = SimpleNamespace(content=[SimpleNamespace(text="ciao"), {"text": "mondo"}, SimpleNamespace(text=None)])
    assert _response_text(resp) == "ciao\nmondo"
    assert _response_text(SimpleNamespace(content=None)) == ""
    assert _response_text(object()) == ""


def _models_section():
    from mammamiradio.core.config import ModelsSection

    return ModelsSection(
        catalog={"anthropic": {"haiku": "claude-haiku-test"}},
        routing={"transition": "fast"},
        profiles={"balanced": {"anthropic": {"fast": "haiku"}}},
        default_profile="balanced",
        active_profile="balanced",
    )


def test_resolve_anthropic_fast_model_delegates_to_single_resolver():
    from mammamiradio.home.catalog import _resolve_anthropic_fast_model

    config = SimpleNamespace(models=_models_section())
    # transition routes to the fast role -> the fast catalog model id.
    assert _resolve_anthropic_fast_model(config) == "claude-haiku-test"


def test_parse_label_payload_tolerates_fences_and_garbage():
    from mammamiradio.home.catalog import _parse_label_payload

    item = '{"labels": [{"entity_id": "light.x", "label_it": "Luce", "label_en": "Light"}]}'
    expected = [{"entity_id": "light.x", "label_it": "Luce", "label_en": "Light"}]
    # Plain JSON object.
    assert _parse_label_payload(item) == expected
    # Wrapped in a ```json fence (a common model habit).
    assert _parse_label_payload(f"```json\n{item}\n```") == expected
    # Wrapped in a bare ``` fence.
    assert _parse_label_payload(f"```\n{item}\n```") == expected
    # A bare top-level list is accepted too.
    assert _parse_label_payload('[{"entity_id": "light.x", "label_it": "Luce", "label_en": "Light"}]') == expected
    # Unparseable junk degrades to [] instead of raising.
    assert _parse_label_payload("sorry, I cannot do that") == []
    assert _parse_label_payload("") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count,budget", [(1, 1200), (10, 1200), (25, 2200), (50, 4000)])
async def test_call_anthropic_labels_builds_client_and_parses_labels(count, budget):
    import mammamiradio.home.catalog as catalog
    from mammamiradio.home.catalog import LabelCandidate, _call_anthropic_labels

    candidate = LabelCandidate(
        entity_id="light.counter",
        score=0.6,
        entity_hash="h",
        metadata={"entity_id": "light.counter", "friendly_name": "Counter"},
    )
    config = SimpleNamespace(anthropic_api_key="sk-ant-test", models=_models_section())

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
        labels = await _call_anthropic_labels(
            [candidate] * count, config, role="fast", policy_epoch=catalog._generation_policy_epoch
        )

    assert labels == [{"entity_id": "light.counter", "label_it": "Luce", "label_en": "Light"}]
    assert create.await_args.kwargs["model"] == "claude-haiku-test"
    assert create.await_args.kwargs["max_tokens"] == budget
    # The 45s timeout must be applied (it guards _CATALOG_LOCK from a 10-min stall).
    assert seen_options == {"timeout": 45.0}


def _label_response(labels=(), *, stop_reason="end_turn", text=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(text=text if text is not None else json.dumps({"labels": list(labels)}))],
    )


def _request_entities(call):
    return json.loads(call.kwargs["messages"][0]["content"].rsplit("\n\n", 1)[1])["entities"]


@pytest.fixture
def label_provider(monkeypatch):
    create = AsyncMock()
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic", lambda **kwargs: SimpleNamespace(with_options=lambda **opts: client)
    )
    return SimpleNamespace(anthropic_api_key="sk-ant-test", models=_models_section()), create


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,stops,expected_calls",
    [(50, ["max_tokens", "end_turn"], 2), (2, ["max_tokens"] * 2, 2), (1, ["max_tokens"], 1)],
)
async def test_label_truncation_is_bounded_and_rebudgets_first_half(
    tmp_path, label_provider, count, stops, expected_calls
):
    config, create = label_provider
    states = {f"light.fixture_{i:02}": _state(f"Counter {i}") for i in range(count)}
    old = {"schema_version": 1, "entries": {"light.previous": {"label_it": "Luce", "label_en": "Light", "hash": "old"}}}
    save_catalog(tmp_path, old)
    before = (tmp_path / CATALOG_FILENAME).read_bytes()
    accepted_id = sorted(states)[-1]
    labels = [{"entity_id": accepted_id, "label_it": "Luce bancone", "label_en": "Counter light"}]
    # Even syntactically valid JSON must be discarded when marked truncated.
    create.side_effect = [_label_response(labels, stop_reason=stop) for stop in stops]
    result = await generate_label_catalog(states, cache_dir=tmp_path, config=config)
    assert create.await_count == expected_calls
    first = _request_entities(create.await_args_list[0])
    assert len(first) == count
    if expected_calls == 2:
        assert _request_entities(create.await_args_list[1]) == first[: count // 2]
    if count == 50:
        assert [call.kwargs["max_tokens"] for call in create.await_args_list] == [4000, 2200]
        assert result["entries"][accepted_id]["label_en"] == "Counter light"
        assert result["entries"]["light.previous"] == old["entries"]["light.previous"]
    else:
        assert result["entries"] == old["entries"]
        assert (tmp_path / CATALOG_FILENAME).read_bytes() == before


@pytest.mark.asyncio
async def test_successful_half_batches_leave_pending_selection(tmp_path, label_provider):
    config, create = label_provider
    states = {f"light.fixture_{i}": _state(f"Counter {i}") for i in range(4)}
    seen = []

    async def respond(**kwargs):
        entities = json.loads(kwargs["messages"][0]["content"].rsplit("\n\n", 1)[1])["entities"]
        seen.append([entity["entity_id"] for entity in entities])
        labels = [{"entity_id": entity["entity_id"], "label_it": "Luce", "label_en": "Light"} for entity in entities]
        return _label_response(labels, stop_reason="max_tokens" if len(entities) > 1 else "end_turn")

    create.side_effect = respond
    # First attempt: 4 -> 2, both truncated. Existing data stays intact.
    assert (await generate_label_catalog(states, cache_dir=tmp_path, config=config))["entries"] == {}
    create.reset_mock()
    seen.clear()

    async def successful_half(**kwargs):
        response = await respond(**kwargs)
        if len(seen) % 2 == 0:
            response.stop_reason = "end_turn"
        return response

    create.side_effect = successful_half
    for _ in range(3):
        await generate_label_catalog(states, cache_dir=tmp_path, config=config)
    original = seen[0]
    assert set(original) == set(states)
    assert seen == [original, original[:2], original[2:], [original[2]], [original[3]]]
    assert set(load_catalog(tmp_path)["entries"]) == set(states)
    await generate_label_catalog(states, cache_dir=tmp_path, config=config)
    assert create.await_count == 5


@pytest.mark.asyncio
async def test_revoke_during_truncated_provider_call_prevents_retry_and_save(tmp_path, label_provider):
    import mammamiradio.home.catalog as catalog

    config, create = label_provider
    states = {f"light.fixture_{i}": _state(f"Counter {i}") for i in range(2)}
    save_catalog(tmp_path, {"schema_version": 1, "entries": {}})
    before = (tmp_path / CATALOG_FILENAME).read_bytes()
    entered = asyncio.Event()

    async def cancellation_resistant(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return _label_response(stop_reason="max_tokens")
        raise AssertionError("request completed without cancellation")

    create.side_effect = cancellation_resistant
    with patch("mammamiradio.home.catalog.save_catalog") as save:
        assert schedule_label_generation(states, cache_dir=tmp_path, config=config)
        await asyncio.wait_for(entered.wait(), timeout=1)
        await catalog.revoke_label_generation()
        await asyncio.sleep(0)
    create.assert_awaited_once()
    save.assert_not_called()
    assert (tmp_path / CATALOG_FILENAME).read_bytes() == before
    assert load_catalog(tmp_path)["entries"] == {}
    assert not catalog.generation_in_progress()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parse", "provider", "persistence"])
async def test_catalog_failure_preserves_bytes_without_logging_private_data(tmp_path, label_provider, caplog, failure):
    config, create = label_provider
    canary = "PRIVATE-HOUSEHOLD-CANARY sk-ant-secret-canary light.private_canary"
    states = {"light.private_canary": _state(canary)}
    save_catalog(tmp_path, {"schema_version": 1, "entries": {}})
    before = (tmp_path / CATALOG_FILENAME).read_bytes()
    if failure == "provider":
        create.side_effect = RuntimeError(canary)
    else:
        create.return_value = _label_response(
            [{"entity_id": "light.private_canary", "label_it": "Luce", "label_en": "Light"}],
            text=canary if failure == "parse" else None,
        )
    with patch("mammamiradio.home.catalog.os.replace", side_effect=OSError(canary)):
        result = await generate_label_catalog(states, cache_dir=tmp_path, config=config)
    assert result["entries"] == load_catalog(tmp_path)["entries"] == {}
    assert (tmp_path / CATALOG_FILENAME).read_bytes() == before
    assert caplog.records
    for private_text in canary.split():
        assert private_text not in caplog.text


def test_catalog_persistence_has_no_fallible_step_after_replace(tmp_path):
    import mammamiradio.home.catalog as catalog

    destination = tmp_path / CATALOG_FILENAME
    real_chmod = os.chmod

    def chmod_before_replace(path, mode):
        if path == destination:
            raise OSError("cannot chmod after commit")
        real_chmod(path, mode)

    with patch("mammamiradio.home.catalog.os.chmod", side_effect=chmod_before_replace):
        assert catalog._atomic_write_json(destination, {"entries": {}})
    assert destination.stat().st_mode & 0o777 == 0o600
    assert json.loads(destination.read_text()) == {"entries": {}}


def test_catalog_directory_failure_is_fail_soft(tmp_path, caplog):
    import mammamiradio.home.catalog as catalog

    with patch("pathlib.Path.mkdir", side_effect=OSError("PRIVATE-DIRECTORY-CANARY")):
        assert not catalog._atomic_write_json(tmp_path / CATALOG_FILENAME, {"entries": {}})
    assert "PRIVATE-DIRECTORY-CANARY" not in caplog.text
    assert not (tmp_path / CATALOG_FILENAME).exists()
