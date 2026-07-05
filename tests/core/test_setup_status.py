from __future__ import annotations

from unittest.mock import patch

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.core.setup_status import (
    _playlist_is_demo,
    build_guided_setup,
    build_setup_status,
    classify_station_mode,
    detect_run_mode,
)


def _demo_state() -> StationState:
    return StationState(
        playlist=[Track(title="Volare", artist="Demo", duration_ms=180_000, spotify_id="demo1")],
    )


def _real_state() -> StationState:
    return StationState(
        playlist=[Track(title="Real Song", artist="Artist", duration_ms=180_000, spotify_id="spotify123")],
    )


def test_classify_station_mode_demo():
    config = load_config()
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    mode = classify_station_mode(config, _demo_state())

    assert mode["id"] == "demo"
    assert "demo" in mode["summary"].lower() or "canned" in mode["summary"].lower()


def test_classify_station_mode_full_ai():
    config = load_config()
    config.anthropic_api_key = "sk-ant-test"

    mode = classify_station_mode(config, _real_state())

    assert mode["id"] == "full_ai"


def test_classify_station_mode_full_ai_with_openai_only():
    config = load_config()
    config.anthropic_api_key = ""
    config.openai_api_key = "sk-openai-test"

    mode = classify_station_mode(config, _real_state())

    assert mode["id"] == "full_ai"


def test_classify_station_mode_connected_home():
    config = load_config()
    config.anthropic_api_key = "sk-ant-test"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _real_state()
    state.ha_context = "- Coffee machine: on"

    mode = classify_station_mode(config, state)

    assert mode["id"] == "connected_home"


def test_classify_station_mode_ha_token_without_context_is_full_ai():
    config = load_config()
    config.anthropic_api_key = "sk-ant-test"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"

    mode = classify_station_mode(config, _real_state())

    assert mode["id"] == "full_ai"


def test_build_setup_status_returns_expected_shape_for_addon():
    config = load_config()
    config.is_addon = True
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _demo_state()

    payload = build_setup_status(config, state)

    assert payload["detected_mode"] == "ha_addon"
    assert payload["onboarding_required"] is False
    assert payload["station_mode"]["id"] == "demo"
    assert payload["guided_setup"]["stream"]["status"] == "ready"
    assert payload["guided_setup"]["ai_hosts"]["status"] == "missing"
    assert payload["guided_setup"]["home_context"]["status"] == "waiting_ai"
    assert payload["essentials"][0]["key"] == "llm_keys"
    assert payload["preflight_checks"][0]["key"] == "ffmpeg"
    assert "/config/secrets.env" in payload["addon_options_snippet"]
    assert "ANTHROPIC_API_KEY" in payload["addon_options_snippet"]
    assert "OPENAI_API_KEY" in payload["addon_options_snippet"]
    assert payload["onboarding_steps"]
    assert next(step for step in payload["onboarding_steps"] if step["id"] == "launch")["status"] == "done"
    assert payload["recommended_next_action"]
    assert payload["signature"]


def test_build_setup_status_non_demo_launch_copy():
    config = load_config()
    config.anthropic_api_key = "sk-ant-test"
    state = _real_state()

    with patch("mammamiradio.core.setup_status.detect_run_mode", return_value={"detected": "local", "modes": []}):
        payload = build_setup_status(config, state)

    assert payload["station_mode"]["id"] == "full_ai"
    assert "listener view" in payload["launch"]["post_launch"]


def test_guided_setup_openai_only_marks_ai_hosts_ready():
    config = load_config()
    config.openai_api_key = "sk-openai"

    guided = build_guided_setup(config, _real_state())

    assert guided["stream"]["status"] == "ready"
    assert guided["ai_hosts"]["status"] == "ready"


def test_guided_setup_rejected_single_key_marks_ai_hosts_rejected():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    provider_health = {
        "anthropic": {"key_status": "rejected"},
        "openai": {"key_status": "unverified"},
    }

    guided = build_guided_setup(config, _real_state(), provider_health=provider_health)

    assert guided["ai_hosts"]["status"] == "rejected"


def test_guided_setup_inconclusive_provider_status_keeps_ai_hosts_ready():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    provider_health = {
        "anthropic": {"key_status": "quota"},
        "openai": {"key_status": "unverified"},
    }

    guided = build_guided_setup(config, _real_state(), provider_health=provider_health)

    assert guided["ai_hosts"]["status"] == "ready"


def test_guided_setup_valid_second_key_wins_over_rejected_key():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.openai_api_key = "sk-openai"
    provider_health = {
        "anthropic": {"key_status": "rejected"},
        "openai": {"key_status": "valid"},
    }

    guided = build_guided_setup(config, _real_state(), provider_health=provider_health)

    assert guided["ai_hosts"]["status"] == "ready"


def test_guided_setup_connected_home_requires_safe_context():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _real_state()

    assert build_guided_setup(config, state)["home_context"]["status"] == "checking"

    state.ha_context = "- Coffee machine: on"
    assert build_guided_setup(config, state)["home_context"]["status"] == "ready"


def test_guided_setup_home_context_empty_after_successful_empty_fetch():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _real_state()
    state.ha_context_last_updated = 123.0

    guided = build_guided_setup(config, state)

    assert guided["home_context"]["status"] == "empty"


def test_guided_setup_stream_stopped_and_no_source_states():
    config = load_config()
    state = StationState()
    state.session_stopped = True

    assert build_guided_setup(config, state)["stream"]["status"] == "stopped"

    state.session_stopped = False
    config.allow_ytdlp = True
    assert build_guided_setup(config, state)["stream"]["status"] == "checking"

    config.allow_ytdlp = False
    assert build_guided_setup(config, state)["stream"]["status"] == "blocked"


# --- detect_run_mode ---


def test_detect_run_mode_addon():
    config = load_config()
    config.is_addon = True
    result = detect_run_mode(config)
    assert result["detected"] == "ha_addon"


def test_detect_run_mode_docker():
    config = load_config()
    config.is_addon = False
    with patch("mammamiradio.core.setup_status.Path.exists", return_value=True):
        result = detect_run_mode(config)
    assert result["detected"] == "docker"


def test_detect_run_mode_macos():
    config = load_config()
    config.is_addon = False
    with (
        patch("mammamiradio.core.setup_status.Path.exists", return_value=False),
        patch("mammamiradio.core.setup_status.platform.system", return_value="Darwin"),
    ):
        result = detect_run_mode(config)
    assert result["detected"] == "macos"


def test_detect_run_mode_local_fallback():
    config = load_config()
    config.is_addon = False
    with (
        patch("mammamiradio.core.setup_status.Path.exists", return_value=False),
        patch("mammamiradio.core.setup_status.platform.system", return_value="Linux"),
    ):
        result = detect_run_mode(config)
    assert result["detected"] == "local"


# --- _playlist_is_demo ---


def test_playlist_is_demo_empty():
    state = StationState(playlist=[])
    assert _playlist_is_demo(state) is True


def test_playlist_is_demo_none():
    state = StationState(playlist=None)
    assert _playlist_is_demo(state) is True
