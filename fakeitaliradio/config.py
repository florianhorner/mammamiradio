from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field, fields
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
    cache_dir: Path = Path("cache")
    tmp_dir: Path = Path("tmp")

    # Secrets from env
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    anthropic_api_key: str = ""
    dashboard_api_key: str = ""
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000


def _validate_section_dict(section_name: str, section_data: object, section_type: type) -> dict:
    if section_data is None:
        return {}
    if not isinstance(section_data, dict):
        raise ValueError(f"Section [{section_name}] must be a table/object")

    allowed_keys = {f.name for f in fields(section_type)}
    unknown_keys = sorted(set(section_data.keys()) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"Unknown keys in [{section_name}]: {', '.join(unknown_keys)}"
        )
    return section_data


def _load_bind_port() -> int:
    raw_port = os.getenv("FAKEITALIRADIO_PORT", "8000")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("FAKEITALIRADIO_PORT must be an integer") from exc
    if not (1 <= port <= 65535):
        raise ValueError("FAKEITALIRADIO_PORT must be between 1 and 65535")
    return port


def load_config(path: str = "radio.toml") -> StationConfig:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    station_raw = _validate_section_dict("station", raw.get("station", {}), StationSection)
    playlist_raw = _validate_section_dict("playlist", raw.get("playlist", {}), PlaylistSection)
    pacing_raw = _validate_section_dict("pacing", raw.get("pacing", {}), PacingSection)
    audio_raw = _validate_section_dict("audio", raw.get("audio", {}), AudioSection)

    hosts = [
        HostPersonality(
            name=h["name"],
            voice=h["voice"],
            style=h["style"],
        )
        for h in raw.get("hosts", [])
    ]
    if not hosts:
        raise ValueError("No hosts configured. Add at least one [[hosts]] entry in radio.toml.")

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

    return StationConfig(
        station=StationSection(**station_raw),
        playlist=PlaylistSection(**playlist_raw),
        pacing=PacingSection(**pacing_raw),
        hosts=hosts,
        ads=AdsSection(brands=brands, voices=voices, sfx_dir=sfx_dir),
        audio=AudioSection(**audio_raw),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        dashboard_api_key=os.getenv("DASHBOARD_API_KEY", ""),
        bind_host=os.getenv("FAKEITALIRADIO_HOST", "127.0.0.1"),
        bind_port=_load_bind_port(),
    )
