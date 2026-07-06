from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.core.setup_status import (
    _home_context_copy,
    _playlist_is_demo,
    _stream_status,
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


def test_classify_station_mode_stale_context_without_ha_access_is_full_ai():
    config = load_config()
    config.anthropic_api_key = "sk-ant-test"
    config.homeassistant.enabled = False
    config.ha_token = ""
    state = _real_state()
    state.ha_context = "- Coffee machine: on"

    mode = classify_station_mode(config, state)

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
    strip = payload["guided_setup"]["strip"]
    assert strip["attention_required"] is True
    assert strip["primary_action"] == {"kind": "add_ai_key", "label": "Add AI key", "target": "setup"}
    assert [item["id"] for item in strip["items"]] == ["stream", "ai_hosts", "home_context"]
    assert strip["items"][0]["display_status"] == "Ready"
    assert strip["items"][0]["shape"] == "ok"
    assert strip["items"][1]["tone"] == "warn"
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
    assert guided["strip"]["attention_required"] is False
    assert guided["strip"]["primary_action"] == {"kind": "open_listener", "label": "Open listener", "target": "listen"}


def test_guided_setup_primary_action_prioritizes_stream_attention():
    config = load_config()
    config.allow_ytdlp = False
    state = StationState()

    guided = build_guided_setup(config, state)

    assert guided["stream"]["status"] == "blocked"
    assert guided["strip"]["primary_action"] == {"kind": "fix_stream", "label": "Fix stream", "target": "setup"}


def test_guided_setup_primary_action_prompts_ai_key_when_stream_ready():
    config = load_config()
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    guided = build_guided_setup(config, _real_state())

    assert guided["ai_hosts"]["status"] == "missing"
    assert guided["strip"]["primary_action"] == {"kind": "add_ai_key", "label": "Add AI key", "target": "setup"}


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
    guided = build_guided_setup(config, state)
    assert guided["home_context"]["status"] == "ready"
    assert guided["home_context"]["readiness"] == "prompt_ready"
    assert guided["strip"]["primary_action"] == {
        "kind": "review_home_context",
        "label": "Review home context",
        "target": "setup",
    }


def test_guided_setup_home_context_empty_after_successful_empty_fetch():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _real_state()
    state.ha_context_last_updated = 123.0

    guided = build_guided_setup(config, state)

    assert guided["home_context"]["status"] == "empty"
    assert guided["home_context"]["readiness"] == "empty"
    assert guided["strip"]["primary_action"] == {
        "kind": "review_home_context",
        "label": "Review home context",
        "target": "setup",
    }


def test_guided_setup_standalone_station_without_ha_is_not_configured_not_blocked():
    """Home Assistant is an optional upgrade — a station that never turns it
    on must not show a permanent 'blocked' (error-styled) home context chip."""
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    state = _real_state()

    guided = build_guided_setup(config, state)

    assert guided["home_context"]["status"] == "not_configured"
    assert guided["home_context"]["readiness"] == "disabled"
    assert guided["home_context"]["action"] == "none"
    assert guided["strip"]["items"][2]["display_status"] == "Optional"


def test_guided_setup_home_context_disabled_is_not_stuck_collecting():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "ha-token"
    state = _real_state()

    guided = build_guided_setup(config, state)

    assert guided["home_context"]["status"] == "not_configured"
    assert guided["home_context"]["readiness"] == "disabled"
    assert guided["home_context"]["homeassistant_access"] is True
    assert guided["home_context"]["action"] == "none"
    assert guided["strip"]["items"][2]["display_status"] == "Optional"


def test_guided_setup_ha_enabled_without_token_still_blocked():
    """An operator who DID turn on HA but hasn't finished configuring it
    keeps the real 'blocked' signal — only the never-configured case changes."""
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = ""
    state = _real_state()

    guided = build_guided_setup(config, state)
    assert guided["home_context"]["status"] == "blocked"
    assert guided["home_context"]["readiness"] == "access_missing"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            "blocked",
            {
                "status": "blocked",
                "headline": "Home Assistant needs access.",
                "detail": "Set HA_TOKEN",
                "action": "wait",
                "display_status": "Needs setup",
            },
        ),
        (
            "empty",
            {
                "status": "empty",
                "headline": "Home Assistant is connected, but no prompt-safe context is available.",
                "detail": "Home context preview",
                "action": "review_home_context",
                "display_status": "No safe context yet",
            },
        ),
        (
            "checking",
            {
                "status": "checking",
                "headline": "Home context is still collecting.",
                "detail": "next Home Assistant refresh",
                "action": "wait",
                "display_status": "Checking",
            },
        ),
        (
            "waiting_ai",
            {
                "status": "waiting_ai",
                "headline": "Home context waits for AI hosts.",
                "detail": "Add an Anthropic or OpenAI key",
                "action": "wait",
                "display_status": "Waiting for AI",
            },
        ),
    ],
)
def test_guided_setup_home_context_copy_matches_state(case, expected):
    config = load_config()
    state = _real_state()
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    config.anthropic_api_key = "sk-ant"

    if case == "blocked":
        config.ha_token = ""
    elif case == "empty":
        state.ha_context_last_updated = 123.0
    elif case == "waiting_ai":
        config.anthropic_api_key = ""
        config.openai_api_key = ""

    guided = build_guided_setup(config, state)

    home_context = guided["home_context"]
    assert home_context["status"] == expected["status"]
    assert home_context["headline"] == expected["headline"]
    assert expected["detail"] in home_context["detail"]
    assert home_context["action"] == expected["action"]
    assert guided["strip"]["items"][2]["display_status"] == expected["display_status"]


def test_home_context_copy_has_defensive_fallback():
    config = load_config()

    headline, detail = _home_context_copy(config, "unexpected_state")

    assert headline == "Review Home Assistant context."
    assert detail == "Check Home Assistant access and the context preview."


def test_guided_setup_addon_blocked_copy_does_not_show_standalone_env_vars():
    config = load_config()
    config.is_addon = True
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = ""

    guided = build_guided_setup(config, _real_state())

    assert guided["home_context"]["headline"] == "Home Assistant needs access."
    assert "add-on configuration" in guided["home_context"]["detail"]
    assert "HA_TOKEN" not in guided["home_context"]["detail"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("now_streaming", {"type": "music", "label": "On air"}),
        ("queued_segments", [{"type": "music", "label": "Queued"}]),
        ("playlist", [Track(title="Loaded", artist="Artist", duration_ms=180_000, spotify_id="loaded")]),
        ("last_music_file", Path("cache/song.mp3")),
    ],
)
def test_stream_status_playable_runtime_wins_over_blocking_golden_path(field, value):
    config = load_config()
    config.allow_ytdlp = False
    state = StationState()
    setattr(state, field, value)

    assert _stream_status(config, state, golden_path={"blocking": True}) == "ready"


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
