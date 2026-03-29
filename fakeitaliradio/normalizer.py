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


def generate_tone(output_path: Path, freq_hz: float = 880, duration_sec: float = 0.5) -> Path:
    """Generate a sine tone with fade-in/out envelope (chime/ding sound)."""
    fade = min(0.15, duration_sec / 3)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"sine=frequency={freq_hz}:duration={duration_sec}",
        "-af", f"afade=t=in:d={fade},afade=t=out:st={duration_sec - fade}:d={fade}",
        "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-f", "mp3", str(output_path),
    ]
    _run_ffmpeg(cmd, f"tone {freq_hz}Hz")
    return output_path


def generate_sweep(output_path: Path, start_hz: float = 200, end_hz: float = 2000, duration_sec: float = 0.8) -> Path:
    """Generate a frequency sweep (radio transition whoosh)."""
    fade = min(0.1, duration_sec / 4)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"sine=frequency={start_hz}:duration={duration_sec}:sample_rate=48000",
        "-af",
        f"asetrate=48000*({end_hz}/{start_hz})^(t/{duration_sec}),aresample=48000,"
        f"afade=t=in:d={fade},afade=t=out:st={duration_sec - fade}:d={fade}",
        "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-f", "mp3", str(output_path),
    ]
    _run_ffmpeg(cmd, f"sweep {start_hz}-{end_hz}Hz")
    return output_path


def generate_sfx(output_path: Path, sfx_type: str, sfx_dir: Path | None = None) -> Path:
    """Generate or load a sound effect. Checks sfx_dir for pre-recorded files first."""
    import shutil

    # Check for pre-recorded SFX file
    if sfx_dir and sfx_dir.is_dir():
        for ext in (".mp3", ".wav", ".ogg"):
            candidate = sfx_dir / f"{sfx_type}{ext}"
            if candidate.exists():
                shutil.copy2(candidate, output_path)
                logger.info("Using pre-recorded SFX: %s", candidate.name)
                return output_path

    # Synthetic fallbacks
    if sfx_type in ("chime", "ding"):
        return generate_tone(output_path, freq_hz=880, duration_sec=0.4)
    elif sfx_type == "cash_register":
        # Two quick high tones
        return generate_tone(output_path, freq_hz=1200, duration_sec=0.3)
    elif sfx_type in ("sweep", "whoosh"):
        return generate_sweep(output_path, start_hz=300, end_hz=3000, duration_sec=0.6)
    else:
        # Unknown SFX type — short chime as default
        logger.warning("Unknown SFX type '%s', using default chime", sfx_type)
        return generate_tone(output_path, freq_hz=880, duration_sec=0.4)


def generate_silence(output_path: Path, duration_sec: float = 3.0) -> Path:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
        "-t", str(duration_sec),
        "-b:a", "192k", "-f", "mp3", str(output_path),
    ]
    _run_ffmpeg(cmd, "generate silence")
    return output_path
