"""Capability flag detection — three-tier system (Demo / Full AI / Connected Home)."""

from __future__ import annotations

from mammamiradio.config import StationConfig
from mammamiradio.models import Capabilities, StationState


def get_capabilities(config: StationConfig, state: StationState) -> Capabilities:
    """Derive capability flags from static config and live runtime state.

    Three tiers: Demo Radio → Full AI Radio → Connected Home.
    Music source is always available (local + yt-dlp + charts).
    """
    return Capabilities(
        llm=bool(config.anthropic_api_key or config.openai_api_key),
        ha=bool(config.homeassistant.enabled and config.ha_token),
    )


def next_step(caps: Capabilities) -> dict:
    """Return a single guided hint for the dashboard.

    Priority: Anthropic key → HA token → all set.
    """
    if not caps.llm:
        return {
            "key": "add_llm",
            "message": "Add an Anthropic or OpenAI API key to unlock AI hosts",
            "action": "open_settings",
        }
    if not caps.ha:
        return {
            "key": "enable_ha",
            "message": "Connect Home Assistant for home-aware banter",
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
        },
        "tier": caps.tier,
        "tier_label": caps.tier_label,
        "next_step": next_step(caps),
    }
