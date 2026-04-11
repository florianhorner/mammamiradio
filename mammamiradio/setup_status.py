"""First-run onboarding and setup status classification."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
from pathlib import Path
from typing import Any

from mammamiradio.config import StationConfig
from mammamiradio.models import StationState

RUN_MODES = [
    {
        "id": "ha_addon",
        "label": "Home Assistant Add-on",
        "description": "Best when you want the radio inside Home Assistant with ingress.",
        "surface": "Add-on Configuration",
    },
    {
        "id": "docker",
        "label": "Docker",
        "description": "Runs from docker compose with env vars and one local port.",
        "surface": ".env for docker compose",
    },
    {
        "id": "macos",
        "label": "Mac App",
        "description": "One-click macOS setup with the bundled app launcher and local dashboard.",
        "surface": "Mac app setup files",
    },
    {
        "id": "local",
        "label": "Local Dev",
        "description": "Direct Python run with editable config and full local control.",
        "surface": ".env and radio.toml",
    },
]


def detect_run_mode(config: StationConfig) -> dict:
    """Guess the current deployment path so onboarding can set context."""
    if config.is_addon:
        detected = "ha_addon"
    elif Path("/.dockerenv").exists():
        detected = "docker"
    elif platform.system() == "Darwin":
        detected = "macos"
    else:
        detected = "local"

    modes: list[dict[str, Any]] = []
    for mode in RUN_MODES:
        entry: dict[str, Any] = dict(mode)
        entry["detected"] = mode["id"] == detected
        modes.append(entry)

    return {
        "detected": detected,
        "modes": modes,
    }


def _playlist_is_demo(state: StationState) -> bool:
    """Check if the loaded playlist is the built-in demo set."""
    if not state.playlist:
        return True
    return all(track.spotify_id.startswith("demo") for track in state.playlist[:5])


def classify_station_mode(
    config: StationConfig,
    state: StationState,
    *,
    demo_playlist: bool | None = None,
) -> dict:
    """Collapse many setup details into one operator-friendly runtime mode."""
    if demo_playlist is None:
        demo_playlist = _playlist_is_demo(state)
    has_anthropic = bool(config.anthropic_api_key or config.openai_api_key)
    has_ha = bool(config.homeassistant.enabled and config.ha_token)

    if has_anthropic and has_ha:
        return {
            "id": "connected_home",
            "label": "Connected Home Radio",
            "summary": "Full AI radio with home-aware banter.",
            "detail": "The station references your home state in banter and ads.",
        }

    if has_anthropic:
        return {
            "id": "full_ai",
            "label": "Full AI Radio",
            "summary": "Live Claude-generated banter and ads.",
            "detail": "Add a Home Assistant token to unlock home-aware content.",
        }

    return {
        "id": "demo",
        "label": "Demo Radio",
        "summary": "The station is running with canned banter clips and demo music.",
        "detail": "Add an Anthropic or OpenAI API key to unlock AI-generated banter.",
    }


def addon_options_snippet(config: StationConfig) -> str:
    """Return a copy-friendly JSON block for the Home Assistant add-on UI."""
    values = {
        "anthropic_api_key": "*** configured ***" if config.anthropic_api_key else "<optional>",
    }
    return json.dumps(values, indent=2)


def build_setup_status(config: StationConfig, state: StationState, *, probe: bool = False) -> dict:
    """Produce the full onboarding payload used by the dashboard gate."""
    mode = detect_run_mode(config)
    ffmpeg_bin = shutil.which("ffmpeg")
    demo_playlist = _playlist_is_demo(state)
    station_mode = classify_station_mode(config, state, demo_playlist=demo_playlist)

    essentials = [
        {
            "key": "anthropic",
            "label": "Anthropic",
            "required": False,
            "required_label": "Optional for AI banter and ads",
            "status": "configured" if config.anthropic_api_key else "missing",
            "summary": (
                "Claude generation is available for banter and ads."
                if config.anthropic_api_key
                else "No Anthropic key found. The station still works, but banter and ads use fallback copy."
            ),
            "next_action": "Add ANTHROPIC_API_KEY if you want the full AI radio experience.",
            "skip_outcome": "If you skip this, the station still runs with simpler stock lines.",
            "where": {
                "ha_addon": "Add-on Configuration",
                "docker": ".env used by docker compose",
                "macos": "the generated .env file behind the Mac launcher",
                "local": ".env in the project root",
            },
        },
        {
            "key": "homeassistant",
            "label": "Home Assistant",
            "required": False,
            "required_label": "Optional ambient context",
            "status": (
                "configured"
                if config.homeassistant.enabled and config.ha_token
                else "skipped"
                if not config.homeassistant.enabled
                else "missing"
            ),
            "summary": (
                "Home Assistant context is available for references in banter."
                if config.homeassistant.enabled and config.ha_token
                else "Home Assistant integration is off."
                if not config.homeassistant.enabled
                else "Home Assistant is enabled, but no token is available."
            ),
            "next_action": (
                "Nothing to do. Add-on mode wires this up automatically."
                if config.is_addon
                else "Enable Home Assistant in radio.toml and provide HA_TOKEN if you want live home context."
            ),
            "skip_outcome": "If you skip this, the hosts just stop referencing your home state.",
            "where": {
                "ha_addon": "Automatic via Supervisor",
                "docker": ".env plus [homeassistant] in radio.toml",
                "macos": ".env plus [homeassistant] in radio.toml",
                "local": ".env plus [homeassistant] in radio.toml",
            },
        },
    ]

    preflight_checks = [
        {
            "key": "ffmpeg",
            "label": "ffmpeg",
            "status": "ok" if ffmpeg_bin else "fail",
            "detail": (
                "ffmpeg is available for normalization, ads, and format conversion."
                if ffmpeg_bin
                else "ffmpeg is missing from PATH, so audio rendering will fail."
            ),
        },
        {
            "key": "playlist_loaded",
            "label": "Current loaded tracks",
            "status": "warn" if demo_playlist else "ok",
            "detail": (
                "The station currently has demo tracks loaded."
                if demo_playlist
                else "The station currently uses "
                + (
                    state.playlist_source.label
                    if state.playlist_source and state.playlist_source.label
                    else state.playlist_source.kind
                    if state.playlist_source
                    else "charts"
                )
                + "."
            ),
        },
    ]

    launch = {
        "headline": station_mode["summary"],
        "detail": station_mode["detail"],
        "cta": "Start Station" if station_mode["id"] != "demo" else "Start Demo Station",
        "post_launch": (
            "You will land in the control plane. Open the listener view when you want to hear the live station."
        ),
    }

    onboarding_required = station_mode["id"] == "demo"

    signature_data = {
        "mode": mode["detected"],
        "station_mode": station_mode["id"],
        "essentials": [(item["key"], item["status"]) for item in essentials],
        "checks": [(item["key"], item["status"]) for item in preflight_checks],
    }
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()[:12]

    return {
        "detected_mode": mode["detected"],
        "available_modes": mode["modes"],
        "station_mode": station_mode,
        "onboarding_required": onboarding_required,
        "essentials": essentials,
        "preflight_checks": preflight_checks,
        "launch": launch,
        "addon_options_snippet": addon_options_snippet(config) if config.is_addon else "",
        "signature": signature,
    }
