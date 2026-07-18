"""Text-to-speech assembly for host dialogue and produced ads."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import math
import os
import re
import shutil
import threading
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import uuid4

import edge_tts
import httpx

from mammamiradio.audio.audio_quality import AudioQualityError
from mammamiradio.audio.normalizer import (
    concat_files,
    generate_brand_motif,
    generate_foley_loop,
    generate_music_bed,
    generate_sfx,
    generate_silence,
    mix_with_bed,
    normalize,
    normalize_ad,
    probe_duration_sec,
)
from mammamiradio.audio.synth_cache import duration_bucket_sec, materialize_synth_mp3, next_synth_variant
from mammamiradio.audio.voice_catalog import (
    EDGE_DEFAULT_FALLBACK_VOICE as _EDGE_DEFAULT_FALLBACK_VOICE,
)
from mammamiradio.audio.voice_catalog import (
    is_openai_voice as _catalog_is_openai_voice,
)
from mammamiradio.core.models import DialogueLine, HostPersonality
from mammamiradio.hosts.ad_creative import AdScript, AdVoice

if TYPE_CHECKING:
    from mammamiradio.core.models import StationState

logger = logging.getLogger(__name__)

# Default instructions for OpenAI TTS voice
_OPENAI_TTS_INSTRUCTIONS = "Speak like a charismatic Italian radio host. Warm, energetic, natural pacing."

# Cache: personality hash → instructions string (personality doesn't change mid-session)
_instructions_cache: dict[int, str] = {}

# Singleton OpenAI client — reuses HTTP connection pool across calls
_openai_client = None
_openai_client_key: str = ""
_openai_tts_model: str | None = None
# Whether configure_openai_tts_model() has run. Distinguishes "startup explicitly
# selected no OpenAI TTS model" (stay on Edge) from "not configured yet" (a
# test/CLI caller may resolve the packaged registry). Without this, a startup
# that configures None would fall through to a CWD registry read and could call
# OpenAI with an unrelated model instead of degrading to Edge.
_openai_tts_model_configured: bool = False
# Singleton httpx clients for Azure and ElevenLabs — same pattern as OpenAI
_azure_client: httpx.AsyncClient | None = None
_azure_client_key: tuple[str, str] = ("", "")
_elevenlabs_client: httpx.AsyncClient | None = None
_elevenlabs_client_key: str = ""
# XML 1.0 control characters illegal in SSML (strips before html.escape)
_XML_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Runtime memoization: edge voices and cloud provider voices that failed synthesis
# in this session.
# Prevents repeated per-segment failures for the same voice ID when edge-tts
# returns "Invalid voice" or a cloud provider returns a non-retryable auth/voice
# error. Reset via reset_voice_failures().
_failed_edge_voices: set[str] = set()
_failed_cloud_voices: set[tuple[str, str, str, str]] = set()
_cloud_voice_attempt_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
_cloud_voice_state_lock = threading.Lock()

# Cap concurrent TTS + FFmpeg jobs to avoid CPU/thermal spikes on constrained hardware
# (e.g. Home Assistant Green — fanless ARM SoC). Two slots let one TTS+normalize and
# one SFX/bed generation overlap without saturating all cores.
_HEAVY_SEM = asyncio.Semaphore(2)
_MIN_DIALOGUE_LINE_BYTES = 1024
_MIN_DIALOGUE_LINE_DURATION_SEC = 0.5
# Disclaimer voice rate by ad format. Formats not listed use the +35%
# disclaimer_goblin default shared by the other ad treatments.
_DISCLAIMER_RATE_BY_FORMAT = {
    "classic_pitch": "+55%",
}


class TTSUnavailableError(RuntimeError):
    """Every configured route for required speech failed."""


def _prioritized_failure(results: list[object | BaseException]) -> BaseException | None:
    """Keep cancellation and total voice outage semantics across fan-outs."""

    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            return result
    for result in results:
        if isinstance(result, TTSUnavailableError):
            return result
    return next((result for result in results if isinstance(result, Exception)), None)


async def _settle_owned(*awaitables: Awaitable[object]) -> list[object | BaseException]:
    """Await owned concurrent work to completion, including during cancellation.

    Shielding matters for TTS normalization: cancelling an asyncio wrapper does
    not stop an FFmpeg worker already running in the executor.  Scratch cleanup
    is therefore only safe after the aggregate future has actually settled.
    """
    settled = asyncio.gather(*awaitables, return_exceptions=True)
    try:
        return list(await asyncio.shield(settled))
    except asyncio.CancelledError:
        await settled
        raise


def _looks_like_openai_voice(voice: str) -> bool:
    return _catalog_is_openai_voice(voice)


def configure_openai_tts_model(model: str | None) -> None:
    """Set the registry-selected OpenAI speech model for this running station."""
    global _openai_tts_model, _openai_tts_model_configured
    _openai_tts_model = model.strip() if model and model.strip() else None
    _openai_tts_model_configured = True


def _configured_openai_tts_model() -> str | None:
    """Resolve the OpenAI speech model, or None to stay on Edge.

    Once startup has configured the station (even to None), that decision is
    authoritative — we never second-guess it with a CWD registry read that could
    load an unrelated file. Only an unconfigured test/CLI caller falls back to the
    packaged registry.
    """
    if _openai_tts_model_configured:
        return _openai_tts_model
    from mammamiradio.core.config import MODEL_REGISTRY_FILENAME, load_model_registry

    return load_model_registry(Path(MODEL_REGISTRY_FILENAME)).tts_model("openai")


def reset_voice_failures() -> None:
    """Clear the session-memoized voice failure sets. Used by tests."""
    _failed_edge_voices.clear()
    with _cloud_voice_state_lock:
        _failed_cloud_voices.clear()
        _cloud_voice_attempt_locks.clear()


def _secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else ""


def _cloud_failure_key(
    engine: str,
    voice: str,
    *,
    elevenlabs_model: str = "eleven_multilingual_v2",
) -> tuple[str, str, str, str]:
    """Scope cloud failure memoization to the effective provider model too."""

    engine = engine.strip().lower()
    if engine == "azure":
        credential = f"{os.getenv('AZURE_SPEECH_REGION', '')}:{_secret_fingerprint(os.getenv('AZURE_SPEECH_KEY', ''))}"
        model = ""
    elif engine == "elevenlabs":
        credential = _secret_fingerprint(os.getenv("ELEVENLABS_API_KEY", ""))
        model = elevenlabs_model.strip() if isinstance(elevenlabs_model, str) else ""
    else:
        credential = ""
        model = ""
    return (engine, voice.strip(), credential, model)


def _cloud_voice_failed(cloud_key: tuple[str, str, str, str]) -> bool:
    with _cloud_voice_state_lock:
        return cloud_key in _failed_cloud_voices


def _memoize_failed_cloud_voice(cloud_key: tuple[str, str, str, str]) -> None:
    with _cloud_voice_state_lock:
        _failed_cloud_voices.add(cloud_key)


def _cloud_voice_attempt_lock(cloud_key: tuple[str, str, str, str]) -> asyncio.Lock:
    """Return the per-key async lock for cloud attempts.

    Locks are created lazily inside the running event loop and cleared by tests
    through reset_voice_failures().
    """
    with _cloud_voice_state_lock:
        lock = _cloud_voice_attempt_locks.get(cloud_key)
        if lock is None:
            lock = asyncio.Lock()
            _cloud_voice_attempt_locks[cloud_key] = lock
        return lock


def _non_retryable_cloud_tts_error(exc: Exception) -> str:
    """Return a compact reason for auth/config failures that should not repeat."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403, 404}:
            return f"HTTP {status}"
        body = getattr(exc.response, "text", "").lower()
        if status == 400 and ("invalid" in body or "voice" in body):
            return f"HTTP {status}"
    return ""


