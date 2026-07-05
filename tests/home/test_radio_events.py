from __future__ import annotations

import pytest

from mammamiradio.core.config import RadioEventRule
from mammamiradio.home.radio_events import (
    clear_radio_event_cooldowns,
    commit_radio_event_directive,
    match_radio_events,
)


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    clear_radio_event_cooldowns()
    yield
    clear_radio_event_cooldowns()


def _state(value: object, **attrs: object) -> dict:
    return {"state": value, "attributes": attrs}


def test_script_tts_state_transition_promotes_directive():
    rule = RadioEventRule(
        id="tts_script_started",
        entity_glob="script.*tts*",
        trigger="state",
        from_state="off",
        to_state="on",
        mode="directive",
        directive="One of the house voices just spoke.",
    )

    matches = match_radio_events(
        [rule],
        {"script.kitchen_tts": _state("off")},
        {"script.kitchen_tts": _state("on")},
        now=100.0,
    )

    assert len(matches) == 1
    assert matches[0].mode == "directive"
    assert matches[0].directive == "One of the house voices just spoke."


def test_automation_last_triggered_attribute_promotes_directive():
    rule = RadioEventRule(
        id="tts_automation_fired",
        entity_glob="automation.*tts*",
        trigger="attribute",
        attribute="last_triggered",
        mode="directive",
        directive="A Home Assistant voice automation just fired.",
    )

    matches = match_radio_events(
        [rule],
        {"automation.night_tts": _state("on", last_triggered="2026-07-05T10:00:00+00:00")},
        {"automation.night_tts": _state("on", last_triggered="2026-07-05T10:02:00+00:00")},
        now=200.0,
    )

    assert len(matches) == 1
    assert matches[0].event.entity_id == "automation.night_tts"


def test_binary_sensor_charging_transition_promotes_gag_event():
    rule = RadioEventRule(
        id="device_charging",
        label="A household device started charging",
        domain="binary_sensor",
        device_class="battery_charging",
        trigger="state",
        from_state="off",
        to_state="on",
        mode="gag",
        cooldown_seconds=120,
    )

    matches = match_radio_events(
        [rule],
        {"binary_sensor.phone_charging": _state("off", device_class="battery_charging")},
        {"binary_sensor.phone_charging": _state("on", device_class="battery_charging")},
        now=300.0,
    )

    assert len(matches) == 1
    event = matches[0].event
    assert matches[0].mode == "gag"
    assert event.label == "A household device started charging"
    assert event.force_gag_candidate is True
    assert event.gag_cooldown_seconds == 120


def test_noise_device_classes_do_not_match_under_broad_globs():
    rule = RadioEventRule(
        id="broad_sensor",
        entity_glob="sensor.*",
        trigger="state",
        mode="directive",
        directive="A sensor changed.",
    )
    previous = {
        "sensor.router_rssi": _state("-60", device_class="signal_strength"),
        "sensor.boot_time": _state("2026-07-05T10:00:00+00:00", device_class="timestamp"),
        "sensor.phone_battery": _state("55", device_class="battery"),
    }
    current = {
        "sensor.router_rssi": _state("-59", device_class="signal_strength"),
        "sensor.boot_time": _state("2026-07-05T10:01:00+00:00", device_class="timestamp"),
        "sensor.phone_battery": _state("56", device_class="battery"),
    }

    assert match_radio_events([rule], previous, current, now=400.0) == []


def test_numeric_threshold_requires_crossing():
    rule = RadioEventRule(
        id="washer_power",
        entity_id="sensor.washer_power",
        trigger="numeric_threshold",
        threshold=50.0,
        direction="above",
        mode="directive",
        directive="The washer crossed the power threshold.",
    )

    assert (
        match_radio_events(
            [rule],
            {"sensor.washer_power": _state("49")},
            {"sensor.washer_power": _state("51")},
            now=500.0,
        )
        != []
    )
    assert (
        match_radio_events(
            [rule],
            {"sensor.washer_power": _state("51")},
            {"sensor.washer_power": _state("55")},
            now=501.0,
        )
        == []
    )


def test_directive_cooldown_is_spent_only_after_commit():
    rule = RadioEventRule(
        id="tts_script_started",
        entity_id="script.kitchen_tts",
        trigger="state",
        from_state="off",
        to_state="on",
        mode="directive",
        cooldown_seconds=60,
        directive="One of the house voices just spoke.",
    )
    previous = {"script.kitchen_tts": _state("off")}
    current = {"script.kitchen_tts": _state("on")}

    first = match_radio_events([rule], previous, current, now=1000.0)
    assert len(first) == 1
    assert match_radio_events([rule], previous, current, now=1001.0)

    commit_radio_event_directive(first[0], now=1001.0)

    assert match_radio_events([rule], previous, current, now=1002.0) == []
