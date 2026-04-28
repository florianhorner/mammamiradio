"""Tests for the capability flags system."""

from __future__ import annotations

from unittest.mock import MagicMock

from mammamiradio.capabilities import capabilities_to_dict, get_capabilities, next_step
from mammamiradio.models import Capabilities, StationState


def _config(**overrides):
    """Create a mock config with sensible defaults."""
    cfg = MagicMock()
    cfg.anthropic_api_key = overrides.get("anthropic_api_key", "")
    cfg.openai_api_key = overrides.get("openai_api_key", "")
    cfg.ha_token = overrides.get("ha_token", "")
    cfg.homeassistant.enabled = overrides.get("ha_enabled", False)
    cfg.playlist.jamendo_client_id = overrides.get("jamendo_client_id", "")
    cfg.tts_degraded_voices = overrides.get("tts_degraded_voices", [])
    cfg.allow_ytdlp = overrides.get("allow_ytdlp", False)
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
    c = Capabilities(llm=True)
    assert c.tier == "full_ai"
    assert c.tier_label == "Full AI Radio"


def test_tier_connected_home():
    c = Capabilities(llm=True, ha=True)
    assert c.tier == "connected_home"


def test_tier_ha_only():
    """HA alone doesn't change the tier -- it's an ambient context addon."""
    c = Capabilities(ha=True)
    assert c.tier == "demo"


def test_capabilities_frozen():
    """Capabilities should be immutable."""
    c = Capabilities()
    try:
        c.llm = True  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except AttributeError:
        pass


# --- get_capabilities() ---


def test_get_capabilities_empty():
    caps = get_capabilities(_config(), _state())
    assert caps == Capabilities()


def test_get_capabilities_anthropic():
    caps = get_capabilities(_config(anthropic_api_key="sk-test"), _state())
    assert caps.llm is True


def test_get_capabilities_openai_only_counts_as_llm():
    caps = get_capabilities(_config(openai_api_key="sk-openai"), _state())
    assert caps.llm is True


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
    assert caps == Capabilities(llm=True, ha=True)


def test_get_capabilities_sets_jamendo_flag():
    caps = get_capabilities(_config(jamendo_client_id="jamendo-client"), _state())
    assert caps.jamendo is True


def test_get_capabilities_jamendo_flag_false_for_whitespace_client_id():
    caps = get_capabilities(_config(jamendo_client_id="   "), _state())
    assert caps.jamendo is False


def test_get_capabilities_sets_charts_reload_only_when_ytdlp_enabled():
    assert get_capabilities(_config(allow_ytdlp=False), _state()).charts_reload is False
    assert get_capabilities(_config(allow_ytdlp=True), _state()).charts_reload is True


# --- capabilities_to_dict() ---


def test_capabilities_to_dict_shape():
    caps = Capabilities(llm=True, jamendo=True, charts_reload=True)
    d = capabilities_to_dict(caps)
    assert "capabilities" in d
    assert "tier" in d
    assert "tier_label" in d
    assert d["capabilities"]["llm"] is True
    assert d["capabilities"]["ha"] is False
    assert d["capabilities"]["jamendo"] is True
    assert d["capabilities"]["charts_reload"] is True
    assert d["tier"] == "full_ai"
    assert d["tier_label"] == "Full AI Radio"


def test_next_step_add_llm_when_no_llm():
    step = next_step(Capabilities(llm=False, ha=False))
    assert step["key"] == "add_llm"
    assert step["action"] == "open_settings"


def test_next_step_all_set_when_llm_and_ha_enabled():
    step = next_step(Capabilities(llm=True, ha=True))
    assert step == {"key": "all_set", "message": "", "action": "none"}


# --- All flag combinations produce valid tiers ---


def test_all_4_flag_combos():
    """Every combination of 2 boolean flags should produce a valid tier."""
    valid_tiers = {"demo", "full_ai", "connected_home"}
    for llm in (False, True):
        for ha in (False, True):
            c = Capabilities(llm=llm, ha=ha)
            assert c.tier in valid_tiers, f"Invalid tier {c.tier!r} for flags llm={llm} ha={ha}"
            assert isinstance(c.tier_label, str)
            assert len(c.tier_label) > 0