def _coerce_edge_voice(voice: str, *, edge_fallback_voice: str = "") -> str:
    """Map obvious OpenAI-only voice IDs to a safe Edge fallback before synthesis."""
    if not _looks_like_openai_voice(voice):
        return voice
    fallback = edge_fallback_voice or _EDGE_DEFAULT_FALLBACK_VOICE
    logger.warning("Edge TTS received OpenAI voice '%s'; using fallback voice '%s'", voice, fallback)
    return fallback


def _openai_instructions_for_host(host: HostPersonality) -> str:
    """Build OpenAI TTS instructions from host personality axes and style.

    Cached by personality hash since axes don't change mid-session.
    """
    cache_key = hash((host.personality.energy, host.personality.warmth, host.personality.chaos))
    if cache_key in _instructions_cache:
        return _instructions_cache[cache_key]

    parts = ["Speak like a charismatic Italian radio host."]
    p = host.personality
    if p.energy > 60:
        parts.append("High energy, fast pacing, explosive delivery.")
    elif p.energy < 40:
        parts.append("Calm, measured, deliberate pacing.")
    if p.warmth > 60:
        parts.append("Warm and inviting tone.")
    elif p.warmth < 40:
        parts.append("Cool, detached, razor-sharp delivery.")
    if p.chaos > 60:
        parts.append("Unpredictable rhythm, dramatic shifts in intensity.")
    result = " ".join(parts)
    _instructions_cache[cache_key] = result
    return result


def _openai_instructions_for_ad_voice(voice: AdVoice) -> str:
    parts = ["Perform as an Italian radio commercial character."]
    if voice.role:
        parts.append(f"Role: {voice.role}.")
    if voice.style:
        parts.append(f"Style: {voice.style}.")
    return " ".join(parts)


def _estimate_duration(path: Path) -> float:
    """Rough duration estimate from file size at 192kbps."""
    return max(5.0, path.stat().st_size / (192 * 128))


def _get_openai_client(api_key: str):
    """Return a singleton OpenAI client, reusing the connection pool.

    Recreates the client only if the API key changes (e.g. env reload).
    """
    global _openai_client, _openai_client_key
    if _openai_client is not None and _openai_client_key == api_key:
        return _openai_client
    from openai import OpenAI

    _openai_client = OpenAI(api_key=api_key)
    _openai_client_key = api_key
    return _openai_client


def _get_azure_client(api_key: str, region: str) -> httpx.AsyncClient:
    """Return a singleton httpx client for Azure TTS, reusing the connection pool."""
    global _azure_client, _azure_client_key
    if _azure_client is not None and _azure_client_key == (api_key, region):
        return _azure_client
    _azure_client = httpx.AsyncClient(timeout=30.0)
    _azure_client_key = (api_key, region)
    return _azure_client


def _get_elevenlabs_client(api_key: str) -> httpx.AsyncClient:
    """Return a singleton httpx client for ElevenLabs TTS, reusing the connection pool."""
    global _elevenlabs_client, _elevenlabs_client_key
    if _elevenlabs_client is not None and _elevenlabs_client_key == api_key:
        return _elevenlabs_client
    _elevenlabs_client = httpx.AsyncClient(timeout=30.0)
    _elevenlabs_client_key = api_key
    return _elevenlabs_client


def _notify_paid_provider_success(on_paid_provider_success: Callable[[], None] | None) -> None:
    """Run best-effort paid-use accounting without affecting audio delivery."""
    if on_paid_provider_success is None:
        return
    try:
        on_paid_provider_success()
    except Exception:
        # Keep accounting failures out of the listener-audio path and avoid
        # logging callback details that could contain application state.
        logger.debug("Paid TTS accounting callback failed")


def _schedule_paid_provider_success(
    loop: asyncio.AbstractEventLoop,
    on_paid_provider_success: Callable[[], None] | None,
) -> None:
    """Schedule paid-use accounting from an OpenAI executor worker."""
    if on_paid_provider_success is None:
        return
    try:
        loop.call_soon_threadsafe(_notify_paid_provider_success, on_paid_provider_success)
    except RuntimeError:
        # A late worker can finish while the owning loop is closing. Losing an
        # in-memory estimate is preferable to surfacing a worker error or
        # disrupting the existing Edge rescue path.
        logger.debug("Paid TTS accounting callback skipped because the event loop is closed")


