"""Capability flag detection — replaces the old 64-state mode system."""

from __future__ import annotations

from mammamiradio.config import StationConfig
from mammamiradio.models import Capabilities, StationState


def get_capabilities(config: StationConfig, state: StationState) -> Capabilities:
    """Derive capability flags from static config and live runtime state.

    This is the single source of truth for what the station can do right now.
    The old ``setup_status.classify_station_mode`` collapsed these into named
    modes; capabilities keep them independent so the UI can show each one as
    a progressive upgrade.
    """
    return Capabilities(
        spotify_connected=state.spotify_connected,
        spotify_api=bool(config.spotify_client_id and config.spotify_client_secret),
        anthropic=bool(config.anthropic_api_key),
        ha=bool(config.homeassistant.enabled and config.ha_token),
    )


def capabilities_to_dict(caps: Capabilities) -> dict:
    """Serialize capabilities for the ``/api/capabilities`` JSON response."""
    return {
        "capabilities": {
            "spotify_connected": caps.spotify_connected,
            "spotify_api": caps.spotify_api,
            "anthropic": caps.anthropic,
            "ha": caps.ha,
        },
        "tier": caps.tier,
        "tier_label": caps.tier_label,
    }
