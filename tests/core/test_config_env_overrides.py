"""Tests for environment variable overrides in config.py (Docker/HA add-on support)."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.core.config import load_config

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
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250514")
    config = load_config(TOML_PATH)
    assert config.audio.claude_model == "claude-sonnet-4-5-20250514"


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
    monkeypatch.setenv("CLAUDE_CREATIVE_MODEL", "claude-opus-4-6")
    config = load_config(TOML_PATH)
    assert config.audio.claude_creative_model == "claude-opus-4-6"
