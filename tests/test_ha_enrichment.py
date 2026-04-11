"""Tests for isolated HA enrichment helpers."""

from __future__ import annotations

from collections import deque

from mammamiradio.ha_context import ENTITY_LABELS, STATE_TRANSLATIONS
from mammamiradio.ha_enrichment import (
    EVENT_BUFFER_SIZE,
    EVENT_RETENTION_SECONDS,
    HomeEvent,
    build_events_summary,
    diff_states,
    prune_events,
)


def _event(n: int, *, timestamp: float) -> HomeEvent:
    return HomeEvent(
        entity_id=f"sensor.test_{n}",
        label=f"Evento {n}",
        old_state="spento/a",
        new_state="acceso/a",
        timestamp=timestamp,
    )


def test_diff_states_creates_translated_event():
    old_states = {
        "switch.bar_kaffeemaschine_steckdose": {"state": "off", "attributes": {}},
    }
    new_states = {
        "switch.bar_kaffeemaschine_steckdose": {"state": "on", "attributes": {}},
    }

    events = diff_states(
        old_states,
        new_states,
        existing_events=None,
        entity_labels=ENTITY_LABELS,
        state_translations=STATE_TRANSLATIONS,
        now=1_000.0,
    )

    assert len(events) == 1
    event = events[0]
    assert event.label == "La macchina del caffè"
    assert event.old_state == "spento/a"
    assert event.new_state == "acceso/a"
    assert "1 min fa" in build_events_summary(events, now=1_060.0)


def test_diff_states_skips_unknown_and_untranslated_changes():
    old_states = {
        "person.florian_horner": {"state": "unknown", "attributes": {}},
        "input_select.kaffee_dad_jokes": {"state": "Prima battuta", "attributes": {}},
    }
    new_states = {
        "person.florian_horner": {"state": "home", "attributes": {}},
        "input_select.kaffee_dad_jokes": {"state": "Seconda battuta", "attributes": {}},
    }

    events = diff_states(
        old_states,
        new_states,
        existing_events=None,
        entity_labels=ENTITY_LABELS,
        state_translations=STATE_TRANSLATIONS,
        now=1_000.0,
    )

    assert list(events) == []


def test_prune_events_drops_expired_entries():
    events = deque(
        [
            _event(1, timestamp=10.0),
            _event(2, timestamp=EVENT_RETENTION_SECONDS + 5.0),
        ],
        maxlen=EVENT_BUFFER_SIZE,
    )

    pruned = prune_events(events, now=EVENT_RETENTION_SECONDS + 20.0)

    assert len(pruned) == 1
    assert pruned[0].label == "Evento 2"


def test_diff_states_respects_ring_buffer_bound():
    existing_events = deque(
        [_event(i, timestamp=100.0 + i) for i in range(EVENT_BUFFER_SIZE)],
        maxlen=EVENT_BUFFER_SIZE,
    )
    old_states = {
        "lock.lock_ultra_8d3c": {"state": "locked", "attributes": {}},
    }
    new_states = {
        "lock.lock_ultra_8d3c": {"state": "unlocked", "attributes": {}},
    }

    events = diff_states(
        old_states,
        new_states,
        existing_events=existing_events,
        entity_labels=ENTITY_LABELS,
        state_translations=STATE_TRANSLATIONS,
        now=200.0,
    )

    assert len(events) == EVENT_BUFFER_SIZE
    assert events[-1].label == "Serratura porta d'ingresso"
    assert events[0].label == "Evento 1"


def test_build_events_summary_uses_newest_five_first():
    events = deque((_event(i, timestamp=float(i)) for i in range(6)), maxlen=EVENT_BUFFER_SIZE)

    summary = build_events_summary(events, now=120.0)

    lines = summary.splitlines()
    assert len(lines) == 5
    assert lines[0].startswith("- Evento 5:")
    assert lines[-1].startswith("- Evento 1:")
