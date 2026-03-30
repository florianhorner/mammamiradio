"""Text-to-speech assembly for host dialogue and produced ads."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import edge_tts

from mammamiradio.models import AdScript, AdVoice, HostPersonality
from mammamiradio.normalizer import (
    concat_files,
    generate_music_bed,
    generate_sfx,
    generate_silence,
    mix_with_bed,
    normalize,
)

logger = logging.getLogger(__name__)


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
    voice: AdVoice,
    tmp_dir: Path,
    sfx_dir: Path | None = None,
) -> Path:
    """Assemble a multi-part ad: voice segments + SFX + pauses into a single MP3."""
    parts: list[Path] = []
    loop = asyncio.get_running_loop()

    for _i, part in enumerate(script.parts):
        part_path = tmp_dir / f"adpart_{uuid4().hex[:8]}.mp3"

        if part.type == "voice" and part.text:
            await synthesize(part.text, voice.voice, part_path)
            parts.append(part_path)
        elif part.type == "sfx" and part.sfx:
            await loop.run_in_executor(
                None,
                generate_sfx,
                part_path,
                part.sfx,
                sfx_dir,
            )
            parts.append(part_path)
        elif part.type == "pause":
            duration = part.duration if part.duration > 0 else 0.5
            await loop.run_in_executor(
                None,
                generate_silence,
                part_path,
                duration,
            )
            parts.append(part_path)

    if not parts:
        # Fallback: synthesize brand name
        fallback_path = tmp_dir / f"ad_fallback_{uuid4().hex[:8]}.mp3"
        await synthesize(script.brand, voice.voice, fallback_path)
        return fallback_path

    # Assemble voice+sfx parts
    if len(parts) == 1:
        voice_path = parts[0]
    else:
        voice_path = tmp_dir / f"ad_voice_{uuid4().hex[:8]}.mp3"
        await loop.run_in_executor(None, concat_files, parts, voice_path)
        for p in parts:
            p.unlink(missing_ok=True)

    # Mix with music bed if mood is specified
    mood = script.mood or "lounge"
    output_path = tmp_dir / f"ad_{uuid4().hex[:8]}.mp3"
    try:
        # Get voice duration for bed length (approximate: file size / bitrate)
        voice_size = voice_path.stat().st_size
        voice_duration = max(5.0, voice_size / (192 * 128))  # rough estimate
        bed_path = tmp_dir / f"adbed_{uuid4().hex[:8]}.mp3"
        await loop.run_in_executor(
            None,
            generate_music_bed,
            bed_path,
            mood,
            voice_duration + 1.0,
        )
        await loop.run_in_executor(
            None,
            mix_with_bed,
            voice_path,
            bed_path,
            output_path,
        )
        bed_path.unlink(missing_ok=True)
        voice_path.unlink(missing_ok=True)
        logger.info("Ad with music bed (%s): %s", mood, output_path.name)
    except Exception as e:
        logger.warning("Music bed mixing failed (%s), using voice-only: %s", mood, e)
        # Fallback: just use the voice track without a bed
        if voice_path != output_path:
            import shutil

            shutil.move(str(voice_path), str(output_path))

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
