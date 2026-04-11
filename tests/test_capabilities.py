"""Tests for the capability flags system."""

from __future__ import annotations

from unittest.mock import MagicMock

from mammamiradio.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.models import Capabilities, StationState


def _config(**overrides):
    """Create a mock config with sensible defaults."""
    cfg = MagicMock()
    cfg.anthropic_api_key = overrides.get("anthropic_api_key", "")
    cfg.openai_api_key = overrides.get("openai_api_key", "")
    cfg.ha_token = overrides.get("ha_token", "")
    cfg.homeassistant.enabled = overrides.get("ha_enabled", False)
    return cfg


def _state(**overrides):
    """Create a station state with defaults."""
    return StationState(
        playlist=[],
    )


# --- Capabilities dataclass ---


def test_tier_demo():
    c = Capabilities()
    assert c.tier == "demo"
    assert c.tier_label == "Demo Radio"


def test_tier_full_ai():
    c = Capabilities(anthropic=True)
    assert c.tier == "full_ai"
    assert c.tier_label == "Full AI Radio"


def test_tier_connected_home():
    c = Capabilities(anthropic=True, ha=True)
    assert c.tier == "connected_home"


def test_tier_ha_only():
    """HA alone doesn't change the tier -- it's an ambient context addon."""
    c = Capabilities(ha=True)
    assert c.tier == "demo"


def test_capabilities_frozen():
    """Capabilities should be immutable."""
    c = Capabilities()
    try:
        c.anthropic = True  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except AttributeError:
        pass


# --- get_capabilities() ---


def test_get_capabilities_empty():
    caps = get_capabilities(_config(), _state())
    assert caps == Capabilities()


def test_get_capabilities_anthropic():
    caps = get_capabilities(_config(anthropic_api_key="sk-test"), _state())
    assert caps.anthropic is True


def test_get_capabilities_openai_only_counts_as_ai_enabled():
    caps = get_capabilities(_config(openai_api_key="sk-openai"), _state())
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
            anthropic_api_key="sk-test",
            ha_token="token",
            ha_enabled=True,
        ),
        _state(),
    )
    assert caps == Capabilities(anthropic=True, ha=True)


# --- capabilities_to_dict() ---


def test_capabilities_to_dict_shape():
    caps = Capabilities(anthropic=True)
    d = capabilities_to_dict(caps)
    assert "capabilities" in d
    assert "tier" in d
    assert "tier_label" in d
    assert d["capabilities"]["anthropic"] is True
    assert d["capabilities"]["ha"] is False
    assert d["tier"] == "full_ai"
    assert d["tier_label"] == "Full AI Radio"


# --- All flag combinations produce valid tiers ---


def test_all_4_flag_combos():
    """Every combination of 2 boolean flags should produce a valid tier."""
    valid_tiers = {"demo", "full_ai", "connected_home"}
    for an in (False, True):
        for ha in (False, True):
            c = Capabilities(anthropic=an, ha=ha)
            assert c.tier in valid_tiers, f"Invalid tier {c.tier!r} for flags an={an} ha={ha}"
            assert isinstance(c.tier_label, str)
            assert len(c.tier_label) > 0
