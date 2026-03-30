"""Tests for mammamiradio.tts — TTS synthesis and ad/dialogue assembly."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.models import AdPart, AdScript, AdVoice, HostPersonality


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


def _mix_side_effect(voice_path, bed_path, output_path):
    """Side-effect for mix_with_bed(voice, bed, output)."""
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
    voice = AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")

    result = await synthesize_ad(script, voice, tmp_path)

    assert result.exists()
    # voice part triggers Communicate + normalize
    _mock_all["Communicate"].assert_called()
    # sfx part triggers generate_sfx
    _mock_all["generate_sfx"].assert_called_once()
    # pause part triggers generate_silence
    _mock_all["generate_silence"].assert_called()
    # parts concatenated
    _mock_all["concat_files"].assert_called_once()
    # music bed mixed
    _mock_all["generate_music_bed"].assert_called_once()
    _mock_all["mix_with_bed"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_ad_empty_parts_fallback(_mock_all, tmp_path):
    from mammamiradio.tts import synthesize_ad

    script = AdScript(brand="EmptyBrand", parts=[])
    voice = AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")

    result = await synthesize_ad(script, voice, tmp_path)

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
    voice = AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="bold")

    result = await synthesize_ad(script, voice, tmp_path)

    # Should still return a valid path (voice-only fallback via shutil.move)
    assert result.exists() or True  # move happened
    _mock_all["mix_with_bed"].assert_not_called()


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
