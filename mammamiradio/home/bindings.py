"""Immutable, installation-local Home rules read from a verified private profile.

These values contain no authority on their own. Authorization, live consent and
hard mutes remain independent gates. Tuples keep snapshots safe to send to the
projection process without shared mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HomeEntity:
    entity_id: str
    role: str
    priority: str
    label_it: str
    label_en: str


@dataclass(frozen=True)
class HomeTrigger:
    entity_id: str
    state: str
    directive: str
    cooldown: int


@dataclass(frozen=True)
class HomeThreshold:
    entity_id: str
    threshold: float
    direction: str
    directive: str
    cooldown: int


@dataclass(frozen=True)
class HomeBindings:
    """A private snapshot; never serialize this object into station status."""

    identity: str = field(default="", repr=False)
    entities: tuple[HomeEntity, ...] = field(default=(), repr=False)
    reactive: tuple[HomeTrigger, ...] = field(default=(), repr=False)
    thresholds: tuple[HomeThreshold, ...] = field(default=(), repr=False)
    resident_returns: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    coffee_entities: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_document(cls, document: dict[str, Any], *, identity: str) -> HomeBindings:
        """Decode already-verified inert data; production verifies it in profile.py."""
        return cls(
            identity=identity,
            entities=tuple(
                HomeEntity(**{key: row[key] for key in HomeEntity.__dataclass_fields__}) for row in document["entities"]
            ),
            reactive=tuple(HomeTrigger(**row) for row in document["reactive_triggers"]),
            thresholds=tuple(HomeThreshold(**row) for row in document["threshold_triggers"]),
            resident_returns=tuple((row["entity_id"], row["alias"]) for row in document["resident_returns"]),
            coffee_entities=tuple(document["director_coffee_entities"]),
        )

    def entity(self, role: str) -> str:
        return next((row.entity_id for row in self.entities if row.role == role), "")

    def tier(self, priority: str) -> tuple[str, ...]:
        return tuple(row.entity_id for row in self.entities if row.priority == priority)

    @property
    def reactive_rows(self) -> tuple[tuple[str, str, str, int], ...]:
        return tuple((row.entity_id, row.state, row.directive, row.cooldown) for row in self.reactive)

    @property
    def threshold_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple({key: getattr(row, key) for key in HomeThreshold.__dataclass_fields__} for row in self.thresholds)

    @property
    def labels_it(self) -> dict[str, str]:
        return {"weather.ambient": "Meteo", "sun.ambient": "Luce del giorno"} | {
            row.entity_id: row.label_it for row in self.entities
        }

    @property
    def labels_en(self) -> dict[str, str]:
        return {"weather.ambient": "Weather", "sun.ambient": "Daylight"} | {
            row.entity_id: row.label_en for row in self.entities
        }


EMPTY_HOME_BINDINGS = HomeBindings()
