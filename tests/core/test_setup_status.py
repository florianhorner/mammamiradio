from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.first_listen import FirstListenReceiptV1
from mammamiradio.core.models import PlaylistSource, ScoredEntityStatus, StationState, Track
from mammamiradio.core.setup_status import (
    _home_context_copy,
    _playlist_is_demo,
    _setup_status_shape,
    _stream_status,
    build_guided_setup,
    build_setup_status,
    classify_station_mode,
    detect_run_mode,
    first_listen_onboarding_active,
    has_safe_home_context,
    home_context_availability,
    home_context_value,
)


def _demo_state() -> StationState:
    return StationState(
        playlist=[Track(title="Volare", artist="Demo", duration_ms=180_000, spotify_id="demo1")],
    )


def _real_state() -> StationState:
    return StationState(
        playlist=[Track(title="Real Song", artist="Artist", duration_ms=180_000, spotify_id="spotify123")],
    )


def _scored_entity(entity_id: str) -> ScoredEntityStatus:
    return {
        "entity_id": entity_id,
        "area": None,
        "domain": entity_id.partition(".")[0],
        "score": 0.0,
        "state": None,
        "label": entity_id,
        "label_tier": "fallback",
        "summary": entity_id,
        "device_class": None,
    }


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
    homeassistant = next(item for item in payload["essentials"] if item["key"] == "homeassistant")
    assert homeassistant["status"] == "warn"
    assert "AI hosts are needed" in homeassistant["summary"]
    strip = payload["guided_setup"]["strip"]
    assert strip["attention_required"] is True
    assert strip["primary_action"] == {"kind": "add_ai_key", "label": "Add AI key", "target": "setup"}
    assert [item["id"] for item in strip["items"]] == ["stream", "ai_hosts", "home_context"]
    assert strip["items"][0]["display_status"] == "Ready"
    assert strip["items"][0]["shape"] == "ok"
    assert strip["items"][1]["tone"] == "warn"
    assert payload["identity"]["station_name"] == config.display_station_name
    assert payload["identity"]["preview"]["heard_on_air"]
    assert payload["identity"]["stable_ids"]["media_player"] == "media_player.mammamiradio"
    assert payload["onboarding_steps"][0]["id"] == "identity"
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
    assert guided["strip"]["primary_action"] == {
        "kind": "repair_music_source",
        "label": "Repair music source",
        "target": "setup",
        "focus": "source",
    }


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


def test_sun_only_context_is_ambient_only_not_connected_home() -> None:
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = True
    config.ha_token = "ha-token"
    state = _real_state()
    state.ha_context = "- Daylight: above horizon"
    state.ha_context_last_updated = 123.0
    state.ha_context_entity_count = 1
    state.ha_scored_entities = [_scored_entity("sun.ambient")]

    availability = home_context_availability(config, state)

    assert home_context_value(state) == "ambient_only"
    assert availability.readiness == "empty"
    assert availability.home_context_ready is False
    assert classify_station_mode(config, state)["id"] == "full_ai"


@pytest.mark.parametrize("daylight_id", ["sun.ambient", "sun.sun"])
def test_weather_plus_daylight_is_meaningful_context(daylight_id: str) -> None:
    config = load_config()
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = True
    config.ha_token = "ha-token"
    state = _real_state()
    state.ha_context = "- Weather: cloudy, 20°C\n- Daylight: above horizon"
    state.ha_context_last_updated = 123.0
    state.ha_context_entity_count = 2
    state.ha_scored_entities = [
        _scored_entity(daylight_id),
        _scored_entity("weather.ambient"),
    ]

    availability = home_context_availability(config, state)

    assert home_context_value(state) == "useful"
    assert availability.readiness == "prompt_ready"
    assert availability.home_context_ready is True


def test_has_safe_home_context_requires_useful_radio_value() -> None:
    useful = _real_state()
    useful.ha_scored_entities = [_scored_entity("weather.ambient")]

    assert has_safe_home_context(useful) is True
    assert has_safe_home_context(_real_state()) is False


