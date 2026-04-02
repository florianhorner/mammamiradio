"""FFmpeg-based helpers for shaping all audio into a consistent stream format."""

from __future__ import annotations

import logging
import math
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
    """Re-encode an input file to the station's target loudness and format."""
    sample_rate = str(config.audio.sample_rate) if config else "48000"
    channels = str(config.audio.channels) if config else "2"
    bitrate = f"{config.audio.bitrate}k" if config else "192k"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-ar",
        sample_rate,
        "-ac",
        channels,
        "-b:a",
        bitrate,
        "-filter:a",
        "loudnorm=I=-16:LRA=11:TP=-1.5",
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"normalize {input_path.name}")
    logger.info("Normalized: %s -> %s", input_path.name, output_path.name)
    return output_path


def concat_files(paths: list[Path], output_path: Path, silence_ms: int = 300) -> Path:
    """Concatenate rendered parts into a single MP3 segment."""
    if len(paths) == 1:
        return paths[0]

    inputs = []
    filter_parts = []
    for i, p in enumerate(paths):
        inputs.extend(["-i", str(p)])
        filter_parts.append(f"[{i}:a]")

    filter_str = "".join(filter_parts) + f"concat=n={len(paths)}:v=0:a=1[out]"

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        filter_str,
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"concat {len(paths)} files")
    logger.info("Concatenated %d files -> %s", len(paths), output_path.name)
    return output_path


def generate_tone(output_path: Path, freq_hz: float = 880, duration_sec: float = 0.5) -> Path:
    """Generate a sine tone with fade-in/out envelope (chime/ding sound)."""
    fade = min(0.15, duration_sec / 3)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={freq_hz}:duration={duration_sec}",
        "-af",
        f"afade=t=in:d={fade},afade=t=out:st={duration_sec - fade}:d={fade}",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"tone {freq_hz}Hz")
    return output_path


def generate_sweep(output_path: Path, start_hz: float = 200, end_hz: float = 2000, duration_sec: float = 0.8) -> Path:
    """Generate a frequency sweep (radio transition whoosh)."""
    if start_hz <= 0 or end_hz <= 0:
        raise ValueError("Sweep frequencies must be positive")
    if duration_sec <= 0:
        raise ValueError("Sweep duration must be positive")
    if math.isclose(start_hz, end_hz):
        return generate_tone(output_path, freq_hz=start_hz, duration_sec=duration_sec)

    fade = min(0.1, duration_sec / 4)
    fade_str = f"{fade:g}"
    fade_out_start = f"{max(duration_sec - fade, 0):g}"
    ratio = end_hz / start_hz
    start_hz_str = format(start_hz, ".12g")
    duration_str = format(duration_sec, ".12g")
    ratio_str = format(ratio, ".12g")
    chirp_expr = f"0.2*sin(2*PI*{start_hz_str}*{duration_str}/log({ratio_str})*(({ratio_str})^(t/{duration_str})-1))"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={chirp_expr}|{chirp_expr}:d={duration_str}:s=48000:c=stereo",
        "-af",
        f"afade=t=in:d={fade_str},afade=t=out:st={fade_out_start}:d={fade_str}",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output_path),
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


def generate_music_bed(output_path: Path, mood: str, duration_sec: float) -> Path:
    """Generate a synthetic music bed for an ad based on mood.

    Uses ffmpeg lavfi filters to create simple ambient beds:
    - dramatic: low rumbling drone with slow LFO
    - lounge: warm mid-frequency hum with gentle modulation
    - upbeat: bright rhythmic pulse
    - mysterious: dark filtered noise with reverb feel
    - epic: layered low+high drones
    """
    fade_out = min(1.5, duration_sec / 3)

    mood_configs = {
        "dramatic": (f"sine=frequency=80:duration={duration_sec},sine=frequency=120:duration={duration_sec}"),
        "lounge": (f"sine=frequency=220:duration={duration_sec},sine=frequency=330:duration={duration_sec}"),
        "upbeat": (f"sine=frequency=440:duration={duration_sec},sine=frequency=660:duration={duration_sec}"),
        "mysterious": (f"sine=frequency=100:duration={duration_sec},sine=frequency=150:duration={duration_sec}"),
        "epic": (f"sine=frequency=60:duration={duration_sec},sine=frequency=880:duration={duration_sec}"),
    }

    lavfi = mood_configs.get(mood, mood_configs["lounge"])
    # Two sine sources mixed together with tremolo and fade
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi.split(",")[0],
        "-f",
        "lavfi",
        "-i",
        lavfi.split(",")[1],
        "-filter_complex",
        f"[0:a][1:a]amix=inputs=2:duration=first[mix];"
        f"[mix]tremolo=f=2:d=0.3,"
        f"afade=t=in:d=0.5,afade=t=out:st={duration_sec - fade_out}:d={fade_out},"
        f"volume=0.15[out]",
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"music bed ({mood})")
    logger.info("Generated music bed: %s (%s, %.1fs)", output_path.name, mood, duration_sec)
    return output_path


def mix_with_bed(voice_path: Path, bed_path: Path, output_path: Path) -> Path:
    """Layer a music bed under voice audio. Bed at -18dB relative to voice."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(voice_path),
        "-i",
        str(bed_path),
        "-filter_complex",
        "[1:a]volume=0.12[bed];[0:a][bed]amix=inputs=2:duration=first:dropout_transition=2[out]",
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, "mix voice+bed")
    logger.info("Mixed voice + bed -> %s", output_path.name)
    return output_path


def generate_bumper_jingle(output_path: Path, duration_sec: float = 1.2) -> Path:
    """Generate a short radio bumper jingle (ascending chime pattern)."""
    fade = min(0.1, duration_sec / 4)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=523:duration={duration_sec * 0.3}",  # C5
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=659:duration={duration_sec * 0.3}",  # E5
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=784:duration={duration_sec * 0.4}",  # G5
        "-filter_complex",
        f"[0:a]adelay=0|0[a];[1:a]adelay={int(duration_sec * 300)}|{int(duration_sec * 300)}[b];"
        f"[2:a]adelay={int(duration_sec * 600)}|{int(duration_sec * 600)}[c];"
        f"[a][b][c]amix=inputs=3:duration=longest,"
        f"afade=t=in:d={fade},afade=t=out:st={duration_sec - fade}:d={fade}[out]",
        "-map",
        "[out]",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-t",
        str(duration_sec),
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, "bumper jingle")
    logger.info("Generated bumper jingle: %s", output_path.name)
    return output_path


def generate_silence(output_path: Path, duration_sec: float = 3.0) -> Path:
    """Generate silent audio used for pauses and error recovery."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-t",
        str(duration_sec),
        "-b:a",
        "192k",
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, "generate silence")
    return output_path
