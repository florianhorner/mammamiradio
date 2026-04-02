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
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from mammamiradio.models import AdBrand, AdVoice, HostPersonality, PersonalityAxes

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
    fifo_path: str = "/tmp/mammamiradio.pcm"
    go_librespot_bin: str = "go-librespot"
    go_librespot_config_dir: str = "go-librespot"
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
    is_addon: bool = False


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
    return bool(os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN") or Path("/data/options.json").exists())


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
        "spotify_client_id": "SPOTIFY_CLIENT_ID",
        "spotify_client_secret": "SPOTIFY_CLIENT_SECRET",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "admin_password": "ADMIN_PASSWORD",
    }
    for opt_key, env_key in env_map.items():
        val = options.get(opt_key, "")
        if val and not os.getenv(env_key):
            os.environ[env_key] = val


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
    if config.homeassistant.enabled and not config.ha_token:
        log.warning("Home Assistant enabled but no HA_TOKEN in environment")
    if not config.ads.brands:
        log.warning("No ad brands configured — ad segments will be skipped")
    if not config.spotify_client_id or not config.spotify_client_secret:
        log.warning("No Spotify credentials — using demo playlist")
    # Addon mode: Supervisor handles auth, skip non-local bind check
    if (
        not config.is_addon
        and not _is_loopback_host(config.bind_host)
        and not (config.admin_password or config.admin_token)
    ):
        errors.append("Non-local bind requires ADMIN_PASSWORD or ADMIN_TOKEN")

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
    # Env-var overrides for HA add-on: HA_URL and HA_ENABLED
    if os.getenv("HA_URL"):
        ha_raw["url"] = os.getenv("HA_URL")
    if os.getenv("HA_ENABLED", "").lower() in ("true", "1", "yes"):
        ha_raw["enabled"] = True
    ha_section = HomeAssistantSection(**ha_raw)
    ha_token = os.getenv("HA_TOKEN", "")
    # Auto-enable HA if token is present and URL is set (Docker/add-on convenience)
    if ha_token and ha_section.url and not ha_section.enabled:
        ha_section.enabled = True
    if ha_section.enabled and not ha_token:
        import logging as _log

        _log.getLogger(__name__).warning("Home Assistant enabled but no HA_TOKEN in environment")

    # Env-var overrides for Docker/HA add-on: station identity and playlist
    if os.getenv("STATION_NAME"):
        station_raw["name"] = os.getenv("STATION_NAME")
    if os.getenv("STATION_THEME"):
        station_raw["theme"] = os.getenv("STATION_THEME")
    if os.getenv("PLAYLIST_SPOTIFY_URL"):
        raw.setdefault("playlist", {})["spotify_url"] = os.getenv("PLAYLIST_SPOTIFY_URL")
    if os.getenv("CLAUDE_MODEL"):
        audio_raw["claude_model"] = os.getenv("CLAUDE_MODEL")

    # Env-var overrides for cache/tmp directories (for Docker volume mounts)
    cache_dir = Path(os.getenv("MAMMAMIRADIO_CACHE_DIR", "cache"))
    tmp_dir = Path(os.getenv("MAMMAMIRADIO_TMP_DIR", "tmp"))

    config = StationConfig(
        station=StationSection(**station_raw),
        playlist=PlaylistSection(**raw.get("playlist", {})),
        pacing=PacingSection(**raw.get("pacing", {})),
        hosts=hosts,
        ads=AdsSection(brands=brands, voices=voices, sfx_dir=sfx_dir),
        audio=AudioSection(**audio_raw),
        homeassistant=ha_section,
        cache_dir=cache_dir,
        tmp_dir=tmp_dir,
        bind_host=os.getenv("MAMMAMIRADIO_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("MAMMAMIRADIO_PORT", "8000")),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        ha_token=ha_token,
        is_addon=addon_mode,
    )

    # Addon overrides: persistent paths, auto-enable HA, configurable go-librespot dir
    if addon_mode:
        import logging as _log

        _log.getLogger(__name__).info("Running as Home Assistant addon")
        config.cache_dir = Path("/data/cache")
        config.tmp_dir = Path("/data/tmp")
        config.audio.go_librespot_config_dir = "/data/go-librespot"
        # Auto-enable HA context via Supervisor API
        supervisor_token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN", "")
        if supervisor_token:
            config.homeassistant.enabled = True
            config.homeassistant.url = "http://supervisor/core"
            config.ha_token = supervisor_token

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
        "go_librespot_config_dir": config.audio.go_librespot_config_dir,
        "go_librespot_port": config.audio.go_librespot_port,
        "tmp_dir": str(config.tmp_dir),
    }


def startup_env(config: StationConfig | None = None) -> str:
    """Emit all shell variables start.sh needs in one shot, avoiding repeated Python spawns."""
    import shlex

    from mammamiradio.go_librespot_runtime import build_go_librespot_runtime, read_owned_pid

    rt = runtime_json(config)
    glr = build_go_librespot_runtime(
        go_librespot_bin=rt["go_librespot_bin"],
        config_dir=rt["go_librespot_config_dir"],
        fifo_path=rt["fifo_path"],
        port=rt["go_librespot_port"],
        tmp_dir=rt["tmp_dir"],
    )
    owned_pid = read_owned_pid(glr.state_file, glr.fingerprint)
    lines = [
        f"HOST={shlex.quote(rt['bind_host'])}",
        f"PORT={shlex.quote(str(rt['port']))}",
        f"FIFO={shlex.quote(rt['fifo_path'])}",
        f"GO_LIBRESPOT_BIN={shlex.quote(rt['go_librespot_bin'])}",
        f"GO_LIBRESPOT_CONFIG_DIR={shlex.quote(str(glr.config_dir))}",
        f"GO_LIBRESPOT_PORT={shlex.quote(str(glr.port))}",
        f"TMP_DIR={shlex.quote(str(glr.tmp_dir))}",
        f"GO_LIBRESPOT_FINGERPRINT={shlex.quote(glr.fingerprint)}",
        f"GO_LIBRESPOT_STATE_FILE={shlex.quote(str(glr.state_file))}",
        f"GOLIBRESPOT_OWNED_PID={shlex.quote(str(owned_pid) if owned_pid else '')}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "runtime-json":
        print(json.dumps(runtime_json()))
    elif len(sys.argv) > 1 and sys.argv[1] == "startup-env":
        print(startup_env())
    else:
        print("Usage: python -m mammamiradio.config {runtime-json|startup-env}", file=sys.stderr)
        sys.exit(1)