def test_unrecognized_install_origin_fails_closed_until_onboarding_is_complete() -> None:
    assert first_listen_onboarding_active("malformed", audio_complete=False, privacy_complete=False) is True
    assert first_listen_onboarding_active("malformed", audio_complete=True, privacy_complete=True) is False


def test_onboarding_is_never_mandatory_without_home_assistant_access() -> None:
    """A standalone install cannot complete the speaker check, so it is not owed one."""
    assert (
        first_listen_onboarding_active("fresh", audio_complete=False, privacy_complete=False, ha_access_available=False)
        is False
    )
    assert (
        first_listen_onboarding_active(
            "unknown", audio_complete=False, privacy_complete=False, ha_access_available=False
        )
        is False
    )
    assert (
        first_listen_onboarding_active("fresh", audio_complete=False, privacy_complete=False, ha_access_available=True)
        is True
    )


def test_build_setup_status_homeassistant_essential_requires_prompt_safe_context():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _real_state()

    payload = build_setup_status(config, state)
    homeassistant = next(item for item in payload["essentials"] if item["key"] == "homeassistant")

    assert payload["guided_setup"]["home_context"]["status"] == "checking"
    assert homeassistant["status"] == "warn"
    assert "still being collected" in homeassistant["summary"]
    assert "available for references" not in homeassistant["summary"]

    state.ha_context = "- Coffee machine: on"
    payload = build_setup_status(config, state)
    homeassistant = next(item for item in payload["essentials"] if item["key"] == "homeassistant")

    assert payload["guided_setup"]["home_context"]["status"] == "ready"
    assert homeassistant["status"] == "configured"
    assert "available for references" in homeassistant["summary"]


def test_build_setup_status_homeassistant_essential_marks_missing_token():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = ""
    state = _real_state()

    payload = build_setup_status(config, state)
    homeassistant = next(item for item in payload["essentials"] if item["key"] == "homeassistant")

    assert payload["guided_setup"]["home_context"]["status"] == "blocked"
    assert homeassistant["status"] == "missing"
    assert "no token" in homeassistant["summary"]


def test_build_setup_status_homeassistant_essential_marks_empty_context():
    config = load_config()
    config.anthropic_api_key = "sk-ant"
    config.homeassistant.enabled = True
    config.ha_token = "ha-token"
    state = _real_state()
    state.ha_context_last_updated = 123.0

    payload = build_setup_status(config, state)
    homeassistant = next(item for item in payload["essentials"] if item["key"] == "homeassistant")

    assert payload["guided_setup"]["home_context"]["status"] == "empty"
    assert homeassistant["status"] == "warn"
    assert "no prompt-safe context" in homeassistant["summary"]


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
def test_stream_status_golden_path_wins_over_transient_runtime_state(field, value):
    config = load_config()
    config.allow_ytdlp = False
    state = StationState()
    setattr(state, field, value)

    assert _stream_status(config, state, golden_path={"blocking": True}) == "blocked"


def test_guided_setup_stream_pause_is_separate_from_source_readiness():
    config = load_config()
    state = StationState()
    state.session_stopped = True

    assert build_guided_setup(config, state, golden_path={"blocking": False})["stream"]["status"] == "ready"
    assert build_guided_setup(config, state, golden_path={"blocking": True})["stream"]["status"] == "blocked"

    state.session_stopped = False
    config.allow_ytdlp = True
    assert build_guided_setup(config, state)["stream"]["status"] == "checking"

    config.allow_ytdlp = False
    assert build_guided_setup(config, state)["stream"]["status"] == "blocked"


