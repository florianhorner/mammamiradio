from __future__ import annotations

from unittest.mock import patch

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.core.setup_status import (
    _playlist_is_demo,
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

    mode = classify_station_mode(config, _real_state())

    assert mode["id"] == "connected_home"


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
    assert payload["onboarding_required"] is True
    assert payload["station_mode"]["id"] == "demo"
    assert payload["essentials"][0]["key"] == "llm_keys"
    assert payload["preflight_checks"][0]["key"] == "ffmpeg"
    assert "anthropic_api_key" in payload["addon_options_snippet"]
    assert "openai_api_key" in payload["addon_options_snippet"]
    assert payload["onboarding_steps"]
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
