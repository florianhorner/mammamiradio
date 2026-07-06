"""Configuration loading for mammamiradio.

This module combines checked-in station settings from ``radio.toml`` with
environment-sourced secrets and deployment overrides, then validates the
result before the rest of the app boots.
"""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]
import ipaddress
import math
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from mammamiradio.audio.tts import _EDGE_DEFAULT_FALLBACK_VOICE, _looks_like_openai_voice
from mammamiradio.audio.voice_catalog import is_known_azure_voice, is_known_edge_voice
from mammamiradio.core.models import HostPersonality, PartyMode, PersonalityAxes
from mammamiradio.hosts.ad_creative import AdBrand, AdVoice, CampaignSpine

load_dotenv()

_TRUTHY = {"true", "1", "yes"}
_FALSY = {"false", "0", "no"}

# Canonical name of the local guest-host test balloon. Single source of truth —
# scriptwriter imports this so the roster gate and the prompt logic can never
# drift on the spelling. Disabled by dropping him from ``config.hosts`` at load
# (see MAMMAMIRADIO_GUEST_HOST below); every downstream consumer is then clean.
GUEST_HOST_NAME = "Hans Günther"

_ADDON_PROVIDER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    ("openai_api_key", "OPENAI_API_KEY"),
    ("azure_speech_key", "AZURE_SPEECH_KEY"),
    ("azure_speech_region", "AZURE_SPEECH_REGION"),
    ("elevenlabs_api_key", "ELEVENLABS_API_KEY"),
)
_ADDON_PROVIDER_ENV_KEYS = tuple(env_key for _, env_key in _ADDON_PROVIDER_OPTIONS)

PACING_BOUNDS: dict[str, tuple[int, int]] = {
    "songs_between_banter": (2, 60),
    "songs_between_ads": (1, 60),
    "ad_spots_per_break": (1, 5),
}

# Canonical user-facing station name — the single source of truth. Every
# user-visible surface (HA entities, FastAPI/OpenAPI title, clip sidecar, config
# fallbacks) references this so the name cannot drift the way "Radio MammaMia",
# "MammaMia", "Malamie", and lowercase "mammamiradio" once did. Technical
# identifiers (package name, env vars, entity IDs, slugs) stay "mammamiradio".
DEFAULT_STATION_NAME = "Mamma Mi Radio"
_MAX_STATION_NAME_LEN = 80

_DEFAULT_SONIC_TAGLINE = "Da Windor a Vergen, la voce che non si spegne mai!"
_DEFAULT_SONIC_GEOGRAPHY = "Windor, Vergen"
_DEFAULT_SONIC_FULL_IDENT = "Mamma Mi Radio... da Windor a Vergen, la voce che non si spegne mai!"
_DEFAULT_SONIC_SWEEPERS = (
    "Mamma Mi Radio.",
    "Windor, Vergen... e tutto il mondo che ascolta.",
    "Sei su Mamma Mi Radio.",
    "La radio che tua nonna non approverebbe.",
    "Da Windor a Vergen, nessuno è al sicuro.",
)