@pytest.mark.parametrize(
    ("playlist_source", "playlist", "allow_ytdlp", "expected"),
    [
        (PlaylistSource(kind="charts", label="Charts"), [], False, "ready"),
        (None, [Track(title="Loaded", artist="Artist", duration_ms=180_000)], False, "ready"),
        (None, [], True, "checking"),
        (None, [], False, "blocked"),
    ],
)
def test_stream_status_without_golden_path_uses_source_readiness(
    playlist_source,
    playlist,
    allow_ytdlp,
    expected,
):
    config = load_config()
    config.allow_ytdlp = allow_ytdlp
    state = StationState()
    state.playlist_source = playlist_source
    state.playlist = playlist

    assert _stream_status(config, state) == expected


def test_setup_status_shape_does_not_classify_runtime_pause_as_setup_failure():
    assert _setup_status_shape("stopped") == {
        "tone": "warn",
        "shape": "warn",
        "display_status": "Stopped",
    }


def test_setup_status_shape_describes_backup_audio_without_calling_it_ready():
    assert _setup_status_shape("degraded") == {
        "tone": "warn",
        "shape": "warn",
        "display_status": "Backup audio available",
    }


def test_build_setup_status_identity_preview_uses_station_name(monkeypatch):
    monkeypatch.setenv("STATION_NAME", "Radio Test")
    config = load_config()
    state = _demo_state()

    payload = build_setup_status(config, state)

    assert payload["identity"]["station_name"] == "Radio Test"
    assert payload["identity"]["source"] == "env"
    assert payload["identity"]["preview"]["heard_on_air"].startswith("Radio Test")
    assert "mammamiradio" in payload["identity"]["stable_ids"]["integration_domain"]


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


def _first_listen_source_projection(
    *, jamendo_status: str = "not_configured", recovery_status: str = "cover_only"
) -> dict:
    sources = {}
    for kind, status in (
        ("charts", "unavailable"),
        ("jamendo", jamendo_status),
        ("local", "not_configured"),
        ("demo", "not_bundled"),
        ("recovery", recovery_status),
    ):
        sources[kind] = {
            "kind": kind,
            "label": kind.title(),
            "status": status,
            "detail": f"{kind} detail",
            "configured": status != "not_configured",
            "attempted": status not in {"not_configured", "not_bundled", "cover_only"},
            "candidates": 2 if status == "candidates_only" else 0,
            "playable": 1 if status in {"playable", "on_air"} else 0,
            "on_air": status == "on_air",
            "failure": "source unavailable" if status == "unavailable" else "",
        }
    return {
        "source_revision": 0,
        "sources": sources,
        "current_rotation": {},
        "advanced": None,
        "programming_ready": jamendo_status in {"playable", "on_air"},
        "recovery_cover_available": recovery_status in {"cover_only", "on_air"},
        "recovery_on_air": False,
        "transport_only": False,
    }


def test_stream_status_keeps_primary_music_amber_when_recovery_is_available():
    config = load_config()
    config.allow_ytdlp = False

    status = _stream_status(
        config,
        StationState(),
        golden_path={
            "blocking": True,
            "source_readiness": {
                "programming_ready": False,
                "recovery_cover_available": True,
            },
        },
    )

    assert status == "degraded"


def test_fresh_completed_milestones_stay_active_without_continuity():
    config = load_config()
    config.is_addon = True
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "supervisor-token"
    receipt = FirstListenReceiptV1(
        selected_entity_id="media_player.kitchen",
        accepted_attempt_id="accepted-attempt-1234",
        accepted_at=100.0,
        heard_at=101.0,
        privacy_reviewed_at=102.0,
    )

    guided = build_guided_setup(
        config,
        _demo_state(),
        golden_path={
            "blocking": True,
            "source_readiness": _first_listen_source_projection(recovery_status="unavailable"),
        },
        first_listen_receipt=receipt,
        install_origin="fresh",
        context_choice_explicit=True,
    )

    assert guided["first_listen"]["continuity_available"] is False
    assert guided["first_listen"]["show_ai"] is False
    assert guided["strip"]["items"][0]["status"] == "blocked"
    assert guided["strip"]["primary_action"]["kind"] == "repair_music_source"


