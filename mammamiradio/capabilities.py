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


def next_step(caps: Capabilities) -> dict:
    """Return a single guided hint for the dashboard to show the user.

    Priority order matches value to the listener experience:
    Spotify creds → Spotify Connect → Anthropic key → all set.
    """
    if not caps.spotify_api:
        return {
            "key": "add_spotify",
            "message": "Add Spotify credentials to play your music",
            "action": "open_settings",
        }
    if not caps.spotify_connected:
        return {
            "key": "connect_spotify",
            "message": "Open Spotify and select this station as your playback device",
            "action": "wait",
        }
    if not caps.anthropic:
        return {
            "key": "add_anthropic",
            "message": "Add an Anthropic API key to unlock AI hosts",
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
            "spotify_connected": caps.spotify_connected,
            "spotify_api": caps.spotify_api,
            "anthropic": caps.anthropic,
            "ha": caps.ha,
        },
        "tier": caps.tier,
        "tier_label": caps.tier_label,
        "next_step": next_step(caps),
    }