def coerce_bool(value: object, default: bool = False) -> bool:
    """Type-safe bool coercion that rejects truthy-string-of-falsy-word.

    `bool("false")` is `True` in Python; that's the bug this guards against.
    Accepts: real bool, int (0/1), or str matching _TRUTHY/_FALSY (case-insensitive).
    Anything else (including "false"-as-truthy-string in plain bool() context)
    falls back to ``default``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        return default
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUTHY:
            return True
        if v in _FALSY:
            return False
    return default


@dataclass
class StationSection:
    """Station identity and public stream metadata."""

    name: str = DEFAULT_STATION_NAME
    language: str = "it"
    theme: str = ""


@dataclass
class PlaylistSection:
    """Playlist source selection and ordering preferences."""

    shuffle: bool = True
    allow_explicit: bool = True
    repeat_cooldown: int = 5
    artist_cooldown: int = 3
    max_artist_per_hour: int = 3
    jamendo_client_id: str = ""
    jamendo_tags: str = "pop"
    jamendo_country: str = ""
    jamendo_order: str = ""
    jamendo_limit: int = 200


@dataclass
class ModerationSection:
    """Deterministic listener-request moderation knobs."""

    blocked_names: list[str] = field(default_factory=list)


@dataclass
class PacingSection:
    """Rules that control how often banter and ad breaks occur."""

    songs_between_banter: int = 2
    songs_between_ads: int = 4
    ad_spots_per_break: int = 2
    lookahead_segments: int = 4


@dataclass
class AudioSection:
    """Audio pipeline settings for encoding."""

    sample_rate: int = 48000
    channels: int = 2
    bitrate: int = 192
    # Integrated-LUFS targets for the loudness-reconciliation pass (measure +
    # corrective gain on each finished segment so music, dialogue, bedded banter
    # and ads all land at one perceived level). ad_lufs_target sits 1 LU hotter
    # so ads still pop, without the old jarring 2-LU jump.
    lufs_target: float = -16.0
    ad_lufs_target: float = -15.0
    # FM broadcast "transmitter" chain: when true, every aired non-rescue segment gets
    # one extra ffmpeg pass that colours it like an over-the-air FM signal (gentle
    # pre-emphasis HF shelf, ~15 kHz band-limit, flat loudness-offset trim — no stereo
    # swirl, no dynamics) so the station sounds like radio, not a clean studio file.
    # Default OFF (studio-clean): the colour is deliberately subtle and often
    # imperceptible on good speakers, and the "what should the station sound like"
    # strategy is being revisited. Set true to opt in.
    broadcast_chain: bool = False


# ── Dynamic LLM routing ───────────────────────────────────────────────────
# Script generation never names a model in code. Tasks ask for a ROLE; a
# per-provider catalog maps role→model; a quality profile selects which catalog
# entry each role resolves to. Swap any model by editing radio.toml [models]
# (or an env var) — no code change, no stale dropdown.
#
#   task (caller) ──routing──▶ role ──active_profile──▶ catalog_key ──catalog──▶ model_id
#
# DEFAULT_ROLE and DEFAULT_MODELS are the ONLY places a model identity lives in
# code, and only as the cold-start safety net: if [models] is missing or
# malformed the station still boots and airs on these (degrade, never die).
DEFAULT_ROLE = "creative"

# Built-in fallback catalog. `balanced` reproduces today's exact mapping
# (creative=opus for banter/news/ads, fast=haiku for transitions) so removing
# [models] from radio.toml is behavior-preserving. `fast` is pinned to the
# lowest-latency model in EVERY profile — transitions are the latency-sensitive
# glue between songs and must never risk dead air (leadership principle #2).
_DEFAULT_CATALOG: dict[str, dict[str, str]] = {
    "anthropic": {
        "opus": "claude-opus-4-8",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    },
    "openai": {
        "large": "gpt-5.5",
        "small": "gpt-5.4-mini",
    },
}
_DEFAULT_ROUTING: dict[str, str] = {
    "banter": "creative",
    "news_flash": "creative",
    "ad": "creative",
    "transition": "fast",
    "home_mood": "fast",
    "memory_extract": "fast",
}
_DEFAULT_PROFILES: dict[str, dict[str, dict[str, str]]] = {
    "premium": {
        "anthropic": {"creative": "opus", "fast": "haiku"},
        "openai": {"creative": "large", "fast": "small"},
    },
    "balanced": {
        "anthropic": {"creative": "opus", "fast": "haiku"},
        "openai": {"creative": "large", "fast": "small"},
    },
    "economy": {
        "anthropic": {"creative": "haiku", "fast": "haiku"},
        "openai": {"creative": "small", "fast": "small"},
    },
}


@dataclass
class ModelsSection:
    """Role-based model routing. All fields are plain data (dicts), so adding a
    model or a profile is a config edit, never a code change.

    catalog:  provider → catalog_key → model_id  (the only place model IDs live)
    routing:  task/caller → role
    profiles: profile → provider → role → catalog_key
    """

    catalog: dict[str, dict[str, str]] = field(default_factory=dict)
    routing: dict[str, str] = field(default_factory=dict)
    profiles: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    default_profile: str = "balanced"
    active_profile: str = "balanced"


def _build_default_models() -> ModelsSection:
    """Fresh ModelsSection backed by the built-in catalog (deep-copied so the
    module-level defaults can never be mutated by a running config)."""
    import copy

    return ModelsSection(
        catalog=copy.deepcopy(_DEFAULT_CATALOG),
        routing=copy.deepcopy(_DEFAULT_ROUTING),
        profiles=copy.deepcopy(_DEFAULT_PROFILES),
    )


def resolve_model(models: ModelsSection, caller: str | None, provider: str, profile: str | None = None) -> str:
    """Resolve which model voices `caller` on `provider`, right now.

    Total by construction — never raises, always returns a non-empty model ID:
      1. role  = routing[caller]  (DEFAULT_ROLE if the task isn't routed)
      2. key   = profiles[active|default][provider][role]
      3. floor = profiles[default_profile][provider][role]  (NEVER "first entry":
                 TOML ordering must not leak into production behavior)
      4. id    = catalog[provider][key]  → any catalog entry for the provider as
                 the last resort. `_validate` guarantees catalog[provider] is
                 non-empty for every API-keyed provider.

    A Python exception here would crash segment generation = dead air, so every
    lookup is defensive.
    """
    role = models.routing.get(caller or "", DEFAULT_ROLE)
    prof = profile or models.active_profile or models.default_profile

    def _key_for(profile_name: str) -> str | None:
        prov_map = models.profiles.get(profile_name, {}).get(provider, {})
        return prov_map.get(role) or prov_map.get(DEFAULT_ROLE)

    key = _key_for(prof) or _key_for(models.default_profile)
    provider_catalog = models.catalog.get(provider, {})
    if key and key in provider_catalog:
        return provider_catalog[key]
    # Floor (reached when a profile references a key absent from the catalog —
    # possible for a non-active profile that escaped _validate_models). Choose
    # deterministically: prefer a named low-cost key, else the lexicographically
    # first key. NEVER insertion order — TOML ordering must not leak into which
    # model airs.
    if provider_catalog:
        for _pref in ("haiku", "small"):
            if _pref in provider_catalog:
                return provider_catalog[_pref]
        return provider_catalog[min(provider_catalog)]
    # Last resort: provider catalog entirely empty (_validate_models prevents
    # this for API-keyed providers). Pin to a named built-in low-cost model.
    builtin = _DEFAULT_CATALOG.get(provider, {})
    return builtin.get("haiku") or builtin.get("small") or next(iter(builtin.values()), "claude-haiku-4-5-20251001")


def _parse_models_section(raw: dict) -> ModelsSection:
    """Build a ModelsSection from raw [models] TOML, degrading to the built-in
    catalog on a missing or malformed block (never raises — the station must
    boot and air even with a broken [models] edit)."""
    import logging as _log

    log = _log.getLogger(__name__)
    section = raw.get("models")
    if not section:
        # No [models] block (minimal/legacy radio.toml) → built-in defaults.
        return _build_default_models()
    try:
        catalog = section.get("catalog") or {}
        routing = section.get("routing") or {}
        profiles = section.get("profiles") or {}
        if not (isinstance(catalog, dict) and isinstance(routing, dict) and isinstance(profiles, dict)):
            raise ValueError("models.catalog/routing/profiles must be tables")
        if not catalog or not profiles:
            raise ValueError("models.catalog and models.profiles must be non-empty")
        default_profile = section.get("default_profile", "balanced")
        # Merge operator routing OVER the built-in defaults: a partial or empty
        # [models.routing] must not drop the transition→fast mapping, or
        # transitions would silently resolve to the creative (slow) model and
        # risk dead air between songs. Operator entries still win.
        merged_routing = {**_DEFAULT_ROUTING, **{str(t): str(r) for t, r in routing.items()}}
        return ModelsSection(
            catalog={str(p): {str(k): str(v) for k, v in m.items()} for p, m in catalog.items()},
            routing=merged_routing,
            profiles={
                str(pf): {str(pr): {str(role): str(key) for role, key in rm.items()} for pr, rm in provs.items()}
                for pf, provs in profiles.items()
            },
            default_profile=str(default_profile),
            active_profile=str(default_profile),
        )
    except Exception as exc:
        log.error(
            "Invalid [models] config (%s) — falling back to built-in DEFAULT_MODELS so the station still boots",
            exc,
        )
        return _build_default_models()


def _apply_model_env_overrides(models: ModelsSection) -> None:
    """Back-compat env overrides.

    - CLAUDE_CREATIVE_MODEL → anthropic creative-role model
    - CLAUDE_MODEL          → anthropic fast-role model
    - OPENAI_SCRIPT_MODEL   → every OpenAI catalog entry (one global OpenAI fallback model)

    Anthropic overrides get dedicated catalog keys instead of rewriting whatever
    key a profile currently points at. Economy maps creative and fast to the same
    `haiku` key; mutating that shared key would make balanced/premium
    transitions inherit a creative override and risk slow inter-song links.
    """
    creative_env = os.getenv("CLAUDE_CREATIVE_MODEL")
    fast_env = os.getenv("CLAUDE_MODEL")
    anth_catalog = models.catalog.setdefault("anthropic", {})
    if fast_env:
        anth_catalog["__env_fast"] = fast_env
        for prof_data in models.profiles.values():
            prof_data.setdefault("anthropic", {})["fast"] = "__env_fast"
    if creative_env:
        anth_catalog["__env_creative"] = creative_env
        for prof_data in models.profiles.values():
            prof_data.setdefault("anthropic", {})["creative"] = "__env_creative"
    openai_env = os.getenv("OPENAI_SCRIPT_MODEL")
    if openai_env:
        for key in models.catalog.get("openai", {}):
            models.catalog["openai"][key] = openai_env


def _validate_models(config: StationConfig) -> None:
    """Degrade-don't-die validation for [models]. Every routed role (plus the
    DEFAULT_ROLE floor) must resolve to a real catalog entry for each API-keyed
    provider under both the active and default profile. On any gap, log loud and
    fall back to the built-in DEFAULT_MODELS — a model misconfig must never take
    the station off air (leadership principle #1+#2)."""
    import logging

    log = logging.getLogger(__name__)
    providers = []
    if config.anthropic_api_key:
        providers.append("anthropic")
    if config.openai_api_key or os.getenv("OPENAI_API_KEY"):
        providers.append("openai")
    if not providers:
        return  # No LLM configured — stock copy only, nothing to resolve.

    m = config.models
    problems: list[str] = []
    if not m.catalog:
        problems.append("empty catalog")
    if m.active_profile not in m.profiles:
        problems.append(f"active_profile '{m.active_profile}' undefined")
    if m.default_profile not in m.profiles:
        problems.append(f"default_profile '{m.default_profile}' undefined")
    roles = set(m.routing.values()) | {DEFAULT_ROLE}
    for prof in {m.active_profile, m.default_profile}:
        for prov in providers:
            for role in roles:
                key = m.profiles.get(prof, {}).get(prov, {}).get(role)
                catalog_value = m.catalog.get(prov, {}).get(key) if key else None
                if not key or not catalog_value:
                    problems.append(f"profile '{prof}'/{prov}/role '{role}' unresolved")

    if problems:
        log.error(
            "Invalid [models] (%s) — falling back to built-in DEFAULT_MODELS so the station stays on air",
            "; ".join(problems[:6]),
        )
        prev_active = config.models.active_profile
        config.models = _build_default_models()
        if prev_active in config.models.profiles:
            config.models.active_profile = prev_active
        # Re-apply env overrides — the fresh defaults dropped them.
        _apply_model_env_overrides(config.models)


@dataclass
class TimerInterruptConfig:
    """A single HA timer entity that triggers an immediate host interrupt."""

    entity_id: str
    directive: str
    urgency: str = "pissed"  # "pissed" | "urgent" | "gentle"
    cooldown: int = 60  # seconds before this entity can fire again


@dataclass
class HomeAssistantSection:
    """Optional Home Assistant integration used to seed prompt context."""

    enabled: bool = False
    url: str = ""
    context_enabled: bool = True  # full /api/states prompt-context ingest
    poll_interval: int = 300  # seconds between full state refreshes
    timer_poll_interval: int = 5  # seconds between lightweight timer-entity state checks
    # Wall-clock budget (seconds) the producer gives a single HA context refresh
    # before it airs on last-known context instead of blocking segment production.
    # Audio continuity wins over HA freshness (INSTANT AUDIO). Steady-state value;
    # the one-time cold registry/weather warm-up gets a longer budget in the
    # producer (see _HA_CONTEXT_COLD_LOAD_TIMEOUT).
    context_refresh_timeout: float = 2.0
    # Experimental LLM scene-namer for the home mood. The heuristic ladder stays
    # the always-instant fallback and the default remains off.
    mood_llm_enabled: bool = False
    mood_ttl_seconds: float = 90.0
    timer_interrupts: list[TimerInterruptConfig] = field(default_factory=list)


@dataclass
class EveningGagsSection:
    """Operator overrides for evening running-gag candidacy (Impossible Moments).

    Maps to `[home.running_gags]` in radio.toml. All three lists are empty by
    default, which keeps the built-in domain-based candidacy (a `switch`/`fan`/
    `lock`/`vacuum`/`binary_sensor` toggle is gag-worthy on any home). Set
    `domain_allowlist` to replace the default domain set; `entity_allowlist` to
    restrict to specific entity_ids; `entity_denylist` to silence chatty entities.
    Resolved against the ledger in home/evening_memory.py.
    """

    domain_allowlist: list[str] = field(default_factory=list)
    entity_allowlist: list[str] = field(default_factory=list)
    entity_denylist: list[str] = field(default_factory=list)


@dataclass
class RadioEventRule:
    """Opt-in HA event promotion rule from ``[[home.radio_event]]``."""

    id: str
    label: str = ""
    mode: str = "directive"  # "directive" | "gag"
    entity_id: str = ""
    entity_glob: str = ""
    domain: str = ""
    device_class: str = ""
    trigger: str = "state"  # "state" | "attribute" | "numeric_threshold"
    from_state: str = ""
    to_state: str = ""
    attribute: str = ""
    from_value: str = ""
    to_value: str = ""
    threshold: float | None = None
    direction: str = "above"  # "above" | "below"
    cooldown_seconds: int = 900
    directive: str = ""


@dataclass
class SonicBrandSection:
    """Station sonic identity: jingles, sweepers, and motif configuration."""

    tagline: str = ""
    geography: str = ""
    full_ident: str = ""
    sweepers: list[str] = field(default_factory=list)
    motif_notes: list[int] = field(default_factory=lambda: [523, 659, 784, 1047])
    sweeper_voice: str = ""
    sweeper_engine: str = "edge"
    sweeper_edge_fallback_voice: str = ""


@dataclass
class AdsSection:
    """Structured ad inventory, voices, and optional sound-effect assets."""

    brands: list[AdBrand] = field(default_factory=list)
    voices: list[AdVoice] = field(default_factory=list)
    sfx_dir: str = "sfx"


@dataclass
class ImagingSection:
    """Station imaging controls for stingers and spoken-word beds."""

    bed_volume_db: float = -18.0
    use_music_queue_for_beds: bool = True
    # Asset directory is resolved from the package at runtime by ImagingLibrary.
    # Override with assets_dir (absolute path) if you need a custom location.
    assets_dir: str = ""


@dataclass
class PersonaSection:
    """Cross-session listener memory tuning."""

    arc_thresholds: list[int] = field(default_factory=lambda: [4, 11, 26])
    anthem_threshold: int = 3
    skip_bit_threshold: int = 2


# Volare Refined defaults — fall-back values when [brand] is missing or invalid.
# These match listener.css token defaults and docs/design/system.md.
_BRAND_DEFAULT_PRIMARY = "#F4D048"  # --sun
_BRAND_DEFAULT_ACCENT = "#B82C20"  # --lancia
_BRAND_DEFAULT_BG = "#14110F"  # --shadow
_BRAND_DEFAULT_TEXT = "#F5EDD8"  # --cream (used for contrast checks)
_BRAND_DEFAULT_DISPLAY_FONT = "Playfair Display"
_BRAND_DEFAULT_BODY_FONT = "Outfit"
_BRAND_DEFAULT_MONO_FONT = "JetBrains Mono"

# Curated font list per design D1 — operators may pick from these only.
_BRAND_DISPLAY_FONTS = frozenset(
    {
        "Playfair Display",
        "Cormorant Garamond",
        "Bodoni Moda",
        "Lora",
        "Outfit",
        "JetBrains Mono",
    }
)
_BRAND_BODY_FONTS = frozenset(
    {
        "Outfit",
        "Inter",
        "Source Sans 3",
        "IBM Plex Sans",
    }
)
_BRAND_MONO_FONTS = frozenset({"JetBrains Mono", "IBM Plex Mono"})
_TTS_ENGINES = {"edge", "openai", "azure", "elevenlabs"}
_CLOUD_TTS_ENGINES = {"openai", "azure", "elevenlabs"}


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int] | None:
    """Parse a #RRGGBB hex color into an (r, g, b) tuple. Returns None on invalid input."""
    s = (hex_color or "").strip().lstrip("#")
    if len(s) != 6 or not all(c in "0123456789abcdefABCDEF" for c in s):
        return None
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return None


