"""Configuration loading for fakeitaliradio.

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
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from fakeitaliradio.models import AdBrand, AdVoice, HostPersonality

load_dotenv()


@dataclass
class StationSection:
    """Station identity and public stream metadata."""

    name: str = "Radio Italì"
    language: str = "it"
    theme: str = ""


@dataclass
class PlaylistSection:
    """Playlist source selection and ordering preferences."""

    spotify_url: str = ""
    shuffle: bool = False


@dataclass
class PacingSection:
    """Rules that control how often banter and ad breaks occur."""

    songs_between_banter: int = 2
    songs_between_ads: int = 4
    ad_spots_per_break: int = 1
    lookahead_segments: int = 3


@dataclass
class AudioSection:
    """Audio pipeline settings for encoding and Spotify capture."""

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
    """Optional Home Assistant integration used to seed prompt context."""

    enabled: bool = False
    url: str = ""
    poll_interval: int = 60  # seconds between state refreshes


@dataclass
class AdsSection:
    """Structured ad inventory, voices, and optional sound-effect assets."""

    brands: list[AdBrand] = field(default_factory=list)
    voices: list[AdVoice] = field(default_factory=list)
    sfx_dir: str = "sfx"


@dataclass
class StationConfig:
    """Fully resolved application configuration used at runtime."""

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
    bind_host: str = "127.0.0.1"
    port: int = 8000
    admin_username: str = "admin"
    admin_password: str = ""
    admin_token: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    anthropic_api_key: str = ""
    ha_token: str = ""


def _is_loopback_host(host: str) -> bool:
    """Return whether a bind target should be treated as localhost-only."""
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    if not _is_loopback_host(config.bind_host) and not (config.admin_password or config.admin_token):
        errors.append("Non-local bind requires ADMIN_PASSWORD or ADMIN_TOKEN")

    if errors:
        raise ValueError("Config errors:\n  " + "\n  ".join(errors))


def load_config(path: str = "radio.toml") -> StationConfig:
    """Load ``radio.toml`` plus environment overrides into a validated config."""
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

    # Legacy: station.bitrate → audio.bitrate migration
    station_raw = dict(raw.get("station", {}))
    audio_raw = dict(raw.get("audio", {}))
    if "bitrate" in station_raw:
        import logging as _log

        _log.getLogger(__name__).warning("station.bitrate is deprecated — use audio.bitrate instead")
        if "bitrate" not in audio_raw:
            audio_raw["bitrate"] = station_raw.pop("bitrate")
        else:
            station_raw.pop("bitrate")

    ha_raw = raw.get("homeassistant", {})
    ha_section = HomeAssistantSection(**ha_raw)
    ha_token = os.getenv("HA_TOKEN", "")
    if ha_section.enabled and not ha_token:
        import logging as _log

        _log.getLogger(__name__).warning("Home Assistant enabled but no HA_TOKEN in environment")

    config = StationConfig(
        station=StationSection(**station_raw),
        playlist=PlaylistSection(**raw.get("playlist", {})),
        pacing=PacingSection(**raw.get("pacing", {})),
        hosts=hosts,
        ads=AdsSection(brands=brands, voices=voices, sfx_dir=sfx_dir),
        audio=AudioSection(**audio_raw),
        homeassistant=ha_section,
        bind_host=os.getenv("FAKEITALIRADIO_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("FAKEITALIRADIO_PORT", "8000")),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        ha_token=ha_token,
    )
    _validate(config)
    return config


def runtime_json(config: StationConfig | None = None) -> dict:
    """Return resolved runtime settings for shell consumers."""
    if config is None:
        config = load_config()
    return {
        "bind_host": config.bind_host,
        "port": config.port,
        "fifo_path": config.audio.fifo_path,
        "go_librespot_bin": config.audio.go_librespot_bin,
        "go_librespot_port": config.audio.go_librespot_port,
        "tmp_dir": str(config.tmp_dir),
    }


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "runtime-json":
        print(json.dumps(runtime_json()))
    else:
        print("Usage: python -m fakeitaliradio.config runtime-json", file=sys.stderr)
        sys.exit(1)
