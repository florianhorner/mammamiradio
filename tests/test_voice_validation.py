"""WS3-B guards: voice validation at config load + runtime memoization.

Four mandatory guards from the WS3-B charter:
  1. edge-tts host with voice="onyx" is corrected at config load.
  2. OpenAI host with voice="onyx" is accepted.
  3. runtime voice failure is memoized — no second edge-tts attempt for the
     same voice within the session.
  4. capabilities.py surfaces tts_degraded state when a host is on a
     substituted voice.

Plus Scenario 2 (empty fallback): if the backend fails entirely AFTER the
pre-flight pass, the synthesize path still produces an output file instead
of raising.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.ad_creative import AdBrand, AdVoice
from mammamiradio.capabilities import capabilities_to_dict, get_capabilities
from mammamiradio.config import AdsSection, StationConfig, _normalize_tts_voices
from mammamiradio.models import (
    HostPersonality,
    PersonalityAxes,
    StationState,
)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 256)
    return path


def _build_config(hosts: list[HostPersonality], ad_voices: list[AdVoice] | None = None) -> StationConfig:
    from mammamiradio.config import (
        AudioSection,
        HomeAssistantSection,
        PacingSection,
        PersonaSection,
        PlaylistSection,
        SonicBrandSection,
        StationSection,
    )

    return StationConfig(
        station=StationSection(),
        playlist=PlaylistSection(),
        pacing=PacingSection(),
        hosts=hosts,
        ads=AdsSection(
            brands=[AdBrand(name="TestBrand", tagline="fake", category="general")],
            voices=ad_voices or [],
        ),
        sonic_brand=SonicBrandSection(),
        audio=AudioSection(),
        homeassistant=HomeAssistantSection(),
        persona=PersonaSection(),
    )


# ---------------------------------------------------------------------------
# Guard 1: edge host with openai voice ID gets substituted at load
# ---------------------------------------------------------------------------


def test_edge_host_with_openai_voice_is_rejected_at_load(caplog):
    import logging

    host = HostPersonality(
        name="BadEdgeHost",
        voice="onyx",
        style="cool",
        engine="edge",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])

    with caplog.at_level(logging.WARNING, logger="mammamiradio.config"):
        _normalize_tts_voices(config)

    assert host.voice != "onyx", "onyx must be substituted on edge engine"
    assert host.voice.startswith("it-IT-"), f"expected italian edge voice, got {host.voice}"
    assert "BadEdgeHost" in config.tts_degraded_voices
    assert any("OpenAI voice 'onyx'" in rec.message for rec in caplog.records)


def test_edge_host_with_unknown_voice_is_rejected_at_load(caplog):
    """Voice that is neither a known OpenAI voice nor a known edge voice
    should be replaced rather than wait to fail at synthesis time."""
    import logging

    host = HostPersonality(
        name="UnknownVoiceHost",
        voice="it-IT-NotARealVoiceNeural",
        style="cool",
        engine="edge",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])

    with caplog.at_level(logging.WARNING, logger="mammamiradio.config"):
        _normalize_tts_voices(config)

    assert host.voice == "it-IT-DiegoNeural"
    assert "UnknownVoiceHost" in config.tts_degraded_voices


# ---------------------------------------------------------------------------
# Guard 2: OpenAI host with openai voice is accepted
# ---------------------------------------------------------------------------


def test_openai_host_with_openai_voice_is_accepted():
    host = HostPersonality(
        name="GoodOpenAIHost",
        voice="onyx",
        style="cool",
        engine="openai",
        edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])
    _normalize_tts_voices(config)

    assert host.voice == "onyx"
    assert host.engine == "openai"
    assert "GoodOpenAIHost" not in config.tts_degraded_voices


def test_openai_host_without_fallback_gets_default_fallback():
    host = HostPersonality(
        name="NoFallbackHost",
        voice="nova",
        style="warm",
        engine="openai",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])
    _normalize_tts_voices(config)

    assert host.edge_fallback_voice == "it-IT-DiegoNeural"


def test_openai_host_with_edge_voice_is_flipped_to_edge():
    host = HostPersonality(
        name="MisconfiguredHost",
        voice="it-IT-IsabellaNeural",
        style="warm",
        engine="openai",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])
    _normalize_tts_voices(config)

    assert host.engine == "edge"
    assert host.voice == "it-IT-IsabellaNeural" or host.voice == "it-IT-DiegoNeural"


# ---------------------------------------------------------------------------
# Guard 3: runtime voice failure is memoized — one attempt per voice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_voice_failure_is_memoized_no_repeat_calls(tmp_path):
    from mammamiradio import tts as tts_mod
    from mammamiradio.tts import reset_voice_failures, synthesize

    reset_voice_failures()

    # Track which voices Communicate was asked to render.
    observed_voices: list[str] = []

    class _FlakyComm:
        def __init__(self, text: str, voice: str, rate="+0%", pitch="+0Hz"):
            self.voice = voice
            observed_voices.append(voice)

        async def save(self, path):
            if self.voice == "it-IT-GianniNeural":
                raise RuntimeError("bad voice")
            _touch(Path(path))

    with (
        patch.object(tts_mod.edge_tts, "Communicate", _FlakyComm),
        patch.object(tts_mod, "normalize", side_effect=lambda src, dst, **kw: _touch(dst)),
        patch.object(tts_mod, "generate_silence", side_effect=lambda p, d: _touch(p)),
    ):
        out1 = tmp_path / "a.mp3"
        out2 = tmp_path / "b.mp3"
        out3 = tmp_path / "c.mp3"

        await synthesize("uno", "it-IT-GianniNeural", out1)
        await synthesize("due", "it-IT-GianniNeural", out2)
        await synthesize("tre", "it-IT-GianniNeural", out3)

    # First call: Gianni (fails) → DiegoNeural fallback succeeds → 2 calls.
    # Subsequent calls: memoization routes straight to Diego → 1 call each.
    # Total Gianni attempts across all three calls must be exactly one.
    gianni_attempts = sum(1 for v in observed_voices if v == "it-IT-GianniNeural")
    assert gianni_attempts == 1, f"expected 1 Gianni attempt, got {gianni_attempts} (observed: {observed_voices})"

    # Cleanup for test isolation
    reset_voice_failures()


@pytest.mark.asyncio
async def test_fallback_voice_itself_not_added_to_failed_set(tmp_path):
    """If the fallback voice is the one failing, we still record it but
    don't recurse infinitely — synthesize must return a silence file."""
    from mammamiradio import tts as tts_mod
    from mammamiradio.tts import reset_voice_failures, synthesize

    reset_voice_failures()

    class _AlwaysFail:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            self.voice = voice

        async def save(self, path):
            raise RuntimeError("all voices down")

    silence_calls: list[Path] = []

    def _silence(path, duration):
        silence_calls.append(path)
        _touch(path)
        return path

    with (
        patch.object(tts_mod.edge_tts, "Communicate", _AlwaysFail),
        patch.object(tts_mod, "normalize", side_effect=lambda src, dst, **kw: _touch(dst)),
        patch.object(tts_mod, "generate_silence", side_effect=_silence),
    ):
        out = tmp_path / "out.mp3"
        result = await synthesize("ciao", "it-IT-IsabellaNeural", out)

    # When every voice fails, silence is the final output.
    assert result == out
    assert len(silence_calls) >= 1, "generate_silence must produce a valid output"

    reset_voice_failures()


