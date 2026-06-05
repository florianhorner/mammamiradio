"""Tests for environment variable overrides in config.py (Docker/HA add-on support)."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.core.config import load_config, resolve_model

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


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
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "banter", "anthropic") == "claude-opus-4-6"


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


def test_openai_script_model_override(monkeypatch):
    """OPENAI_SCRIPT_MODEL (back-compat) overrides every OpenAI catalog entry, so it
    applies under any role."""
    monkeypatch.setenv("OPENAI_SCRIPT_MODEL", "gpt-5")
    config = load_config(TOML_PATH)
    assert resolve_model(config.models, "banter", "openai") == "gpt-5"
    assert resolve_model(config.models, "transition", "openai") == "gpt-5"
