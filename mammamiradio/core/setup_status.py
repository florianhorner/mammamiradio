"""First-run onboarding and setup status classification."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import StationState

RUN_MODES = [
    {
        "id": "ha_addon",
        "label": "Home Assistant Add-on",
        "description": "Best when you want the radio inside Home Assistant with ingress.",
        "surface": "/config/secrets.env plus Add-on Configuration",
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


def has_safe_home_context(state: StationState) -> bool:
    """Return True when a prompt-safe HA context slice is actually available."""
    return bool(
        (state.ha_context or "").strip()
        or state.ha_scored_entities
        or (state.ha_context_last_updated and state.ha_context_entity_count > 0)
    )


HomeContextReadiness = Literal["disabled", "access_missing", "collecting", "empty", "prompt_ready"]


@dataclass(frozen=True)
class HomeContextAvailability:
    """User-facing HA context availability for setup and capability surfaces."""

    readiness: HomeContextReadiness
    homeassistant_access: bool
    home_context_ready: bool


def home_context_availability(config: StationConfig, state: StationState) -> HomeContextAvailability:
    """Project Home Assistant context into typed setup-ready states."""
    has_access = bool(config.homeassistant.enabled and config.ha_token)
    home_ready = has_safe_home_context(state)
    readiness: HomeContextReadiness
    if not config.homeassistant.enabled:
        readiness = "disabled"
    elif not has_access:
        readiness = "access_missing"
    elif not config.homeassistant.context_enabled:
        readiness = "disabled"
    elif home_ready:
        readiness = "prompt_ready"
    elif state.ha_context_last_updated:
        readiness = "empty"
    else:
        readiness = "collecting"
    return HomeContextAvailability(
        readiness=readiness,
        homeassistant_access=has_access,
        home_context_ready=readiness == "prompt_ready",
    )


def _llm_key_status(config: StationConfig, provider_health: dict | None = None) -> str:
    has_anthropic = bool(config.anthropic_api_key)
    has_openai = bool(config.openai_api_key)
    if not (has_anthropic or has_openai):
        return "missing"
    if not provider_health:
        return "ready"
    statuses = [
        provider_health.get("anthropic", {}).get("key_status") if has_anthropic else None,
        provider_health.get("openai", {}).get("key_status") if has_openai else None,
    ]
    if "valid" in statuses:
        return "ready"
    if "unverified" in statuses:
        return "checking"
    if "rejected" in statuses:
        return "rejected"
    return "ready"


def _stream_status(config: StationConfig, state: StationState, golden_path: dict | None = None) -> str:
    if state.session_stopped:
        return "stopped"
    if state.now_streaming or state.queued_segments or state.playlist or state.last_music_file:
        return "ready"
    if golden_path is not None:
        return "blocked" if golden_path.get("blocking") else "ready"
    return "checking" if config.allow_ytdlp else "blocked"


def _legacy_home_context_status(has_llm: bool, availability: HomeContextAvailability) -> str:
    if not has_llm:
        return "waiting_ai"
    return {
        "disabled": "not_configured",
        "access_missing": "blocked",
        "collecting": "checking",
        "empty": "empty",
        "prompt_ready": "ready",
    }[availability.readiness]


def _setup_status_shape(status: str) -> dict[str, str]:
    if status == "ready":
        return {"tone": "ok", "shape": "ok", "display_status": "Ready"}
    if status == "not_configured":
        return {"tone": "info", "shape": "info", "display_status": "Optional"}
    if status in {"blocked", "rejected", "stopped"}:
        return {"tone": "error", "shape": "error", "display_status": "Needs setup"}
    display = {
        "missing": "Missing",
        "waiting_ai": "Waiting for AI",
        "checking": "Checking",
        "empty": "No safe context yet",
    }.get(status, status.replace("_", " ").title())
    return {"tone": "warn", "shape": "warn", "display_status": display}


def _build_setup_strip(stages: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for stage in stages:
        status = str(stage.get("status") or "checking")
        shape = _setup_status_shape(status)
        items.append(
            {
                "id": stage.get("id") or stage.get("label", "setup").lower().replace(" ", "_"),
                "label": stage.get("label") or "Setup",
                "status": status,
                **shape,
            }
        )
    primary_action = {"kind": "open_listener", "label": "Open listener", "target": "listen"}
    for stage in stages:
        status = str(stage.get("status") or "checking")
        action = str(stage.get("action") or "")
        if action == "review_home_context":
            primary_action = {"kind": "review_home_context", "label": "Review home context", "target": "setup"}
            break
        if status in {"ready", "not_configured"}:
            continue
        primary_action = {
            "fix_stream": {"kind": "fix_stream", "label": "Fix stream", "target": "setup"},
            "add_ai_key": {"kind": "add_ai_key", "label": "Add AI key", "target": "setup"},
        }.get(action, {"kind": "review_setup", "label": "Review setup", "target": "setup"})
        break
    attention_required = primary_action["kind"] != "open_listener"
    return {
        "items": items,
        "attention_required": attention_required,
        "primary_action": primary_action,
    }


def _home_context_copy(config: StationConfig, home_status: str) -> tuple[str, str]:
    if home_status == "ready":
        return (
            "Home context is available.",
            "Filtered Home Assistant context can be inspected and muted from the admin panel.",
        )
    if home_status == "not_configured":
        return (
            "Home Assistant isn't connected.",
            "Optional — connect Home Assistant if you want the hosts to notice your house.",
        )
    if home_status == "blocked":
        detail = (
            "Check the add-on configuration so the station can reach Home Assistant."
            if config.is_addon
            else "Set HA_TOKEN and HA_URL, then recheck Home Assistant context."
        )
        return ("Home Assistant needs access.", detail)
    if home_status == "empty":
        return (
            "Home Assistant is connected, but no prompt-safe context is available.",
            "Review the Home context preview, muted entities, and prompt-safe devices.",
        )
    if home_status == "checking":
        return (
            "Home context is still collecting.",
            "The next Home Assistant refresh will decide whether anything prompt-safe is available.",
        )
    if home_status == "waiting_ai":
        return (
            "Home context waits for AI hosts.",
            "Add an Anthropic or OpenAI key before the hosts can use Home Assistant context.",
        )
    return (
        "Review Home Assistant context.",
        "Check Home Assistant access and the context preview.",
    )


def _homeassistant_essential_status(
    config: StationConfig,
    guided_home_context: dict,
) -> tuple[str, str, str]:
    """Project guided HA context state into the legacy essentials row."""
    readiness = guided_home_context.get("readiness")
    status = guided_home_context.get("status")
    if guided_home_context.get("home_context_ready"):
        return (
            "configured",
            "Home Assistant context is available for references in banter.",
            "Review the Home context preview when you want to mute or inspect entities.",
        )
    if readiness == "disabled":
        summary = (
            "Home Assistant context is off."
            if guided_home_context.get("homeassistant_access")
            else "Home Assistant integration is off."
        )
        next_action = (
            "Nothing required. Enable Home Assistant context only if you want live home-aware banter."
            if config.is_addon
            else "Enable Home Assistant in radio.toml and provide HA_TOKEN if you want live home context."
        )
        return ("skipped", summary, next_action)
    if readiness == "access_missing":
        return (
            "missing",
            "Home Assistant is enabled, but no token is available.",
            "Check the add-on configuration so the station can reach Home Assistant."
            if config.is_addon
            else "Set HA_TOKEN and HA_URL, then recheck Home Assistant context.",
        )
    if readiness == "empty":
        return (
            "warn",
            "Home Assistant is connected, but no prompt-safe context is available yet.",
            "Review the Home context preview, muted entities, and prompt-safe devices.",
        )
    if status == "waiting_ai":
        return (
            "warn",
            "Home Assistant access is configured; AI hosts are needed before context can be used.",
            "Add an Anthropic or OpenAI key before the hosts can use Home Assistant context.",
        )
    return (
        "warn",
        "Home Assistant access is configured; context is still being collected.",
        "Wait for the next Home Assistant refresh or review the context preview.",
    )


def build_guided_setup(
    config: StationConfig,
    state: StationState,
    *,
    golden_path: dict | None = None,
    provider_health: dict | None = None,
) -> dict:
    """Canonical three-stage onboarding projection shared by admin APIs."""
    has_llm = bool(config.anthropic_api_key or config.openai_api_key)
    home_availability = home_context_availability(config, state)
    has_ha_access = home_availability.homeassistant_access
    home_ready = home_availability.home_context_ready
    stream_status = _stream_status(config, state, golden_path)
    ai_status = _llm_key_status(config, provider_health)
    home_status = _legacy_home_context_status(has_llm, home_availability)

    stream = {
        "id": "stream",
        "status": stream_status,
        "label": "Stream",
        "headline": "Demo Radio is ready to hear." if stream_status == "ready" else "Stream needs attention.",
        "detail": (
            "Open the listener and hear the station before adding keys."
            if stream_status == "ready"
            else "Start the station or add a music source before continuing."
        ),
        "action": "open_listener" if stream_status == "ready" else "fix_stream",
    }
    ai_hosts = {
        "id": "ai_hosts",
        "status": ai_status,
        "label": "AI hosts",
        "headline": "AI hosts are ready." if ai_status == "ready" else "Add one AI host key.",
        "detail": (
            "Anthropic or OpenAI can generate live host breaks."
            if ai_status == "ready"
            else "Add ANTHROPIC_API_KEY or OPENAI_API_KEY when you want generated banter and ads."
        ),
        "action": "review" if ai_status == "ready" else "add_ai_key",
    }
    home_headline, home_detail = _home_context_copy(config, home_status)
    home_context = {
        "id": "home_context",
        "status": home_status,
        "readiness": home_availability.readiness,
        "label": "Home context",
        "headline": home_headline,
        "detail": home_detail,
        "action": (
            "review_home_context"
            if home_status in {"ready", "empty"}
            else "none"
            if home_status == "not_configured"
            else "wait"
        ),
        "homeassistant_access": has_ha_access,
        "home_context_ready": home_ready,
    }
    return {
        "strip": _build_setup_strip([stream, ai_hosts, home_context]),
        "stream": {key: value for key, value in stream.items() if key != "id"},
        "ai_hosts": {key: value for key, value in ai_hosts.items() if key != "id"},
        "home_context": {key: value for key, value in home_context.items() if key != "id"},
    }


def classify_station_mode(
    config: StationConfig,
    state: StationState,
    *,
    demo_playlist: bool | None = None,
) -> dict:
    """Collapse many setup details into one operator-friendly runtime mode."""
    if demo_playlist is None:
        demo_playlist = _playlist_is_demo(state)
    has_llm = bool(config.anthropic_api_key or config.openai_api_key)
    has_ha_context = home_context_availability(config, state).readiness == "prompt_ready"

    if has_llm and has_ha_context:
        return {
            "id": "connected_home",
            "label": "Connected Home Radio",
            "summary": "Full AI radio with home-aware banter.",
            "detail": "The station references your home state in banter and ads.",
        }

    if has_llm:
        return {
            "id": "full_ai",
            "label": "Full AI Radio",
            "summary": "Live AI-generated banter and ads.",
            "detail": "Review Home Assistant context to unlock home-aware content.",
        }

    return {
        "id": "demo",
        "label": "Demo Radio",
        "summary": "The station is running with canned banter clips and demo music.",
        "detail": "Add an Anthropic or OpenAI API key to unlock AI-generated banter.",
    }


def addon_options_snippet(config: StationConfig) -> str:
    """Return a copy-friendly provider secret block for the Home Assistant add-on."""
    values = {
        "ANTHROPIC_API_KEY": "*** configured ***" if config.anthropic_api_key else "<optional>",
        "OPENAI_API_KEY": "*** configured ***" if config.openai_api_key else "<optional>",
        "AZURE_SPEECH_KEY": "*** configured ***" if config.azure_speech_key else "<optional>",
        "AZURE_SPEECH_REGION": "*** configured ***" if config.azure_speech_region else "<optional>",
        "ELEVENLABS_API_KEY": "*** configured ***" if config.elevenlabs_api_key else "<optional>",
    }
    lines = [
        "# /config/secrets.env",
        "# Plaintext add-on config file, not Home Assistant /config/secrets.yaml.",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    return "\n".join(lines)


def identity_status(config: StationConfig) -> dict:
    """Return the operator-facing station identity projection."""
    identity = getattr(config, "identity", None)
    station_name = config.display_station_name
    generated = getattr(identity, "generated", {}) or {}
    return {
        "station_name": station_name,
        "source": getattr(identity, "source", "unknown") if identity is not None else "unknown",
        "custom_copy_preserved": bool(getattr(identity, "custom_copy_preserved", False)),
        "preview": {
            "heard_on_air": generated.get("spoken_ident") or station_name,
            "seen_by_listeners": generated.get("listener_title") or station_name,
            "seen_in_home_assistant": generated.get("home_assistant_name") or station_name,
        },
        "stable_ids": {
            "addon_slug": "mammamiradio",
            "integration_domain": "mammamiradio",
            "media_player": "media_player.mammamiradio",
            "media_source": "media-source://mammamiradio/live",
            "segment_sensor": "sensor.mammamiradio_segment_type",
            "listeners_sensor": "sensor.mammamiradio_listeners",
            "on_air_binary_sensor": "binary_sensor.mammamiradio_on_air",
        },
    }


def build_setup_status(
    config: StationConfig,
    state: StationState,
    *,
    golden_path: dict | None = None,
    provider_health: dict | None = None,
) -> dict:
    """Produce the full onboarding payload used by the dashboard gate."""
    mode = detect_run_mode(config)
    ffmpeg_bin = shutil.which("ffmpeg")
    ytdlp_bin = shutil.which("yt-dlp")
    demo_playlist = _playlist_is_demo(state)
    station_mode = classify_station_mode(config, state, demo_playlist=demo_playlist)
    identity = identity_status(config)
    has_llm = bool(config.anthropic_api_key or config.openai_api_key)
    has_azure_tts = bool(config.azure_speech_key and config.azure_speech_region)
    has_cloud_tts = bool(config.openai_api_key or has_azure_tts or config.elevenlabs_api_key)
    guided_setup = build_guided_setup(config, state, golden_path=golden_path, provider_health=provider_health)
    stream_ready = guided_setup["stream"]["status"] == "ready"
    ha_status, ha_summary, ha_next_action = _homeassistant_essential_status(config, guided_setup["home_context"])

    mode_by_id = {entry["id"]: entry for entry in mode["modes"]}
    detected_mode = mode_by_id.get(mode["detected"], {})

    ffmpeg_install_command = {
        "ha_addon": "Bundled in the add-on image (no action required).",
        "docker": "Bundled in the Docker image (rebuild container if missing).",
        "macos": "brew install ffmpeg",
        "local": "Install ffmpeg with your package manager and verify `ffmpeg -version`.",
    }

    essentials = [
        {
            "key": "llm_keys",
            "label": "AI Script Key",
            "required": False,
            "required_label": "Optional for AI banter and ads",
            "status": "configured" if has_llm else "missing",
            "summary": (
                "At least one AI key is configured. Banter and ads can be generated live."
                if has_llm
                else "No AI key found. The station still runs, but banter and ads use fallback copy."
            ),
            "next_action": "Add ANTHROPIC_API_KEY or OPENAI_API_KEY for full AI banter and ad generation.",
            "skip_outcome": "If you skip this, the station still runs with simpler stock lines.",
            "where": {
                "ha_addon": "/config/secrets.env in the add-on config folder",
                "docker": ".env used by docker compose",
                "macos": "the generated .env file behind the Mac launcher",
                "local": ".env in the project root",
            },
            "accepted_keys": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
            "configured_keys": [
                key
                for key, configured in [
                    ("ANTHROPIC_API_KEY", bool(config.anthropic_api_key)),
                    ("OPENAI_API_KEY", bool(config.openai_api_key)),
                ]
                if configured
            ],
        },
        {
            "key": "tts_keys",
            "label": "Voice Provider Keys",
            "required": False,
            "required_label": "Optional for premium voices",
            "status": "configured" if has_cloud_tts else "missing",
            "summary": (
                "At least one cloud TTS provider is configured for premium voices."
                if has_cloud_tts
                else "No premium TTS key found. The station still runs with Edge voice fallbacks."
            ),
            "next_action": (
                "Add OPENAI_API_KEY, AZURE_SPEECH_KEY plus AZURE_SPEECH_REGION, "
                "or ELEVENLABS_API_KEY for expanded voices."
            ),
            "skip_outcome": "If you skip this, configured cloud voices fall back to Edge voices.",
            "where": {
                "ha_addon": "/config/secrets.env in the add-on config folder",
                "docker": ".env used by docker compose",
                "macos": "the generated .env file behind the Mac launcher",
                "local": ".env in the project root",
            },
            "accepted_keys": ["OPENAI_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "ELEVENLABS_API_KEY"],
            "configured_keys": [
                key
                for key, configured in [
                    ("OPENAI_API_KEY", bool(config.openai_api_key)),
                    ("AZURE_SPEECH_KEY", bool(config.azure_speech_key)),
                    ("AZURE_SPEECH_REGION", bool(config.azure_speech_region)),
                    ("ELEVENLABS_API_KEY", bool(config.elevenlabs_api_key)),
                ]
                if configured
            ],
        },
        {
            "key": "homeassistant",
            "label": "Home Assistant",
            "required": False,
            "required_label": "Optional ambient context",
            "status": ha_status,
            "summary": ha_summary,
            "next_action": ha_next_action,
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
            "where": ffmpeg_bin or "not found in PATH",
            "repair": ffmpeg_install_command.get(mode["detected"], "Install ffmpeg and restart the station."),
        },
        {
            "key": "ytdlp",
            "label": "yt-dlp",
            "status": "ok" if ytdlp_bin else "warn",
            "detail": (
                "yt-dlp is available for fresh charts when enabled."
                if ytdlp_bin
                else "yt-dlp is not installed. The station can still play demo or local tracks."
            ),
            "where": ytdlp_bin or "not found in PATH",
            "repair": "Install yt-dlp if you want live charts; otherwise keep using demo/local sources.",
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

    onboarding_steps = [
        {
            "id": "identity",
            "title": "Name Your Station",
            "status": "done",
            "detail": f"{identity['station_name']} is the name people see and hear.",
        },
        {
            "id": "mode",
            "title": "Choose Run Mode",
            "status": "done",
            "detail": f"Detected mode: {detected_mode.get('label', mode['detected'])}.",
        },
        {
            "id": "llm",
            "title": "Add AI Key (Optional)",
            "status": "done" if has_llm else "todo",
            "detail": (
                "AI key detected."
                if has_llm
                else "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your credentials file for generated banter and ads."
            ),
        },
        {
            "id": "preflight",
            "title": "Pass Preflight Checks",
            "status": "todo" if any(item["status"] == "fail" for item in preflight_checks) else "done",
            "detail": "Check ffmpeg and playlist readiness before going live.",
        },
        {
            "id": "launch",
            "title": "Launch Station",
            "status": "done" if stream_ready else "blocked",
            "detail": (
                "Open listener mode and verify live audio output."
                if stream_ready
                else "Start the station and make sure a music source can serve audio."
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

    onboarding_required = not stream_ready

    signature_data = {
        "mode": mode["detected"],
        "station_mode": station_mode["id"],
        "identity": identity["station_name"],
        "identity_source": identity["source"],
        "essentials": [(item["key"], item["status"]) for item in essentials],
        "checks": [(item["key"], item["status"]) for item in preflight_checks],
    }
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()[:12]

    return {
        "detected_mode": mode["detected"],
        "available_modes": mode["modes"],
        "station_mode": station_mode,
        "identity": identity,
        "onboarding_required": onboarding_required,
        "guided_setup": guided_setup,
        "essentials": essentials,
        "preflight_checks": preflight_checks,
        "onboarding_steps": onboarding_steps,
        "recommended_next_action": (
            "Fix stream readiness before setup continues."
            if not stream_ready
            else "Add an AI key to unlock full station behavior."
            if not has_llm
            else "Install ffmpeg before launch."
            if not ffmpeg_bin
            else "Review the Home Assistant context preview."
            if guided_setup["home_context"]["status"] in {"ready", "empty"}
            else "Open the listener and verify live playback."
        ),
        "launch": launch,
        "addon_options_snippet": addon_options_snippet(config) if config.is_addon else "",
        "signature": signature,
    }
