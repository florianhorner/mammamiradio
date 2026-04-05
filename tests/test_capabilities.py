"""Tests for the capability flags system that replaces the old 64-state mode wizard."""

from __future__ import annotations

from unittest.mock import MagicMock

from mammamiradio.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.models import Capabilities, StationState


def _config(**overrides):
    """Create a mock config with sensible defaults."""
    cfg = MagicMock()
    cfg.spotify_client_id = overrides.get("spotify_client_id", "")
    cfg.spotify_client_secret = overrides.get("spotify_client_secret", "")
    cfg.anthropic_api_key = overrides.get("anthropic_api_key", "")
    cfg.ha_token = overrides.get("ha_token", "")
    cfg.homeassistant.enabled = overrides.get("ha_enabled", False)
    return cfg


def _state(**overrides):
    """Create a station state with defaults."""
    return StationState(
        playlist=[],
        spotify_connected=overrides.get("spotify_connected", False),
    )


# --- Capabilities dataclass ---


def test_tier_demo():
    c = Capabilities()
    assert c.tier == "demo"
    assert c.tier_label == "On Air"


def test_tier_demo_ai():
    c = Capabilities(anthropic=True)
    assert c.tier == "demo_ai"
    assert c.tier_label == "On Air"


def test_tier_your_music_basic():
    c = Capabilities(spotify_connected=True)
    assert c.tier == "your_music_basic"
    assert c.tier_label == "Your Station"


def test_tier_your_music_full():
    c = Capabilities(spotify_connected=True, spotify_api=True)
    assert c.tier == "your_music_full"
    assert c.tier_label == "Your Station"


def test_tier_full_ai():
    c = Capabilities(spotify_connected=True, spotify_api=True, anthropic=True)
    assert c.tier == "full_ai"
    assert c.tier_label == "Live Broadcast"


def test_tier_connected_with_anthropic_no_api():
    """Connected + Anthropic but no Client ID = basic music with AI banter."""
    c = Capabilities(spotify_connected=True, anthropic=True)
    # spotify_connected alone = your_music_basic (anthropic doesn't upgrade spotify tier)
    assert c.tier == "your_music_basic"


def test_tier_all_flags():
    c = Capabilities(spotify_connected=True, spotify_api=True, anthropic=True, ha=True)
    assert c.tier == "full_ai"


def test_tier_ha_only():
    """HA alone doesn't change the tier — it's an ambient context addon."""
    c = Capabilities(ha=True)
    assert c.tier == "demo"


def test_capabilities_frozen():
    """Capabilities should be immutable."""
    c = Capabilities()
    try:
        c.spotify_connected = True  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except AttributeError:
        pass


# --- get_capabilities() ---


def test_get_capabilities_empty():
    caps = get_capabilities(_config(), _state())
    assert caps == Capabilities()


def test_get_capabilities_spotify_connected():
    caps = get_capabilities(_config(), _state(spotify_connected=True))
    assert caps.spotify_connected is True
    assert caps.spotify_api is False


def test_get_capabilities_spotify_api():
    caps = get_capabilities(
        _config(spotify_client_id="id", spotify_client_secret="secret"),
        _state(),
    )
    assert caps.spotify_connected is False
    assert caps.spotify_api is True


def test_get_capabilities_anthropic():
    caps = get_capabilities(_config(anthropic_api_key="sk-test"), _state())
    assert caps.anthropic is True


def test_get_capabilities_ha():
    caps = get_capabilities(
        _config(ha_token="token", ha_enabled=True),
        _state(),
    )
    assert caps.ha is True


def test_get_capabilities_ha_disabled():
    """HA token present but integration disabled = ha=False."""
    caps = get_capabilities(
        _config(ha_token="token", ha_enabled=False),
        _state(),
    )
    assert caps.ha is False


def test_get_capabilities_all_on():
    caps = get_capabilities(
        _config(
            spotify_client_id="id",
            spotify_client_secret="secret",
            anthropic_api_key="sk-test",
            ha_token="token",
            ha_enabled=True,
        ),
        _state(spotify_connected=True),
    )
    assert caps == Capabilities(spotify_connected=True, spotify_api=True, anthropic=True, ha=True)
    assert caps.tier == "full_ai"


# --- capabilities_to_dict() ---


def test_capabilities_to_dict_shape():
    caps = Capabilities(spotify_connected=True, anthropic=True)
    d = capabilities_to_dict(caps)
    assert "capabilities" in d
    assert "tier" in d
    assert "tier_label" in d
    assert d["capabilities"]["spotify_connected"] is True
    assert d["capabilities"]["spotify_api"] is False
    assert d["capabilities"]["anthropic"] is True
    assert d["capabilities"]["ha"] is False
    assert d["tier"] == "your_music_basic"
    assert d["tier_label"] == "Your Station"


# --- All 16 flag combinations produce valid tiers ---


def test_all_16_flag_combos():
    """Every combination of 4 boolean flags should produce a valid tier."""
    valid_tiers = {"demo", "demo_ai", "your_music_basic", "your_music_full", "full_ai"}
    for sc in (False, True):
        for sa in (False, True):
            for an in (False, True):
                for ha in (False, True):
                    c = Capabilities(spotify_connected=sc, spotify_api=sa, anthropic=an, ha=ha)
                    assert c.tier in valid_tiers, f"Invalid tier {c.tier!r} for flags sc={sc} sa={sa} an={an} ha={ha}"
                    assert isinstance(c.tier_label, str)
                    assert len(c.tier_label) > 0