def _hex_lightness(hex_color: str) -> float | None:
    """Return HSL lightness in 0-100 range, or None if hex is invalid."""
    rgb = _hex_to_rgb(hex_color)
    if rgb is None:
        return None
    r, g, b = (c / 255.0 for c in rgb)
    cmax, cmin = max(r, g, b), min(r, g, b)
    return (cmax + cmin) / 2.0 * 100.0


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.1 relative luminance from sRGB triplet."""

    def channel(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float | None:
    """WCAG contrast ratio between two hex colors. Returns None if either is invalid."""
    fg = _hex_to_rgb(fg_hex)
    bg = _hex_to_rgb(bg_hex)
    if fg is None or bg is None:
        return None
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


@dataclass
class BrandHost:
    """Per-host brand identity layer — what the LISTENER reads."""

    engine_host: str  # FK to HostPersonality.name
    display_name: str
    description: str = ""


@dataclass
class BrandTheme:
    """Per-station visual identity tokens. All optional — fall back to Volare Refined."""

    primary_color: str = _BRAND_DEFAULT_PRIMARY
    accent_color: str = _BRAND_DEFAULT_ACCENT
    background_color: str = _BRAND_DEFAULT_BG
    display_font: str = _BRAND_DEFAULT_DISPLAY_FONT
    body_font: str = _BRAND_DEFAULT_BODY_FONT
    mono_font: str = _BRAND_DEFAULT_MONO_FONT


@dataclass
class BrandSection:
    """The brand-fiction layer: what listeners see, separate from the engine config."""

    station_name: str = DEFAULT_STATION_NAME
    frequency: str = ""
    city: str = ""
    founded: int = 0
    tagline: str = ""
    about: str = ""
    opengraph_subtitle: str = ""
    # Absolute http(s) URL to the station logo, surfaced as the HA media_player
    # entity_picture fallback when a segment has no real cover (voice/ad/idle).
    # Blank → the engine's built-in default logo (see ha_context). HA resolves
    # entity_picture against its own origin, so a relative path is rejected here.
    artwork_url: str = ""
    hosts: list[BrandHost] = field(default_factory=list)
    theme: BrandTheme = field(default_factory=BrandTheme)


@dataclass
class IdentitySection:
    """Resolved station identity shared by every human-facing surface."""

    station_name: str = DEFAULT_STATION_NAME
    source: str = "default"
    custom_copy_preserved: bool = False
    generated: dict[str, Any] = field(default_factory=dict)


@dataclass
class StationConfig:
    """Fully resolved application configuration used at runtime."""

    station: StationSection
    playlist: PlaylistSection
    pacing: PacingSection
    hosts: list[HostPersonality]
    ads: AdsSection
    imaging: ImagingSection = field(default_factory=ImagingSection)
    sonic_brand: SonicBrandSection = field(default_factory=SonicBrandSection)
    audio: AudioSection = field(default_factory=AudioSection)
    models: ModelsSection = field(default_factory=_build_default_models)
    homeassistant: HomeAssistantSection = field(default_factory=HomeAssistantSection)
    running_gags: EveningGagsSection = field(default_factory=EveningGagsSection)
    radio_events: list[RadioEventRule] = field(default_factory=list)
    moderation: ModerationSection = field(default_factory=ModerationSection)
    persona: PersonaSection = field(default_factory=PersonaSection)
    identity: IdentitySection = field(default_factory=IdentitySection)
    brand: BrandSection = field(default_factory=BrandSection)
    brand_warnings: list[str] = field(default_factory=list)
    cache_dir: Path = Path("cache")
    tmp_dir: Path = Path("tmp")
    max_cache_size_mb: int = 500

    # Secrets from env
    bind_host: str = "127.0.0.1"
    port: int = 8000
    admin_username: str = "admin"
    admin_password: str = ""
    admin_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    elevenlabs_api_key: str = ""
    ha_token: str = ""
    is_addon: bool = False
    allow_ytdlp: bool = False
    super_italian_mode: bool = True
    party_mode: PartyMode | None = None
    # Provenance ledger (Show Memory): opt-in, off by default. Records how each
    # aired moment was made to a daily-rotated JSONL under cache_dir/ledger.
    ledger_enabled: bool = False
    ledger_retention_days: int = 14
    ledger_queue_max: int = 2000
    # Names of hosts or ad voices that had their configured voice replaced
    # during config load because the configured ID wasn't valid for the chosen
    # backend. Empty when all voices passed validation.
    tts_degraded_voices: list[str] = field(default_factory=list)

    @property
    def ledger_dir(self) -> Path:
        """Provenance ledger directory, derived from cache_dir (never hardcoded).

        Inherits the addon (/data/cache) vs standalone (./cache) vs /tmp fallback
        resolution that cache_dir already performs.
        """
        return self.cache_dir / "ledger"

    @property
    def display_station_name(self) -> str:
        """Compatibility alias for the resolved listener-facing station name."""
        return self.identity.station_name or DEFAULT_STATION_NAME


def _normalize_tts_voices(config: StationConfig) -> None:
    """Sanitize host/ad voice config before runtime to prevent avoidable TTS errors.

    Pre-flight validation: each voice is checked against the catalog for its
    backend. Invalid voices are logged once (WARNING) and substituted with a
    known-good default before any synthesis is attempted. Substitutions are
    recorded on config.tts_degraded_voices so capability reporting can
    surface a degraded-TTS state to the dashboard.
    """
    import logging

    log = logging.getLogger(__name__)
    degraded: list[str] = []

    def _clean_engine(engine: str, owner: str) -> str:
        result = (engine or "edge").strip().lower()
        if result not in _TTS_ENGINES:
            log.warning("Voice '%s' has unknown engine '%s'; using edge", owner, engine)
            return "edge"
        return result

    def _fallback(owner: str, configured: str = "") -> str:
        voice = (configured or "").strip() or _EDGE_DEFAULT_FALLBACK_VOICE
        if _looks_like_openai_voice(voice) or not is_known_edge_voice(voice):
            log.warning(
                "Voice '%s' has invalid edge fallback '%s'; using fallback '%s'",
                owner,
                voice,
                _EDGE_DEFAULT_FALLBACK_VOICE,
            )
            return _EDGE_DEFAULT_FALLBACK_VOICE
        return voice

    def _normalize_edge_voice(owner: str, voice: str, fallback_voice: str = "") -> str:
        if _looks_like_openai_voice(voice):
            fallback = _fallback(owner, fallback_voice)
            log.warning(
                "Voice '%s' is configured with OpenAI voice '%s' on edge engine; using fallback '%s'",
                owner,
                voice,
                fallback,
            )
            degraded.append(owner)
            return fallback
        if voice and not is_known_edge_voice(voice):
            fallback = _fallback(owner, fallback_voice)
            log.warning(
                "Voice '%s' has unknown edge voice '%s'; using fallback '%s'",
                owner,
                voice,
                fallback,
            )
            degraded.append(owner)
            return fallback
        return voice

    def _cloud_fallback(owner: str, engine: str, fallback_voice: str) -> str:
        fallback = _fallback(owner, fallback_voice)
        if not fallback_voice:
            log.warning(
                "Voice '%s' uses %s TTS but has no edge fallback voice; defaulting to %s",
                owner,
                engine,
                fallback,
            )
        return fallback

    for host in config.hosts:
        host.engine = _clean_engine(host.engine, host.name)

        if host.engine in _CLOUD_TTS_ENGINES:
            host.edge_fallback_voice = _cloud_fallback(host.name, host.engine, host.edge_fallback_voice)

        # Validate edge-engine hosts against the edge voice catalog.
        if host.engine == "edge":
            host.voice = _normalize_edge_voice(host.name, host.voice, host.edge_fallback_voice)
        elif host.engine == "openai" and not host.voice:
            fallback = _fallback(host.name, host.edge_fallback_voice)
            log.warning(
                "Host '%s' has engine='openai' but no voice ID; switching to edge fallback '%s'",
                host.name,
                fallback,
            )
            host.engine = "edge"
            host.voice = fallback
            degraded.append(host.name)
        elif host.engine == "openai" and not _looks_like_openai_voice(host.voice):
            # engine=openai but voice isn't an OpenAI ID → runtime would fail.
            # Flip the host to edge using the fallback voice so synthesis works.
            fallback = _fallback(host.name, host.edge_fallback_voice)
            log.warning(
                "Host '%s' has engine='openai' but non-OpenAI voice '%s'; switching to edge fallback '%s'",
                host.name,
                host.voice,
                fallback,
            )
            host.engine = "edge"
            host.voice = fallback
            degraded.append(host.name)
        elif host.engine == "azure" and not host.voice:
            fallback = _fallback(host.name, host.edge_fallback_voice)
            log.warning(
                "Host '%s' has engine='azure' but no voice; switching to edge fallback '%s'",
                host.name,
                fallback,
            )
            host.engine = "edge"
            host.voice = fallback
            degraded.append(host.name)
        elif host.engine == "azure" and host.voice.startswith("it-IT-") and not is_known_azure_voice(host.voice):
            log.info("Host '%s' uses Azure voice '%s' outside the curated local catalog", host.name, host.voice)
        elif host.engine == "elevenlabs" and not host.voice:
            fallback = _fallback(host.name, host.edge_fallback_voice)
            log.warning(
                "Host '%s' has engine='elevenlabs' but no voice ID; switching to edge fallback '%s'",
                host.name,
                fallback,
            )
            host.engine = "edge"
            host.voice = fallback
            degraded.append(host.name)

    for voice in config.ads.voices:
        voice.engine = _clean_engine(voice.engine, voice.name)
        if voice.engine in _CLOUD_TTS_ENGINES:
            voice.edge_fallback_voice = _cloud_fallback(voice.name, voice.engine, voice.edge_fallback_voice)

        if voice.engine == "edge":
            voice.voice = _normalize_edge_voice(voice.name, voice.voice, voice.edge_fallback_voice)
        elif voice.engine == "openai" and not voice.voice:
            fallback = _fallback(voice.name, voice.edge_fallback_voice)
            log.warning(
                "Ad voice '%s' has engine='openai' but no voice ID; switching to edge fallback '%s'",
                voice.name,
                fallback,
            )
            voice.engine = "edge"
            voice.voice = fallback
            degraded.append(voice.name)
        elif voice.engine == "openai" and not _looks_like_openai_voice(voice.voice):
            fallback = _fallback(voice.name, voice.edge_fallback_voice)
            log.warning(
                "Ad voice '%s' has engine='openai' but non-OpenAI voice '%s'; switching to edge fallback '%s'",
                voice.name,
                voice.voice,
                fallback,
            )
            voice.engine = "edge"
            voice.voice = fallback
            degraded.append(voice.name)
        elif voice.engine == "azure" and not voice.voice:
            fallback = _fallback(voice.name, voice.edge_fallback_voice)
            log.warning(
                "Ad voice '%s' has engine='azure' but no voice; switching to edge fallback '%s'",
                voice.name,
                fallback,
            )
            voice.engine = "edge"
            voice.voice = fallback
            degraded.append(voice.name)
        elif voice.engine == "azure" and voice.voice.startswith("it-IT-") and not is_known_azure_voice(voice.voice):
            log.info("Ad voice '%s' uses Azure voice '%s' outside the curated local catalog", voice.name, voice.voice)
        elif voice.engine == "elevenlabs" and not voice.voice:
            fallback = _fallback(voice.name, voice.edge_fallback_voice)
            log.warning(
                "Ad voice '%s' has engine='elevenlabs' but no voice ID; switching to edge fallback '%s'",
                voice.name,
                fallback,
            )
            voice.engine = "edge"
            voice.voice = fallback
            degraded.append(voice.name)

    sb = config.sonic_brand
    sb.sweeper_engine = _clean_engine(sb.sweeper_engine, "sonic_brand.sweeper_voice")
    if not sb.sweeper_voice and sb.sweeper_engine in _CLOUD_TTS_ENGINES:
        log.warning(
            "Sonic brand sweeper has engine='%s' but no voice; resetting to edge",
            sb.sweeper_engine,
        )
        sb.sweeper_engine = "edge"
    if sb.sweeper_voice:
        if sb.sweeper_engine in _CLOUD_TTS_ENGINES:
            sb.sweeper_edge_fallback_voice = _cloud_fallback(
                "sonic_brand.sweeper_voice",
                sb.sweeper_engine,
                sb.sweeper_edge_fallback_voice,
            )
        if sb.sweeper_engine == "edge":
            sb.sweeper_voice = _normalize_edge_voice(
                "sonic_brand.sweeper_voice",
                sb.sweeper_voice,
                sb.sweeper_edge_fallback_voice,
            )
        elif sb.sweeper_engine == "openai" and not _looks_like_openai_voice(sb.sweeper_voice):
            fallback = _fallback("sonic_brand.sweeper_voice", sb.sweeper_edge_fallback_voice)
            log.warning(
                "Sonic brand sweeper has engine='openai' but non-OpenAI voice '%s'; switching to edge fallback '%s'",
                sb.sweeper_voice,
                fallback,
            )
            sb.sweeper_engine = "edge"
            sb.sweeper_voice = fallback
            degraded.append("sonic_brand.sweeper_voice")
        elif (
            sb.sweeper_engine == "azure"
            and sb.sweeper_voice.startswith("it-IT-")
            and not is_known_azure_voice(sb.sweeper_voice)
        ):
            log.info("Sonic brand sweeper uses Azure voice '%s' outside the curated local catalog", sb.sweeper_voice)

    config.tts_degraded_voices = degraded


def _is_loopback_host(host: str) -> bool:
    """Return whether a bind target should be treated as localhost-only.

    An empty bind host is NOT loopback: ``socket.bind("")`` listens on all
    interfaces (equivalent to ``0.0.0.0``), so it must satisfy the same
    credential requirement as any other non-loopback bind.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_addon() -> bool:
    """Detect if running as a Home Assistant addon."""
    # Only trust Supervisor-provided tokens as addon signals.
    # /data/options.json may exist in non-addon environments (e.g. mounted test/dev paths).
    return bool(os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN"))


def _apply_addon_options() -> None:
    """Read add-on files and set env vars for add-on credentials."""
    import json

    options_path = Path("/data/options.json")
    options = {}
    if options_path.exists():
        try:
            options = json.loads(options_path.read_text())
        except (json.JSONDecodeError, OSError):
            options = {}

    provider_values = {}
    for opt_key, env_key in _ADDON_PROVIDER_OPTIONS:
        val = options.get(opt_key, "")
        if val:
            provider_values[env_key] = str(val)
    provider_values.update(_read_addon_provider_secrets(Path("/config/secrets.env")))
    for env_key, val in provider_values.items():
        if val and not os.getenv(env_key):
            os.environ[env_key] = val

    env_map = {
        "station_name": "STATION_NAME",
        "admin_password": "ADMIN_PASSWORD",
        "jamendo_client_id": "JAMENDO_CLIENT_ID",
    }
    for opt_key, env_key in env_map.items():
        val = options.get(opt_key, "")
        if val and not os.getenv(env_key):
            os.environ[env_key] = val

    si = options.get("super_italian_mode")
    if isinstance(si, bool) and not os.getenv("MAMMAMIRADIO_SUPER_ITALIAN"):
        os.environ["MAMMAMIRADIO_SUPER_ITALIAN"] = "true" if si else "false"

    fm = options.get("festival_mode")
    if isinstance(fm, bool) and not os.getenv("MAMMAMIRADIO_FESTIVAL_MODE"):
        os.environ["MAMMAMIRADIO_FESTIVAL_MODE"] = "true" if fm else "false"

    bc = options.get("broadcast_chain")
    if isinstance(bc, bool) and not os.getenv("MAMMAMIRADIO_BROADCAST_CHAIN"):
        os.environ["MAMMAMIRADIO_BROADCAST_CHAIN"] = "true" if bc else "false"

    qp = options.get("quality_profile")
    if isinstance(qp, str) and qp and not os.getenv("MAMMAMIRADIO_QUALITY"):
        os.environ["MAMMAMIRADIO_QUALITY"] = qp
    legacy_claude_model = options.get("claude_model") if not qp else None
    if isinstance(legacy_claude_model, str) and legacy_claude_model and not os.getenv("CLAUDE_MODEL"):
        os.environ["CLAUDE_MODEL"] = legacy_claude_model

    # Pacing (mirrors the toggles above): map persisted /data/options.json values
    # to env for the non-run.sh add-on boot path. run.sh normally exports these
    # first, and the `not os.getenv` guard keeps that export authoritative; the
    # load-time override loop clamps to range, so no clamp is needed here. bool is
    # excluded because it is an int subclass.
    for opt_key, env_key in (
        ("songs_between_banter", "MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER"),
        ("songs_between_ads", "MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS"),
        ("ad_spots_per_break", "MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK"),
    ):
        pv = options.get(opt_key)
        if isinstance(pv, int) and not isinstance(pv, bool) and not os.getenv(env_key):
            os.environ[env_key] = str(pv)


def _read_addon_provider_secrets(path: Path) -> dict[str, str]:
    """Parse /config/secrets.env without logging raw secret file contents."""
    if not path.exists():
        return {}

    import logging

    log = logging.getLogger(__name__)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        log.warning("Could not read /config/secrets.env")
        return {}

    values: dict[str, str] = {}
    for line_no, raw_line in enumerate(lines, 1):
        line = raw_line.lstrip("\ufeff") if line_no == 1 else raw_line
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            log.warning("Ignoring /config/secrets.env line %s: missing KEY=VALUE", line_no)
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if key not in _ADDON_PROVIDER_ENV_KEYS:
            log.warning("Ignoring /config/secrets.env line %s: unsupported key", line_no)
            continue
        value = raw_value.strip()
        if value[:1] in ('"', "'"):
            try:
                parts = shlex.split(value, comments=False, posix=True)
            except ValueError:
                log.warning("Ignoring /config/secrets.env line %s: invalid quoting", line_no)
                continue
            if len(parts) != 1:
                log.warning("Ignoring /config/secrets.env line %s: invalid quoted value", line_no)
                continue
            value = parts[0].strip()
        if value:
            values[key] = value
    return values


def is_absolute_http_url(value: str) -> bool:
    """True only for an absolute http(s) URL that has a host.

    Raise-safe by contract: a malformed value (e.g. an unterminated IPv6 literal
    like ``http://[::1`` makes ``urlsplit`` raise ``ValueError``, and a bad port
    makes ``.hostname`` raise) returns ``False`` rather than propagating. Both
    callers depend on this: the config loader must never fail boot, and the HA
    push must never raise into the audio path. Used for the ``[brand]
    artwork_url`` guardrail and the ``album_art`` cover check (``ha_context``).
    """
    try:
        parsed = urlsplit(value)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        return False


def _parse_brand(raw: dict, hosts: list[HostPersonality]) -> tuple[BrandSection, list[str]]:
    """Parse [brand] from radio.toml; apply guardrails per design D1.

    Returns (BrandSection, warnings_list). Never raises — degrades gracefully so
    INSTANT AUDIO leadership principle holds even when [brand] config is bad.
    """
    warnings: list[str] = []
    brand_raw = dict(raw.get("brand", {}))

    # If [brand] is missing entirely, derive defaults from existing config
    if not brand_raw:
        return (
            BrandSection(
                station_name=raw.get("station", {}).get("name", DEFAULT_STATION_NAME),
                hosts=[
                    BrandHost(engine_host=h.name, display_name=h.name, description=(h.style or "")[:160]) for h in hosts
                ],
            ),
            warnings,
        )

    # Pull nested blocks out before constructing the dataclass
    theme_raw = dict(brand_raw.pop("theme", {}))
    brand_hosts_raw = brand_raw.pop("hosts", [])

    # Theme guardrails per design D1
    theme = BrandTheme()
    for field_name in ("primary_color", "accent_color", "background_color"):
        if field_name in theme_raw:
            value = theme_raw[field_name]
            rgb = _hex_to_rgb(value)
            if rgb is None:
                warnings.append(f"brand.theme.{field_name}={value!r} is not a valid hex color; using Volare default")
                continue
            # Background-specific guardrails (Volare Refined invariants):
            # 1. Must be dark (lightness <= 25) — dark-canvas theme
            # 2. Body text (--cream) must achieve 4.5:1 contrast against this bg
            # primary_color and accent_color are decorative (not body text), so
            # they only need hex validity — no contrast check.
            if field_name == "background_color":
                lightness = _hex_lightness(value)
                if lightness is not None and lightness > 25:
                    warnings.append(
                        f"brand.theme.background_color={value!r} is too light "
                        f"(L={lightness:.0f} > 25); Volare Refined requires a dark canvas. Using default."
                    )
                    continue
                ratio = _contrast_ratio(_BRAND_DEFAULT_TEXT, value)
                if ratio is not None and ratio < 4.5:
                    warnings.append(
                        f"brand.theme.background_color={value!r} fails 4.5:1 contrast against "
                        f"--cream body text (got {ratio:.2f}:1); using Volare default for accessibility"
                    )
                    continue
            setattr(theme, field_name, value)
    # Font guardrails — must be from curated lists
    for font_field, allowed in (
        ("display_font", _BRAND_DISPLAY_FONTS),
        ("body_font", _BRAND_BODY_FONTS),
        ("mono_font", _BRAND_MONO_FONTS),
    ):
        if font_field in theme_raw:
            value = theme_raw[font_field]
            if value not in allowed:
                warnings.append(
                    f"brand.theme.{font_field}={value!r} is not in the approved list. "
                    f"Pick one of: {', '.join(sorted(allowed))}. Using default."
                )
                continue
            setattr(theme, font_field, value)

    # Brand hosts — every brand_host.engine_host must reference an existing [[hosts]].name
    valid_host_names = {h.name for h in hosts}
    brand_hosts: list[BrandHost] = []
    for bh in brand_hosts_raw:
        engine_host = bh.get("engine_host", "")
        if engine_host not in valid_host_names:
            warnings.append(
                f"brand.host.engine_host={engine_host!r} does not match any [[hosts]] "
                f"entry; dropping this brand host. Valid: {sorted(valid_host_names)}"
            )
            continue
        brand_hosts.append(
            BrandHost(
                engine_host=engine_host,
                display_name=bh.get("display_name", engine_host),
                description=bh.get("description", ""),
            )
        )
    # Auto-fill: every engine host SHOULD have a brand host
    covered = {bh.engine_host for bh in brand_hosts}
    for h in hosts:
        if h.name not in covered:
            brand_hosts.append(BrandHost(engine_host=h.name, display_name=h.name, description=(h.style or "")[:160]))

    # Validate founded year
    founded = brand_raw.get("founded", 0)
    if founded:
        try:
            year = int(founded)
        except (TypeError, ValueError):
            warnings.append(f"brand.founded={founded!r} is not a valid year; dropping field")
            brand_raw.pop("founded", None)
        else:
            from datetime import datetime as _dt

            current_year = _dt.now().year
            if year < 1900 or year > current_year + 1:
                warnings.append(f"brand.founded={year} is outside 1900..{current_year + 1}; dropping field")
                brand_raw.pop("founded", None)
            else:
                brand_raw["founded"] = year

    # Artwork URL guardrail: HA resolves entity_picture against its own origin,
    # so only an absolute http(s) URL with a host is usable. A relative, non-http,
    # scheme-only ("http://"), hostless-authority ("https://:443/logo.png"), or
    # malformed value would 404 (or be rejected) on the HA media card; warn and
    # fall back to the engine default (blank). is_absolute_http_url is raise-safe.
    artwork_url = str(brand_raw.get("artwork_url", "") or "").strip()
    if artwork_url and not is_absolute_http_url(artwork_url):
        warnings.append(
            f"brand.artwork_url={artwork_url!r} is not an absolute http(s) URL with a host; "
            "ignoring it and using the default station logo"
        )
        artwork_url = ""

    brand = BrandSection(
        station_name=brand_raw.get("station_name", raw.get("station", {}).get("name", DEFAULT_STATION_NAME)),
        frequency=brand_raw.get("frequency", ""),
        city=brand_raw.get("city", ""),
        founded=int(brand_raw.get("founded", 0)),
        tagline=brand_raw.get("tagline", ""),
        about=brand_raw.get("about", ""),
        opengraph_subtitle=brand_raw.get("opengraph_subtitle", ""),
        artwork_url=artwork_url,
        hosts=brand_hosts,
        theme=theme,
    )
    return brand, warnings


def _err(field: str, msg: str) -> str:
    """Format a config validation error with a hint about which TOML section to edit.

    >>> _err("pacing.ad_spots_per_break", "must be <= 5")
    'pacing.ad_spots_per_break must be <= 5 (set in radio.toml [pacing])'
    """
    section = field.split(".", 1)[0]
    return f"{field} {msg} (set in radio.toml [{section}])"


def _validate(config: StationConfig) -> None:
    """Fail fast on bad config instead of cryptic runtime errors."""
    import logging

    log = logging.getLogger(__name__)
    errors = []

    # Models degrade rather than fail boot — a model misconfig must never take
    # the station off air. Runs before the fail-fast checks below.
    _validate_models(config)

    if not config.hosts:
        errors.append("No hosts configured — banter requires at least one host (set in radio.toml [[hosts]])")
    # Bounds are shared with env-load clamping and PATCH /api/pacing so the
    # accepted range cannot drift between boot and live admin changes.
    for _pacing_attr, (_lo, _hi) in PACING_BOUNDS.items():
        _value = getattr(config.pacing, _pacing_attr)
        if _value < _lo:
            errors.append(_err(f"pacing.{_pacing_attr}", f"must be >= {_lo}"))
        if _value > _hi:
            errors.append(_err(f"pacing.{_pacing_attr}", f"must be <= {_hi}"))
    if config.pacing.lookahead_segments < 1:
        errors.append(_err("pacing.lookahead_segments", "must be >= 1"))
    if config.homeassistant.poll_interval < 1:
        errors.append(_err("homeassistant.poll_interval", "must be >= 1"))
    if config.homeassistant.timer_poll_interval < 1:
        errors.append(_err("homeassistant.timer_poll_interval", "must be >= 1"))
    _ctx_timeout = config.homeassistant.context_refresh_timeout
    if (
        isinstance(_ctx_timeout, bool)
        or not isinstance(_ctx_timeout, int | float)
        or not math.isfinite(_ctx_timeout)
        or _ctx_timeout <= 0
    ):
        errors.append(_err("homeassistant.context_refresh_timeout", "must be a positive number"))
    if not isinstance(config.homeassistant.mood_llm_enabled, bool):
        errors.append(_err("homeassistant.mood_llm_enabled", "must be true or false"))
    _mood_ttl = config.homeassistant.mood_ttl_seconds
    if (
        isinstance(_mood_ttl, bool)
        or not isinstance(_mood_ttl, int | float)
        or not math.isfinite(_mood_ttl)
        or _mood_ttl <= 0
    ):
        errors.append(_err("homeassistant.mood_ttl_seconds", "must be a positive number"))
    _allowed_urgencies = {"pissed", "urgent", "gentle"}
    for idx, timer_cfg in enumerate(config.homeassistant.timer_interrupts):
        if timer_cfg.cooldown < 1:
            errors.append(_err(f"homeassistant.timer_interrupt[{idx}].cooldown", "must be >= 1"))
        if timer_cfg.urgency not in _allowed_urgencies:
            errors.append(
                _err(f"homeassistant.timer_interrupt[{idx}].urgency", f"must be one of {sorted(_allowed_urgencies)}")
            )
    if not isinstance(config.persona.anthem_threshold, int) or config.persona.anthem_threshold < 1:
        errors.append(_err("persona.anthem_threshold", "must be >= 1"))
    if not isinstance(config.persona.skip_bit_threshold, int) or config.persona.skip_bit_threshold < 1:
        errors.append(_err("persona.skip_bit_threshold", "must be >= 1"))
    if config.playlist.jamendo_client_id:
        config.playlist.jamendo_client_id = config.playlist.jamendo_client_id.strip()
    if config.playlist.jamendo_client_id and not re.match(r"^[A-Za-z0-9_-]+$", config.playlist.jamendo_client_id):
        log.warning("Invalid jamendo_client_id format — Jamendo source disabled")
        config.playlist.jamendo_client_id = ""
    if config.playlist.jamendo_country and not re.match(r"^[A-Z]{3}$", config.playlist.jamendo_country):
        errors.append(
            _err(
                "playlist.jamendo_country",
                "must be a 3-letter uppercase ISO 3166-1 alpha-3 code (e.g. 'ITA', 'DEU', 'FRA') or empty",
            )
        )
    _valid_jamendo_orders = {
        "popularity_total",
        "popularity_month",
        "popularity_week",
        "releasedate_desc",
    }
    if config.playlist.jamendo_order and config.playlist.jamendo_order not in _valid_jamendo_orders:
        errors.append(_err("playlist.jamendo_order", f"must be one of {sorted(_valid_jamendo_orders)} or empty"))
    if not isinstance(config.playlist.jamendo_limit, int) or isinstance(config.playlist.jamendo_limit, bool):
        errors.append(_err("playlist.jamendo_limit", "must be an integer between 1 and 200"))
    elif not 1 <= config.playlist.jamendo_limit <= 200:
        errors.append(_err("playlist.jamendo_limit", "must be between 1 and 200"))

    if not (config.anthropic_api_key or config.openai_api_key):
        log.warning("No ANTHROPIC_API_KEY or OPENAI_API_KEY — banter/ads will use fallback text")
    if config.homeassistant.mood_llm_enabled and not config.anthropic_api_key:
        log.warning("Home Assistant mood LLM enabled but no ANTHROPIC_API_KEY — using heuristic home mood")
    if config.homeassistant.enabled and not config.ha_token:
        log.warning("Home Assistant enabled but no HA_TOKEN in environment")
    if not config.ads.brands:
        log.warning("No ad brands configured — ad segments will be skipped")
    if (
        not _is_loopback_host(config.bind_host)
        and not (config.admin_password or config.admin_token)
        and not config.is_addon
    ):
        errors.append("Set ADMIN_PASSWORD or ADMIN_TOKEN when binding to a non-loopback host")

    if errors:
        raise ValueError("Config errors:\n  " + "\n  ".join(errors))


def _env_positive_float(name: str) -> float | None:
    """Parse a positive finite float from an env var; warn and return None on invalid.

    Shared by the `[homeassistant]` numeric env overrides so the
    parse/validate/warn behavior can't drift between knobs.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    import logging

    log = logging.getLogger(__name__)
    try:
        value = float(raw)
    except ValueError:
        log.warning("Ignoring %s=%r (not a number)", name, raw)
        return None
    if math.isfinite(value) and value > 0:
        return value
    log.warning("Ignoring %s=%r (must be a finite number > 0)", name, raw)
    return None


def _env_positive_int(name: str) -> int | None:
    """Parse a positive integer from an env var; warn and return None on invalid."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    import logging

    log = logging.getLogger(__name__)
    try:
        value = int(raw)
    except ValueError:
        log.warning("Ignoring %s=%r (not an integer)", name, raw)
        return None
    if value > 0:
        return value
    log.warning("Ignoring %s=%r (must be > 0)", name, raw)
    return None


def _clean_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def sanitize_station_name(value: object) -> str:
    """Return a safe, bounded station name for human-facing surfaces."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_MAX_STATION_NAME_LEN].rstrip()


def _is_default_station_name(value: object) -> bool:
    return sanitize_station_name(value).casefold() == DEFAULT_STATION_NAME.casefold()


def _default_identity_pack(station_name: str, sonic_brand: SonicBrandSection | None = None) -> dict[str, Any]:
    """Deterministic station-copy pack derived from the chosen station name."""
    name = sanitize_station_name(station_name) or DEFAULT_STATION_NAME
    tagline = (
        sanitize_station_name(getattr(sonic_brand, "tagline", ""))
        if sonic_brand is not None
        else _DEFAULT_SONIC_TAGLINE
    ) or _DEFAULT_SONIC_TAGLINE
    geography = (
        sanitize_station_name(getattr(sonic_brand, "geography", ""))
        if sonic_brand is not None
        else _DEFAULT_SONIC_GEOGRAPHY
    ) or _DEFAULT_SONIC_GEOGRAPHY
    sweepers = [
        f"{name}.",
        f"{geography}... e tutto il mondo che ascolta.",
        f"Sei su {name}.",
        "La radio che tua nonna non approverebbe.",
        "Da Windor a Vergen, nessuno è al sicuro.",
    ]
    return {
        "spoken_ident": f"{name}... {tagline}",
        "sweepers": sweepers,
        "listener_title": name,
        "share_title": f"{name} - live radio",
        "home_assistant_name": name,
    }


def _resolve_identity(
    *,
    station_raw: dict,
    brand: BrandSection,
    sonic_brand: SonicBrandSection,
    addon_mode: bool,
    env_station_name: str,
    brand_warnings: list[str],
) -> IdentitySection:
    """Resolve one station identity after TOML, env, and add-on inputs are known."""
    station_raw_name = sanitize_station_name(station_raw.get("name"))
    brand_name = sanitize_station_name(brand.station_name)

    if env_station_name:
        station_name = env_station_name
        source = "ha_addon" if addon_mode else "env"
    elif station_raw_name and not _is_default_station_name(station_raw_name):
        station_name = station_raw_name
        source = "station"
    elif brand_name and not _is_default_station_name(brand_name):
        station_name = brand_name
        source = "brand"
    else:
        station_name = station_raw_name or brand_name or DEFAULT_STATION_NAME
        source = "default"

    station_name = sanitize_station_name(station_name) or DEFAULT_STATION_NAME
    station_raw["name"] = station_name
    pack = _default_identity_pack(station_name, sonic_brand)

    custom_copy_preserved = False
    if brand_name and not _is_default_station_name(brand_name) and brand_name != station_name:
        custom_copy_preserved = True
    brand.station_name = station_name

    full_ident = str(sonic_brand.full_ident or "")
    if not full_ident or full_ident == _DEFAULT_SONIC_FULL_IDENT:
        sonic_brand.full_ident = str(pack["spoken_ident"])
    elif station_name not in full_ident:
        custom_copy_preserved = True

    if not sonic_brand.sweepers or tuple(sonic_brand.sweepers) == _DEFAULT_SONIC_SWEEPERS:
        sonic_brand.sweepers = list(pack["sweepers"])
    elif not any(station_name in line for line in sonic_brand.sweepers):
        custom_copy_preserved = True

    if custom_copy_preserved:
        brand_warnings.append(
            "custom identity copy preserved; regenerate it deliberately if it should use the current station name"
        )

    return IdentitySection(
        station_name=station_name,
        source=source,
        custom_copy_preserved=custom_copy_preserved,
        generated=pack,
    )


def _parse_radio_event_rules(value: object) -> list[RadioEventRule]:
    """Parse ``[[home.radio_event]]`` rules.

    Bad rule blocks are ignored with a warning. This feature is opt-in and
    operator-facing, so a malformed custom event must not stop the station.
    """
    import logging

    log = logging.getLogger(__name__)
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        log.warning("Ignoring [home.radio_event]: expected an array of tables")
        return []

    rules: list[RadioEventRule] = []
    seen_ids: set[str] = set()
    for idx, raw_rule in enumerate(value):
        if not isinstance(raw_rule, dict):
            log.warning("Ignoring home.radio_event[%d]: expected a table", idx)
            continue
        rule_id = _clean_str(raw_rule.get("id"))
        if not rule_id:
            log.warning("Ignoring home.radio_event[%d]: id is required", idx)
            continue
        if rule_id in seen_ids:
            log.warning("Ignoring home.radio_event[%d]: duplicate id %r", idx, rule_id)
            continue

        mode = _clean_str(raw_rule.get("mode")) or "directive"
        trigger = _clean_str(raw_rule.get("trigger")) or "state"
        direction = _clean_str(raw_rule.get("direction")) or "above"
        if mode not in {"directive", "gag"}:
            log.warning("Ignoring home.radio_event[%d] %r: mode must be directive or gag", idx, rule_id)
            continue
        if trigger not in {"state", "attribute", "numeric_threshold"}:
            log.warning(
                "Ignoring home.radio_event[%d] %r: trigger must be state, attribute, or numeric_threshold",
                idx,
                rule_id,
            )
            continue
        if direction not in {"above", "below"}:
            log.warning("Ignoring home.radio_event[%d] %r: direction must be above or below", idx, rule_id)
            continue

        entity_id = _clean_str(raw_rule.get("entity_id"))
        entity_glob = _clean_str(raw_rule.get("entity_glob"))
        domain = _clean_str(raw_rule.get("domain"))
        device_class = _clean_str(raw_rule.get("device_class"))
        if not any((entity_id, entity_glob, domain)):
            log.warning("Ignoring home.radio_event[%d] %r: one selector is required", idx, rule_id)
            continue

        label = _clean_str(raw_rule.get("label"))
        directive = _clean_str(raw_rule.get("directive"))
        if mode == "directive" and not directive:
            log.warning("Ignoring home.radio_event[%d] %r: directive is required for directive mode", idx, rule_id)
            continue
        if mode == "gag" and not label:
            log.warning("Ignoring home.radio_event[%d] %r: label is required for gag mode", idx, rule_id)
            continue

        attribute = _clean_str(raw_rule.get("attribute"))
        if trigger == "attribute" and not attribute:
            log.warning("Ignoring home.radio_event[%d] %r: attribute is required", idx, rule_id)
            continue

        threshold: float | None = None
        if trigger == "numeric_threshold":
            raw_threshold = raw_rule.get("threshold")
            if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, int | float | str):
                log.warning("Ignoring home.radio_event[%d] %r: threshold must be numeric", idx, rule_id)
                continue
            try:
                threshold = float(raw_threshold)
            except (TypeError, ValueError):
                log.warning("Ignoring home.radio_event[%d] %r: threshold must be numeric", idx, rule_id)
                continue
            if not math.isfinite(threshold):
                log.warning("Ignoring home.radio_event[%d] %r: threshold must be finite", idx, rule_id)
                continue

        cooldown_raw = raw_rule.get("cooldown_seconds", 900)
        cooldown_seconds = 900
        if not isinstance(cooldown_raw, bool):
            try:
                parsed_cooldown = int(cooldown_raw)
            except (TypeError, ValueError):
                parsed_cooldown = 900
            if parsed_cooldown >= 1:
                cooldown_seconds = parsed_cooldown

        rules.append(
            RadioEventRule(
                id=rule_id,
                label=label,
                mode=mode,
                entity_id=entity_id,
                entity_glob=entity_glob,
                domain=domain,
                device_class=device_class,
                trigger=trigger,
                from_state=_clean_str(raw_rule.get("from_state")),
                to_state=_clean_str(raw_rule.get("to_state")),
                attribute=attribute,
                from_value=_clean_str(raw_rule.get("from_value")),
                to_value=_clean_str(raw_rule.get("to_value")),
                threshold=threshold,
                direction=direction,
                cooldown_seconds=cooldown_seconds,
                directive=directive,
            )
        )
        seen_ids.add(rule_id)
    return rules


def load_config(path: str = "radio.toml") -> StationConfig:
    """Load ``radio.toml`` plus environment overrides into a validated config."""
    addon_mode = _is_addon()
    if addon_mode:
        _apply_addon_options()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    hosts = [
        HostPersonality(
            name=h["name"],
            voice=h["voice"],
            style=h["style"],
            personality=PersonalityAxes.from_dict(h.get("personality", {})),
            engine=h.get("engine", "edge"),
            edge_fallback_voice=h.get("edge_fallback_voice", ""),
            voice_settings=dict(h.get("voice_settings", {})),
        )
        for h in raw.get("hosts", [])
    ]

    # Parse ads section with structured brands and voices
    ads_raw = raw.get("ads", {})
    if "brand_pool" in ads_raw:
        # Backward compat: convert flat string list to AdBrand objects
        brands = [AdBrand(name=s, tagline="", category="general") for s in ads_raw["brand_pool"]]
        voices = []
        sfx_dir = ads_raw.get("sfx_dir", "sfx")
    else:
        brands = []
        for b in ads_raw.get("brands", []):
            campaign_raw = b.get("campaign")
            campaign = None
            if campaign_raw and isinstance(campaign_raw, dict):
                campaign = CampaignSpine(
                    premise=campaign_raw.get("premise", ""),
                    sonic_signature=campaign_raw.get("sonic_signature", ""),
                    format_pool=campaign_raw.get("format_pool", []),
                    spokesperson=campaign_raw.get("spokesperson", ""),
                    escalation_rule=campaign_raw.get("escalation_rule", ""),
                )
            brands.append(
                AdBrand(
                    name=b["name"],
                    tagline=b.get("tagline", ""),
                    category=b.get("category", "general"),
                    recurring=b.get("recurring", True),
                    campaign=campaign,
                )
            )
        voices = [
            AdVoice(
                name=v["name"],
                voice=v["voice"],
                style=v.get("style", ""),
                role=v.get("role", ""),
                engine=v.get("engine", "edge"),
                edge_fallback_voice=v.get("edge_fallback_voice", ""),
            )
            for v in ads_raw.get("voices", [])
        ]
        sfx_dir = ads_raw.get("sfx_dir", "sfx")

    # Legacy: station.bitrate → audio.bitrate migration
    station_raw = dict(raw.get("station", {}))
    audio_raw = dict(raw.get("audio", {}))
    if "bitrate" in station_raw:
        import logging as _log

        _log.getLogger(__name__).warning("station.bitrate is deprecated — use audio.bitrate instead")
        # pop() cleans station_raw so StationSection(**station_raw) won't get an unexpected kwarg
        if "bitrate" not in audio_raw:
            audio_raw["bitrate"] = station_raw.pop("bitrate")
        else:
            station_raw.pop("bitrate")

    # Legacy: model IDs moved from [audio] to [models]. An upgraded standalone
    # radio.toml may still carry claude_model / claude_creative_model /
    # openai_script_model in [audio]; drop them so AudioSection(**audio_raw) does
    # not raise TypeError and refuse to boot. Model selection now lives in
    # [models] (or the built-in defaults); the matching env vars still override
    # the catalog. Leadership principle #2: the station must always boot.
    _legacy_audio_model_keys = [
        k for k in ("claude_model", "claude_creative_model", "openai_script_model") if k in audio_raw
    ]
    if _legacy_audio_model_keys:
        import logging as _log

        _log.getLogger(__name__).warning(
            "Ignoring deprecated [audio] keys %s — model selection now lives in [models] "
            "(see CLAUDE.md). Remove them from radio.toml.",
            _legacy_audio_model_keys,
        )
        for _k in _legacy_audio_model_keys:
            audio_raw.pop(_k, None)

    # Env override for the FM broadcast chain (HA add-on `broadcast_chain` option →
    # MAMMAMIRADIO_BROADCAST_CHAIN via run.sh) so addon operators can toggle on-air
    # colouring without rebuilding the baked-in radio.toml. env > toml.
    _bc_env = os.getenv("MAMMAMIRADIO_BROADCAST_CHAIN", "").strip().lower()
    if _bc_env in _TRUTHY:
        audio_raw["broadcast_chain"] = True
    elif _bc_env in _FALSY:
        audio_raw["broadcast_chain"] = False

    ha_raw = raw.get("homeassistant", {})
    # Env-var overrides for HA add-on: HA_URL and HA_ENABLED
    if os.getenv("HA_URL"):
        ha_raw["url"] = os.getenv("HA_URL")
    ha_enabled_env = os.getenv("HA_ENABLED", "").strip().lower()
    ha_force_disabled = ha_enabled_env in _FALSY
    if ha_enabled_env in _TRUTHY:
        ha_raw["enabled"] = True
    elif ha_force_disabled:
        ha_raw["enabled"] = False
    _ha_context_env = os.getenv("MAMMAMIRADIO_HA_CONTEXT_ENABLED", "").strip().lower()
    if _ha_context_env in _TRUTHY:
        ha_raw["context_enabled"] = True
    elif _ha_context_env in _FALSY:
        ha_raw["context_enabled"] = False
    elif _ha_context_env:
        import logging as _ha_context_logging

        _ha_context_logging.getLogger(__name__).warning(
            "Ignoring MAMMAMIRADIO_HA_CONTEXT_ENABLED=%r (use true/1/yes or false/0/no)",
            _ha_context_env,
        )
    _ha_mood_llm_env = os.getenv("MAMMAMIRADIO_HA_MOOD_LLM", "").strip().lower()
    if _ha_mood_llm_env in _TRUTHY:
        ha_raw["mood_llm_enabled"] = True
    elif _ha_mood_llm_env in _FALSY:
        ha_raw["mood_llm_enabled"] = False
    elif _ha_mood_llm_env:
        # A typo ("ture") must not silently leave the experiment off while the
        # operator believes it is on.
        import logging as _mood_logging

        _mood_logging.getLogger(__name__).warning(
            "Ignoring MAMMAMIRADIO_HA_MOOD_LLM=%r (use true/1/yes or false/0/no)",
            _ha_mood_llm_env,
        )
    # Env overrides for positive-float HA knobs. Reject non-float / non-positive
    # values (keep the toml/default) rather than letting a typo disable them.
    _ctx_override = _env_positive_float("MAMMAMIRADIO_HA_CONTEXT_REFRESH_TIMEOUT")
    if _ctx_override is not None:
        ha_raw["context_refresh_timeout"] = _ctx_override
    _poll_override = _env_positive_int("MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL")
    if _poll_override is not None:
        ha_raw["poll_interval"] = _poll_override
    _mood_ttl_override = _env_positive_float("MAMMAMIRADIO_HA_MOOD_TTL_SECONDS")
    if _mood_ttl_override is not None:
        ha_raw["mood_ttl_seconds"] = _mood_ttl_override
    # Parse [[ha.timer_interrupt]] blocks — extracted before ** expansion
    timer_interrupts_raw = ha_raw.pop("timer_interrupt", [])
    timer_interrupts = [
        TimerInterruptConfig(
            entity_id=t["entity_id"],
            directive=t["directive"],
            urgency=t.get("urgency", "pissed"),
            cooldown=int(t.get("cooldown", 60)),
        )
        for t in timer_interrupts_raw
        if isinstance(t, dict) and t.get("entity_id") and t.get("directive")
    ]
    ha_section = HomeAssistantSection(**ha_raw)
    ha_section.timer_interrupts = timer_interrupts

    # Evening running-gag candidacy overrides ([home.running_gags]). Degrade to
    # built-in domain-based defaults on any malformed value — never raise (a bad
    # operator config must not stop the station booting).
    def _str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]

    home_raw = raw.get("home", {})
    radio_events_raw = home_raw.get("radio_event", []) if isinstance(home_raw, dict) else []
    radio_events = _parse_radio_event_rules(radio_events_raw)
    gags_raw = home_raw.get("running_gags", {}) if isinstance(home_raw, dict) else {}
    if not isinstance(gags_raw, dict):
        gags_raw = {}
    running_gags = EveningGagsSection(
        domain_allowlist=_str_list(gags_raw.get("domain_allowlist")),
        entity_allowlist=_str_list(gags_raw.get("entity_allowlist")),
        entity_denylist=_str_list(gags_raw.get("entity_denylist")),
    )
    moderation_raw = raw.get("moderation", {})
    if not isinstance(moderation_raw, dict):
        moderation_raw = {}
    moderation = ModerationSection(blocked_names=_str_list(moderation_raw.get("blocked_names")))
    ha_token = os.getenv("HA_TOKEN", "")
    # Auto-enable HA if token is present and URL is set (Docker/add-on convenience)
    if ha_token and ha_section.url and not ha_section.enabled and not ha_force_disabled:
        ha_section.enabled = True
    if ha_section.enabled and not ha_token:
        import logging as _log

        _log.getLogger(__name__).warning("Home Assistant enabled but no HA_TOKEN in environment")

    # Env-var overrides for Docker/HA add-on: station identity and playlist
    env_station_name = sanitize_station_name(os.getenv("STATION_NAME", ""))
    if env_station_name:
        station_raw["name"] = env_station_name
    if os.getenv("STATION_THEME"):
        station_raw["theme"] = os.getenv("STATION_THEME")
    # Dynamic LLM routing: model IDs live in [models], never in code. Parse the
    # catalog/routing/profiles (degrade to built-in DEFAULT_MODELS on malformed
    # config so the station always boots), then apply back-compat env overrides.
    models_section = _parse_models_section(raw)
    _apply_model_env_overrides(models_section)
    playlist_raw = dict(raw.get("playlist", {}))
    if os.getenv("JAMENDO_CLIENT_ID") is not None:
        playlist_raw["jamendo_client_id"] = os.getenv("JAMENDO_CLIENT_ID", "").strip()
    if os.getenv("JAMENDO_COUNTRY") is not None:
        playlist_raw["jamendo_country"] = os.getenv("JAMENDO_COUNTRY", "").strip()
    if os.getenv("JAMENDO_ORDER") is not None:
        playlist_raw["jamendo_order"] = os.getenv("JAMENDO_ORDER", "").strip()
    jamendo_limit_env = os.getenv("JAMENDO_LIMIT")
    if jamendo_limit_env is not None and jamendo_limit_env.strip():
        try:
            playlist_raw["jamendo_limit"] = int(jamendo_limit_env.strip())
        except ValueError:
            playlist_raw["jamendo_limit"] = jamendo_limit_env.strip()

    # Env-var overrides for cache/tmp directories (for Docker volume mounts)
    cache_dir = Path(os.getenv("MAMMAMIRADIO_CACHE_DIR", "cache"))
    tmp_dir = Path(os.getenv("MAMMAMIRADIO_TMP_DIR", "tmp"))

    # Parse sonic brand section
    sonic_brand_raw = raw.get("sonic_brand", {})
    sonic_brand_sweepers = sonic_brand_raw.pop("sweepers", [])
    sonic_brand_motif = sonic_brand_raw.pop("motif_notes", [523, 659, 784, 1047])
    # Tolerate legacy keys from older operator radio.toml files.
    sonic_brand_raw.pop("short_sting", None)
    sonic_brand_raw.pop("sweeper_probability", None)
    sonic_brand = SonicBrandSection(
        **sonic_brand_raw,
        sweepers=sonic_brand_sweepers,
        motif_notes=sonic_brand_motif,
    )

    # Parse brand-fiction layer (separate from operator-truth engine config).
    # Validation is graceful — invalid values fall back to Volare Refined defaults
    # and surface as brand_warnings for the operator (Engine Room panel).
    brand, brand_warnings = _parse_brand(raw, hosts)
    identity = _resolve_identity(
        station_raw=station_raw,
        brand=brand,
        sonic_brand=sonic_brand,
        addon_mode=addon_mode,
        env_station_name=env_station_name,
        brand_warnings=brand_warnings,
    )
    if brand_warnings:
        import logging as _log

        log = _log.getLogger(__name__)
        for w in brand_warnings:
            log.warning("brand: %s", w)

    config = StationConfig(
        station=StationSection(**station_raw),
        playlist=PlaylistSection(**playlist_raw),
        pacing=PacingSection(**raw.get("pacing", {})),
        hosts=hosts,
        ads=AdsSection(brands=brands, voices=voices, sfx_dir=sfx_dir),
        imaging=ImagingSection(**raw.get("imaging", {})),
        sonic_brand=sonic_brand,
        audio=AudioSection(**audio_raw),
        models=models_section,
        homeassistant=ha_section,
        running_gags=running_gags,
        radio_events=radio_events,
        moderation=moderation,
        persona=PersonaSection(**raw.get("persona", {})),
        identity=identity,
        brand=brand,
        brand_warnings=brand_warnings,
        cache_dir=cache_dir,
        tmp_dir=tmp_dir,
        max_cache_size_mb=int(os.getenv("MAMMAMIRADIO_MAX_CACHE_MB", "500")),
        bind_host=os.getenv("MAMMAMIRADIO_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("MAMMAMIRADIO_PORT", "8000")),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        azure_speech_key=os.getenv("AZURE_SPEECH_KEY", ""),
        azure_speech_region=os.getenv("AZURE_SPEECH_REGION", ""),
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        ha_token=ha_token,
        is_addon=addon_mode,
        allow_ytdlp=os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes"),
        super_italian_mode=coerce_bool(raw.get("super_italian_mode", True)),
    )

    _super_italian_env = os.getenv("MAMMAMIRADIO_SUPER_ITALIAN", "").strip().lower()
    if _super_italian_env in _TRUTHY:
        config.super_italian_mode = True
    elif _super_italian_env in _FALSY:
        config.super_italian_mode = False

    _festival_env = os.getenv("MAMMAMIRADIO_FESTIVAL_MODE", "").strip().lower()
    if _festival_env in _TRUTHY:
        config.party_mode = "festival"
    elif _festival_env in _FALSY:
        config.party_mode = None

    # Guest-host off switch. Default ON (he stays on the roster). An explicit
    # falsy value drops him from config.hosts before anything reads the roster,
    # so the prompt, the system-prompt cache key, and voice validation are all
    # clean with no per-call gating.
    _guest_host_env = os.getenv("MAMMAMIRADIO_GUEST_HOST", "").strip().lower()
    if _guest_host_env in _FALSY:
        config.hosts = [h for h in config.hosts if h.name != GUEST_HOST_NAME]
        config.brand.hosts = [h for h in config.brand.hosts if h.engine_host != GUEST_HOST_NAME]

    _ledger_env = os.getenv("MAMMAMIRADIO_LEDGER_ENABLED", "").strip().lower()
    if _ledger_env in _TRUTHY:
        config.ledger_enabled = True
    elif _ledger_env in _FALSY:
        config.ledger_enabled = False
    _ledger_retention = os.getenv("MAMMAMIRADIO_LEDGER_RETENTION_DAYS", "").strip()
    if _ledger_retention.isdigit() and int(_ledger_retention) > 0:
        config.ledger_retention_days = int(_ledger_retention)

    # Env overrides for pacing (HA addon pacing options -> MAMMAMIRADIO_PACING_*
    # via run.sh; also the admin slider persistence path writing standalone .env).
    # Values are clamped to the same bounds as _validate()/PATCH /api/pacing so a
    # stale or hand-edited env can never brick boot — audio continuity wins over a
    # strict reject (INSTANT AUDIO).
    for _pacing_env, _pacing_attr in (
        ("MAMMAMIRADIO_PACING_SONGS_BETWEEN_BANTER", "songs_between_banter"),
        ("MAMMAMIRADIO_PACING_SONGS_BETWEEN_ADS", "songs_between_ads"),
        ("MAMMAMIRADIO_PACING_AD_SPOTS_PER_BREAK", "ad_spots_per_break"),
    ):
        _pacing_raw = os.getenv(_pacing_env, "").strip()
        if not _pacing_raw:
            continue
        try:
            _pacing_val = int(_pacing_raw)
        except ValueError:
            continue
        _lo, _hi = PACING_BOUNDS[_pacing_attr]
        setattr(config.pacing, _pacing_attr, max(_lo, min(_hi, _pacing_val)))

    # Quality dial: pick the active model profile (premium|balanced|economy).
    # Mirrors the MAMMAMIRADIO_SUPER_ITALIAN env pattern; the HA addon maps its
    # quality_profile option to this var via run.sh.
    _quality_env = os.getenv("MAMMAMIRADIO_QUALITY", "").strip().lower()
    if _quality_env:
        if _quality_env in config.models.profiles:
            config.models.active_profile = _quality_env
        else:
            import logging as _log

            _log.getLogger(__name__).warning(
                "MAMMAMIRADIO_QUALITY=%s is not a defined profile (%s) — keeping %s",
                _quality_env,
                sorted(config.models.profiles),
                config.models.active_profile,
            )

    # Addon overrides: persistent paths, auto-enable HA
    if addon_mode:
        import logging as _log

        _log.getLogger(__name__).info("Running as Home Assistant addon")
        config.cache_dir = Path(os.getenv("MAMMAMIRADIO_CACHE_DIR", "/data/cache"))
        config.tmp_dir = Path(os.getenv("MAMMAMIRADIO_TMP_DIR", "/data/tmp"))
        # Auto-enable HA context via Supervisor API unless explicitly disabled.
        supervisor_token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN", "")
        if supervisor_token and not ha_force_disabled:
            config.homeassistant.enabled = True
            config.homeassistant.url = "http://supervisor/core"
            config.ha_token = supervisor_token
        elif ha_force_disabled:
            config.homeassistant.enabled = False
            config.ha_token = ""

    _normalize_tts_voices(config)
    _validate(config)
    from mammamiradio.hosts.persona import set_arc_thresholds

    set_arc_thresholds(config.persona.arc_thresholds)
    return config


def runtime_json(config: StationConfig | None = None) -> dict:
    """Return resolved runtime settings for shell consumers."""
    if config is None:
        config = load_config()
    return {
        "bind_host": config.bind_host,
        "port": config.port,
        "tmp_dir": str(config.tmp_dir),
    }


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "runtime-json":
        print(json.dumps(runtime_json()))
    else:
        print("Usage: python -m mammamiradio.core.config runtime-json", file=sys.stderr)
        sys.exit(1)
