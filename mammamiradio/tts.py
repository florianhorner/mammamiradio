"""Text-to-speech assembly for host dialogue and produced ads."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from uuid import uuid4

import edge_tts

from mammamiradio.models import AdScript, AdVoice, HostPersonality
from mammamiradio.normalizer import (
    concat_files,
    generate_brand_motif,
    generate_music_bed,
    generate_sfx,
    generate_silence,
    mix_with_bed,
    normalize,
    normalize_ad,
)

logger = logging.getLogger(__name__)


def _estimate_duration(path: Path) -> float:
    """Rough duration estimate from file size at 192kbps."""
    return max(5.0, path.stat().st_size / (192 * 128))


async def synthesize(
    text: str,
    voice: str,
    output_path: Path,
    *,
    rate: str | None = None,
    pitch: str | None = None,
) -> Path:
    """Render text with Edge TTS, then normalize it to station output settings."""
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate or "+0%", pitch=pitch or "+0Hz")
        raw_path = output_path.with_suffix(".raw.mp3")
        await asyncio.wait_for(comm.save(str(raw_path)), timeout=15.0)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, normalize, raw_path, output_path)
        raw_path.unlink(missing_ok=True)

        logger.info("Synthesized: %s (%s)", output_path.name, voice)
        return output_path
    except Exception as e:
        logger.error("TTS failed with %s: %s", voice, e)
        # Retry with a fallback voice before resorting to silence
        fallback = "it-IT-DiegoNeural"
        if voice != fallback:
            try:
                logger.info("Retrying TTS with fallback voice: %s", fallback)
                comm = edge_tts.Communicate(text, fallback, rate=rate or "+0%", pitch=pitch or "+0Hz")
                raw_path = output_path.with_suffix(".raw.mp3")
                await asyncio.wait_for(comm.save(str(raw_path)), timeout=15.0)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, normalize, raw_path, output_path)
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
            return await synthesize(part.text, voice_for_part.voice, part_path)
        elif part.type == "sfx" and part.sfx:
            sfx_name = part.sfx if part.sfx in AVAILABLE_SFX_TYPES else "chime"
            return await loop.run_in_executor(None, generate_sfx, part_path, sfx_name, sfx_dir)
        elif part.type == "pause":
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

    # 3+4. Generate env bed + music bed in parallel, then mix sequentially
    env_name = script.sonic.environment if script.sonic else ""
    mood = script.mood or (script.sonic.music_bed if script.sonic else "lounge")
    voice_duration = _estimate_duration(voice_path)
    output_path = tmp_dir / f"ad_{uuid4().hex[:8]}.mp3"

    env_bed_path = tmp_dir / f"envbed_{uuid4().hex[:8]}.mp3" if env_name else None
    bed_path = tmp_dir / f"adbed_{uuid4().hex[:8]}.mp3"

    # Generate both beds concurrently
    bed_tasks = [loop.run_in_executor(None, generate_music_bed, bed_path, mood, voice_duration + 1.0)]
    if env_bed_path:
        bed_tasks.append(loop.run_in_executor(None, generate_music_bed, env_bed_path, env_name, voice_duration + 1.0))
    try:
        await asyncio.gather(*bed_tasks)
    except Exception as e:
        logger.warning("Bed generation failed: %s", e)

    # Mix env bed first (if present), then music bed
    if env_bed_path and env_bed_path.exists():
        try:
            env_mixed_path = tmp_dir / f"envmix_{uuid4().hex[:8]}.mp3"
            await loop.run_in_executor(None, mix_with_bed, voice_path, env_bed_path, env_mixed_path, 0.10)
            env_bed_path.unlink(missing_ok=True)
            voice_path.unlink(missing_ok=True)
            voice_path = env_mixed_path
        except Exception as e:
            logger.warning("Environment bed mixing failed (%s), continuing without: %s", env_name, e)

    try:
        await loop.run_in_executor(None, mix_with_bed, voice_path, bed_path, output_path, 0.20)
        bed_path.unlink(missing_ok=True)
        voice_path.unlink(missing_ok=True)
        logger.info("Ad with music bed (%s): %s", mood, output_path.name)
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

    # Synthesize all lines concurrently — each is an independent TTS + normalize
    parts = list(
        await asyncio.gather(
            *(
                synthesize(text, host.voice, path, **_prosody_for_host(host))
                for (host, text), path in zip(lines, paths, strict=False)
            )
        )
    )

    if len(parts) == 1:
        return parts[0]

    output_path = tmp_dir / f"dialogue_{uuid4().hex[:8]}.mp3"
    loop = asyncio.get_running_loop()
    # Skip redundant loudnorm — each line already normalized by synthesize()
    await loop.run_in_executor(None, concat_files, parts, output_path, 300, False)

    for p in parts:
        p.unlink(missing_ok=True)

    return output_path
