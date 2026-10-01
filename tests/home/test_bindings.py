"""Private rules must follow the selected installation, never a compiled map."""

import time
from collections import deque
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import pytest

from mammamiradio.core.listener_truth import home_return_authority_for_directive
from mammamiradio.home.authorization import HomeAuthorization, HomeAuthorizationMode
from mammamiradio.home.bindings import EMPTY_HOME_BINDINGS, HomeBindings
from mammamiradio.home.evening_memory import GagBucket
from mammamiradio.home.ha_context import (
    HomeContext,
    _format_state,
    check_reactive_triggers,
    classify_home_mood,
    get_cached_home_context,
    invalidate_all_home_context,
)
from mammamiradio.home.ha_enrichment import HomeEvent
from tests.home_fixtures import SYNTHETIC_BINDINGS, synthetic_home_document


@pytest.fixture(params=("example_a", "example_b"))
def bindings(request):
    return HomeBindings.from_document(synthetic_home_document(request.param), identity=request.param)


def test_bindings_are_immutable_and_nonrevealing(bindings):
    with pytest.raises(FrozenInstanceError):
        bindings.identity = "changed"
    assert bindings.entity("resident_one") not in repr(bindings)
    labels = bindings.labels_it
    labels.clear()
    assert bindings.labels_it


def test_empty_legacy_authority_is_rejected():
    with pytest.raises(ValueError):
        HomeAuthorization.legacy(EMPTY_HOME_BINDINGS)
    direct = HomeAuthorization(HomeAuthorizationMode.LEGACY)
    assert not direct.allows_household_moments
    assert not direct.allows_label_generation
    assert not direct.allows_derived_mood
    assert not direct.project({"person.example_resident": {"state": "home"}}).states


def test_labels_mood_and_priority_follow_bindings(bindings):
    vacuum = bindings.entity("vacuum_one")
    assert bindings.labels_it[vacuum] in _format_state(vacuum, {"state": "cleaning"}, bindings=bindings)
    assert "pulendo" in classify_home_mood({vacuum: {"state": "cleaning"}}, bindings=bindings).lower()
    now = time.time()
    gold = GagBucket(
        entity_id=bindings.entity("coffee_switch"),
        label="Coffee",
        old_state="off",
        new_state="on",
        count=2,
        last_ts=now,
    )
    generic = replace(gold, entity_id="switch.example_unbound")
    assert gold.salience(now=now, bindings=bindings) == 3 * generic.salience(now=now, bindings=bindings)


def test_trigger_order_cooldowns_and_arrival_authority_follow_bindings(bindings):
    invalidate_all_home_context()
    source = bindings.entity("resident_one")
    rows = [row for row in bindings.reactive if row.entity_id == source and row.state == "home"]
    assert len(rows) == 1
    now = time.time()
    event = HomeEvent(
        entity_id=source,
        label="Resident",
        old_state="away",
        new_state="a casa",
        timestamp=now,
        raw_old_state="not_home",
        raw_new_state="home",
    )
    directive = check_reactive_triggers(deque([event]), {source: {"state": "home"}}, bindings=bindings)
    assert directive == rows[0].directive
    assert check_reactive_triggers(deque([event]), {source: {"state": "home"}}, bindings=bindings) is None
    authority = home_return_authority_for_directive(f"ha:{source}", directive, bindings=bindings)
    assert authority is not None
    assert authority.allows("Bentornato, Residente uno!")
    assert not authority.allows("Bentornato, Residente due!")
    lock = bindings.entity("entry_lock")
    assert home_return_authority_for_directive(f"ha:{lock}", directive, bindings=bindings) is None
    other = SYNTHETIC_BINDINGS.entity("resident_one")
    assert home_return_authority_for_directive(f"ha:{other}", directive, bindings=bindings) is None


def test_cached_context_from_another_profile_is_rejected(bindings):
    old = HomeContext(authorization_mode="legacy", bindings=bindings, timestamp=time.time(), summary="private")
    other = replace(bindings, identity="another-installation")
    with patch("mammamiradio.home.ha_context._ha_cache", old):
        assert get_cached_home_context(authorization=HomeAuthorization.legacy(other)) is None
        assert get_cached_home_context(authorization=HomeAuthorization.legacy(bindings)) is old


@pytest.mark.asyncio
async def test_forecast_requests_and_cache_follow_each_profile(bindings, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import mammamiradio.home.ha_context as context

    entity = bindings.entity("weather")
    client = AsyncMock()
    client.get.return_value = MagicMock(json=lambda: {"attributes": {"temperature_unit": "°C"}})
    client.post.return_value = MagicMock(
        json=lambda: {entity: {"forecast": [{"condition": "sunny", "temperature": 20}]}}
    )
    monkeypatch.setattr(context, "_get_ha_client", lambda: client)
    monkeypatch.setattr(context, "_weather_forecast_fetched_at", 0.0)
    result = await context.fetch_weather_forecast("http://example.invalid", "synthetic", bindings=bindings)
    assert result
    assert client.get.call_args.args[0].endswith("/api/states/" + entity)
    assert client.post.call_args.kwargs["json"]["entity_id"] == entity
    other = replace(bindings, identity="unrelated-profile")
    assert context.get_weather_arc_en(bindings=other, ha_url="http://example.invalid") == ""