# ---------------------------------------------------------------------------
# Guard 4: capabilities.py surfaces tts_degraded
# ---------------------------------------------------------------------------


def test_capabilities_reports_tts_degraded_after_voice_substitution():
    host = HostPersonality(
        name="BadVoiceHost",
        voice="onyx",
        style="cool",
        engine="edge",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])
    config.anthropic_api_key = "sk-test"
    _normalize_tts_voices(config)

    state = StationState()
    caps = get_capabilities(config, state)
    payload = capabilities_to_dict(caps)

    assert caps.tts_degraded is True
    assert payload["tts_degraded"] is True


def test_capabilities_clean_when_all_voices_valid():
    host = HostPersonality(
        name="GoodHost",
        voice="it-IT-IsabellaNeural",
        style="warm",
        engine="edge",
        personality=PersonalityAxes(),
    )
    config = _build_config([host])
    _normalize_tts_voices(config)

    state = StationState()
    caps = get_capabilities(config, state)
    payload = capabilities_to_dict(caps)

    assert caps.tts_degraded is False
    assert payload["tts_degraded"] is False


# ---------------------------------------------------------------------------
# Scenario 2: backend entirely unreachable after pre-flight pass → silence path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario2_backend_unreachable_still_produces_output(tmp_path):
    """After pre-flight validation passes, if edge-tts is entirely unreachable
    at runtime (network down, API moved, etc.), synthesize must still hand
    back an output file — silence is acceptable, dead air is not."""
    from mammamiradio import tts as tts_mod
    from mammamiradio.tts import reset_voice_failures, synthesize

    reset_voice_failures()

    class _NetworkDown:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            pass

        async def save(self, path):
            raise ConnectionError("edge-tts endpoint unreachable")

    silence_out: list[Path] = []

    def _silence(path, duration):
        silence_out.append(path)
        _touch(path)
        return path

    with (
        patch.object(tts_mod.edge_tts, "Communicate", _NetworkDown),
        patch.object(tts_mod, "normalize", side_effect=lambda src, dst, **kw: _touch(dst)),
        patch.object(tts_mod, "generate_silence", side_effect=_silence),
    ):
        out = tmp_path / "out.mp3"
        result = await synthesize("ciao", "it-IT-IsabellaNeural", out)

    assert result == out
    assert silence_out, "generate_silence must be called when backend is unreachable"

    reset_voice_failures()


# ---------------------------------------------------------------------------
# Voice catalog sanity (source-of-truth tests)
# ---------------------------------------------------------------------------


def test_voice_catalog_contains_station_defaults():
    from mammamiradio.voice_catalog import (
        EDGE_DEFAULT_FALLBACK_VOICE,
        EDGE_ITALIAN_VOICES,
        OPENAI_VOICES,
    )

    # Italian baseline — anything we configure in radio.toml must be here.
    assert "it-IT-DiegoNeural" in EDGE_ITALIAN_VOICES
    assert "it-IT-IsabellaNeural" in EDGE_ITALIAN_VOICES
    assert "it-IT-GiuseppeMultilingualNeural" in EDGE_ITALIAN_VOICES
    assert EDGE_DEFAULT_FALLBACK_VOICE in EDGE_ITALIAN_VOICES

    # OpenAI — spec explicitly lists these six as the core set.
    for v in ("alloy", "echo", "fable", "onyx", "nova", "shimmer"):
        assert v in OPENAI_VOICES


def test_is_openai_voice_case_insensitive():
    from mammamiradio.voice_catalog import is_openai_voice

    assert is_openai_voice("onyx")
    assert is_openai_voice("ONYX")
    assert is_openai_voice("  Onyx  ")
    assert not is_openai_voice("it-IT-DiegoNeural")


def test_is_known_edge_voice_exact_match():
    from mammamiradio.voice_catalog import is_known_edge_voice

    assert is_known_edge_voice("it-IT-IsabellaNeural")
    assert not is_known_edge_voice("onyx")
    assert not is_known_edge_voice("it-IT-NotARealVoice")
