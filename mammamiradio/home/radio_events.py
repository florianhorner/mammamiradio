"""Configurable HA event promotion for radio-worthy moments.

This module is deliberately separate from the ambient HA context scoring path:
rules opt specific transitions in, but they never make the source entity visible
to the general prompt context.
"""

from __future__ import annotations

import fnmatch
import re
import time
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass

from mammamiradio.core.config import RadioEventRule
from mammamiradio.home.ha_enrichment import HomeEvent

DENIED_DEVICE_CLASSES = {"signal_strength", "timestamp", "battery"}
DENIED_ENTITY_CATEGORIES = {"diagnostic", "config"}
DENIED_PRIVACY_DOMAINS = {"device_tracker", "camera", "alarm_control_panel"}
IGNORED_STATES = {"unknown", "unavailable"}

_DIRECTIVE_COOLDOWNS: dict[str, float] = {}
_INJECTION_RE = re.compile(r"(ignore previous|disregard|system override|forget your)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


@dataclass(frozen=True)
class RadioEventMatch:
    """A configured rule matched a concrete HA transition."""

    rule_id: str
    mode: str
    directive: str
    event: HomeEvent
    cooldown_seconds: int
    matched_at: float


def clear_radio_event_cooldowns() -> None:
    """Test/support hook for resetting directive cooldown state."""
    _DIRECTIVE_COOLDOWNS.clear()


def commit_radio_event_directive(match: RadioEventMatch, *, now: float | None = None) -> None:
    """Spend a directive rule cooldown after the producer accepts the directive."""
    if match.mode != "directive":
        return
    _DIRECTIVE_COOLDOWNS[match.rule_id] = time.time() if now is None else now


def build_radio_event_baseline(
    states: Mapping[str, dict],
    rules: Sequence[RadioEventRule] | None,
) -> dict[str, dict]:
    """Copy only rule-relevant state needed for the next matcher pass."""
    if not rules:
        return {}
    attr_keys = {
        rule.attribute for rule in rules if rule.attribute and rule.trigger in {"attribute", "numeric_threshold"}
    }
    baseline: dict[str, dict] = {}
    for entity_id, state_data in states.items():
        if not isinstance(state_data, dict):
            continue
        matching_rules = [rule for rule in rules if _rule_selects_entity(rule, entity_id, state_data)]
        if not matching_rules or _is_denied_entity(entity_id, state_data):
            continue
        attrs = state_data.get("attributes", {}) or {}
        if not isinstance(attrs, dict):
            attrs = {}
        copied_attrs: dict[str, object] = {}
        for key in {"device_class", "entity_category", *attr_keys}:
            if key in attrs:
                copied_attrs[key] = attrs[key]
        baseline[entity_id] = {
            "state": state_data.get("state"),
            "attributes": copied_attrs,
        }
    return baseline


def match_radio_events(
    rules: Sequence[RadioEventRule] | None,
    previous_states: Mapping[str, dict] | None,
    current_states: Mapping[str, dict],
    *,
    now: float | None = None,
    cooldowns: MutableMapping[str, float] | None = None,
) -> list[RadioEventMatch]:
    """Return configured event matches between the previous baseline and now.

    Directive cooldowns are checked here but committed by the producer only after
    it accepts the offered directive. Gag cooldowns are owned by EveningLedger so
    they are spent only after generated banter queues successfully.
    """
    if not rules or not previous_states:
        return []
    ref_now = time.time() if now is None else now
    directive_cooldowns = _DIRECTIVE_COOLDOWNS if cooldowns is None else cooldowns
    matches: list[RadioEventMatch] = []

    for entity_id, current in current_states.items():
        if not isinstance(current, dict) or _is_denied_entity(entity_id, current):
            continue
        previous = previous_states.get(entity_id)
        if not isinstance(previous, dict):
            continue
        for rule in rules:
            if not _rule_selects_entity(rule, entity_id, current):
                continue
            last_commit = directive_cooldowns.get(rule.id)
            if rule.mode == "directive" and last_commit is not None and ref_now - last_commit < rule.cooldown_seconds:
                continue
            transition = _match_rule_transition(rule, previous, current)
            if transition is None:
                continue
            old_raw, new_raw = transition
            matches.append(
                RadioEventMatch(
                    rule_id=rule.id,
                    mode=rule.mode,
                    directive=rule.directive if rule.mode == "directive" else "",
                    event=HomeEvent(
                        entity_id=entity_id,
                        label=rule.label or rule.id,
                        old_state=_safe_value(old_raw),
                        new_state=_safe_value(new_raw),
                        timestamp=ref_now,
                        raw_old_state=_safe_value(old_raw),
                        raw_new_state=_safe_value(new_raw),
                        force_gag_candidate=rule.mode == "gag",
                        gag_cooldown_seconds=float(rule.cooldown_seconds) if rule.mode == "gag" else 0.0,
                    ),
                    cooldown_seconds=rule.cooldown_seconds,
                    matched_at=ref_now,
                )
            )
    return matches


def _rule_selects_entity(rule: RadioEventRule, entity_id: str, state_data: Mapping[str, object]) -> bool:
    domain = _domain(entity_id)
    attrs = state_data.get("attributes", {}) if isinstance(state_data, Mapping) else {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    if rule.device_class and str(attrs.get("device_class", "")) != rule.device_class:
        return False
    return bool(
        (rule.entity_id and entity_id == rule.entity_id)
        or (rule.entity_glob and fnmatch.fnmatchcase(entity_id, rule.entity_glob))
        or (rule.domain and domain == rule.domain)
    )


def _is_denied_entity(entity_id: str, state_data: Mapping[str, object]) -> bool:
    domain = _domain(entity_id)
    object_id = entity_id.split(".", 1)[-1]
    if object_id.startswith("mammamiradio"):
        return True
    if domain in DENIED_PRIVACY_DOMAINS:
        return True
    attrs = state_data.get("attributes", {})
    if not isinstance(attrs, Mapping):
        attrs = {}
    if str(attrs.get("entity_category", "")) in DENIED_ENTITY_CATEGORIES:
        return True
    if str(attrs.get("device_class", "")) in DENIED_DEVICE_CLASSES:
        return True
    return str(state_data.get("state", "")).lower() in IGNORED_STATES


def _match_rule_transition(
    rule: RadioEventRule,
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> tuple[object, object] | None:
    if rule.trigger == "state":
        old_raw = previous.get("state")
        new_raw = current.get("state")
        if old_raw == new_raw:
            return None
        if rule.from_state and str(old_raw) != rule.from_state:
            return None
        if rule.to_state and str(new_raw) != rule.to_state:
            return None
        return old_raw, new_raw

    if rule.trigger == "attribute":
        old_raw = _attr(previous, rule.attribute)
        new_raw = _attr(current, rule.attribute)
        if old_raw == new_raw:
            return None
        if rule.from_value and str(old_raw) != rule.from_value:
            return None
        if rule.to_value and str(new_raw) != rule.to_value:
            return None
        return old_raw, new_raw

    old_num = _numeric_rule_value(previous, rule)
    new_num = _numeric_rule_value(current, rule)
    if old_num is None or new_num is None or rule.threshold is None:
        return None
    if rule.direction == "above" and old_num <= rule.threshold < new_num:
        return old_num, new_num
    if rule.direction == "below" and old_num >= rule.threshold > new_num:
        return old_num, new_num
    return None


def _numeric_rule_value(state_data: Mapping[str, object], rule: RadioEventRule) -> float | None:
    raw = _attr(state_data, rule.attribute) if rule.attribute else state_data.get("state")
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _attr(state_data: Mapping[str, object], name: str) -> object:
    attrs = state_data.get("attributes", {})
    if not isinstance(attrs, Mapping):
        return None
    return attrs.get(name)


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _safe_value(value: object, *, max_len: int = 80) -> str:
    text = str(value if value is not None else "").strip()[:max_len]
    if not text:
        return ""
    if _INJECTION_RE.search(text) or _EMAIL_RE.search(text) or _IP_RE.search(text):
        return "(filtered)"
    return text
