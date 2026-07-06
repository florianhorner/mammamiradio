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

    assert CATALOG_VERSION
    assert {
        "morning_launch",
        "cooking_kitchen",
        "shower_bathroom",
        "sleep_wake",
        "media_betrayal",
        "fridge_freezer_raid",
        "windows_airing",
        "chores_reminders",
        "safety_saves",
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


def test_safety_recipe_uses_interrupt_lane_and_public_coarse_label():
    previous = {"binary_sensor.sink_leak": _state("off", device_class="moisture", friendly_name="Sink leak")}
    current = {"binary_sensor.sink_leak": _state("on", device_class="moisture", friendly_name="Sink leak")}

    matches = match_ritual_recipes(None, previous, current, now=300.0)

    assert len(matches) == 1
    match = matches[0]
    assert match.recipe.id == "safety_saves"
    assert match.recipe.delivery_lane == "interrupt"
    assert match.recipe.privacy_class == "safety"
    assert public_family_labels(matches) == ["Safety moment"]
    assert match.to_status_dict()["entity_id"] == "binary_sensor.sink_leak"


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
    previous = {
        "binary_sensor.front_door": _state("off", device_class="door", friendly_name="Front door"),
        "media_player.music_assistant": _state(
            "playing",
            friendly_name="Music Assistant speaker",
            source="Mamma Mi Radio",
        ),
        "input_select.house_sitter_mode": _state("home", friendly_name="House sitter mode"),
    }
    current = {
        "binary_sensor.front_door": _state("on", device_class="door", friendly_name="Front door"),
        "media_player.music_assistant": _state(
            "playing",
            friendly_name="Music Assistant speaker",
            source="Suspicious Other Station",
        ),
        "input_select.house_sitter_mode": _state("housesitter", friendly_name="House sitter mode"),
    }

    matches = match_ritual_recipes(None, previous, current, now=380.0)
    matched_ids = {match.recipe.id for match in matches}

    assert {"safety_saves", "media_betrayal", "vacation_house_sitter"}.issubset(matched_ids)


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
