"""Capability flag detection — three-tier system (Demo / Full AI / Connected Home)."""

from __future__ import annotations

from mammamiradio.core.config import StationConfig
from mammamiradio.core.models import Capabilities, StationState
from mammamiradio.core.setup_status import home_context_availability


def get_capabilities(config: StationConfig, state: StationState) -> Capabilities:
    """Derive capability flags from static config and live runtime state.

    Three tiers: Demo Radio → Full AI Radio → Connected Home.
    Music source is always available (local + yt-dlp + charts).
    """
    home_availability = home_context_availability(config, state)
    return Capabilities(
        llm=bool(config.anthropic_api_key or config.openai_api_key),
        ha=bool(config.homeassistant.enabled and config.ha_token),
        home_context_ready=home_availability.home_context_ready,
        home_context_enabled=home_availability.readiness != "disabled",
        jamendo=bool((config.playlist.jamendo_client_id or "").strip()),
        charts_reload=bool(config.allow_ytdlp),
        tts_degraded=bool(getattr(config, "tts_degraded_voices", []))
        or any(
            (provider_class == "tts_provider" or provider_class.startswith("tts:")) and details.get("fallback_active")
            for provider_class, details in getattr(state, "runtime_provider_state", {}).items()
        ),
    )


def next_step(caps: Capabilities) -> dict:
    """Return a single guided hint for the dashboard.

    Priority: AI key → finish requested HA access → review configured HA context → all set.
    """
    if not caps.llm:
        return {
            "key": "add_llm",
            "message": "Add an Anthropic or OpenAI API key to unlock AI hosts",
            "action": "open_settings",
        }
    if caps.home_context_enabled and not caps.ha:
        return {
            "key": "enable_ha",
            "message": "Finish Home Assistant access for home-aware banter",
            "action": "open_settings",
        }
    if caps.ha and caps.home_context_enabled and not caps.home_context_ready:
        return {
            "key": "review_ha_context",
            "message": "Review Home Assistant context before calling it Connected Home",
            "action": "open_settings",
        }
    return {
        "key": "all_set",
        "message": "",
        "action": "none",
    }


def capabilities_to_dict(caps: Capabilities) -> dict:
    """Serialize capabilities for the ``/api/capabilities`` JSON response."""
    return {
        "capabilities": {
            "llm": caps.llm,
            "ha": caps.ha,
            "homeassistant_access": caps.ha,
            "home_context_ready": caps.home_context_ready,
            "home_context_enabled": caps.home_context_enabled,
            "jamendo": caps.jamendo,
            "charts_reload": caps.charts_reload,
        },
        "tier": caps.tier,
        "tier_label": caps.tier_label,
        "tts_degraded": caps.tts_degraded,
        "next_step": next_step(caps),
    }
