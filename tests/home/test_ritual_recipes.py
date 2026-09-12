from __future__ import annotations

import pytest

from mammamiradio.home.ritual_recipes import (
    CATALOG_VERSION,
    DEFAULT_RITUAL_RECIPES,
    audit_ritual_recipes,
    clear_ritual_recipe_cooldowns,
    commit_ritual_recipe_match,
    match_ritual_recipes,
    public_family_labels,
)


def _state(value: object, **attrs: object) -> dict:
    return {"state": value, "attributes": attrs}


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    clear_ritual_recipe_cooldowns()
    yield
    clear_ritual_recipe_cooldowns()


def test_catalog_contains_priority_v1_families():
    families = {recipe.family for recipe in DEFAULT_RITUAL_RECIPES}

    assert CATALOG_VERSION == "2026-09-12.community-v2"
    assert {
        "morning_launch",
        "cooking_kitchen",
        "shower_bathroom",
        "sleep_wake",
        "media_betrayal",
        "fridge_freezer_raid",
        "windows_airing",
        "chores_reminders",
        "vacation_house_sitter",
        "vacuum_doorbell_protocol",
    }.issubset(families)


def test_numeric_morning_launch_recipe_matches_local_transition():
    previous = {"sensor.kitchen_coffee_power": _state("0", friendly_name="Kitchen coffee machine power")}
    current = {"sensor.kitchen_coffee_power": _state("75", friendly_name="Kitchen coffee machine power")}

    matches = match_ritual_recipes(None, previous, current, now=100.0)

    assert [match.recipe.id for match in matches] == ["morning_launch"]
    assert matches[0].recipe.delivery_lane == "directive"
    assert matches[0].recipe.privacy_class == "private"
    assert public_family_labels(matches) == ["Morning launch"]


def test_attribute_media_recipe_matches_sonos_source_change():
    previous = {
        "media_player.living_sonos": _state(
            "playing",
            friendly_name="Living Sonos",
            source="Mamma Mi Radio",
        )
    }
    current = {
        "media_player.living_sonos": _state(
            "playing",
            friendly_name="Living Sonos",
            source="Suspicious Other Station",
        )
    }

    matches = match_ritual_recipes(None, previous, current, now=200.0)

    assert any(match.recipe.id == "media_betrayal" for match in matches)


def test_safety_signals_no_longer_produce_a_ritual_moment():
    """Safety device classes must not produce ritual moments."""
    for device_class in ("moisture", "smoke", "gas", "carbon_monoxide"):
        entity_id = f"binary_sensor.test_garden_{device_class}"
        attrs = {
            "device_class": device_class,
            "friendly_name": f"Test Garden {device_class}",
        }
        previous = {entity_id: _state("off", **attrs)}
        current = {entity_id: _state("on", **attrs)}

        matches = match_ritual_recipes(None, previous, current, now=300.0)

        assert matches == [], f"{device_class} produced {[m.recipe.id for m in matches]}"


def test_match_status_dict_carries_admin_detail():
    """Cover the admin-facing projection with a synthetic catalog match."""
    entity_id = "binary_sensor.test_lounge_window"
    previous = {entity_id: _state("off", device_class="window", friendly_name="Test Lounge Window")}
    current = {entity_id: _state("on", device_class="window", friendly_name="Test Lounge Window")}

    matches = match_ritual_recipes(None, previous, current, now=300.0)
    assert matches, "expected a window match to build the projection from"

    payload = matches[0].to_status_dict()

    assert payload["entity_id"] == entity_id
    assert payload["recipe_id"] == "windows_airing"
    assert payload["delivery_lane"] == "running_gag"
    assert payload["public_family_label"] == "Window ritual"
    assert isinstance(payload["confidence"], float)


def test_an_ordinary_door_opening_produces_no_moment():
    """An ordinary door opening must not produce a ritual match."""
    entity_id = "binary_sensor.test_front_door"
    attrs = {"device_class": "door", "friendly_name": "Test Front Door", "area_name": "Test Hallway"}
    previous = {entity_id: {"state": "off", "attributes": dict(attrs)}}
    current = {entity_id: {"state": "on", "attributes": dict(attrs)}}

    matches = match_ritual_recipes(None, previous, current, now=300.0)

    assert matches == [], f"door opening produced {[(m.recipe.id, m.recipe.delivery_lane) for m in matches]}"


