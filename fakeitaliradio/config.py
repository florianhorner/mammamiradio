from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
import os

from fakeitaliradio.models import HostPersonality

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
    brand_pool: list[str] = field(default_factory=list)


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

    return StationConfig(
        station=StationSection(**raw.get("station", {})),
        playlist=PlaylistSection(**raw.get("playlist", {})),
        pacing=PacingSection(**raw.get("pacing", {})),
        hosts=hosts,
        ads=AdsSection(**raw.get("ads", {})),
        audio=AudioSection(**raw.get("audio", {})),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    )
