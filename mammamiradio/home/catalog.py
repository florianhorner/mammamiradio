"""Home Assistant label catalog and resolver.

The resolver has four tiers:
1. curated Italian/English labels checked into this module;
2. generated catalog entries whose metadata hash still matches;
3. safe Home Assistant display names as a temporary fallback while generation
   is pending or unavailable;
4. drop the entity rather than leaking raw entity IDs into host prompts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mammamiradio.core.config import StationConfig, resolve_model

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "ha_label_catalog.json"
SCHEMA_VERSION = 1
MAX_BATCH_ENTITIES = 50
MAX_INPUT_TOKENS = 4000
MAX_LABEL_LENGTH = 80

_CATALOG_LOCK = asyncio.Lock()
_generation_scheduled: bool = False
_generation_tasks: set[asyncio.Task] = set()
_catalog_cache: dict | None = None
_catalog_cache_path: Path | None = None

_UUID_RE = re.compile(r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$")
_HEX_TOKEN_RE = re.compile(r"^[a-fA-F0-9]{32,}$")
_GENERIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_LAT_LON_RE = re.compile(r"[-+]?\d{1,2}\.\d{4,}\s*,\s*[-+]?\d{1,3}\.\d{4,}")
_PROMPT_INJECTION_RE = re.compile(r"(ignore previous|disregard|system override|forget your|</?home_state_data)", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# Italian-friendly labels for entity states
ENTITY_LABELS = {
    # Synthetic, privacy-safe R0 ambient projections. These IDs never identify
    # the operator's real HA source entity.
    "weather.ambient": "Meteo",
    "sun.ambient": "Luce del giorno",
    "switch.bar_kaffeemaschine_steckdose": "La macchina del caffè",
    "input_select.kaffee_dad_jokes": "Dad joke del caffè",
    "vacuum.goldstaubsucher": "Robot aspirapolvere Goldstaubsucher",
    "vacuum.matrix10_ultra": "Robot aspirapolvere Matrix10 Ultra",
    "weather.forecast_home": "Meteo al PentFLOuse",
    "person.florian_horner": "Florian",
    "person.sabrina": "Sabrina",
    "person.schnuffi": "Schnuffi",
    "lock.lock_ultra_8d3c": "Serratura porta d'ingresso",
    "input_button.foyer_fahrstuhl_fingerbot_push_button": "Ascensore (ultimo utilizzo)",
    "binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar": (
        "Presenza nel soggiorno/sala da pranzo/bar/cucina"
    ),
    "input_select.bedroom_occupancy_state": "Camera da letto",
    "switch.bad_gross_waschmaschine_steckdose": "Lavatrice",
    "media_player.samsung_s95ca_65": "Televisore Samsung",
    "media_player.wohnzimmer_sonos_arc_lautsprecher": "Sonos Arc soggiorno",
    "media_player.esszimmer": "Sonos sala da pranzo",
    "climate.wohnzimmer_tado_heizung": "Riscaldamento soggiorno",
    "climate.schlafzimmer": "Riscaldamento camera da letto",
    "sun.sun": "Sole",
    "fan.bad_gross_lufter_shelly": "Ventilatore bagno grande",
    "fan.bad_klein_lufter": "Ventilatore bagno piccolo",
    "fan.kuche_lufter": "Ventilatore cucina",
    "input_datetime.last_sleep_time": "Ultimo orario di sonno",
    "input_datetime.last_wake_time": "Ultimo orario di sveglia",
    "binary_sensor.buro_9_ring_intercom_klingelt": "Citofono",
    "light.magic_areas_light_groups_wohnzimmer_all_lights": "Luci soggiorno",
    "light.magic_areas_light_groups_schlafzimmer_all_lights": "Luci camera da letto",
    "light.magic_areas_light_groups_kuche_all_lights": "Luci cucina",
    "light.magic_areas_light_groups_esszimmer_all_lights": "Luci sala da pranzo",
    "sensor.bar_bali_boot_steckdose_power": "Lavatrice (consumo)",
    "sensor.kuche_kaffeemaschine_steckdose_power": "Caffettiera (consumo)",
    "light.schlafzimmer_sternenlicht_projektor_2": "Proiettore stelle camera",
    "light.kleiderschrank_sternenlicht_projektor": "Proiettore stelle guardaroba",
    "light.terrasse_9_outdoor_lichtschlauch": "Luci terrazza",
    "sensor.haushalt_stromverbrauch_gesamt": "Consumo elettrico totale",
}


# English entity labels for admin UI display (parallel to ENTITY_LABELS)
ENTITY_LABELS_EN: dict[str, str] = {
    "weather.ambient": "Weather",
    "sun.ambient": "Daylight",
    "switch.bar_kaffeemaschine_steckdose": "Coffee machine",
    "input_select.kaffee_dad_jokes": "Coffee dad joke",
    "vacuum.goldstaubsucher": "Robot vacuum Goldstaubsucher",
    "vacuum.matrix10_ultra": "Robot vacuum Matrix10 Ultra",
    "weather.forecast_home": "Weather at PentFLOuse",
    "person.florian_horner": "Florian",
    "person.sabrina": "Sabrina",
    "person.schnuffi": "Schnuffi",
    "lock.lock_ultra_8d3c": "Front door lock",
    "input_button.foyer_fahrstuhl_fingerbot_push_button": "Elevator (last used)",
    "binary_sensor.8_stockwerk_group_sensor_wohnzimmer_esszimmer_bar": "Living room/dining/bar presence",
    "input_select.bedroom_occupancy_state": "Bedroom",
    "switch.bad_gross_waschmaschine_steckdose": "Washing machine",
    "media_player.samsung_s95ca_65": "Samsung TV",
    "media_player.wohnzimmer_sonos_arc_lautsprecher": "Sonos Arc living room",
    "media_player.esszimmer": "Sonos dining room",
    "climate.wohnzimmer_tado_heizung": "Heating (living room)",
    "climate.schlafzimmer": "Heating (bedroom)",
    "sun.sun": "Sun",
    "fan.bad_gross_lufter_shelly": "Large bathroom fan",
    "fan.bad_klein_lufter": "Small bathroom fan",
    "fan.kuche_lufter": "Kitchen fan",
    "input_datetime.last_sleep_time": "Last sleep time",
    "input_datetime.last_wake_time": "Last wake time",
    "binary_sensor.buro_9_ring_intercom_klingelt": "Intercom",
    "light.magic_areas_light_groups_wohnzimmer_all_lights": "Living room lights",
    "light.magic_areas_light_groups_schlafzimmer_all_lights": "Bedroom lights",
    "light.magic_areas_light_groups_kuche_all_lights": "Kitchen lights",
    "light.magic_areas_light_groups_esszimmer_all_lights": "Dining room lights",
    "sensor.bar_bali_boot_steckdose_power": "Washing machine (power)",
    "sensor.kuche_kaffeemaschine_steckdose_power": "Coffee machine (power)",
    "light.schlafzimmer_sternenlicht_projektor_2": "Star projector (bedroom)",
    "light.kleiderschrank_sternenlicht_projektor": "Star projector (wardrobe)",
    "light.terrasse_9_outdoor_lichtschlauch": "Terrace lights",
    "sensor.haushalt_stromverbrauch_gesamt": "Total household power",
}


@dataclass(frozen=True)
class LabelResolution:
    label_it: str
    label_en: str
    tier: str


@dataclass(frozen=True)
class LabelCandidate:
    entity_id: str
    score: float
    entity_hash: str
    metadata: dict[str, Any]


def _catalog_path(cache_dir: Path) -> Path:
    """Return the on-disk catalog path for a cache directory."""
    return Path(cache_dir) / CATALOG_FILENAME


def _empty_catalog() -> dict:
    """Return a valid, empty catalog structure."""
    return {"schema_version": SCHEMA_VERSION, "generated_at": 0.0, "entries": {}}


def reset_catalog_cache() -> None:
    """Clear in-memory catalog state for tests and hot-reload style workflows."""
    global _catalog_cache, _catalog_cache_path, _generation_scheduled
    _catalog_cache = None
    _catalog_cache_path = None
    _generation_scheduled = False
    _generation_tasks.clear()


def _looks_sensitive_value(value: str) -> bool:
    """Return True if the string looks like a UUID, token, IP, MAC, email, or geo pair."""
    candidate = value.strip()
    if not candidate:
        return False
    return bool(
        _UUID_RE.fullmatch(candidate)
        or _HEX_TOKEN_RE.fullmatch(candidate)
        or _GENERIC_TOKEN_RE.fullmatch(candidate)
        or _IP_RE.search(candidate)
        or _MAC_RE.search(candidate)
        or _EMAIL_RE.search(candidate)
        or _LAT_LON_RE.search(candidate)
    )


def _sanitize_label_source(value: object, *, max_len: int = MAX_LABEL_LENGTH) -> str:
    """Strip control chars, angle brackets, and length; drop prompt-injection or sensitive values."""
    text = str(value or "").strip()
    text = _CONTROL_RE.sub("", text)
    text = text.replace("<", "").replace(">", "")
    text = re.sub(r"\s+", " ", text)[:max_len].strip()
    if _PROMPT_INJECTION_RE.search(text) or _looks_sensitive_value(text):
        return ""
    return text


def _entity_domain(entity_id: str) -> str:
    """Return the domain prefix of an entity_id (the part before the first dot)."""
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _attrs_dict(state_data: dict) -> dict:
    """Return the entity's attributes as a dict, tolerating malformed payloads.

    A non-dict ``attributes`` value (list, string, null) from a malformed Home
    Assistant snapshot must not raise ``AttributeError`` into the label path.
    """
    attrs = state_data.get("attributes", {})
    return attrs if isinstance(attrs, dict) else {}


def _metadata_value(attrs: dict, *keys: str) -> str:
    """Return the first non-empty sanitized attribute value among ``keys``."""
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            sanitized = _sanitize_label_source(value)
            if sanitized:
                return sanitized
    return ""


def _candidate_metadata(entity_id: str, state_data: dict) -> dict[str, Any]:
    """Build the safe, non-empty metadata sent to the LLM and used for hashing."""
    attrs = _attrs_dict(state_data)
    metadata = {
        "entity_id": entity_id,
        "domain": _entity_domain(entity_id),
        "friendly_name": _metadata_value(attrs, "friendly_name"),
        "registry_entity_name": _metadata_value(attrs, "registry_entity_name"),
        "registry_device_name": _metadata_value(attrs, "registry_device_name"),
        "area": _metadata_value(attrs, "area", "area_name", "area_id"),
        "device_class": _metadata_value(attrs, "device_class"),
        "unit": _metadata_value(attrs, "unit_of_measurement"),
    }
    return {key: value for key, value in metadata.items() if value}


def compute_hash(entity_id: str, state_data: dict) -> str:
    """Return a stable hash of safe label metadata, excluding sensitive attrs."""
    payload = _candidate_metadata(entity_id, state_data)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fallback_label(entity_id: str, state_data: dict) -> str | None:
    """Build a tier-3 label from registry/friendly names plus area, or None."""
    attrs = _attrs_dict(state_data)
    label = _metadata_value(attrs, "registry_entity_name", "friendly_name", "registry_device_name")
    area = _metadata_value(attrs, "area", "area_name", "area_id")
    if not label:
        return None
    if area and area.lower() not in label.lower():
        return f"{label} ({area})"
    return label


def _catalog_entry_valid(entity_id: str, entry: object, expected_hash: str) -> tuple[str, str] | None:
    """Return (label_it, label_en) if the cached entry matches the hash and validates, else None."""
    if not isinstance(entry, dict):
        return None
    if entry.get("hash") != expected_hash:
        return None
    label_it = _sanitize_label_source(entry.get("label_it"))
    label_en = _sanitize_label_source(entry.get("label_en"))
    if not (validate_label(label_it, entity_id) and validate_label(label_en, entity_id)):
        return None
    return label_it, label_en


def validate_label(label: str, entity_id: str) -> bool:
    """Reject labels that are unsafe, raw, too long, or prompt-like."""
    text = str(label or "").strip()
    object_id = entity_id.split(".", 1)[-1]
    if not text:
        return False
    if len(text) > MAX_LABEL_LENGTH:
        return False
    if "\n" in text or "\r" in text or _CONTROL_RE.search(text):
        return False
    if "<" in text or ">" in text or "{" in text or "}" in text:
        return False
    if _PROMPT_INJECTION_RE.search(text):
        return False
    lowered = text.lower()
    raw_object_id = object_id.lower()
    if entity_id.lower() in lowered or ("_" in raw_object_id and raw_object_id in lowered):
        return False
    return not _looks_sensitive_value(text)


def load_catalog_snapshot(cache_dir: Path | None) -> dict:
    """Read a detached catalog snapshot without touching the module cache.

    This is safe for the HA projection worker: it performs the same tolerant
    file validation as :func:`load_catalog` but owns the returned object and
    never races with the event-loop cache.
    """
    if cache_dir is None:
        return _empty_catalog()
    try:
        data = json.loads(_catalog_path(Path(cache_dir)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        data = _empty_catalog()
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        data = _empty_catalog()
    if not isinstance(data.get("entries"), dict):
        data = _empty_catalog()
    return data


def load_catalog(cache_dir: Path | None) -> dict:
    """Load the generated catalog once per cache path, degrading to empty."""
    global _catalog_cache, _catalog_cache_path
    if cache_dir is None:
        return _empty_catalog()
    path = _catalog_path(Path(cache_dir))
    if _catalog_cache is not None and _catalog_cache_path == path:
        return _catalog_cache
    data = load_catalog_snapshot(cache_dir)
    _catalog_cache = data
    _catalog_cache_path = path
    return data


def _atomic_write_json(path: Path, payload: dict) -> bool:
    """Write JSON to a unique temp file then atomically replace; owner-only perms. Returns success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        return True
    except OSError as exc:
        logger.warning("Failed to write HA label catalog %s: %s", path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def save_catalog(cache_dir: Path, catalog: dict) -> bool:
    """Persist a generated catalog with atomic replace and owner-only perms."""
    global _catalog_cache, _catalog_cache_path
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "entries": dict(catalog.get("entries") or {}),
    }
    path = _catalog_path(Path(cache_dir))
    if not _atomic_write_json(path, payload):
        return False
    _catalog_cache = payload
    _catalog_cache_path = path
    return True


