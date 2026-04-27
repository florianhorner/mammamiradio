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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from mammamiradio.ad_creative import AdBrand, AdVoice, CampaignSpine
from mammamiradio.models import HostPersonality, PersonalityAxes
from mammamiradio.tts import _EDGE_DEFAULT_FALLBACK_VOICE, _looks_like_openai_voice
from mammamiradio.voice_catalog import is_known_edge_voice

load_dotenv()

_TRUTHY = {"true", "1", "yes"}
_FALSY = {"false", "0", "no"}


@dataclass
class StationSection:
    """Station identity and public stream metadata."""

    name: str = "Mamma Mi Radio"
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


@dataclass
class PacingSection:
    """Rules that control how often banter and ad breaks occur."""

    songs_between_banter: int = 2
    songs_between_ads: int = 4
    ad_spots_per_break: int = 1
    lookahead_segments: int = 3


@dataclass
class AudioSection:
    """Audio pipeline settings for encoding."""

    sample_rate: int = 48000
    channels: int = 2
    bitrate: int = 192
    claude_model: str = "claude-haiku-4-5-20251001"
    claude_creative_model: str = "claude-opus-4-6"


@dataclass
class HomeAssistantSection:
    """Optional Home Assistant integration used to seed prompt context."""

    enabled: bool = False
    url: str = ""
    poll_interval: int = 60  # seconds between state refreshes


@dataclass
class SonicBrandSection:
    """Station sonic identity: jingles, sweepers, and motif configuration."""

    tagline: str = ""
    geography: str = ""
    full_ident: str = ""
    short_sting: str = ""
    sweepers: list[str] = field(default_factory=list)
    motif_notes: list[int] = field(default_factory=lambda: [523, 659, 784, 1047])
    sweeper_voice: str = ""
    sweeper_probability: float = 0.25


@dataclass
class AdsSection:
    """Structured ad inventory, voices, and optional sound-effect assets."""

    brands: list[AdBrand] = field(default_factory=list)
    voices: list[AdVoice] = field(default_factory=list)
    sfx_dir: str = "sfx"


@dataclass
class PersonaSection:
    """Cross-session listener memory tuning."""

    arc_thresholds: list[int] = field(default_factory=lambda: [4, 11, 26])
    anthem_threshold: int = 3
    skip_bit_threshold: int = 2


# Volare Refined defaults — fall-back values when [brand] is missing or invalid.
# These match listener.css token defaults and DESIGN.md.
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

    station_name: str = "mammamiradio"
    frequency: str = ""
    city: str = ""
    founded: int = 0
    tagline: str = ""
    about: str = ""
    opengraph_subtitle: str = ""
    hosts: list[BrandHost] = field(default_factory=list)
    theme: BrandTheme = field(default_factory=BrandTheme)