def test_recommended_next_action_reassures_on_backup_audio_after_onboarding():
    """Regression: this branch was reachable but had zero test coverage.

    Once First Listen onboarding is already behind an install (or never
    required), a degraded stream backed by a usable recovery cover must get
    the reassuring "backup audio" copy, not the generic "fix stream
    readiness" fallback meant for a station with no audio at all.
    """
    config = load_config()
    config.allow_ytdlp = False

    setup = build_setup_status(
        config,
        StationState(),
        golden_path={
            "blocking": True,
            "source_readiness": _first_listen_source_projection(recovery_status="cover_only"),
        },
        install_origin="existing",
    )

    assert setup["guided_setup"]["stream"]["status"] == "degraded"
    assert setup["recommended_next_action"] == (
        "Repair the primary music source; backup audio is keeping the station playing."
    )


def test_fresh_no_key_setup_orders_speaker_and_privacy_before_optional_ai():
    config = load_config()
    config.is_addon = True
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "supervisor-token"
    golden_path = {
        "blocking": True,
        "source_readiness": _first_listen_source_projection(),
    }

    setup = build_setup_status(
        config,
        _demo_state(),
        golden_path=golden_path,
        install_origin="fresh",
        context_choice_explicit=False,
    )

    guided = setup["guided_setup"]
    assert [item["id"] for item in guided["strip"]["items"]] == [
        "source_readiness",
        "speaker",
        "verification",
        "privacy",
    ]
    assert guided["strip"]["primary_action"] == {
        "kind": "find_speaker",
        "label": "Find speaker",
        "target": "setup",
    }
    assert guided["first_listen"]["show_ai"] is False
    assert guided["ai_hosts"]["status"] == "missing"
    assert guided["privacy"]["enabled"] is False
    assert setup["recommended_next_action"].startswith("Find a Home Assistant speaker")
    assert setup["onboarding_required"] is True


def test_unknown_install_origin_keeps_compatibility_layout_after_onboarding_complete():
    config = load_config()
    config.is_addon = False
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "ha-token"
    receipt = FirstListenReceiptV1(
        selected_entity_id="media_player.kitchen",
        accepted_attempt_id="abcdefghijklmnop",
        accepted_at=100.0,
        heard_at=101.0,
        privacy_reviewed_at=102.0,
    )

    guided = build_guided_setup(
        config,
        _demo_state(),
        golden_path={"blocking": True, "source_readiness": _first_listen_source_projection()},
        install_origin="unknown",
        first_listen_receipt=receipt,
        context_choice_explicit=False,
    )

    assert guided["first_listen"]["fresh_install"] is False
    assert guided["first_listen"]["show_ai"] is True
    assert [item["id"] for item in guided["strip"]["items"]] == [
        "stream",
        "ai_hosts",
        "home_context",
    ]


def test_unknown_install_origin_routes_unproven_installs_through_first_listen():
    config = load_config()
    config.is_addon = False
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "ha-token"

    guided = build_guided_setup(
        config,
        _demo_state(),
        golden_path={"blocking": True, "source_readiness": _first_listen_source_projection()},
        install_origin="unknown",
        context_choice_explicit=False,
    )

    assert guided["first_listen"]["fresh_install"] is False
    assert guided["first_listen"]["show_ai"] is False
    assert guided["privacy"]["enabled"] is False
    assert guided["privacy"]["choice_explicit"] is False
    assert [item["id"] for item in guided["strip"]["items"]] == [
        "source_readiness",
        "speaker",
        "verification",
        "privacy",
    ]
    assert guided["strip"]["primary_action"]["kind"] == "find_speaker"


