from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _run_ffmpeg(cmd: list[str], description: str) -> subprocess.CompletedProcess:
    """Run an ffmpeg command with stderr capture and logging on failure."""
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-500:]
        logger.error("ffmpeg failed (%s): %s", description, stderr)
        result.check_returncode()  # raises CalledProcessError
    return result


def normalize(input_path: Path, output_path: Path, config=None) -> Path:
    sample_rate = str(config.audio.sample_rate) if config else "48000"
    channels = str(config.audio.channels) if config else "2"
    bitrate = f"{config.audio.bitrate}k" if config else "192k"

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-ar", sample_rate, "-ac", channels, "-b:a", bitrate,
        "-filter:a", "loudnorm=I=-16:LRA=11:TP=-1.5",
        "-f", "mp3", str(output_path),
    ]
    _run_ffmpeg(cmd, f"normalize {input_path.name}")
    logger.info("Normalized: %s -> %s", input_path.name, output_path.name)
    return output_path


def concat_files(paths: list[Path], output_path: Path, silence_ms: int = 300) -> Path:
    if len(paths) == 1:
        return paths[0]

    inputs = []
    filter_parts = []
    for i, p in enumerate(paths):
        inputs.extend(["-i", str(p)])
        filter_parts.append(f"[{i}:a]")

    filter_str = "".join(filter_parts) + f"concat=n={len(paths)}:v=0:a=1[out]"

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str,
        "-map", "[out]",
        "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-f", "mp3", str(output_path),
    ]
    _run_ffmpeg(cmd, f"concat {len(paths)} files")
    logger.info("Concatenated %d files -> %s", len(paths), output_path.name)
    return output_path


def generate_silence(output_path: Path, duration_sec: float = 3.0) -> Path:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
        "-t", str(duration_sec),
        "-b:a", "192k", "-f", "mp3", str(output_path),
    ]
    _run_ffmpeg(cmd, "generate silence")
    return output_path