@dataclass
class StationConfig:
    """Fully resolved application configuration used at runtime."""

    station: StationSection
    playlist: PlaylistSection
    pacing: PacingSection
    hosts: list[HostPersonality]
    ads: AdsSection
    sonic_brand: SonicBrandSection = field(default_factory=SonicBrandSection)
    audio: AudioSection = field(default_factory=AudioSection)
    homeassistant: HomeAssistantSection = field(default_factory=HomeAssistantSection)
    persona: PersonaSection = field(default_factory=PersonaSection)
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
    ha_token: str = ""
    is_addon: bool = False
    allow_ytdlp: bool = False
    # Names of hosts or ad voices that had their configured voice replaced
    # during config load because the configured ID wasn't valid for the chosen
    # backend. Empty when all voices passed validation.
    tts_degraded_voices: list[str] = field(default_factory=list)


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
    for host in config.hosts:
        host.engine = (host.engine or "edge").strip().lower()
        if host.engine not in {"edge", "openai"}:
            log.warning("Host '%s' has unknown engine '%s'; using edge", host.name, host.engine)
            host.engine = "edge"

        if host.engine == "openai" and not host.edge_fallback_voice:
            host.edge_fallback_voice = _EDGE_DEFAULT_FALLBACK_VOICE
            log.warning(
                "Host '%s' uses OpenAI TTS but has no edge_fallback_voice; defaulting to %s",
                host.name,
                host.edge_fallback_voice,
            )

        # Validate edge-engine hosts against the edge voice catalog.
        if host.engine == "edge":
            if _looks_like_openai_voice(host.voice):
                fallback = host.edge_fallback_voice or _EDGE_DEFAULT_FALLBACK_VOICE
                log.warning(
                    "Host '%s' is configured with OpenAI voice '%s' on edge engine; using fallback '%s'",
                    host.name,
                    host.voice,
                    fallback,
                )
                host.voice = fallback
                degraded.append(host.name)
            elif host.voice and not is_known_edge_voice(host.voice):
                fallback = host.edge_fallback_voice or _EDGE_DEFAULT_FALLBACK_VOICE
                log.warning(
                    "Host '%s' has unknown edge voice '%s'; using fallback '%s'",
                    host.name,
                    host.voice,
                    fallback,
                )
                host.voice = fallback
                degraded.append(host.name)
        elif host.engine == "openai" and host.voice and not _looks_like_openai_voice(host.voice):
            # engine=openai but voice isn't an OpenAI ID → runtime would fail.
            # Flip the host to edge using the fallback voice so synthesis works.
            fallback = host.edge_fallback_voice or _EDGE_DEFAULT_FALLBACK_VOICE
            log.warning(
                "Host '%s' has engine='openai' but non-OpenAI voice '%s'; switching to edge fallback '%s'",
                host.name,
                host.voice,
                fallback,
            )
            host.engine = "edge"
            host.voice = fallback
            degraded.append(host.name)

    for voice in config.ads.voices:
        if _looks_like_openai_voice(voice.voice):
            log.warning(
                "Ad voice '%s' uses OpenAI voice id '%s'; replacing with fallback '%s'",
                voice.name,
                voice.voice,
                _EDGE_DEFAULT_FALLBACK_VOICE,
            )
            voice.voice = _EDGE_DEFAULT_FALLBACK_VOICE
            degraded.append(voice.name)
        elif voice.voice and not is_known_edge_voice(voice.voice):
            log.warning(
                "Ad voice '%s' has unknown edge voice '%s'; replacing with fallback '%s'",
                voice.name,
                voice.voice,
                _EDGE_DEFAULT_FALLBACK_VOICE,
            )
            voice.voice = _EDGE_DEFAULT_FALLBACK_VOICE
            degraded.append(voice.name)

    config.tts_degraded_voices = degraded


def _is_loopback_host(host: str) -> bool:
    """Return whether a bind target should be treated as localhost-only."""
    if host in {"localhost", ""}:
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
    """Read /data/options.json and set env vars for addon secrets."""
    import json

    options_path = Path("/data/options.json")
    if not options_path.exists():
        return
    try:
        options = json.loads(options_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    env_map = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "admin_password": "ADMIN_PASSWORD",
    }
    for opt_key, env_key in env_map.items():
        val = options.get(opt_key, "")
        if val and not os.getenv(env_key):
            os.environ[env_key] = val


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
                station_name=raw.get("station", {}).get("name", "mammamiradio"),
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

    brand = BrandSection(
        station_name=brand_raw.get("station_name", raw.get("station", {}).get("name", "mammamiradio")),
        frequency=brand_raw.get("frequency", ""),
        city=brand_raw.get("city", ""),
        founded=int(brand_raw.get("founded", 0)),
        tagline=brand_raw.get("tagline", ""),
        about=brand_raw.get("about", ""),
        opengraph_subtitle=brand_raw.get("opengraph_subtitle", ""),
        hosts=brand_hosts,
        theme=theme,
    )
    return brand, warnings


