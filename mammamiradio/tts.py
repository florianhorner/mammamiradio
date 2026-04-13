"""Text-to-speech assembly for host dialogue and produced ads."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

import edge_tts

from mammamiradio.models import AdScript, AdVoice, HostPersonality
from mammamiradio.normalizer import (
    concat_files,
    generate_brand_motif,
    generate_foley_loop,
    generate_music_bed,
    generate_sfx,
    generate_silence,
    mix_with_bed,
    normalize,
    normalize_ad,
)

logger = logging.getLogger(__name__)

# Default instructions for OpenAI TTS voice
_OPENAI_TTS_INSTRUCTIONS = "Speak like a charismatic Italian radio host. Warm, energetic, natural pacing."

# Cache: personality hash → instructions string (personality doesn't change mid-session)
_instructions_cache: dict[int, str] = {}

# Singleton OpenAI client — reuses HTTP connection pool across calls
_openai_client = None
_openai_client_key: str = ""
_EDGE_DEFAULT_FALLBACK_VOICE = "it-IT-DiegoNeural"
_OPENAI_VOICE_IDS = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
}

# Cap concurrent TTS + FFmpeg jobs to avoid CPU/thermal spikes on constrained hardware
# (e.g. Home Assistant Green — fanless ARM SoC). Two slots let one TTS+normalize and
# one SFX/bed generation overlap without saturating all cores.
_HEAVY_SEM = asyncio.Semaphore(2)


def _looks_like_openai_voice(voice: str) -> bool:
    return voice.strip().lower() in _OPENAI_VOICE_IDS


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


async def synthesize_openai(
    text: str,
    voice: str,
    output_path: Path,
    *,
    instructions: str = "",
    loudnorm: bool = True,
) -> Path:
    """Render text with OpenAI gpt-4o-mini-tts, then normalize to station settings."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = _get_openai_client(api_key)
    loop = asyncio.get_running_loop()

    def _call_openai() -> bytes:
        response = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=voice,
            input=text,
            instructions=instructions or _OPENAI_TTS_INSTRUCTIONS,
        )
        return response.content

    raw_path = output_path.with_suffix(".raw.mp3")
    audio_bytes = await asyncio.wait_for(
        loop.run_in_executor(None, _call_openai),
        timeout=30.0,
    )
    raw_path.write_bytes(audio_bytes)

    await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
    raw_path.unlink(missing_ok=True)

    logger.info("Synthesized (OpenAI): %s (%s)", output_path.name, voice)
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
) -> Path:
    """Render text via the chosen TTS engine, then normalize to station output settings.

    engine="openai" uses OpenAI gpt-4o-mini-tts. Falls back to edge-tts if
    OPENAI_API_KEY is missing. When falling back, uses edge_fallback_voice if set.

    loudnorm=False skips the EBU R128 pass — use for intermediate lines that will
    be assembled and loudnorm'd as a single unit by the caller.
    """
    if engine == "openai":
        if os.getenv("OPENAI_API_KEY", ""):
            try:
                async with _HEAVY_SEM:
                    return await synthesize_openai(
                        text, voice, output_path, instructions=openai_instructions, loudnorm=loudnorm
                    )
            except Exception as e:
                logger.warning("OpenAI TTS failed, falling back to edge-tts: %s", e)
        else:
            logger.debug("OpenAI TTS requested but OPENAI_API_KEY not set, using edge-tts")
        # Use edge fallback voice when falling back from OpenAI
        if edge_fallback_voice:
            voice = edge_fallback_voice

    edge_voice = _coerce_edge_voice(voice, edge_fallback_voice=edge_fallback_voice)

    async with _HEAVY_SEM:
        try:
            comm = edge_tts.Communicate(text, edge_voice, rate=rate or "+0%", pitch=pitch or "+0Hz")
            raw_path = output_path.with_suffix(".raw.mp3")
            await asyncio.wait_for(comm.save(str(raw_path)), timeout=15.0)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
            raw_path.unlink(missing_ok=True)

            logger.info("Synthesized: %s (%s)", output_path.name, edge_voice)
            return output_path
        except Exception as e:
            logger.error("TTS failed with %s: %s", edge_voice, e)
            # Retry with a fallback voice before resorting to silence
            fallback = _EDGE_DEFAULT_FALLBACK_VOICE
            if edge_voice != fallback:
                try:
                    logger.info("Retrying TTS with fallback voice: %s", fallback)
                    comm = edge_tts.Communicate(text, fallback, rate=rate or "+0%", pitch=pitch or "+0Hz")
                    raw_path = output_path.with_suffix(".raw.mp3")
                    await asyncio.wait_for(comm.save(str(raw_path)), timeout=15.0)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, lambda: normalize(raw_path, output_path, loudnorm=loudnorm))
                    raw_path.unlink(missing_ok=True)
                    logger.info("Fallback synthesized: %s (%s)", output_path.name, fallback)
                    return output_path
                except Exception as e2:
                    logger.error("Fallback TTS also failed: %s", e2)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, generate_silence, output_path, 2.0)
            return output_path


