"""Tests for mammamiradio.tts — TTS synthesis and ad/dialogue assembly."""

from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mammamiradio.core.models import HostPersonality, StationState
from mammamiradio.hosts.ad_creative import AdPart, AdScript, AdVoice, SonicWorld


def _touch(path: Path) -> Path:
    """Helper: create a small dummy file so downstream code sees it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 2048)
    return path


def _normalize_side_effect(input_path, output_path, config=None, *, loudnorm=True):
    """Side-effect for normalize(input_path, output_path, config, loudnorm)."""
    _touch(output_path)
    return output_path


def _concat_side_effect(paths, output_path, silence_ms=300, loudnorm=True, **kwargs):
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


@pytest.fixture(autouse=True)
def _reset_openai_tts_model():
    """Keep the module-level OpenAI TTS model selection out of cross-test state.

    The selection is a process global; without this reset a test that configures
    it (even to None, which now marks the station as explicitly configured) would
    change what a later, unconfigured test resolves — order-dependent under
    pytest-randomly.
    """
    import mammamiradio.audio.tts as tts_mod

    tts_mod._openai_tts_model = None
    tts_mod._openai_tts_model_configured = False
    yield
    tts_mod._openai_tts_model = None
    tts_mod._openai_tts_model_configured = False


@pytest.fixture
def _mock_all(monkeypatch):
    """Patch every external dependency used by tts.py."""
    import mammamiradio.audio.tts as tts_mod

    def _reset_provider_clients() -> None:
        tts_mod._openai_client = None
        tts_mod._openai_client_key = ""
        tts_mod._azure_client = None
        tts_mod._azure_client_key = ("", "")
        tts_mod._elevenlabs_client = None
        tts_mod._elevenlabs_client_key = ""
        tts_mod._failed_edge_voices.clear()  # edge-failure memoization leaks across tests otherwise
        tts_mod._failed_cloud_voices.clear()
        tts_mod._failed_cloud_routes.clear()
        tts_mod._cloud_voice_attempt_locks.clear()

    _reset_provider_clients()

    # edge_tts.Communicate
    mock_comm_instance = MagicMock()
    mock_comm_instance.save = AsyncMock(side_effect=lambda p: _touch(Path(p)))
    mock_communicate = MagicMock(return_value=mock_comm_instance)

    with (
        patch("mammamiradio.audio.tts.edge_tts.Communicate", mock_communicate),
        patch("mammamiradio.audio.tts.normalize", side_effect=_normalize_side_effect) as mock_normalize,
        patch("mammamiradio.audio.tts.concat_files", side_effect=_concat_side_effect) as mock_concat,
        patch("mammamiradio.audio.tts.generate_music_bed", side_effect=_music_bed_side_effect) as mock_bed,
        patch("mammamiradio.audio.tts.generate_sfx", side_effect=_single_path_side_effect) as mock_sfx,
        patch("mammamiradio.audio.tts.generate_silence", side_effect=_single_path_side_effect) as mock_silence,
        patch("mammamiradio.audio.tts.generate_foley_loop", side_effect=_single_path_side_effect) as mock_foley,
        patch("mammamiradio.audio.tts.mix_with_bed", side_effect=_mix_side_effect) as mock_mix,
        patch("mammamiradio.audio.tts.generate_brand_motif", side_effect=_single_path_side_effect) as mock_motif,
        patch("mammamiradio.audio.tts.probe_duration_sec", return_value=1.0) as mock_duration,
    ):
        yield {
            "Communicate": mock_communicate,
            "comm_instance": mock_comm_instance,
            "normalize": mock_normalize,
            "concat_files": mock_concat,
            "generate_music_bed": mock_bed,
            "generate_sfx": mock_sfx,
            "generate_silence": mock_silence,
            "generate_foley_loop": mock_foley,
            "mix_with_bed": mock_mix,
            "generate_brand_motif": mock_motif,
            "ffprobe_duration": mock_duration,
        }
    _reset_provider_clients()


# ---------------------------------------------------------------------------
# synthesize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_happy_path(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize

    output = tmp_path / "out.mp3"
    result = await synthesize("Ciao mondo", "it-IT-IsabellaNeural", output)

    assert result == output
    _mock_all["Communicate"].assert_called_once_with("Ciao mondo", "it-IT-IsabellaNeural", rate="+0%", pitch="+0Hz")
    _mock_all["comm_instance"].save.assert_awaited_once()
    _mock_all["normalize"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_edge_coerces_openai_voice_to_fallback(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize

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
    """When primary voice fails, synthesize retries with the house voice."""
    from mammamiradio.audio.tts import synthesize

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
async def test_synthesize_error_fails_closed_and_removes_partial_files(_mock_all, tmp_path):
    """Total voice failure raises and never leaves silent or partial speech."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize

    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("all voices down"))

    output = tmp_path / "out.mp3"
    raw = output.with_suffix(".raw.mp3")
    output.write_bytes(b"partial normalized speech")
    raw.write_bytes(b"partial provider speech")

    with pytest.raises(TTSUnavailableError, match="all configured TTS routes") as exc_info:
        await synthesize("Ciao", "it-IT-IsabellaNeural", output)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "all voices down" in str(exc_info.value.__cause__)
    assert not output.exists()
    assert not raw.exists()
    _mock_all["generate_silence"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_cloud_then_configured_edge_then_house_total_failure_order(_mock_all, tmp_path, monkeypatch):
    """A cloud outage exhausts the configured Edge voice before the house voice."""
    from mammamiradio.audio.tts import TTSUnavailableError, configure_openai_tts_model, synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    configure_openai_tts_model("registry-selected-tts")
    configured_edge = "it-IT-GiuseppeMultilingualNeural"
    house_edge = "it-IT-DiegoNeural"
    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("edge routes down"))

    output = tmp_path / "cloud_edge_house_failure.mp3"
    raw = output.with_suffix(".raw.mp3")
    output.write_bytes(b"partial final")
    raw.write_bytes(b"partial raw")

    with (
        patch("mammamiradio.audio.tts._get_openai_client", side_effect=RuntimeError("cloud down")),
        pytest.raises(TTSUnavailableError, match="all configured TTS routes"),
    ):
        await synthesize(
            "Ciao",
            "onyx",
            output,
            engine="openai",
            edge_fallback_voice=configured_edge,
        )

    attempted_edge_voices = [call.args[1] for call in _mock_all["Communicate"].call_args_list]
    assert attempted_edge_voices == [configured_edge, house_edge]
    assert not output.exists()
    assert not raw.exists()
    _mock_all["generate_silence"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_cleanup_unlink_error_preserves_tts_unavailable(
    _mock_all,
    tmp_path,
    monkeypatch,
    caplog,
):
    """Best-effort scratch cleanup cannot replace the actionable TTS failure."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize

    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("all voices down"))
    output = tmp_path / "busy_output.mp3"
    output.write_bytes(b"partial final")
    original_unlink = Path.unlink

    def _busy_final_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == output:
            raise OSError("file is busy")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _busy_final_unlink)

    with pytest.raises(TTSUnavailableError, match="all configured TTS routes") as exc_info:
        await synthesize("Ciao", "it-IT-IsabellaNeural", output)

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert output.exists(), "the simulated filesystem refusal should leave only the undeletable file"
    assert "Could not remove TTS scratch file" in caplog.text
    assert "file is busy" in caplog.text
    _mock_all["generate_silence"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_diego_skips_fallback_retry(_mock_all, tmp_path):
    """When DiegoNeural itself fails, don't retry with DiegoNeural again."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize

    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("diego down"))

    output = tmp_path / "out.mp3"
    with pytest.raises(TTSUnavailableError, match="all configured TTS routes"):
        await synthesize("Ciao", "it-IT-DiegoNeural", output)

    # Should only be called once (no self-retry)
    assert _mock_all["Communicate"].call_count == 1
    _mock_all["generate_silence"].assert_not_called()


# ---------------------------------------------------------------------------
# synthesize with engine="openai"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_openai_happy_path(_mock_all, tmp_path, monkeypatch):
    """When engine='openai' and OPENAI_API_KEY is set, use OpenAI TTS."""
    from mammamiradio.audio.tts import configure_openai_tts_model, synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    configure_openai_tts_model("registry-selected-tts")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    try:
        with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client_instance) as mock_get_client:
            output = tmp_path / "openai_out.mp3"
            result = await synthesize("Ciao mondo", "onyx", output, engine="openai")

            assert result == output
            mock_get_client.assert_called_once_with("sk-test-key")
            mock_client_instance.audio.speech.create.assert_called_once_with(
                model="registry-selected-tts",
                voice="onyx",
                input="Ciao mondo",
                instructions="Speak like a charismatic Italian radio host. Warm, energetic, natural pacing.",
            )
            _mock_all["normalize"].assert_called_once()
            # Edge TTS should NOT have been called
            _mock_all["Communicate"].assert_not_called()
    finally:
        configure_openai_tts_model(None)


@pytest.mark.asyncio
async def test_synthesize_openai_falls_back_to_edge_when_tts_model_unavailable(_mock_all, tmp_path, monkeypatch):
    """Registry TTS model missing -> OpenAI synth raises, synthesize() lands on Edge.

    _configured_openai_tts_model() is neutralized so the test exercises the None
    branch rather than silently reading the packaged registry's real model.
    """
    from mammamiradio.audio import tts as tts_mod
    from mammamiradio.audio.tts import configure_openai_tts_model, synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    configure_openai_tts_model(None)
    monkeypatch.setattr(tts_mod, "_configured_openai_tts_model", lambda: None)

    openai_client = MagicMock()
    try:
        with patch("mammamiradio.audio.tts._get_openai_client", return_value=openai_client) as mock_get_client:
            output = tmp_path / "edge_fallback.mp3"
            result = await synthesize("Ciao mondo", "onyx", output, engine="openai")

            assert result == output
            # OpenAI speech was never billed; Edge covered the render.
            openai_client.audio.speech.create.assert_not_called()
            mock_get_client.assert_not_called()
            _mock_all["Communicate"].assert_called_once()
    finally:
        configure_openai_tts_model(None)


