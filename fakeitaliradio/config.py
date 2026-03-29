from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
import os

from fakeitaliradio.models import AdBrand, AdVoice, HostPersonality

load_dotenv()


@dataclass
class StationSection:
    name: str = "Radio Italì"
    language: str = "it"
    theme: str = ""
    bitrate: int = 192


@dataclass
class PlaylistSection:
    spotify_url: str = ""
    shuffle: bool = False


@dataclass
class PacingSection:
    songs_between_banter: int = 2
    songs_between_ads: int = 4
    ad_spots_per_break: int = 1
    lookahead_segments: int = 3


@dataclass
class AudioSection:
    sample_rate: int = 48000
    channels: int = 2
    bitrate: int = 192
    spotify_bitrate: int = 320
    fifo_path: str = "/tmp/fakeitaliradio.pcm"
    go_librespot_bin: str = "/opt/homebrew/opt/go-librespot/bin/go-librespot"
    go_librespot_port: int = 3678
    claude_model: str = "claude-haiku-4-5-20251001"


@dataclass
class HomeAssistantSection:
    enabled: bool = False
    url: str = ""
    poll_interval: int = 60  # seconds between state refreshes


@dataclass
class AdsSection:
    brands: list[AdBrand] = field(default_factory=list)
    voices: list[AdVoice] = field(default_factory=list)
    sfx_dir: str = "sfx"


@dataclass
class StationConfig:
    station: StationSection
    playlist: PlaylistSection
    pacing: PacingSection
    hosts: list[HostPersonality]
    ads: AdsSection
    audio: AudioSection = field(default_factory=AudioSection)
    homeassistant: HomeAssistantSection = field(default_factory=HomeAssistantSection)
    cache_dir: Path = Path("cache")
    tmp_dir: Path = Path("tmp")

    # Secrets from env
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    anthropic_api_key: str = ""
    ha_token: str = ""


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

    if not config.anthropic_api_key:
        log.warning("No ANTHROPIC_API_KEY — banter/ads will use fallback text")
    if not config.ads.brands:
        log.warning("No ad brands configured — ad segments will be skipped")
    if not config.spotify_client_id or not config.spotify_client_secret:
        log.warning("No Spotify credentials — using demo playlist")

    if errors:
        raise ValueError("Config errors:\n  " + "\n  ".join(errors))


def load_config(path: str = "radio.toml") -> StationConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    hosts = [
        HostPersonality(
            name=h["name"],
            voice=h["voice"],
            style=h["style"],
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
        brands = [
            AdBrand(
                name=b["name"],
                tagline=b.get("tagline", ""),
                category=b.get("category", "general"),
                recurring=b.get("recurring", True),
            )
            for b in ads_raw.get("brands", [])
        ]
        voices = [
            AdVoice(
                name=v["name"],
                voice=v["voice"],
                style=v.get("style", ""),
            )
            for v in ads_raw.get("voices", [])
        ]
        sfx_dir = ads_raw.get("sfx_dir", "sfx")

    # Parse homeassistant section
    ha_raw = raw.get("homeassistant", {})
    ha_section = HomeAssistantSection(**ha_raw)
    # Token always from env for security
    ha_token = os.getenv("HA_TOKEN", "")
    if ha_section.enabled and not ha_token:
        import logging
        logging.getLogger(__name__).warning(
            "Home Assistant enabled but no HA_TOKEN in environment"
        )

    config = StationConfig(
        station=StationSection(**raw.get("station", {})),
        playlist=PlaylistSection(**raw.get("playlist", {})),
        pacing=PacingSection(**raw.get("pacing", {})),
        hosts=hosts,
        ads=AdsSection(brands=brands, voices=voices, sfx_dir=sfx_dir),
        audio=AudioSection(**raw.get("audio", {})),
        homeassistant=ha_section,
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        ha_token=ha_token,
    )
    _validate(config)
    return config
