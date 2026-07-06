"""Tests for environment variable overrides in config.py (Docker/HA add-on support)."""

from __future__ import annotations

import logging
from pathlib import Path

from mammamiradio.core.config import GUEST_HOST_NAME, load_config, resolve_model

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def test_guest_host_present_by_default(monkeypatch):
    # The roster ships with the guest host; the switch defaults ON.
    monkeypatch.delenv("MAMMAMIRADIO_GUEST_HOST", raising=False)
    config = load_config(TOML_PATH)
    assert any(h.name == GUEST_HOST_NAME for h in config.hosts)
    assert any(h.engine_host == GUEST_HOST_NAME for h in config.brand.hosts)


def test_guest_host_disabled_drops_him_from_roster(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_GUEST_HOST", "false")
    config = load_config(TOML_PATH)
    assert all(h.name != GUEST_HOST_NAME for h in config.hosts)
    assert all(h.engine_host != GUEST_HOST_NAME for h in config.brand.hosts)
    # Regular hosts survive — only the guest is removed.
    assert len(config.hosts) >= 1
    assert any(h.engine_host != GUEST_HOST_NAME for h in config.brand.hosts)


def test_guest_host_enabled_explicit_keeps_him(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_GUEST_HOST", "true")
    config = load_config(TOML_PATH)
    assert any(h.name == GUEST_HOST_NAME for h in config.hosts)
    assert any(h.engine_host == GUEST_HOST_NAME for h in config.brand.hosts)


def test_ha_url_override(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://supervisor/core/api")
    config = load_config(TOML_PATH)
    assert config.homeassistant.url == "http://supervisor/core/api"


def test_ha_enabled_override(monkeypatch):
    monkeypatch.setenv("HA_ENABLED", "true")
    monkeypatch.setenv("HA_TOKEN", "test-token")
    config = load_config(TOML_PATH)
    assert config.homeassistant.enabled is True


def test_ha_enabled_false_override_blocks_auto_enable(monkeypatch):
    monkeypatch.setenv("HA_ENABLED", "false")
    monkeypatch.setenv("HA_TOKEN", "test-token")
    monkeypatch.setenv("HA_URL", "http://supervisor/core/api")
    config = load_config(TOML_PATH)
    assert config.homeassistant.enabled is False


def test_ha_context_enabled_env_disable(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_ENABLED", "false")
    config = load_config(TOML_PATH)
    assert config.homeassistant.context_enabled is False


def test_ha_context_enabled_env_invalid_ignored(monkeypatch, caplog):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_ENABLED", "sometimes")
    with caplog.at_level(logging.WARNING):
        config = load_config(TOML_PATH)
    assert config.homeassistant.context_enabled is True
    assert "Ignoring MAMMAMIRADIO_HA_CONTEXT_ENABLED" in caplog.text


def test_ha_context_poll_interval_env_override(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL", "600")
    config = load_config(TOML_PATH)
    assert config.homeassistant.poll_interval == 600


def test_ha_context_poll_interval_env_invalid_ignored(monkeypatch, caplog):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL", "soon")
    with caplog.at_level(logging.WARNING):
        config = load_config(TOML_PATH)
    assert config.homeassistant.poll_interval == 300
    assert "Ignoring MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL" in caplog.text


def test_ha_context_poll_interval_env_non_positive_ignored(monkeypatch, caplog):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL", "0")
    with caplog.at_level(logging.WARNING):
        config = load_config(TOML_PATH)
    assert config.homeassistant.poll_interval == 300
    assert "Ignoring MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL" in caplog.text


def test_ha_context_refresh_timeout_default():
    config = load_config(TOML_PATH)
    assert config.homeassistant.context_refresh_timeout == 2.0


def test_ha_context_refresh_timeout_env_override(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_REFRESH_TIMEOUT", "3.5")
    config = load_config(TOML_PATH)
    assert config.homeassistant.context_refresh_timeout == 3.5


def test_ha_context_refresh_timeout_env_non_float_ignored(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_REFRESH_TIMEOUT", "soon")
    config = load_config(TOML_PATH)
    assert config.homeassistant.context_refresh_timeout == 2.0


def test_ha_context_refresh_timeout_env_non_positive_ignored(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_REFRESH_TIMEOUT", "0")
    config = load_config(TOML_PATH)
    assert config.homeassistant.context_refresh_timeout == 2.0


def test_ha_context_refresh_timeout_env_infinite_ignored(monkeypatch):
    # inf would disable the deadline entirely — reject it, keep the default.
    monkeypatch.setenv("MAMMAMIRADIO_HA_CONTEXT_REFRESH_TIMEOUT", "inf")
    config = load_config(TOML_PATH)
    assert config.homeassistant.context_refresh_timeout == 2.0


def test_ha_mood_llm_env_enable(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_LLM", "true")
    config = load_config(TOML_PATH)
    assert config.homeassistant.mood_llm_enabled is True


def test_ha_mood_llm_env_disable(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_LLM", "false")
    config = load_config(TOML_PATH)
    assert config.homeassistant.mood_llm_enabled is False


def test_ha_mood_ttl_seconds_env_override(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_TTL_SECONDS", "45")
    config = load_config(TOML_PATH)
    assert config.homeassistant.mood_ttl_seconds == 45


def test_ha_mood_ttl_seconds_env_non_positive_ignored(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_TTL_SECONDS", "0")
    config = load_config(TOML_PATH)
    assert config.homeassistant.mood_ttl_seconds == 90.0


def test_ha_mood_ttl_seconds_env_non_integer_ignored(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_TTL_SECONDS", "soon")
    config = load_config(TOML_PATH)
    assert config.homeassistant.mood_ttl_seconds == 90.0


def test_ha_mood_ttl_seconds_env_infinite_ignored(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_TTL_SECONDS", "inf")
    config = load_config(TOML_PATH)
    assert config.homeassistant.mood_ttl_seconds == 90.0


def test_ha_mood_llm_env_garbage_warns_and_keeps_default(monkeypatch, caplog):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_LLM", "ture")
    with caplog.at_level(logging.WARNING, logger="mammamiradio.core.config"):
        config = load_config(TOML_PATH)
    assert config.homeassistant.mood_llm_enabled is False
    assert "Ignoring MAMMAMIRADIO_HA_MOOD_LLM='ture'" in caplog.text


def test_ha_mood_llm_warns_when_enabled_without_anthropic_key(monkeypatch, caplog):
    monkeypatch.setenv("MAMMAMIRADIO_HA_MOOD_LLM", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="mammamiradio.core.config"):
        config = load_config(TOML_PATH)

    assert config.homeassistant.mood_llm_enabled is True
    assert config.openai_api_key == "sk-openai-test"
    assert config.anthropic_api_key == ""
    assert "Home Assistant mood LLM enabled but no ANTHROPIC_API_KEY" in caplog.text


def test_ha_auto_enable_with_token_and_url(monkeypatch):
    """HA should auto-enable when both token and URL are present."""
    monkeypatch.setenv("HA_TOKEN", "test-token")
    monkeypatch.setenv("HA_URL", "http://supervisor/core/api")
    config = load_config(TOML_PATH)
    assert config.homeassistant.enabled is True
    assert config.ha_token == "test-token"


def test_ha_stays_disabled_without_url(monkeypatch):
    """HA should not auto-enable with just a token and no URL."""
    monkeypatch.setenv("HA_TOKEN", "test-token")
    monkeypatch.delenv("HA_URL", raising=False)
    config = load_config(TOML_PATH)
    assert config.homeassistant.enabled is False


def test_station_name_override(monkeypatch):
    monkeypatch.setenv("STATION_NAME", "Radio Test")
    config = load_config(TOML_PATH)
    assert config.station.name == "Radio Test"


def test_station_theme_override(monkeypatch):
    monkeypatch.setenv("STATION_THEME", "test theme")
    config = load_config(TOML_PATH)
    assert config.station.theme == "test theme"


def test_claude_model_override(monkeypatch):
    """CLAUDE_MODEL (back-compat) overrides the fast-role Anthropic model."""
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "transition", "anthropic") == "claude-sonnet-4-6"


def test_cache_dir_override(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_CACHE_DIR", "/data/cache")
    config = load_config(TOML_PATH)
    assert config.cache_dir == Path("/data/cache")


def test_tmp_dir_override(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_TMP_DIR", "/data/tmp")
    config = load_config(TOML_PATH)
    assert config.tmp_dir == Path("/data/tmp")


def test_defaults_without_overrides(monkeypatch):
    """Without env overrides, config uses radio.toml defaults."""
    monkeypatch.delenv("STATION_NAME", raising=False)
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("MAMMAMIRADIO_CACHE_DIR", raising=False)
    config = load_config(TOML_PATH)
    assert config.station.name == "Mamma Mi Radio"
    assert config.homeassistant.url == ""
    assert config.cache_dir == Path("cache")


def test_claude_creative_model_override(monkeypatch):
    """CLAUDE_CREATIVE_MODEL (back-compat) overrides the creative-role Anthropic model."""
    monkeypatch.setenv("CLAUDE_CREATIVE_MODEL", "claude-opus-4-6")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "banter", "anthropic") == "claude-opus-4-6"
    assert resolve_model(config.models, "transition", "anthropic") == "claude-haiku-4-5-20251001"


def test_claude_creative_model_override_under_economy_profile(monkeypatch):
    """CLAUDE_CREATIVE_MODEL must be honored even when the active profile maps
    creative to a different catalog key than the default profile (e.g. economy
    uses 'haiku' not 'opus' for creative — both must be patched).
    CLAUDE_MODEL is explicitly cleared so the fast override doesn't interfere
    with the creative→haiku key in the economy profile."""
    monkeypatch.setenv("CLAUDE_CREATIVE_MODEL", "claude-opus-4-6")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.setenv("MAMMAMIRADIO_QUALITY", "economy")
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "banter", "anthropic") == "claude-opus-4-6"
    assert resolve_model(config.models, "transition", "anthropic") == "claude-haiku-4-5-20251001"


def test_claude_model_override_under_economy_does_not_override_creative(monkeypatch):
    """CLAUDE_MODEL targets only the fast role even when economy normally shares
    the haiku catalog key between creative and fast."""
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    monkeypatch.delenv("CLAUDE_CREATIVE_MODEL", raising=False)
    monkeypatch.setenv("MAMMAMIRADIO_QUALITY", "economy")
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "transition", "anthropic") == "claude-sonnet-4-6"
    assert resolve_model(config.models, "banter", "anthropic") == "claude-haiku-4-5-20251001"


def test_openai_script_model_override(monkeypatch):
    """OPENAI_SCRIPT_MODEL (back-compat) overrides every OpenAI catalog entry, so it
    applies under any role."""
    monkeypatch.setenv("OPENAI_SCRIPT_MODEL", "gpt-5")
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "banter", "openai") == "gpt-5"
    assert resolve_model(config.models, "transition", "openai") == "gpt-5"


def test_broadcast_chain_env_disable(monkeypatch):
    """MAMMAMIRADIO_BROADCAST_CHAIN=false (HA add-on `broadcast_chain` option) turns the
    FM colouring off without editing the baked-in radio.toml — the addon's escape hatch
    to studio-clean output."""
    monkeypatch.setenv("MAMMAMIRADIO_BROADCAST_CHAIN", "false")
    config = load_config(TOML_PATH)
    assert config.audio.broadcast_chain is False


def test_broadcast_chain_env_enable_overrides_toml(monkeypatch, tmp_path):
    """env > toml proven against an explicit toml=false (not just the default): an
    operator who set broadcast_chain = false in radio.toml is overridden ON by the env
    var. Uses a tmp toml so the assertion can't pass merely because the repo default
    happens to be true."""
    toml_src = Path(TOML_PATH).read_text().replace("broadcast_chain = true", "broadcast_chain = false")
    toml_file = tmp_path / "radio.toml"
    toml_file.write_text(toml_src)
    # Sanity: with no env, the tmp toml's explicit false stands.
    monkeypatch.delenv("MAMMAMIRADIO_BROADCAST_CHAIN", raising=False)
    assert load_config(str(toml_file)).audio.broadcast_chain is False
    # env=true overrides the explicit toml false.
    monkeypatch.setenv("MAMMAMIRADIO_BROADCAST_CHAIN", "true")
    assert load_config(str(toml_file)).audio.broadcast_chain is True


def test_broadcast_chain_defaults_off_without_env(monkeypatch):
    """No env set → the radio.toml/default value stands (OFF by default — studio-clean;
    the FM colour is opt-in)."""
    monkeypatch.delenv("MAMMAMIRADIO_BROADCAST_CHAIN", raising=False)
    config = load_config(TOML_PATH)
    assert config.audio.broadcast_chain is False