@pytest.mark.asyncio
async def test_configured_none_does_not_read_cwd_registry(_mock_all, tmp_path, monkeypatch):
    """Explicit startup config of None must win over a real CWD registry model.

    The repo-root model_registry.toml (the process CWD in tests) DOES carry an
    OpenAI TTS model. Once the station is explicitly configured to None, that
    decision is authoritative: synthesize() must land on Edge and never call
    OpenAI with the unrelated CWD registry's model.
    """
    from mammamiradio.audio.tts import _configured_openai_tts_model, configure_openai_tts_model, synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    configure_openai_tts_model(None)

    # No monkeypatch on the disk read: the flag alone must suppress it.
    assert _configured_openai_tts_model() is None

    openai_client = MagicMock()
    with patch("mammamiradio.audio.tts._get_openai_client", return_value=openai_client) as mock_get_client:
        output = tmp_path / "cwd_guard.mp3"
        result = await synthesize("Ciao mondo", "onyx", output, engine="openai")

    assert result == output
    mock_get_client.assert_not_called()
    openai_client.audio.speech.create.assert_not_called()
    _mock_all["Communicate"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_openai_passes_loudnorm_flag(_mock_all, tmp_path, monkeypatch):
    """OpenAI synth forwards loudnorm=False into normalize()."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client_instance):
        output = tmp_path / "openai_fast.mp3"
        result = await synthesize("Ciao mondo", "onyx", output, engine="openai", loudnorm=False)

    assert result == output
    normalize_call = _mock_all["normalize"].call_args
    assert normalize_call.kwargs["loudnorm"] is False


@pytest.mark.asyncio
async def test_synthesize_openai_falls_back_to_edge_when_no_key(_mock_all, tmp_path, monkeypatch):
    """When engine='openai' but OPENAI_API_KEY is missing, fall back to edge-tts."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    output = tmp_path / "fallback_out.mp3"
    result = await synthesize("Ciao mondo", "onyx", output, engine="openai")

    assert result == output
    # Should have fallen back to edge-tts
    _mock_all["Communicate"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_openai_falls_back_to_edge_on_error(_mock_all, tmp_path, monkeypatch):
    """When OpenAI TTS fails, fall back to edge-tts."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    with patch("mammamiradio.audio.tts._get_openai_client", side_effect=RuntimeError("API down")):
        output = tmp_path / "error_fallback.mp3"
        result = await synthesize("Ciao", "onyx", output, engine="openai")

        assert result == output
        # Should have fallen back to edge-tts
        _mock_all["Communicate"].assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_openai_fallback_uses_edge_fallback_voice(_mock_all, tmp_path, monkeypatch):
    """When OpenAI fails and edge_fallback_voice is set, use it instead of the OpenAI voice."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    with patch("mammamiradio.audio.tts._get_openai_client", side_effect=RuntimeError("API down")):
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
async def test_synthesize_openai_404_disables_only_that_voice_not_the_route(_mock_all, tmp_path, monkeypatch):
    """A bad OpenAI voice ID must not push every other OpenAI voice to Edge.

    Regression coverage: openai.APIStatusError does not subclass
    httpx.HTTPStatusError, so a naive isinstance(exc, httpx.HTTPStatusError)
    check never recognized an OpenAI 404 as voice-specific — every OpenAI
    voice error, including a simple bad voice ID, disabled the whole route.
    """
    import openai as openai_mod

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-404-key")
    calls = {"cloud": 0}

    async def _fake_openai(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        if voice == "bad-voice-id":
            request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
            response = httpx.Response(404, content=b"voice not found", request=request)
            raise openai_mod.NotFoundError("Not Found", response=response, body=None)
        return _touch(Path(output_path))

    monkeypatch.setattr("mammamiradio.audio.tts.synthesize_openai", _fake_openai)

    bad = await synthesize("Ciao", "bad-voice-id", tmp_path / "openai_404_bad.mp3", engine="openai")
    good = await synthesize("Ancora", "onyx", tmp_path / "openai_404_good.mp3", engine="openai")

    assert bad.exists() and good.exists()
    # Both voices reached the cloud call — the 404 did not disable the route.
    assert calls["cloud"] == 2
    assert _mock_all["Communicate"].call_count == 1


@pytest.mark.asyncio
async def test_synthesize_openai_auth_error_stays_disabled_past_cooldown_window(_mock_all, tmp_path, monkeypatch):
    """An OpenAI auth failure stays disabled for the session, not just 30s.

    Regression coverage: because openai.AuthenticationError never satisfied
    the old isinstance(exc, httpx.HTTPStatusError) check,
    ``_non_retryable_cloud_tts_error`` always returned "" for OpenAI, so
    every OpenAI failure — including a definitely-dead API key — was
    classified as merely transient and got retried every 30 seconds forever.
    """
    import openai as openai_mod

    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-dead-key")
    clock = [100.0]
    calls = {"cloud": 0}
    monkeypatch.setattr(tts_mod.time, "monotonic", lambda: clock[0])

    async def _fake_openai(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        request = httpx.Request("POST", "https://api.openai.com/v1/audio/speech")
        response = httpx.Response(401, content=b"invalid_api_key", request=request)
        raise openai_mod.AuthenticationError("Incorrect API key provided", response=response, body=None)

    monkeypatch.setattr(tts_mod, "synthesize_openai", _fake_openai)

    first = await synthesize("Ciao", "onyx", tmp_path / "openai_dead_first.mp3", engine="openai")
    assert first.exists()
    assert calls["cloud"] == 1

    # Past the transient-failure cooldown window, a truly dead key must NOT
    # get a half-open retry — it stays disabled until the session restarts.
    clock[0] += tts_mod._CLOUD_ROUTE_COOLDOWN_SECONDS + 0.1
    second = await synthesize("Ancora", "nova", tmp_path / "openai_dead_second.mp3", engine="openai")

    assert second.exists()
    assert calls["cloud"] == 1


@pytest.mark.asyncio
async def test_synthesize_openai_route_retries_after_transient_cooldown(_mock_all, tmp_path, monkeypatch):
    """A transient OpenAI route failure gets one half-open retry after its cooldown.

    Mirrors the Azure cooldown test — this path shares the same circuit
    breaker, but OpenAI's error classification only started working once
    ``_cloud_http_status`` learned to recognize openai.APIStatusError too.
    """
    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-cooldown-key")
    clock = [100.0]
    calls = {"cloud": 0}
    monkeypatch.setattr(tts_mod.time, "monotonic", lambda: clock[0])

    async def _fake_openai(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        if calls["cloud"] == 1:
            raise TimeoutError("temporary OpenAI outage")
        return _touch(Path(output_path))

    monkeypatch.setattr(tts_mod, "synthesize_openai", _fake_openai)

    first = await synthesize("Prima", "onyx", tmp_path / "openai_cooldown_first.mp3", engine="openai")
    second = await synthesize("Seconda", "nova", tmp_path / "openai_cooldown_second.mp3", engine="openai")

    assert first.exists() and second.exists()
    assert calls["cloud"] == 1

    clock[0] += tts_mod._CLOUD_ROUTE_COOLDOWN_SECONDS + 0.1
    third = await synthesize("Terza", "onyx", tmp_path / "openai_cooldown_third.mp3", engine="openai")

    assert third.exists()
    assert calls["cloud"] == 2


@pytest.mark.asyncio
async def test_openai_instructions_from_personality(_mock_all, tmp_path, monkeypatch):
    """synthesize_dialogue passes personality-aware instructions to OpenAI."""
    from mammamiradio.audio.tts import synthesize_dialogue
    from mammamiradio.core.models import PersonalityAxes

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

    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client_instance):
        await synthesize_dialogue([(marco, "Buongiorno!")], tmp_path)

    call_kwargs = mock_client_instance.audio.speech.create.call_args
    instructions = call_kwargs.kwargs.get("instructions") or call_kwargs[1].get("instructions", "")
    assert "High energy" in instructions
    assert "Warm" in instructions
    assert "Unpredictable" in instructions


# ---------------------------------------------------------------------------
# synthesize with cloud provider engines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_azure_happy_path(_mock_all, tmp_path, monkeypatch):
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    seen: dict[str, object] = {}

    class _AzureClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, content):
            seen["url"] = url
            seen["headers"] = headers
            seen["content"] = content.decode("utf-8")
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _AzureClient)

    output = tmp_path / "azure_out.mp3"
    result = await synthesize(
        "Ciao mondo",
        "it-IT-Isabella:DragonHDLatestNeural",
        output,
        engine="azure",
        rate="+10%",
        pitch="+5Hz",
    )

    assert result == output
    assert seen["url"] == "https://westeurope.tts.speech.microsoft.com/cognitiveservices/v1"
    assert seen["headers"]["Ocp-Apim-Subscription-Key"] == "azure-secret"
    assert 'voice name="it-IT-Isabella:DragonHDLatestNeural"' in str(seen["content"])
    assert 'rate="+10%"' in str(seen["content"])
    _mock_all["normalize"].assert_called_once()
    _mock_all["Communicate"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_azure_missing_key_falls_back_to_edge(_mock_all, tmp_path, monkeypatch):
    from mammamiradio.audio.tts import synthesize

    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)

    output = tmp_path / "azure_fallback.mp3"
    result = await synthesize(
        "Ciao",
        "it-IT-Alessio:DragonHDLatestNeural",
        output,
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert result == output
    call = _mock_all["Communicate"].call_args
    assert call[0][1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_cloud_fallback_records_route_provenance(_mock_all, tmp_path, monkeypatch, caplog):
    """A cloud fallback must be visible as degradation, not as ordinary Edge synthesis."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AZURE_SPEECH_REGION", raising=False)
    state = StationState()

    with caplog.at_level(logging.INFO, logger="mammamiradio.audio.tts"):
        result = await synthesize(
            "Ciao",
            "it-IT-Alessio:DragonHDLatestNeural",
            tmp_path / "azure_provenance.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
            host_name="Roberto",
            state=state,
        )

    assert result.exists()
    assert "TTS fallback provider=azure" in caplog.text
    assert "requested_voice=it-IT-Alessio:DragonHDLatestNeural" in caplog.text
    assert "effective_provider=edge" in caplog.text
    assert "reason=missing_credentials" in caplog.text
    assert "Synthesized (Edge fallback):" in caplog.text
    assert state.runtime_provider_state["tts_provider"]["current_provider"] == "edge"
    assert state.runtime_provider_state["tts_provider"]["fallback_active"] is True
    assert "missing_credentials" in state.runtime_provider_state["tts_provider"]["reason"]


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_happy_path(_mock_all, tmp_path, monkeypatch):
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")
    seen: dict[str, object] = {}

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)

    output = tmp_path / "eleven_out.mp3"
    result = await synthesize("Ciao mondo", "voice_italian_character", output, engine="elevenlabs")

    assert result == output
    assert seen["url"] == "https://api.elevenlabs.io/v1/text-to-speech/voice_italian_character"
    assert seen["headers"]["xi-api-key"] == "eleven-secret"
    assert seen["json"] == {
        "text": "Ciao mondo",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.42,
            "similarity_boost": 0.78,
            "style": 0.45,
            "use_speaker_boost": True,
        },
    }
    _mock_all["normalize"].assert_called_once()
    _mock_all["Communicate"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_voice_settings_default_and_override(_mock_all, tmp_path, monkeypatch):
    """voice_settings=None uses the house tuning; a dict merges over it — the
    audition harness sweeps stability without changing production callers."""
    from mammamiradio.audio.tts import synthesize_elevenlabs

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret-settings")
    seen: dict[str, object] = {}

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            seen["json"] = json
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)

    # Default: the station's house tuning (stability 0.42).
    await synthesize_elevenlabs("Ciao", "voice_a", tmp_path / "d.mp3")
    assert seen["json"]["voice_settings"]["stability"] == 0.42
    assert seen["json"]["voice_settings"]["similarity_boost"] == 0.78

    # Override merges over the defaults — only stability changes.
    await synthesize_elevenlabs("Ciao", "voice_a", tmp_path / "o.mp3", voice_settings={"stability": 0.7})
    assert seen["json"]["voice_settings"]["stability"] == 0.7
    assert seen["json"]["voice_settings"]["similarity_boost"] == 0.78  # untouched default
    assert seen["json"]["voice_settings"]["use_speaker_boost"] is True


@pytest.mark.asyncio
async def test_synthesize_threads_voice_settings_to_elevenlabs(_mock_all, tmp_path, monkeypatch):
    """synthesize(engine='elevenlabs', voice_settings=...) forwards the override to the
    ElevenLabs payload — the path a host's per-voice settings travel (e.g. Marco's stability)."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-thread-key")
    seen: dict[str, object] = {}

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            seen["json"] = json
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)

    await synthesize("Ciao", "voice_x", tmp_path / "t.mp3", engine="elevenlabs", voice_settings={"stability": 0.6})
    assert seen["json"]["voice_settings"]["stability"] == 0.6
    assert seen["json"]["voice_settings"]["similarity_boost"] == 0.78  # other house defaults preserved


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_v3_uses_only_stability_and_code_owned_tag(
    _mock_all, tmp_path, monkeypatch, caplog
):
    """V3 receives only compatible tuning and semantic—not raw—delivery markup."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-v3-key")
    seen: dict[str, object] = {}

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, headers, json):
            seen["json"] = json
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)

    with caplog.at_level(logging.INFO, logger="mammamiradio.audio.tts"):
        await synthesize(
            "Ciao Giulia",
            "voice_marco",
            tmp_path / "v3.mp3",
            engine="elevenlabs",
            elevenlabs_model="eleven_v3",
            delivery_profile="marco",
            delivery_cue="energetic",
            voice_settings={"stability": 0.6},
            host_name="Marco",
        )

    assert seen["json"] == {
        "text": "[excited] Ciao Giulia",
        "model_id": "eleven_v3",
        "voice_settings": {"stability": 0.6},
    }
    assert "host=Marco model=eleven_v3 delivery=energetic" in caplog.text


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_v3_keeps_provider_default_and_rejects_raw_tag_input(
    _mock_all, tmp_path, monkeypatch
):
    """A raw bracket tag is not an instruction; Giulia's omitted stability stays provider-default."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-v3-default-key")
    seen: dict[str, object] = {}

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, headers, json):
            seen["json"] = json
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)

    await synthesize(
        "Non mi impressiona.",
        "voice_giulia",
        tmp_path / "v3-default.mp3",
        engine="elevenlabs",
        elevenlabs_model="eleven_v3",
        delivery_profile="giulia",
        delivery_cue="[laughs]",
    )

    assert seen["json"] == {
        "text": "Non mi impressiona.",
        "model_id": "eleven_v3",
    }


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_v3_fallback_receives_clean_text(_mock_all, tmp_path, monkeypatch):
    """A V3 failure cannot make Edge pronounce its internal performance tag."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-v3-fallback-key")
    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _HttpErrorClient)

    await synthesize(
        "Questo resta pulito.",
        "voice_marco",
        tmp_path / "v3-fallback.mp3",
        engine="elevenlabs",
        edge_fallback_voice="it-IT-DiegoNeural",
        elevenlabs_model="eleven_v3",
        delivery_profile="marco",
        delivery_cue="energetic",
    )

    assert _mock_all["Communicate"].call_args.args[0] == "Questo resta pulito."
    assert _mock_all["Communicate"].call_args.args[1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_v3_strips_model_bracket_directives_from_payload(_mock_all, tmp_path, monkeypatch):
    """Non-banter host speech (transitions/news) is not pre-cleaned, so the V3 boundary
    must strip model-emitted bracket directives — only the code-owned tag may be markup."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-v3-strip-key")
    seen: dict[str, object] = {}

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, headers, json):
            seen["json"] = json
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)

    # Neutral delivery (e.g. a news flash): raw directive stripped, no code tag added.
    await synthesize(
        "[sospira] Ultim'ora dalla redazione.",
        "voice_marco",
        tmp_path / "v3-news.mp3",
        engine="elevenlabs",
        elevenlabs_model="eleven_v3",
        delivery_profile="marco",
    )
    assert seen["json"]["text"] == "Ultim'ora dalla redazione."

    # Authorized cue: the code-owned tag survives, the smuggled directive does not.
    await synthesize(
        "Musica [laughs] adesso.",
        "voice_marco",
        tmp_path / "v3-banter.mp3",
        engine="elevenlabs",
        elevenlabs_model="eleven_v3",
        delivery_profile="marco",
        delivery_cue="energetic",
    )
    assert seen["json"]["text"] == "[excited] Musica adesso."


def test_elevenlabs_failure_memoization_key_is_model_specific(monkeypatch):
    from mammamiradio.audio.tts import _cloud_failure_key

    monkeypatch.setenv("ELEVENLABS_API_KEY", "same-account")
    v2_key = _cloud_failure_key("elevenlabs", "same-voice", elevenlabs_model="eleven_multilingual_v2")
    v3_key = _cloud_failure_key("elevenlabs", "same-voice", elevenlabs_model="eleven_v3")

    assert v2_key != v3_key
    assert v2_key[-1] == "eleven_multilingual_v2"
    assert v3_key[-1] == "eleven_v3"


