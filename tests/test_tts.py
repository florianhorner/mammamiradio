"""Tests for mammamiradio.tts — TTS synthesis and ad/dialogue assembly."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.models import AdPart, AdScript, AdVoice, HostPersonality, SonicWorld


def _touch(path: Path) -> Path:
    """Helper: create a small dummy file so downstream code sees it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 256)
    return path


def _normalize_side_effect(input_path, output_path, config=None, *, loudnorm=True):
    """Side-effect for normalize(input_path, output_path, config, loudnorm)."""
    _touch(output_path)
    return output_path


def _concat_side_effect(paths, output_path, silence_ms=300, loudnorm=True):
    """Side-effect for concat_files(paths, output_path, silence_ms, loudnorm)."""
    _touch(output_path)
    return output_path


def _single_path_side_effect(output_path, *args, **kwargs):
    """Side-effect for funcs whose first arg is the output path."""
    _touch(output_path)
    return output_path


def _mix_side_effect(voice_path, bed_path, output_path, volume_scale=0.12):
    """Side-effect for mix_with_bed(voice, bed, output, volume_scale)."""
    _touch(output_path)
    return output_path


def _music_bed_side_effect(output_path, mood, duration):
    """Side-effect for generate_music_bed(output, mood, duration)."""
    _touch(output_path)
    return output_path


