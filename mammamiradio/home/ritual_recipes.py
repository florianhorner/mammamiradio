"""Community-inspired Home Assistant ritual recipes for Impossible Moments.

The catalog is intentionally pure: it scores local HA evidence and returns
matches that can be fed into the existing interrupt / directive / running-gag /
ambient lanes. A recipe never activates from a generic idea alone; live airtime
requires a concrete local state transition or explicit future config.
"""

from __future__ import annotations

import fnmatch
import re
import time
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Literal

from mammamiradio.home.ha_enrichment import HomeEvent

DeliveryLane = Literal["interrupt", "directive", "running_gag", "ambient_context"]
PrivacyClass = Literal["public", "private", "intimate", "safety"]
PatternTrigger = Literal["state", "attribute", "numeric_threshold"]

CATALOG_VERSION = "2026-07-06.community-v1"

DENIED_DEVICE_CLASSES = {"signal_strength", "timestamp", "battery"}
DENIED_ENTITY_CATEGORIES = {"diagnostic", "config"}
DENIED_PRIVACY_DOMAINS = {"device_tracker", "camera", "alarm_control_panel"}
IGNORED_STATES = {"unknown", "unavailable"}
OPEN_STATES = {"on", "open", "opening", "unlocked", "detected"}
CLOSED_STATES = {"off", "closed", "closing", "locked", "clear"}