def test_every_shipped_pattern_clears_its_own_recipe_floor():
    """Every shipped pattern must clear its recipe's min_confidence.

    Five patterns sit exactly at their floor. Lowering their confidence or
    raising their floor by 0.01 suppresses them, so this guard catches either
    catalog edit.
    """
    offenders = [
        (recipe.id, pattern.id, pattern.confidence, recipe.min_confidence)
        for recipe in DEFAULT_RITUAL_RECIPES
        for pattern in recipe.evidence_patterns
        if pattern.confidence < recipe.min_confidence
    ]
    assert offenders == [], f"patterns silently suppressed by their own recipe floor: {offenders}"


def test_no_default_recipe_may_interrupt_the_stream():
    """No shipped ritual recipe may use the interrupt lane.

    Callers may pass custom recipe sequences to ``match_ritual_recipes``.
    Configured timers and ``POST /api/interrupt`` still invoke
    ``_fire_interrupt``. Reintroducing a shipped interrupt recipe requires
    explicit operator arming, a bounded delivery deadline, deterministic
    fallback copy, and per-entity consent.
    """
    # Guards against the assertion going vacuous over an emptied or renamed tuple.
    assert len(DEFAULT_RITUAL_RECIPES) >= 11

    offenders = [r.id for r in DEFAULT_RITUAL_RECIPES if r.delivery_lane == "interrupt"]
    assert offenders == [], f"recipes on the interrupt lane: {offenders}"

    urgent = [r.id for r in DEFAULT_RITUAL_RECIPES if r.interrupt_urgency == "urgent"]
    assert urgent == [], f"recipes with urgent urgency: {urgent}"


def test_pattern_below_its_recipe_floor_is_suppressed():
    """A pattern below ``min_confidence`` must be suppressed."""
    import dataclasses

    recipe = next(r for r in DEFAULT_RITUAL_RECIPES if r.id == "windows_airing")
    entity_id = "binary_sensor.test_lounge_window"
    attrs = {"device_class": "window", "friendly_name": "Test Lounge Window"}
    previous = {entity_id: {"state": "off", "attributes": dict(attrs)}}
    current = {entity_id: {"state": "on", "attributes": dict(attrs)}}

    assert match_ritual_recipes([recipe], previous, current, now=300.0), "baseline should match"

    weakened = dataclasses.replace(
        recipe,
        evidence_patterns=tuple(dataclasses.replace(p, confidence=0.10) for p in recipe.evidence_patterns),
    )
    assert match_ritual_recipes([weakened], previous, current, now=300.0) == []


def test_sleep_wake_ignores_incidental_bedroom_entities():
    previous = {
        "binary_sensor.bedroom_window": _state(
            "off",
            device_class="window",
            friendly_name="Bedroom window",
        ),
        "sensor.bedroom_temperature": _state("20", friendly_name="Bedroom temperature"),
    }
    current = {
        "binary_sensor.bedroom_window": _state(
            "on",
            device_class="window",
            friendly_name="Bedroom window",
        ),
        "sensor.bedroom_temperature": _state("21", friendly_name="Bedroom temperature"),
    }

    matches = match_ritual_recipes(None, previous, current, now=350.0)

    assert all(match.recipe.id != "sleep_wake" for match in matches)


def test_sleep_wake_still_matches_sleep_helpers_and_bed_occupancy():
    previous = {
        "input_select.sleep_mode": _state("awake", friendly_name="Sleep mode"),
        "binary_sensor.bed_occupancy": _state(
            "off",
            device_class="occupancy",
            friendly_name="Bed occupancy",
        ),
    }
    current = {
        "input_select.sleep_mode": _state("asleep", friendly_name="Sleep mode"),
        "binary_sensor.bed_occupancy": _state(
            "on",
            device_class="occupancy",
            friendly_name="Bed occupancy",
        ),
    }

    matches = match_ritual_recipes(None, previous, current, now=360.0)

    assert [match.recipe.id for match in matches] == ["sleep_wake"]
    assert matches[0].pattern.id in {"sleep_state_changed", "bed_occupancy_changed"}


def test_keyword_matching_requires_word_or_phrase_boundary():
    previous = {"binary_sensor.plant_watering": _state("off", friendly_name="Plant watering")}
    current = {"binary_sensor.plant_watering": _state("on", friendly_name="Plant watering")}

    matches = match_ritual_recipes(None, previous, current, now=370.0)

    assert all(match.recipe.id != "vacuum_doorbell_protocol" for match in matches)
    assert any(match.recipe.id == "pets_plants_optional" for match in matches)


