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
)

logger = logging.getLogger(__name__)


def _estimate_duration(path: Path) -> float:
    """Rough duration estimate from file size at 192kbps."""
    return max(5.0, path.stat().st_size / (192 * 128))


async def synthesize(text: str, voice: str, output_path: Path) -> Path:
    """Render text with Edge TTS, then normalize it to station output settings."""
    try:
        comm = edge_tts.Communicate(text, voice)
        raw_path = output_path.with_suffix(".raw.mp3")
        await comm.save(str(raw_path))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, normalize, raw_path, output_path)
        raw_path.unlink(missing_ok=True)

        logger.info("Synthesized: %s (%s)", output_path.name, voice)
        return output_path
    except Exception as e:
        logger.error("TTS failed: %s", e)
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

    # 1. Brand motif (prepend only, skip on failure)
    sonic_sig = script.sonic.sonic_signature if script.sonic else ""
    if sonic_sig:
        try:
            motif_path = tmp_dir / f"motif_{uuid4().hex[:8]}.mp3"
            await loop.run_in_executor(None, generate_brand_motif, motif_path, sonic_sig, sfx_dir)
            ad_parts.append(motif_path)
        except Exception as e:
            logger.warning("Brand motif generation failed, skipping: %s", e)

    # 2. Assemble voice + SFX + pause + environment parts
    voice_sfx_parts: list[Path] = []
    for _i, part in enumerate(script.parts):
        part_path = tmp_dir / f"adpart_{uuid4().hex[:8]}.mp3"

        if part.type == "voice" and part.text:
            # Resolve voice by role
            voice_for_part = voices.get(part.role, default_voice) if part.role else default_voice
            await synthesize(part.text, voice_for_part.voice, part_path)
            voice_sfx_parts.append(part_path)
        elif part.type == "sfx" and part.sfx:
            await loop.run_in_executor(None, generate_sfx, part_path, part.sfx, sfx_dir)
            voice_sfx_parts.append(part_path)
        elif part.type == "pause":
            duration = part.duration if part.duration > 0 else 0.5
            await loop.run_in_executor(None, generate_silence, part_path, duration)
            voice_sfx_parts.append(part_path)
        elif part.type == "environment":
            # Environment cues are handled via the bed mixing below
            pass

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
        await loop.run_in_executor(None, concat_files, voice_sfx_parts, voice_path)
        for p in voice_sfx_parts:
            p.unlink(missing_ok=True)

    # 3. Layer environment bed if present (quieter than music bed)
    env_name = script.sonic.environment if script.sonic else ""
    if env_name:
        try:
            voice_duration = _estimate_duration(voice_path)
            env_bed_path = tmp_dir / f"envbed_{uuid4().hex[:8]}.mp3"
            await loop.run_in_executor(None, generate_music_bed, env_bed_path, env_name, voice_duration + 1.0)
            env_mixed_path = tmp_dir / f"envmix_{uuid4().hex[:8]}.mp3"
            await loop.run_in_executor(None, mix_with_bed, voice_path, env_bed_path, env_mixed_path, 0.06)
            env_bed_path.unlink(missing_ok=True)
            voice_path.unlink(missing_ok=True)
            voice_path = env_mixed_path
        except Exception as e:
            logger.warning("Environment bed mixing failed (%s), continuing without: %s", env_name, e)

    # 4. Mix with music bed
    mood = script.mood or (script.sonic.music_bed if script.sonic else "lounge")
    output_path = tmp_dir / f"ad_{uuid4().hex[:8]}.mp3"
    try:
        voice_duration = _estimate_duration(voice_path)
        bed_path = tmp_dir / f"adbed_{uuid4().hex[:8]}.mp3"
        await loop.run_in_executor(None, generate_music_bed, bed_path, mood, voice_duration + 1.0)
        await loop.run_in_executor(None, mix_with_bed, voice_path, bed_path, output_path)
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
            return final_path
        except Exception as e:
            logger.warning("Motif concat failed, using ad without motif: %s", e)
            for p in ad_parts[:-1]:
                p.unlink(missing_ok=True)
            return output_path

    return output_path


async def synthesize_dialogue(
    lines: list[tuple[HostPersonality, str]],
    tmp_dir: Path,
) -> Path:
    """Render each host line separately and stitch the exchange together."""
    parts: list[Path] = []

    for _i, (host, text) in enumerate(lines):
        part_path = tmp_dir / f"line_{uuid4().hex[:8]}.mp3"
        await synthesize(text, host.voice, part_path)
        parts.append(part_path)

    if len(parts) == 1:
        return parts[0]

    output_path = tmp_dir / f"dialogue_{uuid4().hex[:8]}.mp3"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, concat_files, parts, output_path)

    for p in parts:
        p.unlink(missing_ok=True)

    return output_path