def test_fresh_jamendo_readiness_is_human_visible_but_ai_stays_later():
    config = load_config()
    config.is_addon = True
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "supervisor-token"
    golden_path = {
        "blocking": False,
        "source_readiness": _first_listen_source_projection(jamendo_status="playable"),
    }
    receipt = FirstListenReceiptV1(
        selected_entity_id="media_player.kitchen",
        accepted_attempt_id="accepted-attempt-1234",
        accepted_at=100.0,
        heard_at=101.0,
    )

    guided = build_guided_setup(
        config,
        _demo_state(),
        golden_path=golden_path,
        first_listen_receipt=receipt,
        install_origin="fresh",
        context_choice_explicit=False,
    )

    jamendo = next(row for row in guided["source_readiness"]["rows"] if row["kind"] == "jamendo")
    assert jamendo["status"] == "playable"
    assert guided["source_readiness"]["healthy"] is True
    assert guided["verification"]["milestone"] == "First listen achieved"
    assert guided["first_listen"]["show_ai"] is False
    assert guided["strip"]["primary_action"] == {
        "kind": "review_home_context",
        "label": "Review home context",
        "target": "setup",
    }

    reviewed = FirstListenReceiptV1(
        selected_entity_id=receipt.selected_entity_id,
        accepted_attempt_id=receipt.accepted_attempt_id,
        accepted_at=receipt.accepted_at,
        heard_at=receipt.heard_at,
        privacy_reviewed_at=102.0,
    )
    after_review = build_guided_setup(
        config,
        _demo_state(),
        golden_path=golden_path,
        first_listen_receipt=reviewed,
        install_origin="fresh",
        context_choice_explicit=True,
    )
    assert after_review["first_listen"]["show_ai"] is True
    assert after_review["strip"]["primary_action"] == {
        "kind": "add_ai_key",
        "label": "Add AI key",
        "target": "setup",
    }


def test_fresh_recovery_strip_advances_through_audio_and_privacy_before_repair():
    config = load_config()
    config.is_addon = True
    config.homeassistant.enabled = True
    config.homeassistant.context_enabled = False
    config.ha_token = "supervisor-token"
    golden_path = {
        "blocking": True,
        "source_readiness": _first_listen_source_projection(),
    }
    accepted = FirstListenReceiptV1(
        selected_entity_id="media_player.kitchen",
        accepted_attempt_id="accepted-attempt-1234",
        accepted_at=100.0,
    )

    waiting_for_confirmation = build_guided_setup(
        config,
        _demo_state(),
        golden_path=golden_path,
        first_listen_receipt=accepted,
        install_origin="fresh",
        context_choice_explicit=False,
    )
    assert waiting_for_confirmation["strip"]["primary_action"] == {
        "kind": "verify_audio",
        "label": "Did you hear it?",
        "target": "setup",
    }

    heard = FirstListenReceiptV1(
        selected_entity_id=accepted.selected_entity_id,
        accepted_attempt_id=accepted.accepted_attempt_id,
        accepted_at=accepted.accepted_at,
        heard_at=101.0,
    )
    waiting_for_privacy = build_guided_setup(
        config,
        _demo_state(),
        golden_path=golden_path,
        first_listen_receipt=heard,
        install_origin="fresh",
        context_choice_explicit=False,
    )
    assert waiting_for_privacy["strip"]["primary_action"]["kind"] == "review_home_context"
    assert waiting_for_privacy["source_readiness"]["transport_audible"] is False

    reviewed = FirstListenReceiptV1(
        selected_entity_id=heard.selected_entity_id,
        accepted_attempt_id=heard.accepted_attempt_id,
        accepted_at=heard.accepted_at,
        heard_at=heard.heard_at,
        privacy_reviewed_at=102.0,
    )
    repair = build_guided_setup(
        config,
        _demo_state(),
        golden_path=golden_path,
        first_listen_receipt=reviewed,
        install_origin="fresh",
        context_choice_explicit=True,
    )
    assert repair["strip"]["primary_action"] == {
        "kind": "repair_music_source",
        "label": "Repair music source",
        "target": "setup",
        "focus": "source",
    }
