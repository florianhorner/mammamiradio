"""First-run onboarding and setup status classification."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
from pathlib import Path
from typing import Any

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
    if golden_path is not None:
        return "blocked" if golden_path.get("blocking") else "ready"
    if state.now_streaming or state.queued_segments or state.playlist or state.last_music_file:
        return "ready"
    return "checking" if config.allow_ytdlp else "blocked"


def build_guided_setup(
    config: StationConfig,
    state: StationState,
    *,
    golden_path: dict | None = None,
    provider_health: dict | None = None,
) -> dict:
    """Canonical three-stage onboarding projection shared by admin APIs."""
    has_llm = bool(config.anthropic_api_key or config.openai_api_key)
    has_ha_access = bool(config.homeassistant.enabled and config.ha_token)
    home_ready = has_safe_home_context(state)
    stream_status = _stream_status(config, state, golden_path)
    ai_status = _llm_key_status(config, provider_health)

    if not has_llm:
        home_status = "waiting_ai"
    elif home_ready:
        home_status = "ready"
    elif not config.homeassistant.enabled:
        # Home Assistant is an optional upgrade, not a required completion
        # state — a standalone station that never turned it on is done, not
        # blocked. "blocked" stays reserved for HA enabled-but-not-working.
        home_status = "not_configured"
    elif has_ha_access and state.ha_context_last_updated:
        home_status = "empty"
    elif has_ha_access:
        home_status = "checking"
    else:
        home_status = "blocked"

    return {
        "stream": {
            "status": stream_status,
            "label": "Stream",
            "headline": "Demo Radio is ready to hear." if stream_status == "ready" else "Stream needs attention.",
            "detail": (
                "Open the listener and hear the station before adding keys."
                if stream_status == "ready"
                else "Start the station or add a music source before continuing."
            ),
            "action": "open_listener" if stream_status == "ready" else "fix_stream",
        },
        "ai_hosts": {
            "status": ai_status,
            "label": "AI hosts",
            "headline": "AI hosts are ready." if ai_status == "ready" else "Add one AI host key.",
            "detail": (
                "Anthropic or OpenAI can generate live host breaks."
                if ai_status == "ready"
                else "Add ANTHROPIC_API_KEY or OPENAI_API_KEY when you want generated banter and ads."
            ),
            "action": "review" if ai_status == "ready" else "add_ai_key",
        },
        "home_context": {
            "status": home_status,
            "label": "Home context",
            "headline": (
                "Home context is available."
                if home_status == "ready"
                else "Home Assistant isn't connected."
                if home_status == "not_configured"
                else "Review home context after AI is ready."
            ),
            "detail": (
                "Filtered Home Assistant context can be inspected and muted from the admin panel."
                if home_status == "ready"
                else "Optional — connect Home Assistant if you want the hosts to notice your house."
                if home_status == "not_configured"
                else (
                    "Supervisor access is automatic in the add-on; "
                    "AI hosts must be ready before home context is useful."
                )
            ),
            "action": (
                "review_home_context"
                if home_status in {"ready", "empty"}
                else "none"
                if home_status == "not_configured"
                else "wait"
            ),
            "homeassistant_access": has_ha_access,
            "home_context_ready": home_ready,
        },
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
    has_ha_context = bool(config.homeassistant.enabled and config.ha_token and has_safe_home_context(state))

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
    has_llm = bool(config.anthropic_api_key or config.openai_api_key)
    has_azure_tts = bool(config.azure_speech_key and config.azure_speech_region)
    has_cloud_tts = bool(config.openai_api_key or has_azure_tts or config.elevenlabs_api_key)
    has_ha = bool(config.homeassistant.enabled and config.ha_token)
    is_ha_enabled = bool(config.homeassistant.enabled)
    guided_setup = build_guided_setup(config, state, golden_path=golden_path, provider_health=provider_health)
    stream_ready = guided_setup["stream"]["status"] == "ready"

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
            "status": ("configured" if has_ha else "skipped" if not is_ha_enabled else "missing"),
            "summary": (
                "Home Assistant context is available for references in banter."
                if has_ha
                else "Home Assistant integration is off."
                if not is_ha_enabled
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
        "essentials": [(item["key"], item["status"]) for item in essentials],
        "checks": [(item["key"], item["status"]) for item in preflight_checks],
    }
    signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()[:12]

    return {
        "detected_mode": mode["detected"],
        "available_modes": mode["modes"],
        "station_mode": station_mode,
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