def _validate(config: StationConfig) -> None:
    """Fail fast on bad config instead of cryptic runtime errors."""
    import logging

    log = logging.getLogger(__name__)
    errors = []

    if not config.hosts:
        errors.append("No hosts configured — banter requires at least one host")
    if config.pacing.songs_between_banter < 1:
        errors.append("pacing.songs_between_banter must be >= 1")
    if config.pacing.songs_between_ads < 1:
        errors.append("pacing.songs_between_ads must be >= 1")
    if config.pacing.lookahead_segments < 1:
        errors.append("pacing.lookahead_segments must be >= 1")
    if not isinstance(config.persona.anthem_threshold, int) or config.persona.anthem_threshold < 1:
        errors.append("persona.anthem_threshold must be >= 1")
    if not isinstance(config.persona.skip_bit_threshold, int) or config.persona.skip_bit_threshold < 1:
        errors.append("persona.skip_bit_threshold must be >= 1")
    if config.playlist.jamendo_client_id and not re.match(r"^[A-Za-z0-9_-]+$", config.playlist.jamendo_client_id):
        errors.append("playlist.jamendo_client_id must contain only letters, digits, hyphens, or underscores")

    if not (config.anthropic_api_key or config.openai_api_key):
        log.warning("No ANTHROPIC_API_KEY or OPENAI_API_KEY — banter/ads will use fallback text")
    if config.homeassistant.enabled and not config.ha_token:
        log.warning("Home Assistant enabled but no HA_TOKEN in environment")
    if not config.ads.brands:
        log.warning("No ad brands configured — ad segments will be skipped")
    # Non-loopback bind without auth is fine — admin access trusts private
    # networks (RFC1918, Tailscale CGNAT). Auth is only needed for public access.

    if errors:
        raise ValueError("Config errors:\n  " + "\n  ".join(errors))


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
    ha_section = HomeAssistantSection(**ha_raw)
    ha_token = os.getenv("HA_TOKEN", "")
    # Auto-enable HA if token is present and URL is set (Docker/add-on convenience)
    if ha_token and ha_section.url and not ha_section.enabled and not ha_force_disabled:
        ha_section.enabled = True
    if ha_section.enabled and not ha_token:
        import logging as _log

        _log.getLogger(__name__).warning("Home Assistant enabled but no HA_TOKEN in environment")

    # Env-var overrides for Docker/HA add-on: station identity and playlist
    if os.getenv("STATION_NAME"):
        station_raw["name"] = os.getenv("STATION_NAME")
    if os.getenv("STATION_THEME"):
        station_raw["theme"] = os.getenv("STATION_THEME")
    if os.getenv("CLAUDE_MODEL"):
        audio_raw["claude_model"] = os.getenv("CLAUDE_MODEL")
    if os.getenv("CLAUDE_CREATIVE_MODEL"):
        audio_raw["claude_creative_model"] = os.getenv("CLAUDE_CREATIVE_MODEL")
    playlist_raw = dict(raw.get("playlist", {}))
    if os.getenv("JAMENDO_CLIENT_ID") is not None:
        playlist_raw["jamendo_client_id"] = os.getenv("JAMENDO_CLIENT_ID", "").strip()

    # Env-var overrides for cache/tmp directories (for Docker volume mounts)
    cache_dir = Path(os.getenv("MAMMAMIRADIO_CACHE_DIR", "cache"))
    tmp_dir = Path(os.getenv("MAMMAMIRADIO_TMP_DIR", "tmp"))

    # Parse sonic brand section
    sonic_brand_raw = raw.get("sonic_brand", {})
    sonic_brand_sweepers = sonic_brand_raw.pop("sweepers", [])
    sonic_brand_motif = sonic_brand_raw.pop("motif_notes", [523, 659, 784, 1047])
    sonic_brand = SonicBrandSection(
        **sonic_brand_raw,
        sweepers=sonic_brand_sweepers,
        motif_notes=sonic_brand_motif,
    )

    # Parse brand-fiction layer (separate from operator-truth engine config).
    # Validation is graceful — invalid values fall back to Volare Refined defaults
    # and surface as brand_warnings for the operator (Engine Room panel).
    brand, brand_warnings = _parse_brand(raw, hosts)
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
        sonic_brand=sonic_brand,
        audio=AudioSection(**audio_raw),
        homeassistant=ha_section,
        persona=PersonaSection(**raw.get("persona", {})),
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
        ha_token=ha_token,
        is_addon=addon_mode,
        allow_ytdlp=os.getenv("MAMMAMIRADIO_ALLOW_YTDLP", "false").lower() in ("true", "1", "yes"),
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
    from mammamiradio.persona import set_arc_thresholds

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
        print("Usage: python -m mammamiradio.config runtime-json", file=sys.stderr)
        sys.exit(1)