def test_multiword_keyword_recipes_still_match():
    """The vacation house-sitter recipe preserves multiword-keyword coverage.

    It matches "House sitter mode" through the two-token "house sitter"
    phrase. The single-token "housesitter" spelling does not match.
    """
    previous = {
        "media_player.music_assistant": _state(
            "playing",
            friendly_name="Music Assistant speaker",
            source="Mamma Mi Radio",
        ),
        "input_select.house_sitter_mode": _state("home", friendly_name="House sitter mode"),
    }
    current = {
        "media_player.music_assistant": _state(
            "playing",
            friendly_name="Music Assistant speaker",
            source="Suspicious Other Station",
        ),
        "input_select.house_sitter_mode": _state("housesitter", friendly_name="House sitter mode"),
    }

    matches = match_ritual_recipes(None, previous, current, now=380.0)
    matched_ids = {match.recipe.id for match in matches}

    assert {"media_betrayal", "vacation_house_sitter"}.issubset(matched_ids)


def test_away_mode_ignores_alarm_panel_but_matches_select_helpers():
    alarm_previous = {"alarm_control_panel.home": _state("disarmed", friendly_name="Home alarm")}
    alarm_current = {"alarm_control_panel.home": _state("armed_away", friendly_name="Home alarm")}

    assert match_ritual_recipes(None, alarm_previous, alarm_current, now=390.0) == []

    helper_previous = {"input_select.house_mode": _state("home", friendly_name="House away mode")}
    helper_current = {"input_select.house_mode": _state("away", friendly_name="House away mode")}

    matches = match_ritual_recipes(None, helper_previous, helper_current, now=391.0)

    assert [match.recipe.id for match in matches] == ["vacation_house_sitter"]


def test_noise_device_classes_do_not_become_recipe_moments():
    previous = {
        "sensor.router_rssi": _state("-60", device_class="signal_strength", friendly_name="Kitchen RSSI"),
        "sensor.boot_time": _state("2026-07-06T10:00:00+00:00", device_class="timestamp", friendly_name="Wake time"),
        "sensor.phone_battery": _state("55", device_class="battery", friendly_name="Kitchen battery"),
    }
    current = {
        "sensor.router_rssi": _state("-59", device_class="signal_strength", friendly_name="Kitchen RSSI"),
        "sensor.boot_time": _state("2026-07-06T10:01:00+00:00", device_class="timestamp", friendly_name="Wake time"),
        "sensor.phone_battery": _state("56", device_class="battery", friendly_name="Kitchen battery"),
    }

    assert match_ritual_recipes(None, previous, current, now=400.0) == []


def test_recipe_cooldown_is_spent_only_after_commit():
    previous = {"binary_sensor.fridge_door": _state("off", device_class="door", friendly_name="Kitchen fridge door")}
    current = {"binary_sensor.fridge_door": _state("on", device_class="door", friendly_name="Kitchen fridge door")}

    first = match_ritual_recipes(None, previous, current, now=1000.0)
    assert len(first) == 1
    assert match_ritual_recipes(None, previous, current, now=1001.0)

    commit_ritual_recipe_match(first[0], now=1001.0)

    assert match_ritual_recipes(None, previous, current, now=1002.0) == []


def test_audit_reports_instrumented_and_opportunity_recipes():
    states = {
        "binary_sensor.mailbox": _state("off", device_class="door", friendly_name="Mailbox flap"),
    }

    audit = audit_ritual_recipes(states=states)

    chores = next(item for item in audit if item["recipe_id"] == "chores_reminders")
    pets = next(item for item in audit if item["recipe_id"] == "pets_plants_optional")
    assert chores["status"] == "instrumented"
    assert "mailbox opens" in chores["local_evidence"]
    assert pets["status"] == "opportunity"


def test_recipe_audit_ignores_privacy_denied_alarm_panel_entities():
    alarm_audit = audit_ritual_recipes(
        states={
            "alarm_control_panel.home": _state("armed_away", friendly_name="Home alarm"),
        }
    )
    alarm_item = next(item for item in alarm_audit if item["recipe_id"] == "vacation_house_sitter")
    assert alarm_item["status"] == "opportunity"

    helper_audit = audit_ritual_recipes(
        states={
            "input_select.house_mode": _state("away", friendly_name="House away mode"),
        }
    )
    helper_item = next(item for item in helper_audit if item["recipe_id"] == "vacation_house_sitter")
    assert helper_item["status"] == "instrumented"
