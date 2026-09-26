"""Tests for environment variable overrides in config.py (Docker/HA add-on support)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

# Captured while pytest is still importing this module, i.e. during collection.
# That is when core/config.py's module-level load_dotenv() runs, so this is the
# only moment at which the switch matters. If the assignment in tests/conftest.py
# ever moves into a fixture, this reads None and the ordering test below fails.
_DOTENV_FLAG_AT_COLLECTION = os.environ.get("PYTHON_DOTENV_DISABLED")

from mammamiradio.core.config import GUEST_HOST_NAME, load_config, resolve_model  # noqa: E402

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
    assert config.brand.station_name == "Radio Test"
    assert config.display_station_name == "Radio Test"
    assert config.identity.station_name == "Radio Test"
    assert config.identity.source == "env"
    assert config.identity.custom_copy_preserved is False
    assert "Radio Test" in config.sonic_brand.full_ident
    assert any("Radio Test" in line for line in config.sonic_brand.sweepers)


def test_station_name_override_sanitizes_controls(monkeypatch):
    monkeypatch.setenv("STATION_NAME", "Radio Test\r\nX-Evil: 1")
    config = load_config(TOML_PATH)
    assert config.display_station_name == "Radio Test X-Evil: 1"
    assert "\r" not in config.display_station_name
    assert "\n" not in config.display_station_name


def test_station_name_override_preserves_custom_sonic_copy(monkeypatch, tmp_path):
    source = Path(TOML_PATH)
    custom = source.read_text().replace(
        'full_ident = "Mamma Mi Radio... da Windor a Vergen, la voce che non si spegne mai!"',
        'full_ident = "Old Custom Radio... handcrafted station id!"',
    )
    custom = custom.replace(
        '"Mamma Mi Radio.",',
        '"Old Custom Radio.",',
        1,
    )
    custom_path = tmp_path / "radio.toml"
    custom_path.write_text(custom)

    monkeypatch.setenv("STATION_NAME", "Radio Test")
    config = load_config(str(custom_path))

    assert config.display_station_name == "Radio Test"
    assert config.brand.station_name == "Radio Test"
    assert config.sonic_brand.full_ident == "Old Custom Radio... handcrafted station id!"
    assert "Old Custom Radio." in config.sonic_brand.sweepers
    assert config.identity.custom_copy_preserved is True
    assert any("custom identity copy preserved" in warning for warning in config.brand_warnings)


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


# Normalization cache ceiling
# A 500 MB ceiling left more than half of a 200-track rotation cold and caused
# repeated ~65-second renders on HA Green. The add-on now defaults to 1500 MB.
# Config parsing must not raise because it runs during startup.


def test_max_cache_size_defaults_to_500_standalone(monkeypatch):
    monkeypatch.delenv("MAMMAMIRADIO_MAX_CACHE_MB", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    assert load_config(TOML_PATH).max_cache_size_mb == 500


def test_max_cache_size_defaults_to_1500_on_addon(monkeypatch):
    monkeypatch.delenv("MAMMAMIRADIO_MAX_CACHE_MB", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-abc")
    config = load_config(TOML_PATH)
    assert config.is_addon is True
    assert config.max_cache_size_mb == 1500


def test_max_cache_size_env_override_wins(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_MAX_CACHE_MB", "2400")
    assert load_config(TOML_PATH).max_cache_size_mb == 2400


def test_max_cache_size_garbage_degrades_to_default_without_raising(monkeypatch, caplog):
    """Malformed input uses the default and logs a warning instead of aborting."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    monkeypatch.setenv("MAMMAMIRADIO_MAX_CACHE_MB", "1500MB")
    with caplog.at_level(logging.WARNING):
        config = load_config(TOML_PATH)
    assert config.max_cache_size_mb == 500
    assert "MAMMAMIRADIO_MAX_CACHE_MB" in caplog.text