class _HttpErrorClient:
    """httpx.AsyncClient stub whose POST returns a non-2xx response.

    raise_for_status() on the returned response trips, exercising the cloud
    engine's try/except → edge fallback chain (not the pre-flight missing-key
    branch).
    """

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        return httpx.Response(500, content=b"upstream boom", request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_synthesize_azure_auth_error_is_memoized_for_session(_mock_all, tmp_path, monkeypatch, caplog):
    """A non-retryable Azure auth/config failure warns once, then skips cloud retry."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "revoked-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    seen = {"posts": 0}

    class _AuthErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            seen["posts"] += 1
            return httpx.Response(401, content=b"unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _AuthErrorClient)

    with caplog.at_level(logging.WARNING, logger="mammamiradio.audio.tts"):
        await synthesize(
            "Ciao",
            "it-IT-Isabella:DragonHDLatestNeural",
            tmp_path / "first.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
        await synthesize(
            "Ancora",
            "it-IT-Isabella:DragonHDLatestNeural",
            tmp_path / "second.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    assert seen["posts"] == 1
    assert caplog.text.count("Azure TTS disabled for voice 'it-IT-Isabella:DragonHDLatestNeural'") == 1
    assert _mock_all["Communicate"].call_args_list[0].args[1] == "it-IT-DiegoNeural"
    assert _mock_all["Communicate"].call_args_list[1].args[1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_azure_auth_error_lock_collapses_concurrent_attempts(_mock_all, tmp_path, monkeypatch, caplog):
    """Concurrent calls for the same bad Azure voice should not duplicate the cloud POST."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "revoked-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
    post_started = asyncio.Event()
    release_post = asyncio.Event()
    seen = {"posts": 0}

    class _SlowAuthErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            seen["posts"] += 1
            post_started.set()
            await release_post.wait()
            return httpx.Response(401, content=b"unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _SlowAuthErrorClient)

    async def _call(text: str, name: str):
        return await synthesize(
            text,
            "it-IT-Isabella:DragonHDLatestNeural",
            tmp_path / f"{name}.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    with caplog.at_level(logging.WARNING, logger="mammamiradio.audio.tts"):
        first = asyncio.create_task(_call("Ciao", "first"))
        await asyncio.wait_for(post_started.wait(), timeout=1.0)
        second = asyncio.create_task(_call("Ancora", "second"))
        for _ in range(10):
            await asyncio.sleep(0)
            if seen["posts"] > 1:
                break
        assert seen["posts"] == 1
        assert _mock_all["Communicate"].call_count == 0

        release_post.set()
        await asyncio.gather(first, second)

    assert seen["posts"] == 1
    assert caplog.text.count("Azure TTS disabled for voice 'it-IT-Isabella:DragonHDLatestNeural'") == 1
    assert [call.args[1] for call in _mock_all["Communicate"].call_args_list] == [
        "it-IT-DiegoNeural",
        "it-IT-DiegoNeural",
    ]


@pytest.mark.asyncio
async def test_synthesize_azure_401_disables_route_for_a_different_voice_too(_mock_all, tmp_path, monkeypatch):
    """A 401 must block the whole route, not just the voice that hit it.

    The auth-memoization tests above only ever reuse the SAME voice, so they'd
    pass even if just per-voice memoization (not route memoization) were doing
    the work. This proves a 401 on one voice also stops a DIFFERENT voice on
    the same Azure route from reaching the cloud at all.
    """
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-401-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    calls = {"cloud": 0}

    async def _fake_azure(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        request = httpx.Request("POST", "https://example.invalid/tts")
        response = httpx.Response(401, content=b"unauthorized", request=request)
        raise httpx.HTTPStatusError("Unauthorized", request=request, response=response)

    monkeypatch.setattr("mammamiradio.audio.tts.synthesize_azure", _fake_azure)

    first = await synthesize(
        "Ciao",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "azure_401_first.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    second = await synthesize(
        "Ancora",
        "it-IT-Alessio:DragonHDLatestNeural",
        tmp_path / "azure_401_second.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert first.exists() and second.exists()
    # Only the first (different) voice actually reached Azure — the route,
    # not just that one voice, was disabled after the 401.
    assert calls["cloud"] == 1
    assert _mock_all["Communicate"].call_count == 2


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_auth_error_is_memoized_for_session(_mock_all, tmp_path, monkeypatch, caplog):
    """A non-retryable ElevenLabs auth/config failure warns once, then skips cloud retry."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "revoked-key")
    seen = {"posts": 0}

    class _AuthErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            seen["posts"] += 1
            return httpx.Response(401, content=b"unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _AuthErrorClient)

    with caplog.at_level(logging.WARNING, logger="mammamiradio.audio.tts"):
        await synthesize(
            "Ciao",
            "elevenlabs-voice-id",
            tmp_path / "first.mp3",
            engine="elevenlabs",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
        await synthesize(
            "Ancora",
            "elevenlabs-voice-id",
            tmp_path / "second.mp3",
            engine="elevenlabs",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    assert seen["posts"] == 1
    assert caplog.text.count("ElevenLabs TTS disabled for voice 'elevenlabs-voice-id'") == 1
    assert _mock_all["Communicate"].call_args_list[0].args[1] == "it-IT-DiegoNeural"
    assert _mock_all["Communicate"].call_args_list[1].args[1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_auth_error_lock_collapses_concurrent_attempts(
    _mock_all, tmp_path, monkeypatch, caplog
):
    """Concurrent calls for the same bad ElevenLabs voice should not duplicate the cloud POST."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "revoked-key")
    post_started = asyncio.Event()
    release_post = asyncio.Event()
    seen = {"posts": 0}

    class _SlowAuthErrorClient:
        def __init__(self, *args, **kwargs):
            pass

        async def post(self, url, **kwargs):
            seen["posts"] += 1
            post_started.set()
            await release_post.wait()
            return httpx.Response(401, content=b"unauthorized", request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _SlowAuthErrorClient)

    async def _call(text: str, name: str):
        return await synthesize(
            text,
            "elevenlabs-voice-id",
            tmp_path / f"{name}.mp3",
            engine="elevenlabs",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    with caplog.at_level(logging.WARNING, logger="mammamiradio.audio.tts"):
        first = asyncio.create_task(_call("Ciao", "first"))
        await asyncio.wait_for(post_started.wait(), timeout=1.0)
        second = asyncio.create_task(_call("Ancora", "second"))
        for _ in range(10):
            await asyncio.sleep(0)
            if seen["posts"] > 1:
                break
        assert seen["posts"] == 1
        assert _mock_all["Communicate"].call_count == 0

        release_post.set()
        await asyncio.gather(first, second)

    assert seen["posts"] == 1
    assert caplog.text.count("ElevenLabs TTS disabled for voice 'elevenlabs-voice-id'") == 1
    assert [call.args[1] for call in _mock_all["Communicate"].call_args_list] == [
        "it-IT-DiegoNeural",
        "it-IT-DiegoNeural",
    ]


@pytest.mark.asyncio
async def test_synthesize_azure_http_error_falls_back_to_edge(_mock_all, tmp_path, monkeypatch):
    """Azure returning 5xx mid-synthesis must degrade to edge-tts, not dead air."""
    from mammamiradio.audio.tts import synthesize

    # Distinct creds from the happy-path test so the singleton client cache misses.
    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-err-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _HttpErrorClient)

    output = tmp_path / "azure_http_error.mp3"
    result = await synthesize(
        "Ciao",
        "it-IT-Isabella:DragonHDLatestNeural",
        output,
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert result == output
    # Edge fallback ran with the configured edge voice — the illusion is preserved.
    call = _mock_all["Communicate"].call_args
    assert call[0][1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_cloud_route_failure_is_not_retried_for_each_ad_voice(
    _mock_all, tmp_path, monkeypatch, caplog
):
    """A provider-wide 5xx must not spend another cloud timeout on each voice."""
    import logging

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-route-err-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    seen = {"posts": 0}

    class _CountingHttpErrorClient(_HttpErrorClient):
        async def post(self, url, **kwargs):
            seen["posts"] += 1
            return await super().post(url, **kwargs)

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _CountingHttpErrorClient)

    with caplog.at_level(logging.INFO, logger="mammamiradio.audio.tts"):
        first = await synthesize(
            "Prima",
            "it-IT-Isabella:DragonHDLatestNeural",
            tmp_path / "route_first.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
        second = await synthesize(
            "Seconda",
            "it-IT-Alessio:DragonHDLatestNeural",
            tmp_path / "route_second.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    assert first.exists() and second.exists()
    assert seen["posts"] == 1
    assert _mock_all["Communicate"].call_count == 2
    assert "Azure TTS route cooldown" in caplog.text


@pytest.mark.asyncio
async def test_synthesize_healthy_route_renders_different_voices_concurrently(_mock_all, tmp_path, monkeypatch):
    """Marco and Giulia's lines on one healthy route must overlap, not queue.

    Regression guard for the route-wide mutex that briefly serialized every
    dialogue line on a shared provider route — it doubled banter render time
    on every break. Only the half-open probe is single-flight; healthy
    traffic never is.
    """
    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-healthy-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    first_in_provider = asyncio.Event()
    second_in_provider = asyncio.Event()
    release = asyncio.Event()

    async def _fake_azure(text, voice, output_path, **kwargs):
        if voice == "it-IT-Isabella:DragonHDLatestNeural":
            first_in_provider.set()
        else:
            second_in_provider.set()
        await release.wait()
        return _touch(Path(output_path))

    monkeypatch.setattr(tts_mod, "synthesize_azure", _fake_azure)

    first = asyncio.create_task(
        synthesize(
            "Prima",
            "it-IT-Isabella:DragonHDLatestNeural",
            tmp_path / "healthy_first.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
    )
    await asyncio.wait_for(first_in_provider.wait(), timeout=1.0)
    second = asyncio.create_task(
        synthesize(
            "Seconda",
            "it-IT-Alessio:DragonHDLatestNeural",
            tmp_path / "healthy_second.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
    )
    # Both provider calls must be in flight AT THE SAME TIME while the first
    # is still blocked — a route-wide lock would leave the second waiting.
    await asyncio.wait_for(second_in_provider.wait(), timeout=1.0)

    release.set()
    first_result, second_result = await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
    assert first_result.exists() and second_result.exists()


@pytest.mark.asyncio
async def test_synthesize_cloud_route_recheck_stops_timeout_cascade(_mock_all, tmp_path, monkeypatch):
    """A call queued behind an outage must not fire its own doomed request.

    With two render slots, a third voice passes the breaker check while the
    first two calls are still hanging, then waits for a slot. Without the
    post-acquisition re-check it would fire its own full-timeout call as a
    slot freed — stacking timeout waves instead of going straight to Edge.
    """
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-cascade-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    posts_started: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]
    release_posts = asyncio.Event()
    seen = {"posts": 0}

    class _BlockingCountingHttpErrorClient(_HttpErrorClient):
        async def post(self, url, **kwargs):
            seen["posts"] += 1
            if seen["posts"] <= 2:
                posts_started[seen["posts"] - 1].set()
            await release_posts.wait()
            return await super().post(url, **kwargs)

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _BlockingCountingHttpErrorClient)

    voices = [
        "it-IT-Isabella:DragonHDLatestNeural",
        "it-IT-Alessio:DragonHDLatestNeural",
        "it-IT-Marcello:DragonHDLatestNeural",
    ]
    first = asyncio.create_task(
        synthesize(
            "Prima", voices[0], tmp_path / "cascade_1.mp3", engine="azure", edge_fallback_voice="it-IT-DiegoNeural"
        )
    )
    await asyncio.wait_for(posts_started[0].wait(), timeout=1.0)
    second = asyncio.create_task(
        synthesize(
            "Seconda", voices[1], tmp_path / "cascade_2.mp3", engine="azure", edge_fallback_voice="it-IT-DiegoNeural"
        )
    )
    await asyncio.wait_for(posts_started[1].wait(), timeout=1.0)
    # Both render slots are now occupied by hanging cloud calls. The third
    # voice passes the pre-check (breaker still closed) and queues on a slot.
    third = asyncio.create_task(
        synthesize(
            "Terza", voices[2], tmp_path / "cascade_3.mp3", engine="azure", edge_fallback_voice="it-IT-DiegoNeural"
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert seen["posts"] == 2

    release_posts.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, third), timeout=2.0)

    assert all(r.exists() for r in results)
    # The third call re-checked the breaker after getting its slot and went
    # straight to Edge — no third doomed provider request.
    assert seen["posts"] == 2
    assert _mock_all["Communicate"].call_count == 3


@pytest.mark.asyncio
async def test_synthesize_cloud_route_half_open_probe_is_single_flight(_mock_all, tmp_path, monkeypatch):
    """After the cooldown, exactly one caller probes; others stay on Edge."""
    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-probe-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    clock = [100.0]
    monkeypatch.setattr(tts_mod.time, "monotonic", lambda: clock[0])

    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    calls = {"cloud": 0}

    async def _fake_azure(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        if calls["cloud"] == 1:
            raise TimeoutError("temporary Azure outage")
        probe_started.set()
        await release_probe.wait()
        return _touch(Path(output_path))

    monkeypatch.setattr(tts_mod, "synthesize_azure", _fake_azure)

    tripped = await synthesize(
        "Prima",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "probe_trip.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    assert tripped.exists()
    assert calls["cloud"] == 1

    clock[0] += tts_mod._CLOUD_ROUTE_COOLDOWN_SECONDS + 0.1
    probe = asyncio.create_task(
        synthesize(
            "Seconda",
            "it-IT-Alessio:DragonHDLatestNeural",
            tmp_path / "probe_winner.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1.0)
    # While the probe is in flight, another voice must NOT get a second probe.
    bystander = await asyncio.wait_for(
        synthesize(
            "Terza",
            "it-IT-Marcello:DragonHDLatestNeural",
            tmp_path / "probe_bystander.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        ),
        timeout=2.0,
    )
    assert bystander.exists()
    assert calls["cloud"] == 2  # trip + the single probe; no third call

    release_probe.set()
    probe_result = await asyncio.wait_for(probe, timeout=2.0)
    assert probe_result.exists()

    # The successful probe closed the breaker: the next call goes to the cloud.
    recovered = await synthesize(
        "Quarta",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "probe_recovered.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    assert recovered.exists()
    assert calls["cloud"] == 3


@pytest.mark.asyncio
async def test_synthesize_probe_success_outranks_stale_concurrent_failure(_mock_all, tmp_path, monkeypatch):
    """A successful probe reopens the route even if a straggler failed meanwhile.

    A call that was already in flight when the breaker first tripped can fail
    AFTER the probe started and install a fresh cooldown over the probe
    marker. The probe's success is fresher evidence — it must win, or the
    route sits on Edge another 30 seconds despite a proven-healthy provider.
    """
    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-stale-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    clock = [100.0]
    monkeypatch.setattr(tts_mod.time, "monotonic", lambda: clock[0])

    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    calls = {"cloud": 0}

    async def _fake_azure(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        if calls["cloud"] == 1:
            raise TimeoutError("temporary Azure outage")
        probe_started.set()
        await release_probe.wait()
        return _touch(Path(output_path))

    monkeypatch.setattr(tts_mod, "synthesize_azure", _fake_azure)

    tripped = await synthesize(
        "Prima",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "stale_trip.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    assert tripped.exists()

    clock[0] += tts_mod._CLOUD_ROUTE_COOLDOWN_SECONDS + 0.1
    route_key = tts_mod._cloud_route_key("azure")
    probe = asyncio.create_task(
        synthesize(
            "Seconda",
            "it-IT-Alessio:DragonHDLatestNeural",
            tmp_path / "stale_probe.mp3",
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )
    )
    await asyncio.wait_for(probe_started.wait(), timeout=1.0)
    # A straggler from before the trip fails route-wide mid-probe.
    tts_mod._memoize_failed_cloud_route(route_key, retryable=True)

    release_probe.set()
    assert (await asyncio.wait_for(probe, timeout=2.0)).exists()

    # The probe's success reopened the route despite the stale cooldown.
    third = await synthesize(
        "Terza",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "stale_third.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    assert third.exists()
    assert calls["cloud"] == 3


def test_memoize_never_downgrades_a_session_disable_to_a_cooldown():
    """A straggler timeout must not turn a revoked-key disable into 30s retries."""
    import mammamiradio.audio.tts as tts_mod

    tts_mod.reset_voice_failures()
    route_key = ("azure", "westeurope", "fp", "")
    try:
        tts_mod._memoize_failed_cloud_route(route_key, retryable=False)
        tts_mod._memoize_failed_cloud_route(route_key, retryable=True)
        assert tts_mod._claim_cloud_route(route_key) == "permanent"
    finally:
        tts_mod.reset_voice_failures()


def test_should_disable_cloud_route_ignores_local_post_provider_failures():
    """FFmpeg/disk errors after the provider answered say nothing about the route."""
    import openai as openai_mod

    from mammamiradio.audio.tts import _should_disable_cloud_route

    # Local failures: never route evidence.
    assert _should_disable_cloud_route(RuntimeError("normalize failed")) is False
    assert _should_disable_cloud_route(OSError("disk full")) is False

    # Provider/network failures: route-wide.
    assert _should_disable_cloud_route(TimeoutError("hung")) is True
    request = httpx.Request("POST", "https://example.invalid/tts")
    assert _should_disable_cloud_route(httpx.ConnectError("refused", request=request)) is True
    response_500 = httpx.Response(500, request=request)
    assert _should_disable_cloud_route(httpx.HTTPStatusError("boom", request=request, response=response_500)) is True
    response_401 = httpx.Response(401, request=request, content=b"unauthorized")
    assert (
        _should_disable_cloud_route(openai_mod.AuthenticationError("bad key", response=response_401, body=None)) is True
    )

    # Voice-specific statuses: never route-wide, from either error family.
    response_404 = httpx.Response(404, request=request, content=b"voice not found")
    assert _should_disable_cloud_route(httpx.HTTPStatusError("nf", request=request, response=response_404)) is False
    assert _should_disable_cloud_route(openai_mod.NotFoundError("nf", response=response_404, body=None)) is False


@pytest.mark.asyncio
async def test_synthesize_cloud_route_retries_after_transient_cooldown(_mock_all, tmp_path, monkeypatch):
    """A transient route failure gets one half-open retry after its cooldown."""
    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-cooldown-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    clock = [100.0]
    calls = {"cloud": 0}
    monkeypatch.setattr(tts_mod.time, "monotonic", lambda: clock[0])

    async def _fake_azure(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        if calls["cloud"] == 1:
            raise TimeoutError("temporary Azure outage")
        return _touch(Path(output_path))

    monkeypatch.setattr(tts_mod, "synthesize_azure", _fake_azure)

    first = await synthesize(
        "Prima",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "cooldown_first.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    second = await synthesize(
        "Seconda",
        "it-IT-Alessio:DragonHDLatestNeural",
        tmp_path / "cooldown_second.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert first.exists() and second.exists()
    assert calls["cloud"] == 1

    clock[0] += tts_mod._CLOUD_ROUTE_COOLDOWN_SECONDS + 0.1
    third = await synthesize(
        "Terza",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "cooldown_third.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert third.exists()
    assert calls["cloud"] == 2


@pytest.mark.asyncio
async def test_synthesize_azure_404_disables_only_that_voice_not_the_route(_mock_all, tmp_path, monkeypatch):
    """A 404 for one bad voice ID must not push other configured voices to Edge."""
    import mammamiradio.audio.tts as tts_mod
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-404-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "eastus")
    calls = {"cloud": 0}

    async def _fake_azure(text, voice, output_path, **kwargs):
        calls["cloud"] += 1
        if voice == "it-IT-BadVoice:DragonHDLatestNeural":
            request = httpx.Request("POST", "https://example.invalid/tts")
            response = httpx.Response(404, content=b"voice not found", request=request)
            raise httpx.HTTPStatusError("Not Found", request=request, response=response)
        return _touch(Path(output_path))

    monkeypatch.setattr(tts_mod, "synthesize_azure", _fake_azure)

    bad = await synthesize(
        "Ciao",
        "it-IT-BadVoice:DragonHDLatestNeural",
        tmp_path / "bad.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    good = await synthesize(
        "Ancora",
        "it-IT-Isabella:DragonHDLatestNeural",
        tmp_path / "good.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert bad.exists() and good.exists()
    # Both voices reached the cloud call — the 404 did not disable the route.
    assert calls["cloud"] == 2
    # Only the bad voice fell back to Edge; the good voice was served by the cloud stub.
    assert _mock_all["Communicate"].call_count == 1
    assert _mock_all["Communicate"].call_args_list[0].args[1] == "it-IT-DiegoNeural"

    # The bad voice itself stays memoized this session (per-voice, not route-wide).
    again = await synthesize(
        "Ancora una volta",
        "it-IT-BadVoice:DragonHDLatestNeural",
        tmp_path / "bad_again.mp3",
        engine="azure",
        edge_fallback_voice="it-IT-DiegoNeural",
    )
    assert again.exists()
    assert calls["cloud"] == 2


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_http_error_falls_back_to_edge(_mock_all, tmp_path, monkeypatch):
    """ElevenLabs returning 5xx mid-synthesis must degrade to edge-tts, not dead air."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-err-key")
    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _HttpErrorClient)

    output = tmp_path / "eleven_http_error.mp3"
    result = await synthesize(
        "Ciao",
        "voice_italian_character",
        output,
        engine="elevenlabs",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert result == output
    call = _mock_all["Communicate"].call_args
    assert call[0][1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_missing_key_falls_back_to_edge(_mock_all, tmp_path, monkeypatch):
    """No ELEVENLABS_API_KEY → edge fallback (mirrors the Azure missing-key case)."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    output = tmp_path / "eleven_fallback.mp3"
    result = await synthesize(
        "Ciao",
        "voice_italian_character",
        output,
        engine="elevenlabs",
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert result == output
    call = _mock_all["Communicate"].call_args
    assert call[0][1] == "it-IT-DiegoNeural"


@pytest.mark.asyncio
async def test_synthesize_azure_full_failure_fails_closed(_mock_all, tmp_path, monkeypatch):
    """Azure 5xx plus Edge outage propagates after the complete fallback chain."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-silence-key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "northeurope")
    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _HttpErrorClient)
    # Edge save also fails — both the cloud and the edge path are down.
    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("edge down"))

    output = tmp_path / "azure_then_failure.mp3"
    with pytest.raises(TTSUnavailableError, match="all configured TTS routes"):
        await synthesize(
            "Ciao",
            "it-IT-Alessio:DragonHDLatestNeural",
            output,
            engine="azure",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    assert not output.exists()
    _mock_all["generate_silence"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_elevenlabs_full_failure_fails_closed(_mock_all, tmp_path, monkeypatch):
    """ElevenLabs 5xx plus Edge outage propagates after the complete fallback chain."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-silence-key")
    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _HttpErrorClient)
    # Edge save also fails — both the cloud and the edge path are down.
    _mock_all["comm_instance"].save = AsyncMock(side_effect=RuntimeError("edge down"))

    output = tmp_path / "eleven_then_failure.mp3"
    with pytest.raises(TTSUnavailableError, match="all configured TTS routes"):
        await synthesize(
            "Ciao",
            "voice_italian_character",
            output,
            engine="elevenlabs",
            edge_fallback_voice="it-IT-DiegoNeural",
        )

    assert not output.exists()
    _mock_all["generate_silence"].assert_not_called()


# ---------------------------------------------------------------------------
# synthesize_ad
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_ad_disclaimer_goblin_rate(_mock_all, tmp_path):
    """Disclaimer speed is format-scoped and no longer the old near-2x spike."""
    from mammamiradio.audio.tts import synthesize_ad

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

    # Check that Communicate was called with the classic-pitch disclaimer rate.
    calls = _mock_all["Communicate"].call_args_list
    assert len(calls) >= 1
    found_rate = False
    for call in calls:
        kwargs = call.kwargs if call.kwargs else {}
        if kwargs.get("rate") == "+55%":
            found_rate = True
            break
    assert found_rate, f"Expected rate='+55%' in Communicate calls, got: {calls}"


@pytest.mark.asyncio
async def test_synthesize_ad_passes_voice_engine_and_fallback(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

    script = AdScript(
        brand="Velocino",
        parts=[AdPart(type="voice", text="Una macchina che urla!", role="hammer")],
        mood="lounge",
    )
    voices = {
        "hammer": AdVoice(
            name="Roberto",
            voice="marin",
            style="booming",
            role="hammer",
            engine="openai",
            edge_fallback_voice="it-IT-DiegoNeural",
        ),
    }
    synth_calls: list[tuple[str, dict]] = []

    async def _fake_synthesize(text, voice, output_path, **kwargs):
        synth_calls.append((voice, kwargs))
        return _touch(output_path)

    with patch("mammamiradio.audio.tts.synthesize", side_effect=_fake_synthesize):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert synth_calls
    voice, kwargs = synth_calls[0]
    assert voice == "marin"
    assert kwargs["engine"] == "openai"
    assert kwargs["edge_fallback_voice"] == "it-IT-DiegoNeural"
    assert "booming" in kwargs["openai_instructions"]


@pytest.mark.asyncio
async def test_synthesize_ad_voice_sfx_pause(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

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
    from mammamiradio.audio.tts import synthesize_ad

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
async def test_synthesize_ad_optional_sfx_total_failure_removes_partial(_mock_all, tmp_path):
    """A decorative SFX outage is omittable but cannot leak its partial file."""
    from mammamiradio.audio.tts import synthesize_ad

    def _fail_optional(path: Path, *_args, **_kwargs):
        path.write_bytes(b"partial optional audio")
        raise RuntimeError("optional renderer unavailable")

    script = AdScript(
        brand="EspressoPlus",
        parts=[
            AdPart(type="voice", text="Vuoi un caffè?"),
            AdPart(type="sfx", sfx="cash_register"),
        ],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    with (
        patch("mammamiradio.audio.tts.generate_sfx", side_effect=_fail_optional),
        patch("mammamiradio.audio.tts.generate_silence", side_effect=_fail_optional),
    ):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert not list(tmp_path.glob("adpart_*.mp3"))
    assert not list(tmp_path.glob("adpart_*.raw.mp3"))


@pytest.mark.asyncio
async def test_synthesize_ad_required_voice_failure_waits_for_sibling_and_cleans_scratch(_mock_all, tmp_path):
    """One failed voice aborts only after successful sibling writes have settled."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize_ad

    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def _fake_synthesize(text, voice, output_path, **kwargs):
        if text == "bad voice":
            await sibling_started.wait()
            output_path.with_suffix(".raw.mp3").write_bytes(b"partial")
            raise TTSUnavailableError("voice unavailable")
        sibling_started.set()
        await asyncio.sleep(0.01)
        _touch(output_path)
        sibling_finished.set()
        return output_path

    script = AdScript(
        brand="Voce Vera",
        parts=[
            AdPart(type="voice", text="bad voice"),
            AdPart(type="voice", text="good voice"),
            AdPart(type="sfx", sfx="chime"),
        ],
        sonic=SonicWorld(sonic_signature="chime"),
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_fake_synthesize),
        pytest.raises(TTSUnavailableError, match="voice unavailable"),
    ):
        await synthesize_ad(script, voices, tmp_path)

    assert sibling_finished.is_set()
    assert not list(tmp_path.glob("adpart_*.mp3"))
    assert not list(tmp_path.glob("adpart_*.raw.mp3"))
    assert not list(tmp_path.glob("motif_*.mp3"))
    _mock_all["generate_music_bed"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_ad_prioritizes_total_tts_outage_over_sibling_error(_mock_all, tmp_path):
    """A simultaneous generic renderer error cannot hide required voice outage."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize_ad

    async def _failed_voice(text, _voice, output_path, **_kwargs):
        output_path.write_bytes(b"partial voice")
        if text == "generic failure":
            raise RuntimeError("local renderer failed")
        raise TTSUnavailableError("all voice routes unavailable")

    script = AdScript(
        brand="Priorita Voce",
        parts=[
            AdPart(type="voice", text="generic failure"),
            AdPart(type="voice", text="typed failure"),
        ],
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_failed_voice),
        pytest.raises(TTSUnavailableError, match="all voice routes unavailable"),
    ):
        await synthesize_ad(script, voices, tmp_path)

    assert not list(tmp_path.glob("adpart_*.mp3"))
    _mock_all["generate_music_bed"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_ad_cancellation_waits_for_voice_then_cleans_scratch(_mock_all, tmp_path):
    """Cancelling an ad waits for owned voice work before deleting its outputs."""
    from mammamiradio.audio.tts import synthesize_ad

    voice_started = asyncio.Event()
    release_voice = asyncio.Event()
    voice_finished = asyncio.Event()

    async def _late_synthesize(text, voice, output_path, **kwargs):
        voice_started.set()
        await release_voice.wait()
        _touch(output_path)
        output_path.with_suffix(".raw.mp3").write_bytes(b"late raw")
        voice_finished.set()
        return output_path

    script = AdScript(
        brand="Voce Paziente",
        parts=[AdPart(type="voice", text="Aspetta la voce.")],
        sonic=SonicWorld(sonic_signature="chime"),
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.audio.tts.synthesize", side_effect=_late_synthesize):
        task = asyncio.create_task(synthesize_ad(script, voices, tmp_path))
        await asyncio.wait_for(voice_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for the owned voice renderer"
        release_voice.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert voice_finished.is_set()
    assert not list(tmp_path.glob("adpart_*.mp3"))
    assert not list(tmp_path.glob("adpart_*.raw.mp3"))
    assert not list(tmp_path.glob("motif_*.mp3"))


@pytest.mark.asyncio
async def test_synthesize_ad_failed_motif_removes_partial_before_voice_only_success(_mock_all, tmp_path):
    """A decorative motif failure cannot leave its partially-written scratch file."""
    from mammamiradio.audio.tts import synthesize_ad

    def _partial_motif(path: Path, *_args, **_kwargs) -> Path:
        path.write_bytes(b"partial motif")
        raise RuntimeError("motif render failed")

    _mock_all["generate_brand_motif"].side_effect = _partial_motif
    script = AdScript(
        brand="Motivo Pulito",
        parts=[AdPart(type="voice", text="La voce resta completa.")],
        sonic=SonicWorld(sonic_signature="chime"),
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert not list(tmp_path.glob("motif_*.mp3"))


@pytest.mark.asyncio
async def test_synthesize_ad_bed_failure_waits_for_executor_siblings(_mock_all, tmp_path):
    """One failed ad bed cannot let a still-running sibling outlive assembly."""
    from mammamiradio.audio.tts import synthesize_ad

    sibling_started = threading.Event()
    release_sibling = threading.Event()
    sibling_finished = threading.Event()

    def _bed(path: Path, mood: str, duration: float) -> Path:
        if mood == "showroom":
            sibling_started.set()
            assert release_sibling.wait(timeout=2.0)
            _touch(path)
            sibling_finished.set()
            return path
        assert sibling_started.wait(timeout=1.0)
        raise RuntimeError("main bed failed")

    _mock_all["generate_music_bed"].side_effect = _bed
    script = AdScript(
        brand="Letti Uniti",
        parts=[AdPart(type="voice", text="Ogni letto finisce.")],
        mood="lounge",
        sonic=SonicWorld(environment="showroom"),
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    task = asyncio.create_task(synthesize_ad(script, voices, tmp_path))
    async with asyncio.timeout(1.0):
        while not sibling_started.is_set():
            await asyncio.sleep(0.001)
    await asyncio.sleep(0.02)
    completed_before_release = task.done()
    release_sibling.set()
    result = await task

    assert not completed_before_release, "ad assembly must wait until every executor-backed bed settles"
    assert sibling_finished.is_set()
    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_bed_cancellation_waits_then_cleans_owned_audio(_mock_all, tmp_path):
    """Cancellation during optional bed fan-out waits, then removes ad scratch."""
    from mammamiradio.audio.tts import synthesize_ad

    bed_started = threading.Event()
    release_beds = threading.Event()

    def _slow_bed(path: Path, *_args, **_kwargs) -> Path:
        bed_started.set()
        assert release_beds.wait(timeout=2.0)
        _touch(path)
        return path

    _mock_all["generate_music_bed"].side_effect = _slow_bed
    script = AdScript(
        brand="Letti Cancellati",
        parts=[AdPart(type="voice", text="La voce non resta indietro.")],
        mood="lounge",
        sonic=SonicWorld(environment="showroom"),
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    task = asyncio.create_task(synthesize_ad(script, voices, tmp_path))
    async with asyncio.timeout(1.0):
        while not bed_started.is_set():
            await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation must wait for executor-backed bed renderers"
    release_beds.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    for pattern in ("adpart_*.mp3", "adbed_*.mp3", "envbed_*.mp3", "foley_*.mp3"):
        assert not list(tmp_path.glob(pattern))


@pytest.mark.asyncio
async def test_synthesize_ad_final_mix_cancellation_waits_then_cleans_all_audio(_mock_all, tmp_path):
    """Cancellation after bed fan-out waits for the sequential mix worker before cleanup."""
    from mammamiradio.audio.tts import synthesize_ad

    mix_started = threading.Event()
    release_mix = threading.Event()

    def _slow_mix(_voice_path, _bed_path, output_path, _volume_scale=0.12):
        mix_started.set()
        assert release_mix.wait(timeout=2.0)
        _touch(output_path)
        return output_path

    _mock_all["mix_with_bed"].side_effect = _slow_mix
    script = AdScript(
        brand="Mix Cancellato",
        parts=[AdPart(type="voice", text="Il mix deve aspettare.")],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    task = asyncio.create_task(synthesize_ad(script, voices, tmp_path))
    async with asyncio.timeout(1.0):
        while not mix_started.is_set():
            await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation must wait for the sequential mix worker"
    release_mix.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    for pattern in ("ad_*.mp3", "adpart_*.mp3", "adbed_*.mp3", "ad_broadcast_*.mp3"):
        assert not list(tmp_path.glob(pattern))


@pytest.mark.asyncio
async def test_synthesize_ad_sfx_only_script_uses_spoken_brand_fallback(_mock_all, tmp_path):
    """Decorative audio alone cannot count as a completed spoken ad."""
    from mammamiradio.audio.tts import synthesize_ad

    spoken: list[str] = []

    async def _fake_synthesize(text, voice, output_path, **kwargs):
        spoken.append(text)
        return _touch(output_path)

    script = AdScript(brand="Solo Suono", parts=[AdPart(type="sfx", sfx="chime")])
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.audio.tts.synthesize", side_effect=_fake_synthesize):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert spoken == ["Solo Suono"]
    assert not list(tmp_path.glob("adpart_*.mp3"))
    _mock_all["generate_music_bed"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_ad_sfx_only_fallback_voice_failure_propagates(_mock_all, tmp_path):
    """When an SFX-only ad's spoken-brand fallback voice fails, the error must
    propagate (fail closed) instead of silently swallowing it."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize_ad

    async def _fail_fallback(text, voice, output_path, **kwargs):
        raise TTSUnavailableError("fallback brand voice unavailable")

    script = AdScript(brand="Solo Suono", parts=[AdPart(type="sfx", sfx="chime")])
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_fail_fallback),
        pytest.raises(TTSUnavailableError, match="fallback brand voice unavailable"),
    ):
        await synthesize_ad(script, voices, tmp_path)

    assert not list(tmp_path.glob("*.mp3"))
    assert not list(tmp_path.glob("adpart_*.mp3"))
    assert not list(tmp_path.glob("ad_fallback_*.mp3"))
    _mock_all["generate_music_bed"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_ad_empty_parts_fallback(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

    script = AdScript(brand="EmptyBrand", parts=[])
    voices = {"default": AdVoice(name="Announcer", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path)

    # Should have synthesized the brand name as fallback
    _mock_all["Communicate"].assert_called_once_with("EmptyBrand", "it-IT-DiegoNeural", rate="+0%", pitch="+0Hz")
    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_empty_parts_fallback_keeps_direct_voice_settings(tmp_path, monkeypatch):
    """The empty-script rescue keeps its selected character and tuned payload."""
    import mammamiradio.audio.tts as tts

    seen: list[dict[str, object]] = []

    async def _synthesize(text, voice, output_path, **kwargs):
        seen.append({"text": text, "voice": voice, **kwargs})
        output_path.write_bytes(b"x" * 2048)
        return output_path

    monkeypatch.setattr(tts, "synthesize", _synthesize)
    direct = AdVoice(
        name="Il Razzo",
        voice="voice-razzo",
        style="fast",
        role="disclaimer_goblin",
        engine="elevenlabs",
        voice_settings={"stability": 0.6},
    )
    hammer = AdVoice(name="House Hammer", voice="house-hammer", style="clear", role="hammer")

    result = await tts.synthesize_ad(
        AdScript(brand="Scarpe Volanti", parts=[]),
        {"hammer": hammer, "disclaimer_goblin": direct},
        tmp_path,
        default_voice=direct,
    )

    assert result.exists()
    assert seen == [
        {
            "text": "Scarpe Volanti",
            "voice": "voice-razzo",
            "engine": "elevenlabs",
            "edge_fallback_voice": "",
            "openai_instructions": "Perform as an Italian radio commercial character. "
            "Role: disclaimer_goblin. Style: fast.",
            "voice_settings": {"stability": 0.6},
            "host_name": "Il Razzo",
            "state": None,
        }
    ]


@pytest.mark.asyncio
async def test_synthesize_ad_empty_music_bed_uses_voice_only(_mock_all, tmp_path, caplog):
    from mammamiradio.audio.tts import synthesize_ad

    def _empty_music_bed(output_path, _mood, _duration):
        output_path.touch()
        return output_path

    _mock_all["generate_music_bed"].side_effect = _empty_music_bed
    caplog.set_level("WARNING", logger="mammamiradio.audio.tts")

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="dramatic",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="bold")}

    result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert result.stat().st_size > 0
    _mock_all["mix_with_bed"].assert_not_called()
    assert any("Music bed missing or empty at" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Multi-voice and brand motif tests (new for signature ad system)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_ad_multi_voice_dict(_mock_all, tmp_path):
    """Multiple voices: role field on parts resolves to different TTS voice IDs."""
    from mammamiradio.audio.tts import synthesize_ad

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
async def test_synthesize_ad_voice_concat_failure_cleans_all_parts(_mock_all, tmp_path):
    """A failed multi-voice concat cannot leave undiscoverable ad scratch."""
    from mammamiradio.audio.tts import synthesize_ad

    def _failed_concat(_parts, output_path, *_args, **_kwargs):
        output_path.write_bytes(b"partial concatenation")
        raise RuntimeError("voice concat failed")

    _mock_all["concat_files"].side_effect = _failed_concat
    script = AdScript(
        brand="Duo Pulito",
        parts=[
            AdPart(type="voice", text="Prima voce."),
            AdPart(type="voice", text="Seconda voce."),
        ],
        mood="upbeat",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with pytest.raises(RuntimeError, match="voice concat failed"):
        await synthesize_ad(script, voices, tmp_path)

    assert not list(tmp_path.glob("adpart_*.mp3"))
    assert not list(tmp_path.glob("ad_voice_*.mp3"))
    _mock_all["generate_music_bed"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_ad_role_resolution_fallback(_mock_all, tmp_path):
    """Parts with unknown role fall back to first voice in dict."""
    from mammamiradio.audio.tts import synthesize_ad

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
async def test_synthesize_ad_forwards_ad_voice_settings_and_direct_default(_mock_all, tmp_path, monkeypatch):
    """Configured ad tuning reaches TTS, while roleless copy keeps its direct character."""
    import mammamiradio.audio.tts as tts

    seen: list[dict[str, object]] = []

    async def _synthesize(text, voice, output_path, **kwargs):
        seen.append({"text": text, "voice": voice, **kwargs})
        output_path.write_bytes(b"x" * 2048)
        return output_path

    monkeypatch.setattr(tts, "synthesize", _synthesize)
    direct = AdVoice(
        name="Il Razzo",
        voice="voice-razzo",
        style="fast",
        role="disclaimer_goblin",
        engine="elevenlabs",
        voice_settings={"stability": 0.6},
    )
    hammer = AdVoice(name="House Hammer", voice="house-hammer", style="clear", role="hammer")
    script = AdScript(brand="Scarpe Volanti", parts=[AdPart(type="voice", text="Compra ora!")], mood="lounge")

    result = await tts.synthesize_ad(
        script,
        {"hammer": hammer, "disclaimer_goblin": direct},
        tmp_path,
        default_voice=direct,
    )

    assert result.exists()
    assert seen == [
        {
            "text": "Compra ora!",
            "voice": "voice-razzo",
            "engine": "elevenlabs",
            "edge_fallback_voice": "",
            "openai_instructions": "Perform as an Italian radio commercial character. "
            "Role: disclaimer_goblin. Style: fast.",
            "voice_settings": {"stability": 0.6},
            "loudnorm": False,
            "host_name": "Il Razzo",
            "state": None,
        }
    ]


@pytest.mark.asyncio
async def test_synthesize_ad_brand_motif(_mock_all, tmp_path):
    """When sonic_signature is set, brand motif is generated and prepended."""
    from mammamiradio.audio.tts import synthesize_ad

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
    from mammamiradio.audio.tts import synthesize_ad

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


@pytest.mark.asyncio
async def test_synthesize_ad_cache_reuses_brand_motif_and_music_bed(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

    cache_dir = tmp_path / "cache"
    script = AdScript(
        brand="CacheBrand",
        parts=[AdPart(type="voice", text="Sempre pronto.")],
        mood="lounge",
        sonic=SonicWorld(sonic_signature="ice_clink+startup_synth"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    first = await synthesize_ad(script, voices, tmp_path, cache_dir=cache_dir)
    second = await synthesize_ad(script, voices, tmp_path, cache_dir=cache_dir)

    assert first.exists()
    assert second.exists()
    assert _mock_all["generate_brand_motif"].call_count == 1
    assert _mock_all["generate_music_bed"].call_count == 1
    assert len(list(cache_dir.glob("synth_brand_motif_*.mp3"))) == 1
    assert len(list(cache_dir.glob("synth_music_bed_*.mp3"))) == 1


@pytest.mark.asyncio
async def test_synthesize_ad_cache_reuses_environment_music_bed(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

    cache_dir = tmp_path / "cache"
    script = AdScript(
        brand="EnvCache",
        parts=[AdPart(type="voice", text="Dal salone.")],
        mood="lounge",
        sonic=SonicWorld(environment="showroom"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    await synthesize_ad(script, voices, tmp_path, cache_dir=cache_dir)
    await synthesize_ad(script, voices, tmp_path, cache_dir=cache_dir)

    # Main lounge bed + showroom environment bed are generated once each.
    assert _mock_all["generate_music_bed"].call_count == 2
    assert len(list(cache_dir.glob("synth_music_bed_*.mp3"))) == 2


@pytest.mark.asyncio
async def test_synthesize_ad_foley_cache_warms_bounded_variant_pool(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

    cache_dir = tmp_path / "cache"
    script = AdScript(
        brand="FoleyCache",
        parts=[AdPart(type="voice", text="Senti la folla.")],
        mood="dramatic",
        sonic=SonicWorld(environment="stadium"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    for _ in range(4):
        await synthesize_ad(script, voices, tmp_path, cache_dir=cache_dir)

    variants = [call.kwargs["variant"] for call in _mock_all["generate_foley_loop"].call_args_list]
    assert len(variants) == 3
    assert set(variants) == {0, 1, 2}
    assert len(list(cache_dir.glob("synth_foley_*.mp3"))) == 3


@pytest.mark.asyncio
async def test_synthesize_ad_cache_setup_failure_falls_back_to_direct_generation(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_ad

    cache_dir = tmp_path / "cache-file"
    cache_dir.write_bytes(b"not a directory")
    script = AdScript(
        brand="DirectBrand",
        parts=[AdPart(type="voice", text="Va in onda lo stesso.")],
        mood="lounge",
        sonic=SonicWorld(sonic_signature="chime"),
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    result = await synthesize_ad(script, voices, tmp_path, cache_dir=cache_dir)

    assert result.exists()
    _mock_all["generate_brand_motif"].assert_called_once()
    _mock_all["generate_music_bed"].assert_called_once()


# ---------------------------------------------------------------------------
# synthesize_dialogue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_dialogue_multiple_hosts(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_dialogue

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
    assert concat_call.kwargs["strict_duration"] is True
    normalize_calls = _mock_all["normalize"].call_args_list
    assert len(normalize_calls) == 3
    assert normalize_calls[0].kwargs["loudnorm"] is False
    assert normalize_calls[1].kwargs["loudnorm"] is False
    assert "loudnorm" not in normalize_calls[2].kwargs


@pytest.mark.asyncio
async def test_synthesize_dialogue_single_host(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_dialogue

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
async def test_synthesize_dialogue_failure_waits_for_sibling_and_cleans_scratch(_mock_all, tmp_path):
    """Failed parallel dialogue settles every line before removing scratch audio."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    sibling_started = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def _fake_synthesize(text, voice, output_path, **kwargs):
        if text == "bad line":
            await sibling_started.wait()
            output_path.with_suffix(".raw.mp3").write_bytes(b"partial")
            raise TTSUnavailableError("voice unavailable")
        sibling_started.set()
        await asyncio.sleep(0.01)
        _touch(output_path)
        sibling_finished.set()
        return output_path

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_fake_synthesize),
        pytest.raises(TTSUnavailableError, match="voice unavailable"),
    ):
        await synthesize_dialogue([(host, "bad line"), (host, "good line")], tmp_path)

    assert sibling_finished.is_set()
    assert not list(tmp_path.glob("line_*.mp3"))
    assert not list(tmp_path.glob("line_*.raw.mp3"))
    _mock_all["concat_files"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_dialogue_prioritizes_total_tts_outage_over_sibling_error(_mock_all, tmp_path):
    """Required dialogue preserves typed outage semantics across line failures."""
    from mammamiradio.audio.tts import TTSUnavailableError, synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    async def _failed_line(text, _voice, output_path, **_kwargs):
        output_path.write_bytes(b"partial line")
        if text == "generic failure":
            raise RuntimeError("local renderer failed")
        raise TTSUnavailableError("all voice routes unavailable")

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_failed_line),
        pytest.raises(TTSUnavailableError, match="all voice routes unavailable"),
    ):
        await synthesize_dialogue(
            [(host, "generic failure"), (host, "typed failure")],
            tmp_path,
        )

    assert not list(tmp_path.glob("line_*.mp3"))
    _mock_all["concat_files"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_dialogue_cancellation_waits_for_lines_then_cleans_scratch(_mock_all, tmp_path):
    """Cancelling dialogue settles every line before removing final and raw files."""
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    lines_started = asyncio.Event()
    release_lines = asyncio.Event()
    finished_lines: list[str] = []

    async def _late_synthesize(text, voice, output_path, **kwargs):
        lines_started.set()
        await release_lines.wait()
        _touch(output_path)
        output_path.with_suffix(".raw.mp3").write_bytes(b"late raw")
        finished_lines.append(text)
        return output_path

    with patch("mammamiradio.audio.tts.synthesize", side_effect=_late_synthesize):
        task = asyncio.create_task(
            synthesize_dialogue(
                [(host, "prima linea"), (host, "seconda linea")],
                tmp_path,
            )
        )
        await asyncio.wait_for(lines_started.wait(), timeout=1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must wait for every owned dialogue line"
        release_lines.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert sorted(finished_lines) == ["prima linea", "seconda linea"]
    assert not list(tmp_path.glob("line_*.mp3"))
    assert not list(tmp_path.glob("line_*.raw.mp3"))
    _mock_all["concat_files"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_dialogue_concat_cancellation_waits_then_cleans_scratch(_mock_all, tmp_path):
    """Cancellation during dialogue concat waits for FFmpeg before deleting inputs."""
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    concat_started = threading.Event()
    release_concat = threading.Event()

    def _slow_concat(_parts, output_path, *_args, **_kwargs):
        concat_started.set()
        assert release_concat.wait(timeout=2.0)
        _touch(output_path)
        return output_path

    _mock_all["concat_files"].side_effect = _slow_concat
    task = asyncio.create_task(
        synthesize_dialogue(
            [(host, "prima linea"), (host, "seconda linea")],
            tmp_path,
        )
    )
    async with asyncio.timeout(1.0):
        while not concat_started.is_set():
            await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation must wait for dialogue concat"
    release_concat.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    for pattern in ("line_*.mp3", "line_*.raw.mp3", "dialogue_raw_*.mp3", "dialogue_*.mp3"):
        assert not list(tmp_path.glob(pattern))


@pytest.mark.asyncio
async def test_synthesize_dialogue_normalize_cancellation_waits_then_cleans_scratch(_mock_all, tmp_path):
    """Cancellation during final dialogue normalization waits before cleanup."""
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")
    normalize_started = threading.Event()
    release_normalize = threading.Event()

    def _slow_final_normalize(input_path, output_path, config=None, *, loudnorm=True):
        if input_path.name.startswith("dialogue_raw_"):
            normalize_started.set()
            assert release_normalize.wait(timeout=2.0)
        return _normalize_side_effect(input_path, output_path, config, loudnorm=loudnorm)

    _mock_all["normalize"].side_effect = _slow_final_normalize
    task = asyncio.create_task(
        synthesize_dialogue(
            [(host, "prima linea"), (host, "seconda linea")],
            tmp_path,
        )
    )
    async with asyncio.timeout(1.0):
        while not normalize_started.is_set():
            await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "cancellation must wait for final dialogue normalization"
    release_normalize.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    for pattern in ("line_*.mp3", "line_*.raw.mp3", "dialogue_raw_*.mp3", "dialogue_*.mp3"):
        assert not list(tmp_path.glob(pattern))


@pytest.mark.asyncio
async def test_synthesize_dialogue_rejects_zero_byte_intermediate_before_concat(_mock_all, tmp_path):
    from mammamiradio.audio.audio_quality import AudioQualityError
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    async def _synthesize_line(text, voice, output_path, **kwargs):
        if "bad" in text:
            output_path.write_bytes(b"")
        else:
            _touch(output_path)
        return output_path

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_synthesize_line),
        pytest.raises(AudioQualityError, match="too small"),
    ):
        await synthesize_dialogue([(host, "good line"), (host, "bad line")], tmp_path)

    _mock_all["concat_files"].assert_not_called()
    assert not list(tmp_path.glob("line_*.mp3"))


@pytest.mark.asyncio
async def test_synthesize_dialogue_rejects_subthreshold_intermediate_before_concat(_mock_all, tmp_path):
    from mammamiradio.audio.audio_quality import AudioQualityError
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    async def _synthesize_line(text, voice, output_path, **kwargs):
        _touch(output_path)
        return output_path

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_synthesize_line),
        patch("mammamiradio.audio.tts.probe_duration_sec", return_value=0.2),
        pytest.raises(AudioQualityError, match="too short"),
    ):
        await synthesize_dialogue([(host, "prima linea"), (host, "seconda linea")], tmp_path)

    _mock_all["concat_files"].assert_not_called()
    assert not list(tmp_path.glob("line_*.mp3"))


@pytest.mark.asyncio
async def test_synthesize_dialogue_single_line_skips_per_line_validation(_mock_all, tmp_path):
    """Single-line dialogue is not per-line gated — short Italian exclamations are valid."""
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    async def _synthesize_line(text, voice, output_path, **kwargs):
        _touch(output_path)
        return output_path

    # probe returns a sub-threshold duration that would fail _validate_dialogue_part;
    # single-line dialogue must bypass that gate and return the part unchanged.
    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_synthesize_line),
        patch("mammamiradio.audio.tts.probe_duration_sec", return_value=0.2),
    ):
        result = await synthesize_dialogue([(host, "Sì!")], tmp_path)

    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_dialogue_forwards_v3_delivery_sidecar(_mock_all, tmp_path):
    """The dialogue boundary carries semantic cue metadata without changing clean text."""
    from mammamiradio.audio.tts import synthesize_dialogue
    from mammamiradio.core.models import DialogueLine

    host = HostPersonality(
        name="Marco",
        voice="voice_marco",
        style="energetic",
        engine="elevenlabs",
        elevenlabs_model="eleven_v3",
        delivery_profile="marco",
        voice_settings={"stability": 0.6},
    )

    async def _synthesize_line(text, voice, output_path, **kwargs):
        _touch(output_path)
        assert text == "Il testo resta pulito."
        assert voice == "voice_marco"
        assert kwargs["elevenlabs_model"] == "eleven_v3"
        assert kwargs["delivery_profile"] == "marco"
        assert kwargs["delivery_cue"] == "energetic"
        assert kwargs["host_name"] == "Marco"
        return output_path

    with patch("mammamiradio.audio.tts.synthesize", side_effect=_synthesize_line):
        result = await synthesize_dialogue(
            [DialogueLine(host=host, text="Il testo resta pulito.", delivery="energetic")],
            tmp_path,
        )

    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_dialogue_tolerates_unprobeable_intermediate(_mock_all, tmp_path):
    """An unprobeable line (ffprobe timeout on a loaded Pi) is not proof of corruption.

    probe_duration_sec returning None means "couldn't measure", not "bad file" — the
    size check still guards, the duration check is skipped, and assembly proceeds.
    """
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    async def _synthesize_line(text, voice, output_path, **kwargs):
        _touch(output_path)
        return output_path

    with (
        patch("mammamiradio.audio.tts.synthesize", side_effect=_synthesize_line),
        patch("mammamiradio.audio.tts.probe_duration_sec", return_value=None),
    ):
        result = await synthesize_dialogue([(host, "prima linea"), (host, "seconda linea")], tmp_path)

    _mock_all["concat_files"].assert_called_once()
    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_dialogue_concat_failure_cleans_temporary_parts(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    def _concat_fails(paths, output_path, silence_ms=300, loudnorm=True, **kwargs):
        _touch(output_path)
        raise RuntimeError("duration shortfall")

    with (
        patch("mammamiradio.audio.tts.concat_files", side_effect=_concat_fails),
        pytest.raises(RuntimeError, match="duration shortfall"),
    ):
        await synthesize_dialogue([(host, "prima linea"), (host, "seconda linea")], tmp_path)

    assert not list(tmp_path.glob("line_*.mp3"))
    assert not list(tmp_path.glob("dialogue_raw_*.mp3"))
    assert not list(tmp_path.glob("dialogue_*.mp3"))


@pytest.mark.asyncio
async def test_synthesize_dialogue_normalize_failure_cleans_temporary_parts(_mock_all, tmp_path):
    from mammamiradio.audio.tts import synthesize_dialogue

    host = HostPersonality(name="Marco", voice="it-IT-DiegoNeural", style="energetic")

    def _normalize_fails_final(raw_path, output_path, **kwargs):
        if raw_path.name.startswith("dialogue_raw_"):
            raise RuntimeError("normalize failed")
        return _normalize_side_effect(raw_path, output_path, **kwargs)

    with (
        patch("mammamiradio.audio.tts.normalize", side_effect=_normalize_fails_final),
        pytest.raises(RuntimeError, match="normalize failed"),
    ):
        await synthesize_dialogue([(host, "prima linea"), (host, "seconda linea")], tmp_path)

    assert not list(tmp_path.glob("dialogue_raw_*.mp3"))
    assert not list(tmp_path.glob("dialogue_*.mp3"))


@pytest.mark.asyncio
async def test_synthesize_dialogue_empty_lines_rejected(tmp_path):
    from mammamiradio.audio.tts import synthesize_dialogue

    with pytest.raises(ValueError, match="must not be empty"):
        await synthesize_dialogue([], tmp_path)


@pytest.mark.asyncio
async def test_synthesize_dialogue_openai_host(_mock_all, tmp_path, monkeypatch):
    """Host with engine='openai' routes through OpenAI TTS in dialogue."""
    from mammamiradio.audio.tts import synthesize_dialogue

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    from mammamiradio.core.models import PersonalityAxes

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

    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client_instance):
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
    from mammamiradio.audio.tts import synthesize_openai

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512

    mock_client_instance = MagicMock()
    mock_client_instance.audio.speech.create.return_value = mock_response

    _mock_all["normalize"].side_effect = RuntimeError("normalize failed")

    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client_instance):
        output = tmp_path / "openai_out.mp3"
        raw = output.with_suffix(".raw.mp3")
        with pytest.raises(RuntimeError, match="normalize failed"):
            await synthesize_openai("Ciao", "onyx", output)

    assert not raw.exists(), "raw_path must be cleaned up on normalize failure"


# ---------------------------------------------------------------------------
# _instructions_for_host — low-energy and low-warmth branches
# ---------------------------------------------------------------------------


def test_instructions_for_host_low_energy():
    from mammamiradio.audio.tts import _openai_instructions_for_host as _instructions_for_host
    from mammamiradio.core.models import PersonalityAxes

    host = HostPersonality(
        name="Quieta",
        voice="it-IT-IsabellaNeural",
        style="calm",
        personality=PersonalityAxes(energy=30, warmth=70, chaos=50),
    )
    instructions = _instructions_for_host(host)
    assert "Calm" in instructions or "measured" in instructions


def test_instructions_for_host_low_warmth():
    from mammamiradio.audio.tts import _openai_instructions_for_host as _instructions_for_host
    from mammamiradio.core.models import PersonalityAxes

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

    from mammamiradio.audio.tts import synthesize_openai

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    async def _run():
        await synthesize_openai("Ciao", "onyx", Path("/tmp/noop.mp3"))

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(_run())


@pytest.mark.asyncio
async def test_synthesize_ad_motif_generation_failure_is_skipped(_mock_all, tmp_path):
    """When brand motif generation raises, synthesize_ad skips the motif and still returns output."""
    from mammamiradio.audio.tts import synthesize_ad

    script = AdScript(
        brand="Motif Co",
        parts=[AdPart(type="voice", text="Our product!", role="hammer")],
        sonic=SonicWorld(sonic_signature="ice_clink+startup_synth"),
    )
    voices = {"hammer": AdVoice(name="Marco", voice="it-IT-DiegoNeural", style="bold", role="hammer")}

    # Make brand motif generation fail so the exception handler (304-306) is hit
    with patch(
        "mammamiradio.audio.tts.generate_brand_motif", side_effect=RuntimeError("motif unavailable")
    ) as mock_motif:
        result = await synthesize_ad(script, voices, tmp_path)

    # The ad must still be produced even without the brand motif
    assert result.exists()
    mock_motif.assert_called_once()


def test_get_openai_client_singleton(monkeypatch):
    """_get_openai_client returns the same instance for the same API key."""
    import mammamiradio.audio.tts as tts_mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    tts_mod._openai_client = None
    tts_mod._openai_client_key = ""

    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_cls.return_value = mock_instance

    with patch("openai.OpenAI", mock_cls):
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
    from mammamiradio.audio.tts import synthesize_ad

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
    from mammamiradio.audio.tts import synthesize_ad

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
    from mammamiradio.audio.tts import synthesize_ad

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

    with patch("mammamiradio.audio.tts.generate_foley_loop", side_effect=_foley_creates):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_env_bed_mix_failure_continues(_mock_all, tmp_path):
    """When env bed mix raises, ad continues without env bed layer (lines 381-382)."""
    from mammamiradio.audio.tts import synthesize_ad

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
    with patch("mammamiradio.audio.tts.generate_foley_loop"):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()


@pytest.mark.asyncio
async def test_synthesize_ad_music_bed_mix_failure_moves_voice(_mock_all, tmp_path):
    """When music bed mix fails, shutil.move copies voice to output_path (lines 390-393)."""
    from mammamiradio.audio.tts import synthesize_ad

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
    from mammamiradio.audio.tts import synthesize_ad

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
    from mammamiradio.audio.tts import synthesize_ad

    def _normalize_ad_creates(output_path, broadcast_path):
        _touch(broadcast_path)
        return broadcast_path

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.audio.tts.normalize_ad", side_effect=_normalize_ad_creates):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert "broadcast" in result.name


@pytest.mark.asyncio
async def test_synthesize_ad_normalize_ad_empty_falls_back_to_unprocessed(_mock_all, tmp_path):
    """normalize_ad creates an empty broadcast file → unprocessed ad path returned (lines 417-419)."""
    from mammamiradio.audio.tts import synthesize_ad

    def _normalize_ad_empty(output_path, broadcast_path):
        broadcast_path.touch()  # 0-byte file
        return broadcast_path

    script = AdScript(
        brand="TestBrand",
        parts=[AdPart(type="voice", text="Compra ora!")],
        mood="lounge",
    )
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with patch("mammamiradio.audio.tts.normalize_ad", side_effect=_normalize_ad_empty):
        result = await synthesize_ad(script, voices, tmp_path)

    assert result.exists()
    assert "broadcast" not in result.name


# ---------------------------------------------------------------------------
# TTS cost accounting (state.tts_characters) — paid cloud chars only
# ---------------------------------------------------------------------------


def test_cloud_helpers_keep_paid_success_callback_optional() -> None:
    """Direct audition callers may keep omitting the station-only callback."""
    from mammamiradio.audio import tts

    for helper in (tts.synthesize_openai, tts.synthesize_azure, tts.synthesize_elevenlabs):
        callback = inspect.signature(helper).parameters["on_paid_provider_success"]
        assert callback.kind is inspect.Parameter.KEYWORD_ONLY
        assert callback.default is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine", "voice"),
    [
        pytest.param("openai", "onyx", id="openai"),
        pytest.param("azure", "it-IT-IsabellaNeural", id="azure"),
        pytest.param("elevenlabs", "voice_italian_character", id="elevenlabs"),
    ],
)
async def test_synthesize_counts_confirmed_paid_response_before_normalize_failure(
    _mock_all, tmp_path, monkeypatch, engine, voice
):
    """A paid response counts once even when local normalization needs Edge rescue."""
    from mammamiradio.audio.tts import synthesize

    text = "Ciao, costa davvero"
    state = StationState(playlist=[])
    normalizer_calls = 0

    def _fail_first_normalize(*args, **kwargs):
        nonlocal normalizer_calls
        normalizer_calls += 1
        if normalizer_calls == 1:
            raise RuntimeError("cloud normalizer failed")
        return _normalize_side_effect(*args, **kwargs)

    _mock_all["normalize"].side_effect = _fail_first_normalize
    output = tmp_path / f"{engine}.mp3"
    synthesize_kwargs = {
        "engine": engine,
        "state": state,
        "edge_fallback_voice": "it-IT-DiegoNeural",
    }

    if engine == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        response = MagicMock(content=b"\x00" * 512)
        client = MagicMock()
        client.audio.speech.create.return_value = response
        with patch("mammamiradio.audio.tts._get_openai_client", return_value=client):
            result = await synthesize(text, voice, output, **synthesize_kwargs)
    else:
        response = httpx.Response(
            200,
            content=b"\x00" * 512,
            request=httpx.Request("POST", f"https://{engine}.example.test/tts"),
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        if engine == "azure":
            monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
            monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
            client_getter = "mammamiradio.audio.tts._get_azure_client"
        else:
            monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")
            client_getter = "mammamiradio.audio.tts._get_elevenlabs_client"
        with patch(client_getter, return_value=client):
            result = await synthesize(text, voice, output, **synthesize_kwargs)

    assert result == output
    assert _mock_all["normalize"].call_count == 2
    _mock_all["Communicate"].assert_called_once()
    assert _mock_all["Communicate"].call_args.args[1] == "it-IT-DiegoNeural"
    assert state.tts_characters == len(text)
    assert state.tts_characters_by_category["tts"] == len(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine", "voice"),
    [
        pytest.param("openai", "onyx", id="openai"),
        pytest.param("azure", "it-IT-IsabellaNeural", id="azure"),
        pytest.param("elevenlabs", "voice_italian_character", id="elevenlabs"),
    ],
)
async def test_synthesize_counts_confirmed_paid_response_before_raw_write_failure(
    _mock_all, tmp_path, monkeypatch, engine, voice
):
    """A paid response counts once even when raw-file I/O needs Edge rescue."""
    from mammamiradio.audio.tts import synthesize

    text = "Ciao, costa davvero"
    state = StationState(playlist=[])
    output = tmp_path / f"{engine}-write-failure.mp3"
    raw_path = output.with_suffix(".raw.mp3")
    real_write_bytes = Path.write_bytes
    cloud_raw_write_failed = False

    def _fail_first_cloud_raw_write(path, data):
        nonlocal cloud_raw_write_failed
        if path == raw_path and not cloud_raw_write_failed:
            cloud_raw_write_failed = True
            raise OSError("cloud raw write failed")
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", _fail_first_cloud_raw_write)
    synthesize_kwargs = {
        "engine": engine,
        "state": state,
        "edge_fallback_voice": "it-IT-DiegoNeural",
    }

    if engine == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        response = MagicMock(content=b"\x00" * 512)
        client = MagicMock()
        client.audio.speech.create.return_value = response
        with patch("mammamiradio.audio.tts._get_openai_client", return_value=client):
            result = await synthesize(text, voice, output, **synthesize_kwargs)
    else:
        response = httpx.Response(
            200,
            content=b"\x00" * 512,
            request=httpx.Request("POST", f"https://{engine}.example.test/tts"),
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        if engine == "azure":
            monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
            monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
            client_getter = "mammamiradio.audio.tts._get_azure_client"
        else:
            monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")
            client_getter = "mammamiradio.audio.tts._get_elevenlabs_client"
        with patch(client_getter, return_value=client):
            result = await synthesize(text, voice, output, **synthesize_kwargs)

    assert cloud_raw_write_failed
    assert result == output
    assert output.exists()
    assert _mock_all["normalize"].call_count == 1
    _mock_all["Communicate"].assert_called_once()
    assert _mock_all["Communicate"].call_args.args[1] == "it-IT-DiegoNeural"
    assert state.tts_characters == len(text)
    assert state.tts_characters_by_category["tts"] == len(text)


@pytest.mark.asyncio
async def test_synthesize_billing_guard_prevents_double_count(_mock_all, tmp_path, monkeypatch):
    """The billed idempotency guard counts a confirmed paid response once even if the
    provider's success callback fires more than once."""
    from mammamiradio.audio import tts
    from mammamiradio.audio.tts import synthesize

    text = "Ciao, costa davvero"
    state = StationState(playlist=[])
    output = tmp_path / "azure-double-bill.mp3"
    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")

    async def _double_notify(_text, _voice, output_path, *, on_paid_provider_success=None, **_kwargs):
        # A hypothetical double-wired provider path fires the paid-success callback twice;
        # the nonlocal `billed` guard must collapse it to a single billed response.
        if on_paid_provider_success is not None:
            on_paid_provider_success()
            on_paid_provider_success()
        output_path.write_bytes(b"\x00" * 512)
        return output_path

    monkeypatch.setattr(tts, "synthesize_azure", _double_notify)
    result = await synthesize(
        text,
        "it-IT-IsabellaNeural",
        output,
        engine="azure",
        state=state,
        edge_fallback_voice="it-IT-DiegoNeural",
    )

    assert result == output
    # Guard collapses the double callback into a single billed response.
    assert state.tts_characters == len(text)
    assert state.tts_characters_by_category["tts"] == len(text)
    # Confirmed paid response — no Edge fallback.
    _mock_all["Communicate"].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine", "voice"),
    [
        pytest.param("openai", "onyx", id="openai"),
        pytest.param("azure", "it-IT-IsabellaNeural", id="azure"),
        pytest.param("elevenlabs", "voice_italian_character", id="elevenlabs"),
    ],
)
async def test_synthesize_does_not_count_before_paid_provider_response(_mock_all, tmp_path, monkeypatch, engine, voice):
    """Missing/failed provider responses stay out of the paid session estimate."""
    from mammamiradio.audio.tts import synthesize

    text = "Nessuna risposta a pagamento"
    state = StationState(playlist=[])
    output = tmp_path / f"{engine}-failed.mp3"

    if engine == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        client = MagicMock()
        client.audio.speech.create.side_effect = RuntimeError("provider unavailable")
        with patch("mammamiradio.audio.tts._get_openai_client", return_value=client):
            result = await synthesize(text, voice, output, engine=engine, state=state)
    else:
        response = httpx.Response(
            503,
            request=httpx.Request("POST", f"https://{engine}.example.test/tts"),
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        if engine == "azure":
            monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
            monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
            client_getter = "mammamiradio.audio.tts._get_azure_client"
        else:
            monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")
            client_getter = "mammamiradio.audio.tts._get_elevenlabs_client"
        with patch(client_getter, return_value=client):
            result = await synthesize(text, voice, output, engine=engine, state=state)

    assert result == output
    _mock_all["Communicate"].assert_called_once()
    assert state.tts_characters == 0
    assert state.tts_characters_by_category.get("tts", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "voice"),
    [
        pytest.param("openai", "onyx", id="openai"),
        pytest.param("azure", "it-IT-IsabellaNeural", id="azure"),
        pytest.param("elevenlabs", "voice_italian_character", id="elevenlabs"),
    ],
)
async def test_cloud_helpers_ignore_paid_success_callback_errors(_mock_all, tmp_path, monkeypatch, provider, voice):
    """A bookkeeping callback failure cannot turn a successful cloud render into fallback audio."""
    from mammamiradio.audio import tts

    callback = MagicMock(side_effect=RuntimeError("accounting unavailable"))
    output = tmp_path / f"{provider}-callback.mp3"
    text = "Audio remains available"

    if provider == "openai":
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        client = MagicMock()
        client.audio.speech.create.return_value = MagicMock(content=b"\x00" * 512)
        with patch("mammamiradio.audio.tts._get_openai_client", return_value=client):
            result = await tts.synthesize_openai(
                text,
                voice,
                output,
                model="test-tts",
                on_paid_provider_success=callback,
            )
    else:
        response = httpx.Response(
            200,
            content=b"\x00" * 512,
            request=httpx.Request("POST", f"https://{provider}.example.test/tts"),
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        if provider == "azure":
            monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
            monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")
            helper = tts.synthesize_azure
            client_getter = "mammamiradio.audio.tts._get_azure_client"
        else:
            monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")
            helper = tts.synthesize_elevenlabs
            client_getter = "mammamiradio.audio.tts._get_elevenlabs_client"
        with patch(client_getter, return_value=client):
            result = await helper(text, voice, output, on_paid_provider_success=callback)

    await asyncio.sleep(0)
    assert result == output
    assert output.exists()
    callback.assert_called_once_with()
    _mock_all["Communicate"].assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_openai_continues_when_paid_callback_cannot_schedule(_mock_all, tmp_path, monkeypatch):
    """A closing owner loop cannot turn a confirmed cloud response into an audio failure."""
    from mammamiradio.audio.tts import synthesize_openai

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    real_loop = asyncio.get_running_loop()

    class _ClosedSchedulingLoop:
        def run_in_executor(self, *args, **kwargs):
            return real_loop.run_in_executor(*args, **kwargs)

        def call_soon_threadsafe(self, *args, **kwargs):
            raise RuntimeError("Event loop is closed")

    callback = MagicMock()
    client = MagicMock()
    client.audio.speech.create.return_value = MagicMock(content=b"\x00" * 512)
    with (
        patch("mammamiradio.audio.tts._get_openai_client", return_value=client),
        patch("mammamiradio.audio.tts.asyncio.get_running_loop", return_value=_ClosedSchedulingLoop()),
    ):
        result = await synthesize_openai(
            "Audio keeps flowing",
            "onyx",
            tmp_path / "closed-loop.mp3",
            model="test-tts",
            on_paid_provider_success=callback,
        )

    assert result.exists()
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_records_late_openai_success_after_outer_timeout(_mock_all, tmp_path, monkeypatch):
    """A late OpenAI worker response is counted after Edge has already returned."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    worker_started = threading.Event()
    release_worker = threading.Event()
    billing_seen = asyncio.Event()
    original_wait_for = asyncio.wait_for

    class _TrackingState:
        tts_characters = 0

        def record_tts_usage(self, characters):
            self.tts_characters += characters
            billing_seen.set()

    response = MagicMock(content=b"\x00" * 512)
    client = MagicMock()

    def _late_create(**kwargs):
        worker_started.set()
        assert release_worker.wait(timeout=2), "test did not release the OpenAI worker"
        return response

    async def _timeout_only_openai(awaitable, timeout):
        if timeout == 30.0:
            for _ in range(1000):
                if worker_started.is_set():
                    break
                await asyncio.sleep(0.001)
            assert worker_started.is_set(), "OpenAI executor worker did not start"
            raise TimeoutError
        return await original_wait_for(awaitable, timeout)

    client.audio.speech.create.side_effect = _late_create
    state = _TrackingState()
    try:
        with (
            patch("mammamiradio.audio.tts._get_openai_client", return_value=client),
            patch("mammamiradio.audio.tts.asyncio.wait_for", new=_timeout_only_openai),
        ):
            result = await synthesize(
                "Risposta in ritardo", "onyx", tmp_path / "late.mp3", engine="openai", state=state
            )
            assert result.exists()
            assert state.tts_characters == 0
            _mock_all["Communicate"].assert_called_once()
    finally:
        release_worker.set()

    await original_wait_for(billing_seen.wait(), timeout=1.0)
    assert state.tts_characters == len("Risposta in ritardo")


@pytest.mark.asyncio
async def test_synthesize_bills_tts_chars_on_cloud_success(_mock_all, tmp_path, monkeypatch):
    """A successful PAID cloud synth adds len(text) to state.tts_characters."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response

    state = SimpleNamespace(tts_characters=0)
    text = "Ciao mondo"
    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client):
        await synthesize(text, "onyx", tmp_path / "o.mp3", engine="openai", state=state)
    assert state.tts_characters == len(text)


@pytest.mark.asyncio
async def test_synthesize_bills_station_state_tts_category_on_cloud_success(_mock_all, tmp_path, monkeypatch):
    """A successful paid cloud synth updates aggregate and category TTS counters."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response

    state = StationState(playlist=[])
    text = "Ciao mondo"
    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client):
        await synthesize(text, "onyx", tmp_path / "o.mp3", engine="openai", state=state)

    assert state.tts_characters == len(text)
    assert state.tts_characters_by_category["tts"] == len(text)


@pytest.mark.asyncio
async def test_synthesize_does_not_bill_when_cloud_falls_back_to_edge(_mock_all, tmp_path, monkeypatch):
    """A cloud engine that falls back to free Edge (missing key) bills nothing."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = SimpleNamespace(tts_characters=0)
    await synthesize("Ciao mondo", "onyx", tmp_path / "o.mp3", engine="openai", state=state)
    _mock_all["Communicate"].assert_called_once()  # fell back to edge-tts
    assert state.tts_characters == 0  # free → never billed


@pytest.mark.asyncio
async def test_synthesize_does_not_bill_when_cloud_synth_raises(_mock_all, tmp_path, monkeypatch):
    """A paid engine with a key present whose synth RAISES falls back to free Edge, bills nothing.

    Distinct from the missing-key path: this exercises the try/except *after* the paid
    call starts, guarding the `result = ...; _bill_tts(); return result` ordering. A failed
    cloud call delivers no audio, so the counter must stay at 0 — billing lives only on the
    success path, never before the try.
    """
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    state = SimpleNamespace(tts_characters=0)
    with patch("mammamiradio.audio.tts._get_openai_client", side_effect=RuntimeError("API down")):
        await synthesize("Ciao mondo", "onyx", tmp_path / "o.mp3", engine="openai", state=state)
    _mock_all["Communicate"].assert_called_once()  # fell back to edge-tts
    assert state.tts_characters == 0  # failed paid call → never billed


@pytest.mark.asyncio
async def test_synthesize_post_restart_state_bills_normally(_mock_all, tmp_path, monkeypatch):
    """Post-restart scenario: a restart-shaped state (session_stopped set, fresh counter)
    still bills real paid spend accurately. The billing side-channel is restart-agnostic —
    synthesize never reads session_stopped, and a paid call that *succeeded* cost money
    whether or not the prior session was stopped, so the counter must reflect it. (The other
    post-restart shape — a restored/legacy state missing the attr entirely — is covered by
    test_cost_counter_tts_getattr_safe_on_legacy_state in test_quality_dial.py.)
    """
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response

    # Shape mirrors a session restored after an HA-watchdog restart.
    state = SimpleNamespace(tts_characters=0, session_stopped=True)
    text = "Bentornati"
    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client):
        await synthesize(text, "onyx", tmp_path / "o.mp3", engine="openai", state=state)
    assert state.tts_characters == len(text)  # real spend billed regardless of restart flags


@pytest.mark.asyncio
async def test_synthesize_edge_engine_never_billed(_mock_all, tmp_path):
    """Edge is free — an explicit edge engine never touches the counter."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    state = SimpleNamespace(tts_characters=0)
    await synthesize("Ciao", "it-IT-IsabellaNeural", tmp_path / "o.mp3", engine="edge", state=state)
    assert state.tts_characters == 0


@pytest.mark.asyncio
async def test_synthesize_without_state_does_not_raise(_mock_all, tmp_path, monkeypatch):
    """state=None (default) is safe — cost bookkeeping never touches the audio path."""
    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response
    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client):
        out = await synthesize("Ciao", "onyx", tmp_path / "o.mp3", engine="openai")
    assert out == tmp_path / "o.mp3"


@pytest.mark.asyncio
async def test_synthesize_dialogue_forwards_state_billing(_mock_all, tmp_path, monkeypatch):
    """Multi-line dialogue bills each cloud line's characters to state."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize_dialogue

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response

    host = HostPersonality(name="Gio", voice="onyx", style="warm", engine="openai")
    lines = [(host, "Riga uno"), (host, "Riga due")]
    state = SimpleNamespace(tts_characters=0)
    with patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client):
        await synthesize_dialogue(lines, tmp_path, state=state)
    assert state.tts_characters == len("Riga uno") + len("Riga due")


@pytest.mark.asyncio
async def test_synthesize_bills_on_azure_success(_mock_all, tmp_path, monkeypatch):
    """Azure cloud success bills len(text), proving billing isn't OpenAI-only."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("AZURE_SPEECH_KEY", "azure-secret")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "westeurope")

    class _AzureClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, content):
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _AzureClient)
    state = SimpleNamespace(tts_characters=0)
    text = "Ciao Azure"
    await synthesize(text, "it-IT-IsabellaNeural", tmp_path / "az.mp3", engine="azure", state=state)
    assert state.tts_characters == len(text)


@pytest.mark.asyncio
async def test_synthesize_bills_on_elevenlabs_success(_mock_all, tmp_path, monkeypatch):
    """ElevenLabs cloud success bills len(text)."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize

    monkeypatch.setenv("ELEVENLABS_API_KEY", "eleven-secret")

    class _ElevenClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers, json):
            return httpx.Response(200, content=b"\x00" * 512, request=httpx.Request("POST", url))

    monkeypatch.setattr("mammamiradio.audio.tts.httpx.AsyncClient", _ElevenClient)
    state = SimpleNamespace(tts_characters=0)
    text = "Ciao ElevenLabs"
    await synthesize(text, "voice_italian_character", tmp_path / "el.mp3", engine="elevenlabs", state=state)
    assert state.tts_characters == len(text)


@pytest.mark.asyncio
async def test_synthesize_ad_forwards_state_billing(_mock_all, tmp_path, monkeypatch):
    """synthesize_ad threads state into each voice part's synthesize()."""
    from types import SimpleNamespace

    from mammamiradio.audio.tts import synthesize_ad

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    mock_response = MagicMock()
    mock_response.content = b"\x00" * 512
    mock_client = MagicMock()
    mock_client.audio.speech.create.return_value = mock_response

    text = "Compra ora!"
    script = AdScript(brand="TestBrand", parts=[AdPart(type="voice", text=text)], mood="lounge")
    voices = {"default": AdVoice(name="Ann", voice="onyx", style="warm", engine="openai")}
    state = SimpleNamespace(tts_characters=0)

    with (
        patch("mammamiradio.audio.tts._get_openai_client", return_value=mock_client),
        patch("mammamiradio.audio.tts.normalize_ad", side_effect=_normalize_side_effect),
    ):
        await synthesize_ad(script, voices, tmp_path, state=state)
    assert state.tts_characters == len(text)