async def synthesize_ad(
    script: AdScript,
    voices: dict[str, AdVoice],
    tmp_dir: Path,
    sfx_dir: Path | None = None,
) -> Path:
    """Assemble a multi-part ad: voice segments + SFX + pauses into a single MP3.

    Voices is a role->AdVoice map. Parts with a role field use the matching voice;
    parts without a role use the first voice in the dict.
    """
    ad_parts: list[Path] = []
    loop = asyncio.get_running_loop()
    default_voice = next(iter(voices.values()))

    # 1+2. Brand motif AND voice/SFX parts in parallel (motif is just prepended)
    from mammamiradio.normalizer import AVAILABLE_SFX_TYPES

    sonic_sig = script.sonic.sonic_signature if script.sonic else ""
    motif_path = tmp_dir / f"motif_{uuid4().hex[:8]}.mp3" if sonic_sig else None

    async def _render_part(part, part_path):
        if part.type == "voice" and part.text:
            voice_for_part = voices.get(part.role, default_voice) if part.role else default_voice
            # Pharma disclaimers are read at ~2x speed — real Italian radio style
            extra: dict[str, str] = {}
            if part.role == "disclaimer_goblin":
                extra["rate"] = "+90%"
            # Skip per-part loudnorm — normalize_ad() handles the final loudnorm pass
            return await synthesize(part.text, voice_for_part.voice, part_path, **extra, loudnorm=False)
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

    # Launch motif generation + all parts concurrently
    part_tasks = [_render_part(p, path) for p, path in renderable]
    if motif_path and sonic_sig:

        async def _gen_motif():
            try:
                await loop.run_in_executor(None, generate_brand_motif, motif_path, sonic_sig, sfx_dir)
                return motif_path
            except Exception as e:
                logger.warning("Brand motif generation failed, skipping: %s", e)
                return None

        all_results = await asyncio.gather(_gen_motif(), *part_tasks)
        motif_result = all_results[0]
        if motif_result:
            ad_parts.append(motif_result)
        results = all_results[1:]
    else:
        results = await asyncio.gather(*part_tasks)

    voice_sfx_parts = [r for r in results if r is not None]

    if not voice_sfx_parts:
        # Fallback: synthesize brand name
        fallback_path = tmp_dir / f"ad_fallback_{uuid4().hex[:8]}.mp3"
        await synthesize(script.brand, default_voice.voice, fallback_path)
        return fallback_path

    # Concatenate voice+sfx parts
    if len(voice_sfx_parts) == 1:
        voice_path = voice_sfx_parts[0]
    else:
        voice_path = tmp_dir / f"ad_voice_{uuid4().hex[:8]}.mp3"
        # Skip loudnorm — each part already normalized by synthesize()
        await loop.run_in_executor(None, concat_files, voice_sfx_parts, voice_path, 300, False)
        for p in voice_sfx_parts:
            p.unlink(missing_ok=True)

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
    bed_tasks: list = [
        loop.run_in_executor(None, generate_music_bed, bed_path, mood, voice_duration + 1.0),
    ]
    if env_name:
        bed_tasks.append(
            loop.run_in_executor(None, generate_music_bed, env_bed_path, env_name, voice_duration + 1.0)
        )
        bed_tasks.append(
            loop.run_in_executor(None, generate_foley_loop, foley_path, env_name, voice_duration + 1.0)
        )
    try:
        await asyncio.gather(*bed_tasks)
    except Exception as e:
        logger.warning("Bed generation failed: %s", e)

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
    try:
        await loop.run_in_executor(None, mix_with_bed, voice_path, bed_path, output_path, 0.24)
        bed_path.unlink(missing_ok=True)
        voice_path.unlink(missing_ok=True)
        logger.info("Ad with beds (env=%s mood=%s): %s", env_name or "none", mood, output_path.name)
    except Exception as e:
        logger.warning("Music bed mixing failed (%s), using voice-only: %s", mood, e)
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


async def synthesize_dialogue(
    lines: list[tuple[HostPersonality, str]],
    tmp_dir: Path,
) -> Path:
    """Render all host lines in parallel and stitch the exchange together."""
    paths = [tmp_dir / f"line_{uuid4().hex[:8]}.mp3" for _ in lines]
    multi_line = len(lines) > 1

    # For multi-line dialogue: skip per-line loudnorm (just re-encode to station format),
    # concat, then do one final loudnorm on the assembled segment. Reduces N passes → 1.
    # For single-line: one full loudnorm pass is correct and avoids an extra encode cycle.
    parts = list(
        await asyncio.gather(
            *(
                synthesize(
                    text,
                    host.voice,
                    path,
                    **_prosody_for_host(host),
                    engine=host.engine,
                    edge_fallback_voice=host.edge_fallback_voice,
                    openai_instructions=_openai_instructions_for_host(host),
                    loudnorm=not multi_line,
                )
                for (host, text), path in zip(lines, paths, strict=False)
            )
        )
    )

    if not multi_line:
        return parts[0]

    raw_path = tmp_dir / f"dialogue_raw_{uuid4().hex[:8]}.mp3"
    output_path = tmp_dir / f"dialogue_{uuid4().hex[:8]}.mp3"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, concat_files, parts, raw_path, 300, False)
    for p in parts:
        p.unlink(missing_ok=True)

    # One loudnorm pass on the fully assembled dialogue
    async with _HEAVY_SEM:
        await loop.run_in_executor(None, normalize, raw_path, output_path)
    raw_path.unlink(missing_ok=True)
    return output_path