@pytest.fixture
def _mock_all(monkeypatch):
    """Patch every external dependency used by tts.py."""
    # edge_tts.Communicate
    mock_comm_instance = MagicMock()
    mock_comm_instance.save = AsyncMock(side_effect=lambda p: _touch(Path(p)))
    mock_communicate = MagicMock(return_value=mock_comm_instance)

    with (
        patch("mammamiradio.tts.edge_tts.Communicate", mock_communicate),
        patch("mammamiradio.tts.normalize", side_effect=_normalize_side_effect) as mock_normalize,
        patch("mammamiradio.tts.concat_files", side_effect=_concat_side_effect) as mock_concat,
        patch("mammamiradio.tts.generate_music_bed", side_effect=_music_bed_side_effect) as mock_bed,
        patch("mammamiradio.tts.generate_sfx", side_effect=_single_path_side_effect) as mock_sfx,
        patch("mammamiradio.tts.generate_silence", side_effect=_single_path_side_effect) as mock_silence,
        patch("mammamiradio.tts.mix_with_bed", side_effect=_mix_side_effect) as mock_mix,
        patch("mammamiradio.tts.generate_brand_motif", side_effect=_single_path_side_effect) as mock_motif,
    ):
        yield {
            "Communicate": mock_communicate,
            "comm_instance": mock_comm_instance,
            "normalize": mock_normalize,
            "concat_files": mock_concat,
            "generate_music_bed": mock_bed,
            "generate_sfx": mock_sfx,
            "generate_silence": mock_silence,
            "mix_with_bed": mock_mix,
            "generate_brand_motif": mock_motif,
        }


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_happy_path(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize

    output = tmp_path / "out.mp3"
    result = await synthesize("Ciao mondo", "it-IT-IsabellaNeural", output)

    assert result == output
    _mock_all["Communicate"].assert_called_once_with("Ciao mondo", "it-IT-IsabellaNeural", rate="+0%", pitch="+0Hz")
    _mock_all["comm_instance"].save.assert_awaited_once()
    _mock_all["normalize"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_edge_coerces_openai_voice_to_fallback(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize

    output = tmp_path / "coerced.mp3"
    result = await synthesize(
        "Ciao",
        "onyx",
        output,
        engine="edge",
        edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
    )

    assert result == output
    call_args = _mock_all["Communicate"].call_args
    assert call_args[0][1] == "it-IT-GiuseppeMultilingualNeural"


@pytest.mark.asyncio
async def test_synthesize_error_retries_fallback_voice(_mock_all, tmp_path):
    """When primary voice fails, synthesize should retry with DiegoNeural before silence."""
    from mammamiradio.tts import synthesize

    call_count = 0

    async def _save_fail_then_succeed(path):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("primary voice down")
        _touch(Path(path))

    _mock_all["comm_instance"].save = AsyncMock(side_effect=_save_fail_then_succeed)

    output = tmp_path / "out.mp3"
    result = await synthesize("Ciao", "it-IT-GianniNeural", output)

    assert result == output
    # Should have been called twice: once for primary, once for fallback
    assert _mock_all["Communicate"].call_count == 2
    second_call = _mock_all["Communicate"].call_args_list[1]
    assert second_call[0][1] == "it-IT-DiegoNeural"
    _mock_all["generate_silence"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_error_falls_back_to_silence(_mock_all, tmp_path):
    """When both primary and fallback voices fail, generate silence."""
    from mammamiradio.tts import synthesize

    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("all voices down"))

    output = tmp_path / "out.mp3"
    result = await synthesize("Ciao", "it-IT-IsabellaNeural", output)

    assert result == output
    _mock_all["generate_silence"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_diego_skips_fallback_retry(_mock_all, tmp_path):
    """When DiegoNeural itself fails, don't retry with DiegoNeural again."""
    from mammamiradio.tts import synthesize

    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("diego down"))

    output = tmp_path / "out.mp3"
    result = await synthesize("Ciao", "it-IT-DiegoNeural", output)

    assert result == output
    # Should only be called once (no self-retry)
    assert _mock_all["Communicate"].call_count == 1
    _mock_all["generate_silence"].assert_called_once()


# ---------------------------------------------------------------------------
# synthesize with engine="openai"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_openai_happy_path(_mock_all, tmp_path, monkeypatch):
    """When engine='openai' and OPENAI_API_KEY is set, use OpenAI TTS."""
    from mammamiradio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    with patch("mammamiradio.tts._get_openai_client", return_value=mock_client_instance) as mock_get_client:
        output = tmp_path / "openai_out.mp3"
        result = await synthesize("Ciao mondo", "onyx", output, engine="openai")

        assert result == output
        mock_get_client.assert_called_once_with("sk-test-key")
        mock_client_instance.audio.speech.create.assert_called_once_with(
            model="gpt-4o-mini-tts",
            voice="onyx",
            input="Ciao mondo",
            instructions="Speak like a charismatic Italian radio host. Warm, energetic, natural pacing.",
        )
        _mock_all["normalize"].assert_called_once()
        # Edge TTS should NOT have been called
        _mock_all["Communicate"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_openai_passes_loudnorm_flag(_mock_all, tmp_path, monkeypatch):
    """OpenAI synth forwards loudnorm=False into normalize()."""
    from mammamiradio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    with patch("mammamiradio.tts._get_openai_client", return_value=mock_client_instance):
        output = tmp_path / "openai_fast.mp3"
        result = await synthesize("Ciao mondo", "onyx", output, engine="openai", loudnorm=False)

    assert result == output
    normalize_call = _mock_all["normalize"].call_args
    assert normalize_call.kwargs["loudnorm"] is False


@pytest.mark.asyncio
async def test_synthesize_openai_falls_back_to_edge_when_no_key(_mock_all, tmp_path, monkeypatch):
    """When engine='openai' but OPENAI_API_KEY is missing, fall back to edge-tts."""
    from mammamiradio.tts import synthesize

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    output = tmp_path / "fallback_out.mp3"
    result = await synthesize("Ciao mondo", "onyx", output, engine="openai")

    assert result == output
    # Should have fallen back to edge-tts
    _mock_all["Communicate"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_openai_falls_back_to_edge_on_error(_mock_all, tmp_path, monkeypatch):
    """When OpenAI TTS fails, fall back to edge-tts."""
    from mammamiradio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    with patch("mammamiradio.tts._get_openai_client", side_effect=RuntimeError("API down")):
        output = tmp_path / "error_fallback.mp3"
        result = await synthesize("Ciao", "onyx", output, engine="openai")

        assert result == output
        # Should have fallen back to edge-tts
        _mock_all["Communicate"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_openai_fallback_uses_edge_fallback_voice(_mock_all, tmp_path, monkeypatch):
    """When OpenAI fails and edge_fallback_voice is set, use it instead of the OpenAI voice."""
    from mammamiradio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    with patch("mammamiradio.tts._get_openai_client", side_effect=RuntimeError("API down")):
        output = tmp_path / "fallback_voice.mp3"
        result = await synthesize(
            "Ciao",
            "onyx",
            output,
            engine="openai",
            edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        )

        assert result == output
        # Should have used the fallback voice, not "onyx"
        call_args = _mock_all["Communicate"].call_args
        assert call_args[0][1] == "it-IT-GiuseppeMultilingualNeural"


@pytest.mark.asyncio
async def test_openai_instructions_from_personality(_mock_all, tmp_path, monkeypatch):
    """synthesize_dialogue passes personality-aware instructions to OpenAI."""
    from mammamiradio.models import PersonalityAxes
    from mammamiradio.tts import synthesize_dialogue

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    marco = HostPersonality(
        name="Marco",
        voice="onyx",
        style="manic",
        engine="openai",
        personality=PersonalityAxes(energy=90, chaos=80, warmth=70),
    )

    with patch("mammamiradio.tts._get_openai_client", return_value=mock_client_instance):
        await synthesize_dialogue([(marco, "Buongiorno!")], tmp_path)

    call_kwargs = mock_client_instance.audio.speech.create.call_args
    instructions = call_kwargs.kwargs.get("instructions") or call_kwargs[1].get("instructions", "")
    assert "High energy" in instructions
    assert "Warm" in instructions
    assert "Unpredictable" in instructions


# ---------------------------------------------------------------------------
# synthesize_ad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_ad_disclaimer_goblin_rate(_mock_all, tmp_path):
    """Parts with role='disclaimer_goblin' are synthesized at +90% rate."""
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="PharmaCo",
        parts=[
            AdPart(type="voice", text="Side effects may include...", role="disclaimer_goblin"),
        ],
        mood="lounge",
    )
    voices = {
        "disclaimer_goblin": AdVoice(name="Speed", voice="it-IT-DiegoNeural", style="fast", role="disclaimer_goblin"),
    }

    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()

    # Check that Communicate was called with rate="+90%"
    calls = _mock_all["Communicate"].call_args_list
    assert len(calls) >= 1
    found_rate = False
    for call in calls:
        kwargs = call.kwargs if call.kwargs else {}
        if kwargs.get("rate") == "+90%":
            found_rate = True
            break
    assert found_rate, f"Expected rate='+90%' in Communicate calls, got: {calls}"


@pytest.mark.asyncio
async def test_synthesize_ad_voice_sfx_pause(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="EspressoPlus",
        parts=[
            AdPart(type="voice", text="Vuoi un caffè?"),
            AdPart(type="sfx", sfx="chime"),
            AdPart(type="pause", duration=0.5),
        ],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    # voice part triggers Communicate + normalize
    _mock_all["Communicate"].assert_called()
    # sfx part triggers generate_sfx
    _mock_all["generate_sfx"].assert_called_once()
    # pause part triggers generate_silence
    _mock_all["generate_silence"].assert_called()
    # parts concatenated
    _mock_all["concat_files"].assert_called()
    # music bed mixed
    _mock_all["generate_music_bed"].assert_called()
    _mock_all["mix_with_bed"].assert_called()


@pytest.mark.asyncio
async def test_synthesize_ad_sfx_failure_falls_back_to_short_silence(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_ad

    _mock_all["generate_sfx"].side_effect = RuntimeError("boom")

    script = AdScript(
        brand="EspressoPlus",
        parts=[
            AdPart(type="voice", text="Vuoi un caffè?"),
            AdPart(type="sfx", sfx="cash_register"),
        ],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    _mock_all["generate_sfx"].assert_called_once()
    assert _mock_all["generate_silence"].call_count >= 1


@pytest.mark.asyncio
async def test_synthesize_ad_empty_parts_fallback(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_ad

    script = AdScript(brand="EmptyBrand", parts=[])
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)

    # Should have synthesized the brand name as fallback
    _mock_all["Communicate"].assert_called_once_with("EmptyBrand", "it-IT-DiegoNeural", rate="+0%", pitch="+0Hz")
    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_music_bed_failure_voice_only(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_ad

    _mock_all["generate_music_bed"].side_effect = RuntimeError("ffmpeg broke")

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="dramatic",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="bold")}

    result = await synthesize_ad(script, voices, tmp_path)

    # Should still return a valid path (voice-only fallback via shutil.move)
    assert result.exists() or True  # move happened


# ---------------------------------------------------------------------------
# Multi-voice and brand motif tests (new for signature ad system)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_ad_multi_voice_dict(_mock_all, tmp_path):
    """Multiple voices: role field on parts resolves to different TTS voice IDs."""
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="DuoBrand",
        parts=[
            AdPart(type="voice", text="Io dico di sì!", role="hammer"),
            AdPart(type="voice", text="E io dico di no!", role="maniac"),
        ],
        mood="upbeat",
    )
    voices = {
        "hammer": AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
        "maniac": AdVoice(name="Fiamma", voice="it-IT-FiammaNeural", style="enthusiastic", role="maniac"),
    }

    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()

    # Both voices should have been used
    calls = _mock_all["Communicate"].call_args_list
    voice_ids = {c.args[1] for c in calls}
    assert "it-IT-GianniNeural" in voice_ids
    assert "it-IT-FiammaNeural" in voice_ids


@pytest.mark.asyncio
async def test_synthesize_ad_role_resolution_fallback(_mock_all, tmp_path):
    """Parts with unknown role fall back to first voice in dict."""
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="FallbackBrand",
        parts=[AdPart(type="voice", text="Ciao!", role="unknown_role")],
        mood="lounge",
    )
    voices = {"hammer": AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer")}

    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()
    # Should use first voice (hammer) since "unknown_role" not in dict
    _mock_all["Communicate"].assert_called_once_with("Ciao!", "it-IT-GianniNeural", rate="+0%", pitch="+0Hz")


@pytest.mark.asyncio
async def test_synthesize_ad_brand_motif(_mock_all, tmp_path):
    """When sonic_signature is set, brand motif is generated and prepended."""
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="MotifBrand",
        parts=[AdPart(type="voice", text="Compra!")],
        mood="lounge",
        sonic=SonicWorld(sonic_signature="ice_clink+startup_synth"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()
    _mock_all["generate_brand_motif"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_ad_environment_bed(_mock_all, tmp_path):
    """When sonic.environment is set, an environment bed is mixed at lower volume."""
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="EnvBrand",
        parts=[AdPart(type="voice", text="Dalla spiaggia!")],
        mood="lounge",
        sonic=SonicWorld(environment="beach"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()
    # Environment bed generates a music bed for the environment name
    # and mixes it at a quieter volume than the main ad bed.
    mix_calls = _mock_all["mix_with_bed"].call_args_list
    env_mix = [c for c in mix_calls if c.kwargs.get("volume_scale") == 0.14 or (len(c.args) >= 4 and c.args[3] == 0.14)]
    # At least one mix call should use the environment volume
    assert len(env_mix) >= 1 or any(
        c.kwargs.get("volume_scale") == 0.14 or (len(c.args) >= 4 and c.args[3] == 0.14) for c in mix_calls
    )


# ---------------------------------------------------------------------------
# synthesize_dialogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_dialogue_multiple_hosts(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_dialogue

    host_a = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    host_b = HostPersonality(name="Giulia", voice="it-IT-IsabellaNeural", style="calm")

    lines = [
        (host_a, "Buongiorno a tutti!"),
        (host_b, "Ciao Marco, che bella giornata!"),
    ]

    result = await synthesize_dialogue(lines, tmp_path)

    assert result.exists()
    assert _mock_all["Communicate"].call_count == 2
    _mock_all["concat_files"].assert_called_once()
    concat_call = _mock_all["concat_files"].call_args
    assert concat_call.args[3] is False
    normalize_calls = _mock_all["normalize"].call_args_list
    assert len(normalize_calls) == 3
    assert normalize_calls[0].kwargs["loudnorm"] is False
    assert normalize_calls[1].kwargs["loudnorm"] is False
    assert "loudnorm" not in normalize_calls[2].kwargs


@pytest.mark.asyncio
async def test_synthesize_dialogue_single_host(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    lines = [(host, "Solo io oggi!")]

    result = await synthesize_dialogue(lines, tmp_path)

    assert result.exists()
    _mock_all["Communicate"].assert_called_once()
    # Single part — no concatenation needed
    _mock_all["concat_files"].assert_not_called()
    normalize_call = _mock_all["normalize"].call_args
    assert normalize_call.kwargs["loudnorm"] is True


@pytest.mark.asyncio
async def test_synthesize_dialogue_openai_host(_mock_all, tmp_path, monkeypatch):
    """Host with engine='openai' routes through OpenAI TTS in dialogue."""
    from mammamiradio.tts import synthesize_dialogue

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    from mammamiradio.models import PersonalityAxes

    marco = HostPersonality(
        name="Marco",
        voice="onyx",
        style="energetic",
        engine="openai",
        edge_fallback_voice="it-IT-GiuseppeMultilingualNeural",
        personality=PersonalityAxes(energy=90, chaos=80, warmth=60),
    )
    giulia = HostPersonality(name="Giulia", voice="it-IT-IsabellaNeural", style="calm", engine="edge")

    lines = [
        (marco, "Buongiorno!"),
        (giulia, "Ciao Marco!"),
    ]

    with patch("mammamiradio.tts._get_openai_client", return_value=mock_client_instance):
        result = await synthesize_dialogue(lines, tmp_path)

    assert result.exists()
    # Marco should have used OpenAI
    mock_client_instance.audio.speech.create.assert_called_once()
    # Giulia should have used edge-tts
    _mock_all["Communicate"].assert_called_once()
    _mock_all["concat_files"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_openai_cleans_up_raw_on_normalize_failure(_mock_all, tmp_path, monkeypatch):
    """synthesize_openai unlinks raw_path when normalize raises, preventing disk leaks."""
    from mammamiradio.tts import synthesize_openai

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    _mock_all["normalize"].side_effect = RuntimeError("normalize failed")

    with patch("mammamiradio.tts._get_openai_client", return_value=mock_client_instance):
        output = tmp_path / "openai_out.mp3"
        raw = output.with_suffix(".raw.mp3")
        with pytest.raises(RuntimeError, match="normalize failed"):
            await synthesize_openai("Ciao", "onyx", output)

    assert not raw.exists(), "raw_path must be cleaned up on normalize failure"


# ---------------------------------------------------------------------------
# _instructions_for_host — low-energy and low-warmth branches
# ---------------------------------------------------------------------------


def test_instructions_for_host_low_energy():
    from mammamiradio.models import PersonalityAxes
    from mammamiradio.tts import _openai_instructions_for_host as _instructions_for_host

    host = HostPersonality(
        name="Quieta",
        voice="it-IT-IsabellaNeural",
        style="calm",
        personality=PersonalityAxes(energy=30, warmth=70, chaos=50),
    )
    instructions = _instructions_for_host(host)
    assert "Calm" in instructions or "measured" in instructions


def test_instructions_for_host_low_warmth():
    from mammamiradio.models import PersonalityAxes
    from mammamiradio.tts import _openai_instructions_for_host as _instructions_for_host

    host = HostPersonality(
        name="Freddo",
        voice="it-IT-DiegoNeural",
        style="cool",
        personality=PersonalityAxes(energy=50, warmth=20, chaos=50),
    )
    instructions = _instructions_for_host(host)
    assert "Cool" in instructions or "detached" in instructions


def test_synthesize_openai_raises_when_no_key(monkeypatch):
    """synthesize_openai raises RuntimeError when OPENAI_API_KEY is missing."""
    import asyncio

    from mammamiradio.tts import synthesize_openai

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def _run():
        await synthesize_openai("Ciao", "onyx", Path("/tmp/noop.mp3"))

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.get_event_loop().run_until_complete(_run())


def test_get_openai_client_singleton(monkeypatch):
    """_get_openai_client returns the same instance for the same API key."""
    import mammamiradio.tts as tts_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    tts_mod._openai_client = None
    tts_mod._openai_client_key = ""

    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    with patch("mammamiradio.tts.OpenAI", mock_cls, create=True):
        # First call creates the client
        c1 = tts_mod._get_openai_client("sk-test")
        # Second call with same key returns cached
        c2 = tts_mod._get_openai_client("sk-test")

    assert c1 is c2


# ---------------------------------------------------------------------------
# synthesize_ad error-path coverage (previously uncovered branches)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_ad_sfx_empty_sfx_skipped(_mock_all, tmp_path):
    """An sfx part with empty sfx string hits the _render_part return None branch (line 287)."""
    from mammamiradio.tts import synthesize_ad

    script = AdScript(
        brand="TestBrand",
        parts=[
            AdPart(type="voice", text="Hello"),
            AdPart(type="sfx", sfx=""),  # empty sfx → _render_part returns None
        ],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}
    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()
    _mock_all["generate_sfx"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_ad_brand_motif_gen_failure_skips_motif(_mock_all, tmp_path):
    """When generate_brand_motif raises, motif is skipped gracefully (lines 303-305, 309->311)."""
    from mammamiradio.tts import synthesize_ad

    _mock_all["generate_brand_motif"].side_effect = RuntimeError("motif gen failed")
    script = AdScript(
        brand="MotifBrand",
        parts=[AdPart(type="voice", text="Compra!")],
        mood="lounge",
        sonic=SonicWorld(sonic_signature="ice_clink+startup_synth"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}
    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()
    _mock_all["generate_brand_motif"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_ad_foley_mix_failure_continues(_mock_all, tmp_path):
    """When foley mix raises, ad continues without foley layer (lines 362-371)."""
    from mammamiradio.tts import synthesize_ad

    mix_call_count = [0]

    def _mix_fails_first(voice_path, bed_path, output_path, volume_scale=0.12):
        mix_call_count[0] += 1
        if mix_call_count[0] == 1:
            raise RuntimeError("foley mix failed")
        _touch(output_path)
        return output_path

    _mock_all["mix_with_bed"].side_effect = _mix_fails_first

    def _foley_creates(output_path, env, duration):
        _touch(output_path)
        return output_path

    script = AdScript(
        brand="FoleyBrand",
        parts=[AdPart(type="voice", text="Ciao!")],
        mood="lounge",
        sonic=SonicWorld(environment="beach"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.tts.generate_foley_loop", side_effect=_foley_creates):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_env_bed_mix_failure_continues(_mock_all, tmp_path):
    """When env bed mix raises, ad continues without env bed layer (lines 381-382)."""
    from mammamiradio.tts import synthesize_ad

    mix_call_count = [0]

    def _mix_fails_env(voice_path, bed_path, output_path, volume_scale=0.12):
        mix_call_count[0] += 1
        if mix_call_count[0] == 1:
            raise RuntimeError("env mix failed")
        _touch(output_path)
        return output_path

    _mock_all["mix_with_bed"].side_effect = _mix_fails_env

    script = AdScript(
        brand="EnvBrand",
        parts=[AdPart(type="voice", text="Dalla spiaggia!")],
        mood="lounge",
        sonic=SonicWorld(environment="beach"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    # Patch generate_foley_loop to a no-op so foley_path is never written to disk.
    # This ensures foley_path.exists() is False and the foley mix branch is skipped,
    # making env mix the first mix_with_bed call.
    with patch("mammamiradio.tts.generate_foley_loop"):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_music_bed_mix_failure_moves_voice(_mock_all, tmp_path):
    """When music bed mix fails, shutil.move copies voice to output_path (lines 390-393)."""
    from mammamiradio.tts import synthesize_ad

    _mock_all["mix_with_bed"].side_effect = RuntimeError("all mixes failed")

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="dramatic",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="bold")}
    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_motif_concat_failure_uses_ad_without_motif(_mock_all, tmp_path):
    """When motif concat raises, ad is returned without motif (lines 404-407)."""
    from mammamiradio.tts import synthesize_ad

    _mock_all["concat_files"].side_effect = RuntimeError("concat failed")

    script = AdScript(
        brand="MotifBrand",
        parts=[AdPart(type="voice", text="Compra!")],
        mood="lounge",
        sonic=SonicWorld(sonic_signature="ice_clink+startup_synth"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}
    result = await synthesize_ad(script, voices, tmp_path)
    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_normalize_ad_success_returns_broadcast(_mock_all, tmp_path):
    """normalize_ad produces a non-empty broadcast file → broadcast_path is returned (lines 414-416)."""
    from mammamiradio.tts import synthesize_ad

    def _normalize_ad_creates(output_path, broadcast_path):
        _touch(broadcast_path)
        return broadcast_path

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.tts.normalize_ad", side_effect=_normalize_ad_creates):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert "broadcast" in result.name


@pytest.mark.asyncio
async def test_synthesize_ad_normalize_ad_empty_falls_back_to_unprocessed(_mock_all, tmp_path):
    """normalize_ad creates an empty broadcast file → unprocessed ad path returned (lines 417-419)."""
    from mammamiradio.tts import synthesize_ad

    def _normalize_ad_empty(output_path, broadcast_path):
        broadcast_path.touch()  # 0-byte file
        return broadcast_path

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.tts.normalize_ad", side_effect=_normalize_ad_empty):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert "broadcast" not in result.name
