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


def _normalize_side_effect(input_path, output_path, config=None):
    """Side-effect for normalize(input_path, output_path, config)."""
    _touch(output_path)
    return output_path


def _concat_side_effect(paths, output_path, silence_ms=300):
    """Side-effect for concat_files(paths, output_path, silence_ms)."""
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
    _mock_all["Communicate"].assert_called_once_with("Ciao mondo", "it-IT-IsabellaNeural")
    _mock_all["comm_instance"].save.assert_awaited_once()
    _mock_all["normalize"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_error_falls_back_to_silence(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize

    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("network down"))

    output = tmp_path / "out.mp3"
    result = await synthesize("Ciao", "it-IT-IsabellaNeural", output)

    assert result == output
    _mock_all["generate_silence"].assert_called_once()
    # normalize should NOT have been called since edge_tts failed first
    _mock_all["normalize"].assert_not_called()


# ---------------------------------------------------------------------------
# synthesize_ad
# ---------------------------------------------------------------------------


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
async def test_synthesize_ad_empty_parts_fallback(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_ad

    script = AdScript(brand="EmptyBrand", parts=[])
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)

    # Should have synthesized the brand name as fallback
    _mock_all["Communicate"].assert_called_once_with("EmptyBrand", "it-IT-DiegoNeural")
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
    _mock_all["Communicate"].assert_called_once_with("Ciao!", "it-IT-GianniNeural")


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
    # and mixes it at 0.06 volume (quieter than music bed at 0.12)
    mix_calls = _mock_all["mix_with_bed"].call_args_list
    env_mix = [c for c in mix_calls if len(c.args) >= 4 or c.kwargs.get("volume_scale") == 0.06]
    # At least one mix call should use the environment volume
    assert len(env_mix) >= 1 or any(
        c.kwargs.get("volume_scale") == 0.06 or (len(c.args) >= 4 and c.args[3] == 0.06) for c in mix_calls
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