def resolve_label(entity_id: str, state_data: dict, *, cache_dir: Path | None = None) -> LabelResolution | None:
    """Resolve a safe Italian/English display label for an HA entity."""
    if entity_id in ENTITY_LABELS:
        return LabelResolution(
            ENTITY_LABELS[entity_id],
            ENTITY_LABELS_EN.get(entity_id, ENTITY_LABELS[entity_id]),
            "curated",
        )

    expected_hash = compute_hash(entity_id, state_data)
    if cache_dir is not None:
        catalog = load_catalog(cache_dir)
        entry = (catalog.get("entries") or {}).get(entity_id)
        valid = _catalog_entry_valid(entity_id, entry, expected_hash)
        if valid is not None:
            label_it, label_en = valid
            return LabelResolution(label_it, label_en, "catalog")

    fallback = _fallback_label(entity_id, state_data)
    # Anti-illusion guard: a friendly/registry name that is really the raw
    # snake_case object_id (HA's default for unnamed entities) or a dotted
    # entity_id must not air. validate_label is the only tier with that check,
    # so the fallback must clear it too — otherwise drop to tier 4.
    if fallback and validate_label(fallback, entity_id):
        return LabelResolution(fallback, fallback, "fallback")
    return None


def _fallback_score(entity_id: str) -> float:
    """Return a default salience score by domain when no caller score is given."""
    domain = _entity_domain(entity_id)
    return {
        "media_player": 1.0,
        "vacuum": 0.9,
        "lock": 0.8,
        "weather": 0.8,
        "climate": 0.7,
        "light": 0.6,
        "fan": 0.6,
        "switch": 0.5,
        "sensor": 0.2,
        "binary_sensor": 0.2,
    }.get(domain, 0.1)


