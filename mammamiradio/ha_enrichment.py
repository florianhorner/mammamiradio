"""Pure Home Assistant enrichment helpers.

Keeps event derivation logic isolated from HA I/O and prompt assembly.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

EVENT_BUFFER_SIZE = 20
EVENT_RETENTION_SECONDS = 30 * 60
EVENT_SUMMARY_LIMIT = 5
IGNORED_STATES = {"unknown", "unavailable"}


@dataclass
class HomeEvent:
    """A translated state transition derived from successive HA polls."""

    entity_id: str
    label: str
    old_state: str
    new_state: str
    timestamp: float

    def age_seconds(self, *, now: float | None = None) -> float:
        ref_now = time.time() if now is None else now
        return max(0.0, ref_now - self.timestamp)

    def describe(self, *, now: float | None = None) -> str:
        minutes_ago = max(1, round(self.age_seconds(now=now) / 60))
        return f"- {self.label}: {self.old_state} -> {self.new_state} ({minutes_ago} min fa)"


def prune_events(
    events: Iterable[HomeEvent],
    *,
    now: float | None = None,
    max_age_seconds: float = EVENT_RETENTION_SECONDS,
    max_events: int = EVENT_BUFFER_SIZE,
) -> deque[HomeEvent]:
    """Drop expired events while preserving bounded history."""
    ref_now = time.time() if now is None else now
    kept = [event for event in events if ref_now - event.timestamp <= max_age_seconds]
    return deque(kept, maxlen=max_events)


def diff_states(
    old_states: Mapping[str, dict],
    new_states: Mapping[str, dict],
    existing_events: Iterable[HomeEvent] | None,
    *,
    entity_labels: Mapping[str, str],
    state_translations: Mapping[str, str],
    now: float | None = None,
    max_age_seconds: float = EVENT_RETENTION_SECONDS,
    max_events: int = EVENT_BUFFER_SIZE,
) -> deque[HomeEvent]:
    """Generate bounded translated events from state changes."""
    ref_now = time.time() if now is None else now
    events = prune_events(
        existing_events or (),
        now=ref_now,
        max_age_seconds=max_age_seconds,
        max_events=max_events,
    )

    for entity_id, new_state_data in new_states.items():
        old_state_data = old_states.get(entity_id)
        if old_state_data is None:
            continue

        old_raw = str(old_state_data.get("state", "unknown"))
        new_raw = str(new_state_data.get("state", "unknown"))
        if old_raw == new_raw:
            continue
        if old_raw in IGNORED_STATES or new_raw in IGNORED_STATES:
            continue

        label = entity_labels.get(entity_id)
        if not label:
            continue
        # Translate states; pass through raw values (e.g., numeric power sensor readings)
        old_state = state_translations.get(old_raw, old_raw)
        new_state = state_translations.get(new_raw, new_raw)

        events.append(
            HomeEvent(
                entity_id=entity_id,
                label=label,
                old_state=old_state,
                new_state=new_state,
                timestamp=ref_now,
            )
        )

    return events


def build_events_summary(
    events: Iterable[HomeEvent],
    *,
    now: float | None = None,
    max_lines: int = EVENT_SUMMARY_LIMIT,
    max_age_seconds: float = EVENT_RETENTION_SECONDS,
) -> str:
    """Render the newest bounded events as prompt-ready text."""
    ref_now = time.time() if now is None else now
    recent_events = [event for event in events if ref_now - event.timestamp <= max_age_seconds]
    if not recent_events:
        return ""
    lines = [
        event.describe(now=ref_now)
        for event in sorted(recent_events, key=lambda event: event.timestamp, reverse=True)[:max_lines]
    ]
    return "\n".join(lines)