async def synthesize_openai(
    text: str,
    voice: str,
    output_path: Path,
    *,
    instructions: str = "",
    loudnorm: bool = True,
    model: str | None = None,
    on_paid_provider_success: Callable[[], None] | None = None,
) -> Path:
    """Render text with the registry-selected OpenAI speech model."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    model = model or _configured_openai_tts_model()
    if not model:
        raise RuntimeError("OpenAI TTS model is unavailable; check model_registry.toml")

    client = _get_openai_client(api_key)
    loop = asyncio.get_running_loop()

    def _call_openai() -> bytes:
        response = client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            instructions=instructions or _OPENAI_TTS_INSTRUCTIONS,
        )
        _schedule_paid_provider_success(loop, on_paid_provider_success)
        return response.content

    raw_path = output_path.with_suffix(".raw.mp3")
    try:
        audio_bytes = await asyncio.wait_for(
            loop.run_in_executor(None, _call_openai),
            timeout=30.0,
        )
        raw_path.write_bytes(audio_bytes)

        await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
        _unlink_many([raw_path])
    except Exception:
        _unlink_many([raw_path])  # clean up orphaned raw file on any failure
        raise

    logger.info("Synthesized (OpenAI): %s (%s)", output_path.name, voice)
    return output_path


async def synthesize_azure(
    text: str,
    voice: str,
    output_path: Path,
    *,
    rate: str | None = None,
    pitch: str | None = None,
    loudnorm: bool = True,
    on_paid_provider_success: Callable[[], None] | None = None,
) -> Path:
    """Render text with Azure Speech TTS REST API, then normalize to station settings."""
    api_key = os.getenv("AZURE_SPEECH_KEY", "")
    region = os.getenv("AZURE_SPEECH_REGION", "")
    if not api_key or not region:
        raise RuntimeError("AZURE_SPEECH_KEY and AZURE_SPEECH_REGION must be set")

    raw_path = output_path.with_suffix(".raw.mp3")
    clean_text = _XML_CONTROL_CHARS.sub("", text)
    escaped = html.escape(clean_text, quote=False)
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="it-IT">'
        f'<voice name="{html.escape(voice, quote=True)}">'
        f'<prosody rate="{html.escape(rate or "+0%", quote=True)}" pitch="{html.escape(pitch or "+0Hz", quote=True)}">'
        f"{escaped}"
        "</prosody>"
        "</voice>"
        "</speak>"
    )
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-160kbitrate-mono-mp3",
        "User-Agent": "mammamiradio",
    }
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    try:
        client = _get_azure_client(api_key, region)
        response = await client.post(url, headers=headers, content=ssml.encode("utf-8"))
        response.raise_for_status()
        _notify_paid_provider_success(on_paid_provider_success)
        raw_path.write_bytes(response.content)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
        _unlink_many([raw_path])
    except Exception:
        _unlink_many([raw_path])
        raise

    logger.info("Synthesized (Azure): %s (%s)", output_path.name, voice)
    return output_path


# The station's house ElevenLabs voice tuning. Per-call overrides (the voice
# audition harness) merge over these; production callers use them as-is.
_ELEVENLABS_DEFAULT_VOICE_SETTINGS: dict = {
    "stability": 0.42,
    "similarity_boost": 0.78,
    "style": 0.45,
    "use_speaker_boost": True,
}
_ELEVENLABS_V2_MODEL = "eleven_multilingual_v2"
_ELEVENLABS_V3_MODEL = "eleven_v3"
_SUPPORTED_ELEVENLABS_MODELS = frozenset({_ELEVENLABS_V2_MODEL, _ELEVENLABS_V3_MODEL})

# A cue is semantic metadata, never caller-provided provider markup. The map is
# deliberately small and profile-bound so an LLM cannot smuggle arbitrary audio
# tags into a request, and Edge fallback always retains the original clean text.
_ELEVENLABS_V3_DELIVERY_TAGS: dict[str, dict[str, str]] = {
    "marco": {
        "energetic": "[excited]",
        "curious": "[curious]",
        "playful": "[mischievously]",
    },
    "giulia": {
        "dry": "[sarcastic]",
        "curious": "[curious]",
        "playful": "[mischievously]",
    },
}


def _resolve_elevenlabs_v2_voice_settings(voice_settings: dict | None) -> dict:
    """Return the exact ElevenLabs v2 settings payload for a configured voice."""

    if voice_settings is not None and not isinstance(voice_settings, dict):
        raise ValueError("ElevenLabs voice_settings must be a table")
    # Keep this dict construction and key order aligned with the historical v2
    # payload so voices without a deliberate override keep the same request.
    return {**_ELEVENLABS_DEFAULT_VOICE_SETTINGS, **(voice_settings or {})}


def _resolve_elevenlabs_v3_voice_settings(voice_settings: dict | None) -> dict | None:
    """Return V3's safe settings payload, omitting provider-default stability."""

    if voice_settings is None:
        return None
    if not isinstance(voice_settings, dict):
        raise ValueError("ElevenLabs voice_settings must be a table")
    if not voice_settings:
        return None
    if set(voice_settings) != {"stability"}:
        raise ValueError("ElevenLabs V3 voice_settings may contain only stability")
    stability = voice_settings["stability"]
    if isinstance(stability, bool) or not isinstance(stability, int | float) or not math.isfinite(stability):
        raise ValueError("ElevenLabs V3 stability must be a finite number between 0 and 1")
    if not 0 <= stability <= 1:
        raise ValueError("ElevenLabs V3 stability must be between 0 and 1")
    return {"stability": float(stability)}


def _resolve_elevenlabs_v3_delivery_tag(delivery_cue: str, delivery_profile: str) -> tuple[str, str]:
    """Return the code-owned V3 tag and normalized semantic cue, if authorized."""

    cue = delivery_cue.strip().lower() if isinstance(delivery_cue, str) else "neutral"
    profile = delivery_profile.strip().lower() if isinstance(delivery_profile, str) else "none"
    if cue in {"", "neutral"}:
        return "", "neutral"
    tag = _ELEVENLABS_V3_DELIVERY_TAGS.get(profile, {}).get(cue, "")
    if not tag:
        logger.debug("Ignoring unsupported ElevenLabs V3 delivery cue profile=%s cue=%s", profile, cue)
        return "", "neutral"
    return tag, cue


# Model-emitted bracket directives (for example "[sospira]") must never reach a
# V3 payload as an uncontrolled audio tag — only the code-owned tag above may be
# markup. scriptwriter strips these from banter before assembly, but regular
# hosts now render non-banter speech (transitions, news flashes, ad intros)
# under V3 too, so the provider boundary is the single point that strips them on
# every V3 path.
_ELEVENLABS_V3_INLINE_DIRECTIVE_RE = re.compile(r"\[[^\]\r\n]{0,120}\]")


def _strip_v3_inline_directives(text: str) -> str:
    """Remove model-supplied bracket directions from spoken copy before V3 renders it."""
    without_directives = _ELEVENLABS_V3_INLINE_DIRECTIVE_RE.sub(" ", text)
    return re.sub(r"[ \t]+", " ", without_directives).strip()