def _estimate_tokens(payload: dict) -> int:
    """Rough token estimate for a metadata payload (~4 chars per token)."""
    return max(1, len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) // 4)


def select_label_candidates(
    states: dict[str, dict],
    *,
    cache_dir: Path | None = None,
    score_by_entity: dict[str, float] | None = None,
    force: bool = False,
    max_entities: int = MAX_BATCH_ENTITIES,
    max_input_tokens: int = MAX_INPUT_TOKENS,
) -> list[LabelCandidate]:
    """Return salience-sorted generation candidates within entity/token caps."""
    scores = score_by_entity or {}
    catalog = load_catalog(cache_dir) if cache_dir is not None else _empty_catalog()
    entries = catalog.get("entries") or {}
    candidates: list[LabelCandidate] = []
    for entity_id, state_data in states.items():
        if entity_id in ENTITY_LABELS:
            continue
        state = str(state_data.get("state", "unknown"))
        if state in {"unknown", "unavailable"}:
            continue
        entity_hash = compute_hash(entity_id, state_data)
        if not force and _catalog_entry_valid(entity_id, entries.get(entity_id), entity_hash):
            continue
        metadata = _candidate_metadata(entity_id, state_data)
        if not metadata.get("entity_id"):
            continue
        candidates.append(
            LabelCandidate(
                entity_id=entity_id,
                score=float(scores.get(entity_id, _fallback_score(entity_id))),
                entity_hash=entity_hash,
                metadata=metadata,
            )
        )

    selected: list[LabelCandidate] = []
    used_tokens = 0
    for candidate in sorted(candidates, key=lambda item: (item.score, item.entity_id), reverse=True):
        token_cost = _estimate_tokens(candidate.metadata)
        if selected and (len(selected) >= max_entities or used_tokens + token_cost > max_input_tokens):
            continue
        if not selected and token_cost > max_input_tokens:
            continue
        selected.append(candidate)
        used_tokens += token_cost
        if len(selected) >= max_entities:
            break
    return selected


