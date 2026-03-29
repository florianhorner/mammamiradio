from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import edge_tts

from fakeitaliradio.models import HostPersonality
from fakeitaliradio.normalizer import concat_files, normalize, generate_silence

logger = logging.getLogger(__name__)


async def synthesize(text: str, voice: str, output_path: Path) -> Path:
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


async def synthesize_dialogue(
    lines: list[tuple[HostPersonality, str]],
    tmp_dir: Path,
) -> Path:
    parts: list[Path] = []

    for i, (host, text) in enumerate(lines):
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