_RITUAL_COOLDOWNS: dict[str, float] = {}
_INJECTION_RE = re.compile(r"(ignore previous|disregard|system override|forget your)", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class RitualEvidencePattern:
    """One local-evidence shape that can activate a recipe."""

    id: str
    label: str
    trigger: PatternTrigger = "state"
    domains: tuple[str, ...] = ()
    entity_globs: tuple[str, ...] = ()
    device_classes: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    from_states: tuple[str, ...] = ()
    to_states: tuple[str, ...] = ()
    attribute: str = ""
    from_values: tuple[str, ...] = ()
    to_values: tuple[str, ...] = ()
    threshold: float | None = None
    direction: Literal["above", "below"] = "above"
    confidence: float = 0.5


@dataclass(frozen=True)
class RitualRecipe:
    """A reusable community-pattern recipe for an Impossible Moment."""

    id: str
    family: str
    public_family_label: str
    delivery_lane: DeliveryLane
    privacy_class: PrivacyClass
    cooldown_seconds: int
    min_confidence: float
    evidence_patterns: tuple[RitualEvidencePattern, ...]
    directive: str
    sample_host_framing: tuple[str, ...]
    interrupt_urgency: Literal["pissed", "urgent", "gentle"] = "gentle"
    source: str = "community_catalog"


@dataclass(frozen=True)
class RitualRecipeMatch:
    """A recipe matched one concrete HA transition."""

    recipe: RitualRecipe
    pattern: RitualEvidencePattern
    entity_id: str
    label: str
    old_value: str
    new_value: str
    confidence: float
    matched_at: float

    @property
    def rule_id(self) -> str:
        return f"ritual:{self.recipe.id}:{self.pattern.id}"

    def to_home_event(self) -> HomeEvent:
        return HomeEvent(
            entity_id=self.entity_id,
            label=self.recipe.public_family_label,
            old_state=self.old_value,
            new_state=self.new_value,
            timestamp=self.matched_at,
            raw_old_state=self.old_value,
            raw_new_state=self.new_value,
            force_gag_candidate=self.recipe.delivery_lane == "running_gag",
            gag_cooldown_seconds=(
                float(self.recipe.cooldown_seconds) if self.recipe.delivery_lane == "running_gag" else 0.0
            ),
            ritual_family=self.recipe.family,
        )

    def to_status_dict(self) -> dict[str, object]:
        """Admin-safe recipe telemetry; public surfaces get only family labels."""
        return {
            "catalog_version": CATALOG_VERSION,
            "recipe_id": self.recipe.id,
            "family": self.recipe.family,
            "public_family_label": self.recipe.public_family_label,
            "delivery_lane": self.recipe.delivery_lane,
            "privacy_class": self.recipe.privacy_class,
            "confidence": round(self.confidence, 3),
            "evidence": self.pattern.label,
            "entity_id": self.entity_id,
            "label": self.label,
            "sample_host_framing": list(self.recipe.sample_host_framing),
        }


def clear_ritual_recipe_cooldowns() -> None:
    """Test/support hook for resetting recipe cooldown state."""
    _RITUAL_COOLDOWNS.clear()


def commit_ritual_recipe_match(match: RitualRecipeMatch, *, now: float | None = None) -> None:
    """Spend a recipe cooldown after the producer accepts the matched lane."""
    _RITUAL_COOLDOWNS[match.recipe.id] = time.time() if now is None else now


def build_ritual_recipe_baseline(
    states: Mapping[str, dict],
    recipes: Sequence[RitualRecipe] | None = None,
) -> dict[str, dict]:
    """Copy only recipe-relevant HA state needed for the next matcher pass."""
    selected_recipes = tuple(recipes or DEFAULT_RITUAL_RECIPES)
    attr_keys = {"device_class", "entity_category", "friendly_name", "registry_entity_name", "area", "area_name"}
    for recipe in selected_recipes:
        for pattern in recipe.evidence_patterns:
            if pattern.attribute:
                attr_keys.add(pattern.attribute)

    baseline: dict[str, dict] = {}
    for entity_id, state_data in states.items():
        if not isinstance(state_data, dict) or _is_denied_entity(entity_id, state_data):
            continue
        if not any(_recipe_selects_entity(recipe, entity_id, state_data) for recipe in selected_recipes):
            continue
        attrs = state_data.get("attributes", {}) or {}
        if not isinstance(attrs, Mapping):
            attrs = {}
        baseline[entity_id] = {
            "state": state_data.get("state"),
            "attributes": {key: attrs[key] for key in attr_keys if key in attrs},
        }
    return baseline


def match_ritual_recipes(
    recipes: Sequence[RitualRecipe] | None,
    previous_states: Mapping[str, dict] | None,
    current_states: Mapping[str, dict],
    *,
    now: float | None = None,
    cooldowns: MutableMapping[str, float] | None = None,
) -> list[RitualRecipeMatch]:
    """Return recipe matches between the previous baseline and current states."""
    selected_recipes = tuple(recipes or DEFAULT_RITUAL_RECIPES)
    if not selected_recipes or not previous_states:
        return []
    ref_now = time.time() if now is None else now
    recipe_cooldowns = _RITUAL_COOLDOWNS if cooldowns is None else cooldowns
    matches: list[RitualRecipeMatch] = []

    for recipe in selected_recipes:
        last_commit = recipe_cooldowns.get(recipe.id)
        if last_commit is not None and ref_now - last_commit < recipe.cooldown_seconds:
            continue
        best: RitualRecipeMatch | None = None
        for entity_id, current in current_states.items():
            if not isinstance(current, dict) or _is_denied_entity(entity_id, current):
                continue
            previous = previous_states.get(entity_id)
            if not isinstance(previous, dict):
                continue
            for pattern in recipe.evidence_patterns:
                if not _pattern_selects_entity(pattern, entity_id, current):
                    continue
                transition = _match_pattern_transition(pattern, previous, current)
                if transition is None:
                    continue
                old_raw, new_raw = transition
                confidence = min(1.0, max(pattern.confidence, recipe.min_confidence))
                if confidence < recipe.min_confidence:
                    continue
                candidate = RitualRecipeMatch(
                    recipe=recipe,
                    pattern=pattern,
                    entity_id=entity_id,
                    label=_entity_label(entity_id, current),
                    old_value=_safe_value(old_raw),
                    new_value=_safe_value(new_raw),
                    confidence=confidence,
                    matched_at=ref_now,
                )
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
        if best is not None:
            matches.append(best)

    return sorted(matches, key=lambda match: match.confidence, reverse=True)


def public_family_labels(matches: Iterable[RitualRecipeMatch], *, limit: int = 4) -> list[str]:
    """Return coarse family labels for public status/share surfaces."""
    labels: list[str] = []
    for match in matches:
        label = match.recipe.public_family_label
        if label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def audit_ritual_recipes(
    *,
    states: Mapping[str, dict] | None = None,
    logbook_events: Iterable[Mapping[str, object] | HomeEvent] | None = None,
    recipes: Sequence[RitualRecipe] | None = None,
) -> list[dict[str, object]]:
    """Score local recipe evidence and cookbook opportunities.

    ``states`` is the current retained HA state snapshot. ``logbook_events`` can
    be Home Assistant logbook rows or HomeEvent objects. Recipes with no local
    evidence remain visible as opportunities, but they do not activate airtime.
    """
    selected_recipes = tuple(recipes or DEFAULT_RITUAL_RECIPES)
    state_map = states or {}
    event_texts = [_event_search_text(event) for event in (logbook_events or ())]
    results: list[dict[str, object]] = []

    for recipe in selected_recipes:
        evidence: list[str] = []
        for pattern in recipe.evidence_patterns:
            if any(
                isinstance(data, Mapping)
                and not _is_denied_entity(entity_id, data)
                and _pattern_selects_entity(pattern, entity_id, data)
                for entity_id, data in state_map.items()
            ):
                evidence.append(pattern.label)
                continue
            if event_texts and any(_pattern_keywords_match(pattern, text) for text in event_texts):
                evidence.append(pattern.label)
        unique_evidence = list(dict.fromkeys(evidence))
        score = min(1.0, len(unique_evidence) / max(1, min(3, len(recipe.evidence_patterns))))
        results.append(
            {
                "catalog_version": CATALOG_VERSION,
                "recipe_id": recipe.id,
                "family": recipe.family,
                "public_family_label": recipe.public_family_label,
                "delivery_lane": recipe.delivery_lane,
                "privacy_class": recipe.privacy_class,
                "status": "instrumented" if unique_evidence else "opportunity",
                "score": round(score, 3),
                "local_evidence": unique_evidence[:5],
                "missing_evidence": [
                    pattern.label for pattern in recipe.evidence_patterns if pattern.label not in unique_evidence
                ][:5],
                "sample_host_framing": list(recipe.sample_host_framing),
            }
        )

    return sorted(results, key=_audit_sort_key)


def _audit_sort_key(item: Mapping[str, object]) -> tuple[bool, float]:
    score = item.get("score", 0.0)
    numeric_score = float(score) if isinstance(score, int | float) else 0.0
    return item.get("status") != "instrumented", -numeric_score


def _recipe_selects_entity(recipe: RitualRecipe, entity_id: str, state_data: Mapping[str, object]) -> bool:
    return any(_pattern_selects_entity(pattern, entity_id, state_data) for pattern in recipe.evidence_patterns)


def _pattern_selects_entity(pattern: RitualEvidencePattern, entity_id: str, state_data: Mapping[str, object]) -> bool:
    attrs = state_data.get("attributes", {}) if isinstance(state_data, Mapping) else {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    domain = _domain(entity_id)
    device_class = str(attrs.get("device_class", ""))
    if pattern.domains and domain not in pattern.domains:
        return False
    if pattern.entity_globs and not any(fnmatch.fnmatchcase(entity_id, glob) for glob in pattern.entity_globs):
        return False
    if pattern.device_classes and device_class not in pattern.device_classes:
        return False
    if pattern.keywords and not _pattern_keywords_match(pattern, _entity_search_text(entity_id, state_data)):
        return False
    return bool(pattern.domains or pattern.entity_globs or pattern.device_classes or pattern.keywords)


def _pattern_keywords_match(pattern: RitualEvidencePattern, text: str) -> bool:
    if not pattern.keywords:
        return False
    text_tokens = _tokenize_keyword_text(text)
    if not text_tokens:
        return False
    return any(_contains_token_phrase(text_tokens, _tokenize_keyword_text(keyword)) for keyword in pattern.keywords)


def _tokenize_keyword_text(text: str) -> tuple[str, ...]:
    return tuple(_WORD_RE.findall(text.replace("_", " ").replace("-", " ").casefold()))


def _contains_token_phrase(text_tokens: tuple[str, ...], keyword_tokens: tuple[str, ...]) -> bool:
    if not keyword_tokens:
        return False
    if len(keyword_tokens) == 1:
        return keyword_tokens[0] in text_tokens
    phrase_len = len(keyword_tokens)
    return any(
        text_tokens[index : index + phrase_len] == keyword_tokens for index in range(len(text_tokens) - phrase_len + 1)
    )


def _match_pattern_transition(
    pattern: RitualEvidencePattern,
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> tuple[object, object] | None:
    if pattern.trigger == "attribute":
        old_raw = _attr(previous, pattern.attribute)
        new_raw = _attr(current, pattern.attribute)
        if old_raw == new_raw:
            return None
        if pattern.from_values and str(old_raw) not in pattern.from_values:
            return None
        if pattern.to_values and str(new_raw) not in pattern.to_values:
            return None
        return old_raw, new_raw

    if pattern.trigger == "numeric_threshold":
        old_num = _numeric_value(previous, pattern.attribute)
        new_num = _numeric_value(current, pattern.attribute)
        if old_num is None or new_num is None or pattern.threshold is None:
            return None
        if pattern.direction == "above" and old_num <= pattern.threshold < new_num:
            return old_num, new_num
        if pattern.direction == "below" and old_num >= pattern.threshold > new_num:
            return old_num, new_num
        return None

    old_raw = previous.get("state")
    new_raw = current.get("state")
    if old_raw == new_raw:
        return None
    old_state = str(old_raw).casefold()
    new_state = str(new_raw).casefold()
    from_states = tuple(state.casefold() for state in pattern.from_states)
    to_states = tuple(state.casefold() for state in pattern.to_states)
    if from_states and old_state not in from_states:
        return None
    if to_states and new_state not in to_states:
        return None
    return old_raw, new_raw


def _numeric_value(state_data: Mapping[str, object], attr_name: str = "") -> float | None:
    raw = _attr(state_data, attr_name) if attr_name else state_data.get("state")
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _attr(state_data: Mapping[str, object], attr_name: str) -> object:
    if not attr_name:
        return None
    attrs = state_data.get("attributes", {})
    if not isinstance(attrs, Mapping):
        return None
    return attrs.get(attr_name)


def _is_denied_entity(entity_id: str, state_data: Mapping[str, object]) -> bool:
    domain = _domain(entity_id)
    object_id = entity_id.split(".", 1)[-1]
    if object_id.startswith("mammamiradio") or domain in DENIED_PRIVACY_DOMAINS:
        return True
    attrs = state_data.get("attributes", {})
    if not isinstance(attrs, Mapping):
        attrs = {}
    if str(attrs.get("entity_category", "")) in DENIED_ENTITY_CATEGORIES:
        return True
    if str(attrs.get("device_class", "")) in DENIED_DEVICE_CLASSES:
        return True
    return str(state_data.get("state", "")).casefold() in IGNORED_STATES


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _entity_label(entity_id: str, state_data: Mapping[str, object]) -> str:
    attrs = state_data.get("attributes", {}) if isinstance(state_data, Mapping) else {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    for key in ("friendly_name", "registry_entity_name", "registry_device_name"):
        raw = attrs.get(key)
        if raw:
            return _safe_value(raw, max_len=80)
    return entity_id


def _entity_search_text(entity_id: str, state_data: Mapping[str, object]) -> str:
    attrs = state_data.get("attributes", {}) if isinstance(state_data, Mapping) else {}
    if not isinstance(attrs, Mapping):
        attrs = {}
    bits = [entity_id.replace("_", " ")]
    for key in ("friendly_name", "registry_entity_name", "registry_device_name", "area", "area_name", "device_class"):
        value = attrs.get(key)
        if value:
            bits.append(str(value).replace("_", " "))
    return " ".join(bits).casefold()


def _event_search_text(event: Mapping[str, object] | HomeEvent) -> str:
    if isinstance(event, HomeEvent):
        return (
            " ".join(
                [
                    event.entity_id,
                    event.label,
                    event.old_state,
                    event.new_state,
                    event.raw_old_state,
                    event.raw_new_state,
                ]
            )
            .replace("_", " ")
            .casefold()
        )
    bits = []
    for key in ("entity_id", "name", "message", "state", "old_state", "new_state", "domain"):
        value = event.get(key)
        if value:
            bits.append(str(value).replace("_", " "))
    return " ".join(bits).casefold()


def _safe_value(value: object, max_len: int = 100) -> str:
    text = str(value)[:max_len].replace("<", "").replace(">", "")
    if _INJECTION_RE.search(text) or _EMAIL_RE.search(text) or _IP_RE.search(text):
        return "(filtered)"
    return text


DEFAULT_RITUAL_RECIPES: tuple[RitualRecipe, ...] = (
    RitualRecipe(
        id="morning_launch",
        family="morning_launch",
        public_family_label="Morning launch",
        delivery_lane="directive",
        privacy_class="private",
        cooldown_seconds=6 * 60 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="alarm_dismissed",
                label="alarm dismissed",
                domains=("input_boolean", "switch", "binary_sensor"),
                keywords=("alarm", "wecker", "wake"),
                from_states=tuple(OPEN_STATES),
                to_states=tuple(CLOSED_STATES),
                confidence=0.7,
            ),
            RitualEvidencePattern(
                id="coffee_power",
                label="coffee or kettle power starts",
                trigger="numeric_threshold",
                domains=("sensor",),
                keywords=("coffee", "kaffee", "kettle", "wasserkocher"),
                threshold=40.0,
                direction="above",
                confidence=0.8,
            ),
            RitualEvidencePattern(
                id="blinds_open",
                label="morning blinds open",
                domains=("cover",),
                keywords=("blind", "rollo", "shade", "curtain"),
                from_states=("closed", "closing"),
                to_states=("open", "opening"),
                confidence=0.65,
            ),
        ),
        directive=(
            "Morning launch just started at home. Treat it as the family's takeoff checklist: "
            "warm, quick, and lightly teasing, not a report."
        ),
        sample_host_framing=(
            "The apartment is doing the morning launch sequence.",
            "Somebody pressed start on the day and the house is already gossiping.",
        ),
    ),
    RitualRecipe(
        id="cooking_kitchen",
        family="cooking_kitchen",
        public_family_label="Kitchen ritual",
        delivery_lane="running_gag",
        privacy_class="private",
        cooldown_seconds=45 * 60,
        min_confidence=0.6,
        evidence_patterns=(
            RitualEvidencePattern(
                id="kitchen_fan",
                label="kitchen fan starts",
                domains=("fan", "switch"),
                keywords=("kitchen", "kuche", "cooking", "dunst", "extractor"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.75,
            ),
            RitualEvidencePattern(
                id="oven_or_stove_power",
                label="oven or stove power rises",
                trigger="numeric_threshold",
                domains=("sensor",),
                keywords=("oven", "stove", "herd", "backofen", "kochfeld"),
                threshold=200.0,
                direction="above",
                confidence=0.75,
            ),
            RitualEvidencePattern(
                id="kitchen_motion",
                label="kitchen presence starts",
                domains=("binary_sensor",),
                device_classes=("occupancy", "motion", "presence"),
                keywords=("kitchen", "kuche"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.6,
            ),
        ),
        directive=("Kitchen ritual detected. Make it feel like the hosts know the kitchen has become a tiny stage."),
        sample_host_framing=("The kitchen has entered its opera phase.", "The pots are auditioning again."),
    ),
    RitualRecipe(
        id="shower_bathroom",
        family="shower_bathroom",
        public_family_label="Bathroom ritual",
        delivery_lane="running_gag",
        privacy_class="intimate",
        cooldown_seconds=60 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="bathroom_fan",
                label="bathroom fan starts",
                domains=("fan", "switch"),
                keywords=("bath", "bad", "shower", "dusche", "lufter", "luefter"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.75,
            ),
            RitualEvidencePattern(
                id="humidity_rises",
                label="bathroom humidity rises",
                trigger="numeric_threshold",
                domains=("sensor",),
                device_classes=("humidity",),
                keywords=("bath", "bad", "shower", "dusche"),
                threshold=65.0,
                direction="above",
                confidence=0.8,
            ),
        ),
        directive="Bathroom ritual detected. Keep it sitcom-light and never graphic.",
        sample_host_framing=("The bathroom steam department has filed a memo.", "The fan is doing overtime again."),
    ),
    RitualRecipe(
        id="sleep_wake",
        family="sleep_wake",
        public_family_label="Sleep/wake ritual",
        delivery_lane="directive",
        privacy_class="intimate",
        cooldown_seconds=4 * 60 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="sleep_state_changed",
                label="sleep state changes",
                domains=("sensor", "input_select", "select"),
                keywords=("sleep", "sleeping", "asleep", "schlaf", "bed", "bett", "wake", "awake"),
                to_states=(
                    "asleep",
                    "sleeping",
                    "sleep",
                    "schlafen",
                    "occupied",
                    "awake",
                    "waking",
                    "wake",
                    "woke",
                    "up",
                ),
                confidence=0.7,
            ),
            RitualEvidencePattern(
                id="bed_occupancy_changed",
                label="bed occupancy changes",
                domains=("binary_sensor",),
                device_classes=("occupancy", "presence"),
                keywords=("bed", "bett", "sleep", "schlaf"),
                confidence=0.75,
            ),
        ),
        directive=(
            "Sleep or wake rhythm just changed. Mention it with private-audio intimacy: warm, brief, "
            "plausibly observant, never creepy."
        ),
        sample_host_framing=("The house has noticed a chapter change.", "Somebody moved from dream mode to plot mode."),
    ),
    RitualRecipe(
        id="media_betrayal",
        family="media_betrayal",
        public_family_label="Media ritual",
        delivery_lane="directive",
        privacy_class="private",
        cooldown_seconds=45 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="tv_or_streaming_started",
                label="TV or streaming starts",
                domains=("media_player",),
                keywords=("tv", "television", "samsung", "netflix", "prime", "disney", "plex"),
                from_states=("off", "idle", "paused", "standby"),
                to_states=("on", "playing"),
                confidence=0.7,
            ),
            RitualEvidencePattern(
                id="speaker_source_changed",
                label="speaker source changes",
                trigger="attribute",
                domains=("media_player",),
                keywords=("sonos", "speaker", "music assistant"),
                attribute="source",
                confidence=0.7,
            ),
            RitualEvidencePattern(
                id="foreign_station",
                label="another station starts playing",
                domains=("media_player",),
                keywords=("radio", "station", "sonos", "speaker"),
                from_states=("idle", "paused", "off"),
                to_states=("playing", "on"),
                confidence=0.65,
            ),
        ),
        directive=(
            "A media ritual started, possibly betrayal by TV or another station. Tease it like an Italian "
            "family sitcom, not like surveillance."
        ),
        sample_host_framing=(
            "Boooo, another audio source has entered the room.",
            "The TV has begun its bad influence era.",
        ),
    ),
    RitualRecipe(
        id="fridge_freezer_raid",
        family="fridge_freezer_raid",
        public_family_label="Kitchen ritual",
        delivery_lane="running_gag",
        privacy_class="private",
        cooldown_seconds=30 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="fridge_opened",
                label="fridge opens",
                domains=("binary_sensor",),
                device_classes=("door", "opening"),
                keywords=("fridge", "freezer", "kuhlschrank", "gefrier", "gefrierschrank"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.8,
            ),
        ),
        directive="Fridge or freezer ritual detected. Treat the appliance like a recurring character.",
        sample_host_framing=("The fridge has been consulted again.", "The freezer door has opinions."),
    ),
    RitualRecipe(
        id="windows_airing",
        family="windows_airing",
        public_family_label="Window ritual",
        delivery_lane="running_gag",
        privacy_class="private",
        cooldown_seconds=45 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="window_opened",
                label="window opens",
                domains=("binary_sensor", "cover"),
                device_classes=("window", "opening"),
                keywords=("window", "fenster", "balcony", "terrace", "terrasse"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.75,
            ),
        ),
        directive="Window airing ritual detected. Make it about the house taking a dramatic breath.",
        sample_host_framing=("The apartment has opened a lung.", "Fresh air has been given editorial control."),
    ),
    RitualRecipe(
        id="chores_reminders",
        family="chores_reminders",
        public_family_label="Chore ritual",
        delivery_lane="running_gag",
        privacy_class="private",
        cooldown_seconds=2 * 60 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="laundry_done",
                label="laundry power drops",
                trigger="numeric_threshold",
                domains=("sensor",),
                keywords=("washer", "washing", "laundry", "waschmaschine", "dryer", "trockner"),
                threshold=5.0,
                direction="below",
                confidence=0.8,
            ),
            RitualEvidencePattern(
                id="mailbox_opened",
                label="mailbox opens",
                domains=("binary_sensor",),
                device_classes=("door", "opening"),
                keywords=("mailbox", "briefkasten", "mail"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.75,
            ),
            RitualEvidencePattern(
                id="trash_day",
                label="trash reminder fires",
                domains=("calendar", "input_boolean", "binary_sensor", "sensor"),
                keywords=("trash", "garbage", "muell", "mull", "recycling", "gelber sack"),
                to_states=tuple(OPEN_STATES),
                confidence=0.65,
            ),
        ),
        directive="Chore ritual detected. Land it as a recurring household callback, not an alarm.",
        sample_host_framing=(
            "The chore department has issued another memo.",
            "Laundry has reached the applause phase.",
        ),
    ),
    RitualRecipe(
        id="safety_saves",
        family="safety_saves",
        public_family_label="Safety moment",
        delivery_lane="interrupt",
        privacy_class="safety",
        cooldown_seconds=10 * 60,
        min_confidence=0.8,
        interrupt_urgency="urgent",
        evidence_patterns=(
            RitualEvidencePattern(
                id="leak_detected",
                label="leak detected",
                domains=("binary_sensor",),
                device_classes=("moisture",),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.95,
            ),
            RitualEvidencePattern(
                id="smoke_or_gas_detected",
                label="smoke, gas, or CO detected",
                domains=("binary_sensor",),
                device_classes=("smoke", "gas", "carbon_monoxide"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=1.0,
            ),
            RitualEvidencePattern(
                id="garage_or_door_opened",
                label="garage or critical door opens",
                domains=("binary_sensor", "cover", "lock"),
                device_classes=("garage_door", "door", "opening", "lock"),
                keywords=("garage", "front door", "entrance", "eingang", "haustur", "haustr", "door left"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.8,
            ),
        ),
        directive=(
            "Safety moment detected. Interrupt with calm urgency, say only the coarse safety family, and tell the "
            "household to check Home Assistant."
        ),
        sample_host_framing=("Safety department, subito.", "Small interruption, important: check the house."),
    ),
    RitualRecipe(
        id="vacation_house_sitter",
        family="vacation_house_sitter",
        public_family_label="House mode",
        delivery_lane="ambient_context",
        privacy_class="private",
        cooldown_seconds=6 * 60 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="vacation_mode_enabled",
                label="vacation mode enabled",
                domains=("input_boolean", "input_select", "select"),
                keywords=("vacation", "holiday", "urlaub", "guest", "house sitter", "housesitter", "babysitter"),
                to_states=("on", "vacation", "holiday", "guest", "housesitter", "house_sitter"),
                confidence=0.85,
            ),
            RitualEvidencePattern(
                id="away_mode_enabled",
                label="away mode enabled",
                domains=("input_select", "select"),
                keywords=("away", "abwesend", "alarm", "mode"),
                to_states=("armed_away", "away", "on"),
                confidence=0.65,
            ),
        ),
        directive="House mode changed. Keep it as narrative weather for the home, not a security disclosure.",
        sample_host_framing=("The house has put on its away-mode costume.", "House-sitter cinema has begun."),
    ),
    RitualRecipe(
        id="vacuum_doorbell_protocol",
        family="vacuum_doorbell_protocol",
        public_family_label="House protocol",
        delivery_lane="directive",
        privacy_class="private",
        cooldown_seconds=45 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="vacuum_starts",
                label="vacuum starts",
                domains=("vacuum",),
                from_states=("docked", "idle", "paused", "returning"),
                to_states=("cleaning",),
                confidence=0.8,
            ),
            RitualEvidencePattern(
                id="doorbell_rings",
                label="doorbell or intercom rings",
                domains=("binary_sensor", "sensor"),
                keywords=("doorbell", "intercom", "klingel", "ring", "bell"),
                from_states=tuple(CLOSED_STATES),
                to_states=tuple(OPEN_STATES),
                confidence=0.85,
            ),
        ),
        directive=(
            "A house protocol moment fired: vacuum or doorbell. React like the household staff has started a scene."
        ),
        sample_host_framing=(
            "Protocollo casa: everybody look busy.",
            "The vacuum and the doorbell are forming a union.",
        ),
    ),
    RitualRecipe(
        id="pets_plants_optional",
        family="pets_plants_aquarium_pool",
        public_family_label="Care ritual",
        delivery_lane="ambient_context",
        privacy_class="private",
        cooldown_seconds=4 * 60 * 60,
        min_confidence=0.65,
        evidence_patterns=(
            RitualEvidencePattern(
                id="pet_feeding",
                label="pet feeding or litter event",
                domains=("binary_sensor", "switch", "sensor"),
                keywords=("pet", "cat", "dog", "futter", "feeder", "litter", "katze", "hund"),
                confidence=0.7,
            ),
            RitualEvidencePattern(
                id="plant_watering",
                label="plant watering or garden irrigation",
                domains=("switch", "sensor", "binary_sensor"),
                keywords=("plant", "pflanze", "garden", "garten", "watering", "irrigation", "bewasserung"),
                confidence=0.7,
            ),
            RitualEvidencePattern(
                id="aquarium_pool",
                label="aquarium, pond, or pool event",
                domains=("switch", "sensor", "binary_sensor"),
                keywords=("aquarium", "pond", "pool", "teich"),
                confidence=0.7,
            ),
        ),
        directive="Care ritual detected. Treat pets, plants, aquarium, or pool as optional recurring characters.",
        sample_host_framing=(
            "The care-and-feeding department is awake.",
            "Some non-human household stakeholder has notes.",
        ),
    ),
)