def generation_in_progress() -> bool:
    """Return True if a label refresh is scheduled or currently running."""
    return _generation_scheduled or _CATALOG_LOCK.locked()


def schedule_label_generation(
    states: dict[str, dict],
    *,
    cache_dir: Path | None,
    config: StationConfig,
    score_by_entity: dict[str, float] | None = None,
    force: bool = False,
) -> bool:
    """Schedule one background label refresh. Returns False when not scheduled."""
    global _generation_scheduled
    if cache_dir is None or not config.anthropic_api_key:
        return False
    if _generation_scheduled or _CATALOG_LOCK.locked():
        return False
    candidates = select_label_candidates(states, cache_dir=cache_dir, score_by_entity=score_by_entity, force=force)
    if not candidates:
        return False
    _generation_scheduled = True
    task = asyncio.create_task(
        _run_scheduled_generation(
            states,
            cache_dir=Path(cache_dir),
            config=config,
            score_by_entity=score_by_entity,
            force=force,
        )
    )
    _generation_tasks.add(task)
    task.add_done_callback(_generation_tasks.discard)
    return True


async def _run_scheduled_generation(
    states: dict[str, dict],
    *,
    cache_dir: Path,
    config: StationConfig,
    score_by_entity: dict[str, float] | None,
    force: bool,
) -> None:
    """Run one catalog refresh and always clear the scheduled flag when done."""
    global _generation_scheduled
    try:
        await generate_label_catalog(
            states,
            cache_dir=cache_dir,
            config=config,
            score_by_entity=score_by_entity,
            force=force,
        )
    finally:
        _generation_scheduled = False