async def synthesize_elevenlabs(
    text: str,
    voice: str,
    output_path: Path,
    *,
    loudnorm: bool = True,
    voice_settings: dict | None = None,
    elevenlabs_model: str = _ELEVENLABS_V2_MODEL,
    delivery_cue: str = "neutral",
    delivery_profile: str = "none",
    host_name: str = "",
    on_paid_provider_success: Callable[[], None] | None = None,
) -> Path:
    """Render text with ElevenLabs TTS REST API, then normalize to station settings.

    ``voice_settings`` overrides the house defaults per call (used by the voice
    audition harness to sweep stability/style/similarity); when None, the V2
    house defaults apply. V3 accepts only stability and renders an allowlisted
    semantic delivery cue at this final provider boundary.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    if not isinstance(elevenlabs_model, str) or elevenlabs_model not in _SUPPORTED_ELEVENLABS_MODELS:
        raise ValueError(f"Unsupported ElevenLabs model: {elevenlabs_model!r}")

    raw_path = output_path.with_suffix(".raw.mp3")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice, safe='')}"
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    if elevenlabs_model == _ELEVENLABS_V2_MODEL:
        # Preserve this construction and key order byte-for-byte for legacy V2
        # callers; it is also the locked audition baseline.
        payload = {
            "text": text,
            "model_id": _ELEVENLABS_V2_MODEL,
            "voice_settings": _resolve_elevenlabs_v2_voice_settings(voice_settings),
        }
        resolved_cue = "neutral"
    else:
        tag, resolved_cue = _resolve_elevenlabs_v3_delivery_tag(delivery_cue, delivery_profile)
        # Strip any model-emitted bracket directives so only the code-owned tag
        # can reach V3 as markup — covers non-banter host speech (transitions,
        # news, ad intros) that scriptwriter does not pre-clean.
        safe_text = _strip_v3_inline_directives(text)
        payload = {
            "text": f"{tag} {safe_text}" if tag else safe_text,
            "model_id": _ELEVENLABS_V3_MODEL,
        }
        v3_voice_settings = _resolve_elevenlabs_v3_voice_settings(voice_settings)
        if v3_voice_settings is not None:
            payload["voice_settings"] = v3_voice_settings

    logger.info(
        "ElevenLabs TTS request host=%s model=%s delivery=%s",
        host_name or "unspecified",
        elevenlabs_model,
        resolved_cue,
    )
    try:
        client = _get_elevenlabs_client(api_key)
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        _notify_paid_provider_success(on_paid_provider_success)
        raw_path.write_bytes(response.content)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
        _unlink_many([raw_path])
    except Exception:
        _unlink_many([raw_path])
        raise

    logger.info(
        "Synthesized (ElevenLabs): %s (%s, model=%s, delivery=%s)",
        output_path.name,
        voice,
        elevenlabs_model,
        resolved_cue,
    )
    return output_path


async def synthesize(
    text: str,
    voice: str,
    output_path: Path,
    *,
    rate: str | None = None,
    pitch: str | None = None,
    engine: str = "edge",
    edge_fallback_voice: str = "",
    openai_instructions: str = "",
    loudnorm: bool = True,
    voice_settings: dict | None = None,
    elevenlabs_model: str = _ELEVENLABS_V2_MODEL,
    delivery_cue: str = "neutral",
    delivery_profile: str = "none",
    host_name: str = "",
    state: StationState | None = None,
) -> Path:
    """Render text via the chosen TTS engine, then normalize to station output settings.

    engine="openai" uses the registry-selected OpenAI speech model. Falls back
    to edge-tts if the key or registry route is unavailable. When falling back,
    uses edge_fallback_voice if set, then the house Edge voice. If every route
    fails, partial artifacts are removed and ``TTSUnavailableError`` is raised.

    loudnorm=False skips the EBU R128 pass — use for intermediate lines that will
    be assembled and loudnorm'd as a single unit by the caller.

    state: when provided, a confirmed PAID cloud-provider response (OpenAI,
    Azure, ElevenLabs) adds characters to state.tts_characters and the TTS cost
    bucket for the operator's estimate. Edge-tts is free and never counted;
    missing credentials and provider failures remain zero, while a confirmed
    response remains counted if local post-processing falls back to Edge.
    Best-effort only: never raises into the audio path.
    """
    engine = (engine or "edge").strip().lower()
    fallback_voice = edge_fallback_voice or _EDGE_DEFAULT_FALLBACK_VOICE

    billed = False

    def _bill_tts() -> None:
        # Rough by design — folded into a figure the UI labels an estimate.
        nonlocal billed
        if billed:
            return
        billed = True
        if state is not None:
            try:
                if hasattr(state, "record_tts_usage"):
                    state.record_tts_usage(len(text))
                else:
                    state.tts_characters += len(text)
            except Exception:  # never let cost bookkeeping touch the audio path
                pass

    if engine == "openai":
        if os.getenv("OPENAI_API_KEY", ""):
            try:
                async with _HEAVY_SEM:
                    result = await synthesize_openai(
                        text,
                        voice,
                        output_path,
                        instructions=openai_instructions,
                        loudnorm=loudnorm,
                        on_paid_provider_success=_bill_tts,
                    )
                return result
            except Exception as e:
                logger.warning("OpenAI TTS failed, falling back to edge-tts: %s", e)
        else:
            logger.debug("OpenAI TTS requested but OPENAI_API_KEY not set, using edge-tts")
        # Use edge fallback voice when falling back from OpenAI
        voice = fallback_voice
    elif engine == "azure":
        if os.getenv("AZURE_SPEECH_KEY", "") and os.getenv("AZURE_SPEECH_REGION", ""):
            cloud_key = _cloud_failure_key(engine, voice)
            async with _cloud_voice_attempt_lock(cloud_key):
                if _cloud_voice_failed(cloud_key):
                    logger.debug("Azure TTS voice '%s' previously failed this session; using edge fallback", voice)
                else:
                    try:
                        async with _HEAVY_SEM:
                            result = await synthesize_azure(
                                text,
                                voice,
                                output_path,
                                rate=rate,
                                pitch=pitch,
                                loudnorm=loudnorm,
                                on_paid_provider_success=_bill_tts,
                            )
                        return result
                    except Exception as e:
                        reason = _non_retryable_cloud_tts_error(e)
                        if reason:
                            _memoize_failed_cloud_voice(cloud_key)
                            logger.warning(
                                "Azure TTS disabled for voice '%s' this session after %s; falling back to edge-tts",
                                voice,
                                reason,
                            )
                        else:
                            logger.warning("Azure TTS failed, falling back to edge-tts: %s", e)
        else:
            logger.debug("Azure TTS requested but AZURE_SPEECH_KEY/AZURE_SPEECH_REGION not set, using edge-tts")
        voice = fallback_voice
    elif engine == "elevenlabs":
        if os.getenv("ELEVENLABS_API_KEY", ""):
            cloud_key = _cloud_failure_key(engine, voice, elevenlabs_model=elevenlabs_model)
            async with _cloud_voice_attempt_lock(cloud_key):
                if _cloud_voice_failed(cloud_key):
                    logger.debug(
                        "ElevenLabs TTS voice '%s' model '%s' previously failed this session; using edge fallback",
                        voice,
                        elevenlabs_model,
                    )
                else:
                    try:
                        async with _HEAVY_SEM:
                            result = await synthesize_elevenlabs(
                                text,
                                voice,
                                output_path,
                                loudnorm=loudnorm,
                                voice_settings=voice_settings,
                                elevenlabs_model=elevenlabs_model,
                                delivery_cue=delivery_cue,
                                delivery_profile=delivery_profile,
                                host_name=host_name,
                                on_paid_provider_success=_bill_tts,
                            )
                        return result
                    except Exception as e:
                        reason = _non_retryable_cloud_tts_error(e)
                        if reason:
                            _memoize_failed_cloud_voice(cloud_key)
                            logger.warning(
                                "ElevenLabs TTS disabled for voice '%s' model '%s' this session after %s; "
                                "falling back to edge-tts",
                                voice,
                                elevenlabs_model,
                                reason,
                            )
                        else:
                            logger.warning(
                                "ElevenLabs TTS model '%s' failed, falling back to edge-tts: %s",
                                elevenlabs_model,
                                e,
                            )
        else:
            logger.debug("ElevenLabs TTS requested but ELEVENLABS_API_KEY not set, using edge-tts")
        voice = fallback_voice
    elif engine != "edge":
        logger.warning("Unknown TTS engine '%s'; using edge-tts", engine)
        voice = fallback_voice

    edge_voice = _coerce_edge_voice(voice, edge_fallback_voice=edge_fallback_voice)

    # Honour runtime memoization: if this voice already failed once this
    # session, skip the primary attempt and go straight to the fallback.
    if edge_voice in _failed_edge_voices and edge_voice != fallback_voice:
        logger.debug(
            "Edge voice '%s' previously failed this session; using fallback '%s'",
            edge_voice,
            fallback_voice,
        )
        edge_voice = fallback_voice

    async with _HEAVY_SEM:
        raw_path = output_path.with_suffix(".raw.mp3")
        try:
            comm = edge_tts.Communicate(text, edge_voice, rate=rate or "+0%", pitch=pitch or "+0Hz")
            await asyncio.wait_for(comm.save(str(raw_path)), timeout=15.0)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
            _unlink_many([raw_path])

            logger.info("Synthesized: %s (%s)", output_path.name, edge_voice)
            return output_path
        except Exception as e:
            _unlink_many([raw_path])  # clean up orphaned raw file on any failure
            logger.error("TTS failed with %s: %s", edge_voice, e)
            final_error = e
            # Memoize the failure so subsequent segments skip this voice.
            _failed_edge_voices.add(edge_voice)
            # Retry with the station's house voice after the configured Edge
            # fallback. Required speech must never degrade into generated
            # silence: the caller owns the canned/music/continuity rescue.
            fallback = _EDGE_DEFAULT_FALLBACK_VOICE
            if edge_voice != fallback:
                try:
                    logger.info("Retrying TTS with fallback voice: %s", fallback)
                    comm = edge_tts.Communicate(text, fallback, rate=rate or "+0%", pitch=pitch or "+0Hz")
                    await asyncio.wait_for(comm.save(str(raw_path)), timeout=15.0)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
                    _unlink_many([raw_path])
                    logger.info("Fallback synthesized: %s (%s)", output_path.name, fallback)
                    return output_path
                except Exception as e2:
                    _unlink_many([raw_path])  # clean up on fallback failure too
                    logger.error("Fallback TTS also failed: %s", e2)
                    final_error = e2
            _unlink_speech_artifacts([output_path])
            raise TTSUnavailableError("all configured TTS routes are unavailable") from final_error


async def synthesize_ad(
    script: AdScript,
    voices: dict[str, AdVoice],
    tmp_dir: Path,
    sfx_dir: Path | None = None,
    state: StationState | None = None,
    cache_dir: Path | None = None,
    default_voice: AdVoice | None = None,
) -> Path:
    """Assemble a multi-part ad: voice segments + SFX + pauses into a single MP3.

    Voices is a role->AdVoice map. Parts with a role field use the matching voice;
    parts without a role use ``default_voice`` when supplied, otherwise the first
    voice in the dict. A direct campaign character uses this narrow override so a
    roleless script fallback cannot silently become its supporting actor.

    state is forwarded to each voice-part synthesize() for paid-TTS char accounting.
    """
    ad_parts: list[Path] = []
    loop = asyncio.get_running_loop()
    default_voice = default_voice or next(iter(voices.values()))

    # 1+2. Brand motif AND voice/SFX parts in parallel (motif is just prepended)
    from mammamiradio.audio.normalizer import AVAILABLE_SFX_TYPES

    sonic_sig = script.sonic.sonic_signature if script.sonic else ""
    motif_path = tmp_dir / f"motif_{uuid4().hex[:8]}.mp3" if sonic_sig else None

    def _sfx_asset_fingerprint(signature: str) -> list[dict[str, object]]:
        fingerprint: list[dict[str, object]] = []
        for component in [c.strip() for c in signature.split("+") if c.strip()]:
            entry: dict[str, object] = {"component": component, "asset": None}
            if sfx_dir and sfx_dir.is_dir():
                for ext in (".mp3", ".wav", ".ogg"):
                    candidate = sfx_dir / f"{component}{ext}"
                    if not candidate.exists():
                        continue
                    try:
                        stat = candidate.stat()
                    except OSError:
                        entry = {"component": component, "asset": str(candidate)}
                    else:
                        entry = {
                            "component": component,
                            "asset": str(candidate),
                            "mtime_ns": stat.st_mtime_ns,
                            "size": stat.st_size,
                        }
                    break
            fingerprint.append(entry)
        return fingerprint

    def _render_brand_motif(path: Path) -> Path:
        if cache_dir is None:
            return generate_brand_motif(path, sonic_sig, sfx_dir)
        return materialize_synth_mp3(
            cache_dir,
            "brand_motif",
            path,
            {
                "sfx_assets": _sfx_asset_fingerprint(sonic_sig),
                "sonic_signature": sonic_sig,
            },
            lambda out: generate_brand_motif(out, sonic_sig, sfx_dir),
        )

    def _render_music_bed(path: Path, bed_mood: str, duration_sec: float) -> Path:
        if cache_dir is None:
            return generate_music_bed(path, bed_mood, duration_sec)
        bucket = duration_bucket_sec(duration_sec)
        return materialize_synth_mp3(
            cache_dir,
            "music_bed",
            path,
            {
                "duration_sec": bucket,
                "mood": bed_mood,
            },
            lambda out: generate_music_bed(out, bed_mood, float(bucket)),
        )

    def _render_foley(path: Path, environment: str, duration_sec: float) -> Path:
        if cache_dir is None:
            return generate_foley_loop(path, environment, duration_sec)
        bucket = duration_bucket_sec(duration_sec)
        params = {
            "duration_sec": bucket,
            "environment": environment,
        }
        variant = next_synth_variant("foley", params)
        return materialize_synth_mp3(
            cache_dir,
            "foley",
            path,
            params,
            lambda out: generate_foley_loop(out, environment, float(bucket), variant=variant),
            variant=variant,
        )

    async def _render_part(part, part_path):
        if part.type == "voice" and part.text:
            voice_for_part = voices.get(part.role, default_voice) if part.role else default_voice
            # Legal disclaimers are format-scoped, not accidental role spikes.
            extra: dict[str, object] = {
                "engine": voice_for_part.engine,
                "edge_fallback_voice": voice_for_part.edge_fallback_voice,
                "openai_instructions": _openai_instructions_for_ad_voice(voice_for_part),
            }
            if voice_for_part.voice_settings:
                extra["voice_settings"] = voice_for_part.voice_settings
            if part.role == "disclaimer_goblin":
                extra["rate"] = _DISCLAIMER_RATE_BY_FORMAT.get(script.format, "+35%")
            # Skip per-part loudnorm — normalize_ad() handles the final loudnorm pass
            return await synthesize(part.text, voice_for_part.voice, part_path, **extra, loudnorm=False, state=state)
        if part.type == "sfx" and part.sfx:
            sfx_name = part.sfx if part.sfx in AVAILABLE_SFX_TYPES else "chime"
            try:
                return await loop.run_in_executor(None, generate_sfx, part_path, sfx_name, sfx_dir)
            except Exception as e:
                logger.warning("Ad SFX '%s' failed, inserting short fallback: %s", sfx_name, e)
                return await loop.run_in_executor(None, generate_silence, part_path, 0.18)
        if part.type == "pause":
            duration = part.duration if part.duration > 0 else 0.5
            return await loop.run_in_executor(None, generate_silence, part_path, duration)
        return None

    renderable = [
        (part, tmp_dir / f"adpart_{uuid4().hex[:8]}.mp3")
        for part in script.parts
        if part.type in ("voice", "sfx", "pause") and (part.type != "voice" or part.text)
    ]

    # Launch motif generation + all parts concurrently.  Every owned task must
    # settle before cleanup: a sibling can still be writing from an executor
    # after another required voice has already failed.
    part_tasks = [_render_part(p, path) for p, path in renderable]
    motif_result: Path | None = None
    try:
        if motif_path and sonic_sig:

            async def _gen_motif():
                try:
                    await loop.run_in_executor(None, _render_brand_motif, motif_path)
                    return motif_path
                except Exception as e:
                    logger.warning("Brand motif generation failed, skipping: %s", e)
                    _unlink_many([motif_path])
                    return None

            all_results = await _settle_owned(_gen_motif(), *part_tasks)
            motif_value = all_results[0]
            if isinstance(motif_value, Path):
                motif_result = motif_value
            results = all_results[1:]
        else:
            results = await _settle_owned(*part_tasks)
    except BaseException:
        _unlink_speech_artifacts([path for _, path in renderable])
        if motif_path:
            _unlink_many([motif_path])
        raise

    # Base exceptions (notably cancellation) remain fatal even for decorative
    # parts. Ordinary SFX/pause errors are optional; required voice errors are
    # not. Inspect only after every sibling above has settled.
    fatal = next(
        (result for result in results if isinstance(result, BaseException) and not isinstance(result, Exception)),
        None,
    )
    if fatal is not None:
        _unlink_speech_artifacts([path for _, path in renderable])
        if motif_result:
            _unlink_many([motif_result])
        raise fatal

    voice_failures: list[Exception] = []
    successful_results: list[Path] = []
    for (part, part_path), result in zip(renderable, results, strict=True):
        if isinstance(result, Exception):
            if part.type == "voice":
                voice_failures.append(result)
            else:
                logger.warning("Optional ad %s part failed, skipping: %s", part.type, result)
                _unlink_speech_artifacts([part_path])
            continue
        if isinstance(result, Path):
            successful_results.append(result)
        elif part.type == "voice":
            voice_failures.append(TTSUnavailableError("required ad voice produced no audio"))

    if voice_failures:
        _unlink_speech_artifacts(
            [path for _, path in renderable] + successful_results,
        )
        if motif_result:
            _unlink_many([motif_result])
        raise next(
            (failure for failure in voice_failures if isinstance(failure, TTSUnavailableError)),
            voice_failures[0],
        )

    has_required_voice = any(part.type == "voice" for part, _ in renderable)
    if not has_required_voice:
        # SFX-only, pause-only, and empty scripts still get a spoken brand.  A
        # decorative part is never allowed to masquerade as a complete ad.
        _unlink_speech_artifacts(
            [path for _, path in renderable] + successful_results,
        )
        if motif_result:
            _unlink_many([motif_result])
        fallback_path = tmp_dir / f"ad_fallback_{uuid4().hex[:8]}.mp3"
        await synthesize(
            script.brand,
            default_voice.voice,
            fallback_path,
            engine=default_voice.engine,
            edge_fallback_voice=default_voice.edge_fallback_voice,
            openai_instructions=_openai_instructions_for_ad_voice(default_voice),
            voice_settings=default_voice.voice_settings,
            state=state,
        )
        return fallback_path

    if motif_result:
        ad_parts.append(motif_result)

    voice_sfx_parts = successful_results

    # Concatenate voice+sfx parts
    if len(voice_sfx_parts) == 1:
        voice_path = voice_sfx_parts[0]
    else:
        voice_path = tmp_dir / f"ad_voice_{uuid4().hex[:8]}.mp3"
        # Skip loudnorm — each part already normalized by synthesize()
        try:
            concat_results = await _settle_owned(
                loop.run_in_executor(None, concat_files, voice_sfx_parts, voice_path, 300, False)
            )
            concat_failure = _prioritized_failure(concat_results)
            if concat_failure is not None:
                raise concat_failure
        except BaseException:
            _unlink_speech_artifacts([*voice_sfx_parts, voice_path])
            raise
        _unlink_many(voice_sfx_parts)

    # 3+4. Generate foley loop + env bed + music bed in parallel, then mix sequentially.
    # Layer order (quietest → loudest): foley → env bed → music bed → voice.
    env_name = script.sonic.environment if script.sonic else ""
    mood = script.mood or (script.sonic.music_bed if script.sonic else "lounge")
    voice_duration = _estimate_duration(voice_path)
    output_path = tmp_dir / f"ad_{uuid4().hex[:8]}.mp3"

    foley_path = tmp_dir / f"foley_{uuid4().hex[:8]}.mp3" if env_name else None
    env_bed_path = tmp_dir / f"envbed_{uuid4().hex[:8]}.mp3" if env_name else None
    bed_path = tmp_dir / f"adbed_{uuid4().hex[:8]}.mp3"

    # Generate all three beds concurrently
    _dur = voice_duration + 1.0
    bed_paths = [bed_path]
    bed_tasks: list[Awaitable[object]] = [
        loop.run_in_executor(None, lambda: _render_music_bed(bed_path, mood, _dur)),
    ]
    if env_name:
        _env = env_name
        _env_bed_path: Path = env_bed_path  # type: ignore[assignment]  # non-None when env_name is set
        _foley_path: Path = foley_path  # type: ignore[assignment]  # non-None when env_name is set
        bed_paths.extend((_env_bed_path, _foley_path))
        bed_tasks.append(loop.run_in_executor(None, lambda: _render_music_bed(_env_bed_path, _env, _dur)))
        bed_tasks.append(loop.run_in_executor(None, lambda: _render_foley(_foley_path, _env, _dur)))
    try:
        bed_results = await _settle_owned(*bed_tasks)
    except BaseException:
        _unlink_many(bed_paths)
        _unlink_speech_artifacts([voice_path])
        if motif_result:
            _unlink_many([motif_result])
        raise
    for bed_result, generated_path in zip(bed_results, bed_paths, strict=True):
        if isinstance(bed_result, BaseException):
            logger.warning("Bed generation failed for %s: %s", generated_path.name, bed_result)
            _unlink_many([generated_path])

    # Mix foley first (quietest layer — ambient texture under everything else)
    if foley_path and foley_path.exists():
        try:
            foley_mixed_path = tmp_dir / f"foley_mix_{uuid4().hex[:8]}.mp3"
            await loop.run_in_executor(None, mix_with_bed, voice_path, foley_path, foley_mixed_path, 0.07)
            foley_path.unlink(missing_ok=True)
            voice_path.unlink(missing_ok=True)
            voice_path = foley_mixed_path
        except Exception as e:
            logger.warning("Foley mix failed (%s), continuing without: %s", env_name, e)
            if foley_path:
                foley_path.unlink(missing_ok=True)

    # Mix env bed (medium layer — tonal environment character)
    if env_bed_path and env_bed_path.exists():
        try:
            env_mixed_path = tmp_dir / f"envmix_{uuid4().hex[:8]}.mp3"
            await loop.run_in_executor(None, mix_with_bed, voice_path, env_bed_path, env_mixed_path, 0.14)
            env_bed_path.unlink(missing_ok=True)
            voice_path.unlink(missing_ok=True)
            voice_path = env_mixed_path
        except Exception as e:
            logger.warning("Environment bed mixing failed (%s), continuing without: %s", env_name, e)

    # Mix music bed (loudest bed layer — harmonic colour)
    if bed_path.exists() and bed_path.stat().st_size > 0:
        try:
            await loop.run_in_executor(None, mix_with_bed, voice_path, bed_path, output_path, 0.24)
            if output_path.exists() and output_path.stat().st_size > 0:
                bed_path.unlink(missing_ok=True)
                voice_path.unlink(missing_ok=True)
                logger.info("Ad with beds (env=%s mood=%s): %s", env_name or "none", mood, output_path.name)
            else:
                logger.warning("Music bed mixing produced empty output (%s), using voice-only", mood)
                bed_path.unlink(missing_ok=True)
                if voice_path != output_path:
                    output_path.unlink(missing_ok=True)
                    shutil.move(str(voice_path), str(output_path))
        except Exception as e:
            logger.warning("Music bed mixing failed (%s), using voice-only: %s", mood, e)
            bed_path.unlink(missing_ok=True)
            if voice_path != output_path:
                output_path.unlink(missing_ok=True)
                shutil.move(str(voice_path), str(output_path))
    else:
        logger.warning("Music bed missing or empty at %s, using voice-only ad", bed_path)
        bed_path.unlink(missing_ok=True)
        if voice_path != output_path:
            shutil.move(str(voice_path), str(output_path))

    # 5. Prepend brand motif if we have one
    if ad_parts:
        ad_parts.append(output_path)
        final_path = tmp_dir / f"ad_final_{uuid4().hex[:8]}.mp3"
        try:
            await loop.run_in_executor(None, concat_files, ad_parts, final_path, 100)
            for p in ad_parts:
                p.unlink(missing_ok=True)
            output_path = final_path
        except Exception as e:
            logger.warning("Motif concat failed, using ad without motif: %s", e)
            for p in ad_parts[:-1]:
                p.unlink(missing_ok=True)

    # 6. Broadcast-style processing: compression + treble boost + loudness bump
    broadcast_path = tmp_dir / f"ad_broadcast_{uuid4().hex[:8]}.mp3"
    try:
        await loop.run_in_executor(None, normalize_ad, output_path, broadcast_path)
        # Only delete original after verifying broadcast file is non-empty
        if broadcast_path.exists() and broadcast_path.stat().st_size > 0:
            output_path.unlink(missing_ok=True)
            return broadcast_path
        logger.warning("Broadcast processing produced empty file, using unprocessed ad")
        broadcast_path.unlink(missing_ok=True)
        return output_path
    except Exception as e:
        logger.warning("Broadcast processing failed, using unprocessed ad: %s", e)
        return output_path


def _prosody_for_host(host: HostPersonality) -> dict[str, str]:
    """Derive TTS rate/pitch adjustments from personality axes."""
    kwargs: dict[str, str] = {}
    p = host.personality
    if p.energy > 60:
        kwargs["rate"] = "+10%"
    elif p.energy < 40:
        kwargs["rate"] = "-10%"
    if p.warmth > 60:
        kwargs["pitch"] = "-5Hz"
    elif p.warmth < 40:
        kwargs["pitch"] = "+5Hz"
    return kwargs


def _validate_dialogue_part(path: Path, *, line_number: int) -> None:
    """Reject broken intermediate TTS line files before they can hide in concat."""
    if not path.exists():
        raise AudioQualityError(f"dialogue line {line_number} audio missing: {path}")
    size = path.stat().st_size
    if size < _MIN_DIALOGUE_LINE_BYTES:
        raise AudioQualityError(f"dialogue line {line_number} audio is too small ({size} bytes)")
    duration = probe_duration_sec(path)
    # A None probe means ffprobe timed out or is unavailable (common on a
    # loaded Pi) — not proof the file is broken. The size check above already
    # rejects truncated-to-nothing files; skip the duration check rather than
    # reject a plausibly-valid line. Gates 2 and 3 also no-op when probing
    # fails, so this keeps Gate 1 consistent with the rest of the chain.
    if duration is not None and duration < _MIN_DIALOGUE_LINE_DURATION_SEC:
        raise AudioQualityError(
            f"dialogue line {line_number} audio too short ({duration:.2f}s < {_MIN_DIALOGUE_LINE_DURATION_SEC:.2f}s)"
        )


def _unlink_many(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            # Cleanup is best-effort.  Never replace the synthesis failure or
            # cancellation that selected this path with a filesystem error.
            logger.warning("Could not remove TTS scratch file %s: %s", path, exc)


def _unlink_speech_artifacts(paths: list[Path]) -> None:
    """Remove final and raw speech outputs, tolerating duplicate paths."""
    artifacts = {artifact for path in paths for artifact in (path, path.with_suffix(".raw.mp3"))}
    _unlink_many(list(artifacts))


async def synthesize_dialogue(
    lines: Sequence[DialogueLine | tuple[HostPersonality, str]],
    tmp_dir: Path,
    state: StationState | None = None,
) -> Path:
    """Render clean host lines in parallel and stitch the exchange together.

    ``DialogueLine`` carries semantic delivery metadata without changing legacy
    tuple callers. State is forwarded to each per-line synthesize() for paid-TTS
    character accounting.
    """
    if not lines:
        raise ValueError("synthesize_dialogue: lines list must not be empty")

    dialogue_lines = [
        line if isinstance(line, DialogueLine) else DialogueLine(host=line[0], text=line[1]) for line in lines
    ]
    paths = [tmp_dir / f"line_{uuid4().hex[:8]}.mp3" for _ in dialogue_lines]
    multi_line = len(dialogue_lines) > 1

    # For multi-line dialogue: skip per-line loudnorm (just re-encode to station format),
    # concat, then do one final loudnorm on the assembled segment. Reduces N passes → 1.
    # For single-line: one full loudnorm pass is correct and avoids an extra encode cycle.
    try:
        results = await _settle_owned(
            *(
                synthesize(
                    line.text,
                    line.host.voice,
                    path,
                    **_prosody_for_host(line.host),
                    engine=line.host.engine,
                    edge_fallback_voice=line.host.edge_fallback_voice,
                    openai_instructions=_openai_instructions_for_host(line.host),
                    loudnorm=not multi_line,
                    voice_settings=line.host.voice_settings,
                    elevenlabs_model=line.host.elevenlabs_model,
                    delivery_cue=line.delivery,
                    delivery_profile=line.host.delivery_profile,
                    host_name=line.host.name,
                    state=state,
                )
                for line, path in zip(dialogue_lines, paths, strict=True)
            )
        )
    except BaseException:
        _unlink_speech_artifacts(paths)
        raise

    failure = _prioritized_failure(results)
    if failure is not None:
        _unlink_speech_artifacts(paths)
        raise failure

    parts = [result for result in results if isinstance(result, Path)]
    if len(parts) != len(paths):
        _unlink_speech_artifacts(paths)
        raise TTSUnavailableError("required dialogue voice produced no audio")

    if not multi_line:
        return parts[0]

    # Per-line validation catches broken intermediate files before they can hide
    # in concat. Only runs for multi-line: single-line segments are short by design
    # (Italian exclamations like "Sì!" are legitimately < 0.5s) and are guarded
    # by the banter quality gate in the producer instead.
    loop = asyncio.get_running_loop()
    try:
        validation_results = await _settle_owned(
            *(
                loop.run_in_executor(None, partial(_validate_dialogue_part, part, line_number=idx))
                for idx, part in enumerate(parts, start=1)
            )
        )
        validation_failure = _prioritized_failure(validation_results)
        if validation_failure is not None:
            raise validation_failure
    except BaseException:
        _unlink_many(parts)
        raise

    raw_path = tmp_dir / f"dialogue_raw_{uuid4().hex[:8]}.mp3"
    output_path = tmp_dir / f"dialogue_{uuid4().hex[:8]}.mp3"
    try:
        await loop.run_in_executor(
            None,
            partial(concat_files, parts, raw_path, 300, False, strict_duration=True),
        )
    except Exception:
        _unlink_many([*parts, raw_path])
        raise
    _unlink_many(parts)

    # One loudnorm pass on the fully assembled dialogue
    async with _HEAVY_SEM:
        try:
            await loop.run_in_executor(None, normalize, raw_path, output_path)
        except Exception:
            _unlink_many([raw_path, output_path])
            raise
    _unlink_many([raw_path])
    return output_path