def test_max_cache_size_clamps_below_minimum(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_MAX_CACHE_MB", "10")
    assert load_config(TOML_PATH).max_cache_size_mb == 200


def test_max_cache_size_clamps_above_maximum(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_MAX_CACHE_MB", "999999")
    assert load_config(TOML_PATH).max_cache_size_mb == 8000


def test_max_cache_size_negative_clamps_not_raises(monkeypatch):
    monkeypatch.setenv("MAMMAMIRADIO_MAX_CACHE_MB", "-1")
    assert load_config(TOML_PATH).max_cache_size_mb == 200


def test_dotenv_switch_is_set_before_collection() -> None:
    """tests/conftest.py must set PYTHON_DOTENV_DISABLED at module level.

    core/config.py:37 loads .env at import time, which happens while pytest
    collects test modules. A fixture would run after that and leave the leak in
    place while every other test stays green.
    """
    assert _DOTENV_FLAG_AT_COLLECTION == "1", (
        "PYTHON_DOTENV_DISABLED was not set when this module was collected. "
        "Keep the assignment at module level in tests/conftest.py, not in a fixture."
    )


_LEAK_PROBE = "import os\nimport mammamiradio.core.config\nprint(os.environ.get('MAMMAMIRADIO_TEST_LEAK_PROBE'))\n"


def _import_config_in_subprocess(cwd: Path, env: dict[str, str]) -> str:
    # `python -c` on purpose: with no __main__.__file__, python-dotenv's
    # find_dotenv() walks up from the cwd, so it sees the probe .env planted in
    # tmp_path. A script file (or pytest itself) walks up from core/config.py's
    # directory instead and would read the developer's real repo-root .env,
    # which this test must never touch.
    result = subprocess.run(
        [sys.executable, "-c", _LEAK_PROBE],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip()


def test_dotenv_switch_blocks_a_planted_env_file(tmp_path: Path) -> None:
    """The inherited switch must stop config.py from loading a .env at import.

    Runs both directions so the probe itself is proven: with the switch the
    planted key is absent, without it the key appears. A probe that could not
    detect a leak would pass the first half for the wrong reason.
    """
    (tmp_path / ".env").write_text("MAMMAMIRADIO_TEST_LEAK_PROBE=leaked\n", encoding="utf-8")

    inherited = dict(os.environ)
    inherited.pop("MAMMAMIRADIO_TEST_LEAK_PROBE", None)
    assert inherited.get("PYTHON_DOTENV_DISABLED") == "1"
    assert _import_config_in_subprocess(tmp_path, inherited) == "None", (
        "core/config.py loaded a .env despite PYTHON_DOTENV_DISABLED=1. "
        "python-dotenv >= 1.2 is required for the switch; run "
        ".venv/bin/pip install -U 'python-dotenv>=1.2' and re-run."
    )

    without_switch = {k: v for k, v in inherited.items() if k != "PYTHON_DOTENV_DISABLED"}
    assert _import_config_in_subprocess(tmp_path, without_switch) == "leaked", (
        "The probe .env was not loaded without the switch, so this test cannot "
        "detect a leak. Check find_dotenv() behaviour or the probe file."
    )


def test_exported_runtime_settings_do_not_change_config_tests() -> None:
    """The test session must isolate HA and port settings from the parent shell."""
    env = dict(os.environ)
    env.update(
        {
            "HA_ENABLED": "true",
            "HA_URL": "http://127.0.0.1:9",
            "MAMMAMIRADIO_BIND_HOST": "0.0.0.0",
            "MAMMAMIRADIO_PORT": "9001",
        }
    )
    repo_root = Path(__file__).resolve().parents[2]
    ha_test = f"{Path(__file__).resolve()}::test_ha_stays_disabled_without_url"
    port_test = repo_root / "tests/repo/test_stream_watch_server.py"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", ha_test, f"{port_test}::test_upstream_base_url_uses_runtime_port"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