async def generate_label_catalog(
    states: dict[str, dict],
    *,
    cache_dir: Path,
    config: StationConfig,
    score_by_entity: dict[str, float] | None = None,
    force: bool = False,
) -> dict:
    """Refresh generated labels, preserving the old catalog on any LLM failure."""
    if _CATALOG_LOCK.locked():
        return load_catalog(cache_dir)
    async with _CATALOG_LOCK:
        catalog = load_catalog(cache_dir)
        candidates = select_label_candidates(
            states,
            cache_dir=cache_dir,
            score_by_entity=score_by_entity,
            force=force,
        )
        if not candidates or not config.anthropic_api_key:
            return catalog
        try:
            generated = await _call_anthropic_labels(candidates, config, role="fast")
        except Exception as exc:
            logger.warning("HA label generation failed; preserving existing catalog: %s", exc)
            return catalog

        by_entity = {candidate.entity_id: candidate for candidate in candidates}
        entries = dict(catalog.get("entries") or {})
        accepted = 0
        for item in generated:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "")
            candidate = by_entity.get(entity_id)
            if candidate is None:
                continue
            label_it = _sanitize_label_source(item.get("label_it"))
            label_en = _sanitize_label_source(item.get("label_en"))
            if not (validate_label(label_it, entity_id) and validate_label(label_en, entity_id)):
                continue
            entries[entity_id] = {
                "hash": candidate.entity_hash,
                "label_it": label_it,
                "label_en": label_en,
                "generated_at": time.time(),
            }
            accepted += 1

        if accepted == 0:
            return catalog
        updated = {"schema_version": SCHEMA_VERSION, "generated_at": time.time(), "entries": entries}
        if not save_catalog(cache_dir, updated):
            # Fail-soft: a failed disk write must not be reported as a refresh.
            # Keep the existing catalog so the next poll retries.
            logger.warning("HA label catalog produced labels but persistence failed; keeping old catalog")
            return catalog
        logger.info("HA label catalog refreshed: %d/%d labels accepted", accepted, len(candidates))
        return updated


async def _call_anthropic_labels(
    candidates: list[LabelCandidate],
    config: StationConfig,
    *,
    role: str,
) -> list[dict]:
    """Ask the fast Anthropic model for Italian/English labels."""
    from anthropic import AsyncAnthropic

    model = _resolve_anthropic_fast_model(config)
    if not model:
        return []
    client = AsyncAnthropic(api_key=config.anthropic_api_key)
    prompt_payload = [candidate.metadata for candidate in candidates]
    prompt = (
        "Generate concise home-automation labels for an Italian radio host prompt. "
        "Return only JSON with a labels array. Each item must contain entity_id, "
        "label_it, and label_en. Do not include raw entity IDs in labels.\n\n"
        + json.dumps({"entities": prompt_payload}, ensure_ascii=False, sort_keys=True)
    )
    # Cap the request: this is a background labeling call that should finish in
    # seconds. The SDK default is 10 minutes, which would hold _CATALOG_LOCK and
    # block future refreshes if the API stalls.
    response = await client.with_options(timeout=45.0).messages.create(
        model=model,
        max_tokens=1200,
        system="You label Home Assistant entities safely and concisely.",
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_label_payload(_response_text(response))


def _parse_label_payload(text: str) -> list[dict]:
    """Parse the LLM label response, tolerating a markdown code fence.

    Despite the "return only JSON" instruction, models sometimes wrap output in
    ```json ... ```. Strip a leading/trailing fence before parsing. On any parse
    failure return [] (the caller keeps the existing catalog) with a warning,
    rather than raising a generic exception that would silently starve labels.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z0-9_-]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("HA label generation returned unparseable JSON; skipping this batch")
        return []
    labels = data.get("labels") if isinstance(data, dict) else data
    return labels if isinstance(labels, list) else []


def _resolve_anthropic_fast_model(config: StationConfig) -> str | None:
    """Resolve the Anthropic fast-role model id for label generation.

    Delegates to the single resolver (the `transition` task routes to the fast
    role), so env overrides (`CLAUDE_MODEL`), profile selection, and the floor
    logic stay consistent with the rest of the station instead of being
    re-derived here.
    """
    return resolve_model(config.models, "transition", "anthropic")


def _response_text(response: object) -> str:
    """Concatenate the text blocks of an Anthropic message response into one string."""
    parts = getattr(response, "content", None) or []
    chunks: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            chunks.append(text)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()
