"""FFmpeg-based helpers for shaping all audio into a consistent stream format."""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Canonical list of supported SFX types (synthetic fallbacks).
# Pre-recorded files in sfx_dir can extend this, but this list is what the
# LLM prompt advertises as available.
AVAILABLE_SFX_TYPES: list[str] = [
    "chime",
    "ding",
    "cash_register",
    "register_hit",
    "sweep",
    "whoosh",
    "tape_stop",
    "hotline_beep",
    "mandolin_sting",
    "ice_clink",
    "startup_synth",
]


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
    """Concatenate rendered parts into a single MP3 segment.

    When silence_ms > 0, short silence gaps are inserted between each part
    using the FFmpeg anullsrc filter for a more produced feel.
    """
    if len(paths) == 1:
        return paths[0]

    inputs = []
    filter_parts = []
    silence_dur = silence_ms / 1000.0

    if silence_ms > 0 and len(paths) > 1:
        # Interleave silence segments between audio parts
        stream_idx = 0
        for i, p in enumerate(paths):
            inputs.extend(["-i", str(p)])
            filter_parts.append(f"[{stream_idx}:a]")
            stream_idx += 1
            if i < len(paths) - 1:
                # Generate inline silence via lavfi
                inputs.extend(["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo,atrim=duration={silence_dur}"])
                filter_parts.append(f"[{stream_idx}:a]")
                stream_idx += 1
        total_streams = len(paths) + len(paths) - 1
    else:
        for i, p in enumerate(paths):
            inputs.extend(["-i", str(p)])
            filter_parts.append(f"[{i}:a]")
        total_streams = len(paths)

    filter_str = "".join(filter_parts) + f"concat=n={total_streams}:v=0:a=1[out]"

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
    elif sfx_type in ("cash_register", "register_hit"):
        return generate_tone(output_path, freq_hz=1200, duration_sec=0.3)
    elif sfx_type in ("sweep", "whoosh"):
        return generate_sweep(output_path, start_hz=300, end_hz=3000, duration_sec=0.6)
    elif sfx_type == "tape_stop":
        # Descending sweep — tape stopping effect
        return generate_sweep(output_path, start_hz=2000, end_hz=80, duration_sec=0.5)
    elif sfx_type == "hotline_beep":
        # Short dual-tone DTMF-like beep
        return generate_tone(output_path, freq_hz=1336, duration_sec=0.2)
    elif sfx_type == "mandolin_sting":
        # Rapid ascending 3-note arpeggio (plucked character via short envelope)
        return generate_sweep(output_path, start_hz=330, end_hz=880, duration_sec=0.4)
    elif sfx_type == "ice_clink":
        # High-frequency short tone cluster
        return generate_tone(output_path, freq_hz=2400, duration_sec=0.25)
    elif sfx_type == "startup_synth":
        # Ascending sweep with synth bloom character
        return generate_sweep(output_path, start_hz=200, end_hz=1200, duration_sec=0.6)
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
        "tarantella_pop": (f"sine=frequency=523:duration={duration_sec},sine=frequency=659:duration={duration_sec}"),
        "cheap_synth_romance": (
            f"sine=frequency=300:duration={duration_sec},sine=frequency=400:duration={duration_sec}"
        ),
        "overblown_epic": (f"sine=frequency=55:duration={duration_sec},sine=frequency=110:duration={duration_sec}"),
        "suspicious_jazz": (f"sine=frequency=220:duration={duration_sec},sine=frequency=277:duration={duration_sec}"),
        "discount_techno": (f"sine=frequency=440:duration={duration_sec},sine=frequency=880:duration={duration_sec}"),
        "cafe": (f"sine=frequency=180:duration={duration_sec},sine=frequency=260:duration={duration_sec}"),
        "motorway": (f"sine=frequency=60:duration={duration_sec},sine=frequency=90:duration={duration_sec}"),
        "beach": (f"sine=frequency=140:duration={duration_sec},sine=frequency=200:duration={duration_sec}"),
        "showroom": (f"sine=frequency=300:duration={duration_sec},sine=frequency=450:duration={duration_sec}"),
        "stadium": (f"sine=frequency=100:duration={duration_sec},sine=frequency=200:duration={duration_sec}"),
        "luxury_spa": (f"sine=frequency=250:duration={duration_sec},sine=frequency=375:duration={duration_sec}"),
        "occult_basement": (f"sine=frequency=50:duration={duration_sec},sine=frequency=75:duration={duration_sec}"),
        "shopping_channel": (f"sine=frequency=400:duration={duration_sec},sine=frequency=600:duration={duration_sec}"),
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


def mix_with_bed(voice_path: Path, bed_path: Path, output_path: Path, volume_scale: float = 0.12) -> Path:
    """Layer a music bed under voice audio. Default bed at -18dB relative to voice."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(voice_path),
        "-i",
        str(bed_path),
        "-filter_complex",
        f"[1:a]volume={volume_scale}[bed];[0:a][bed]amix=inputs=2:duration=first:dropout_transition=2[out]",
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


def generate_bumper_jingle(output_path: Path, duration_sec: float = 1.5) -> Path:
    """Generate a short radio bumper jingle (ascending + descending chime pattern)."""
    fade = min(0.1, duration_sec / 4)
    note_dur = duration_sec / 6
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=523:duration={note_dur}",  # C5 up
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=659:duration={note_dur}",  # E5 up
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=784:duration={note_dur}",  # G5 peak
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=1047:duration={note_dur}",  # C6 peak
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=784:duration={note_dur}",  # G5 down
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=659:duration={note_dur * 1.2}",  # E5 resolve
        "-filter_complex",
        f"[0:a]adelay=0|0[a];[1:a]adelay={int(note_dur * 1000)}|{int(note_dur * 1000)}[b];"
        f"[2:a]adelay={int(note_dur * 2000)}|{int(note_dur * 2000)}[c];"
        f"[3:a]adelay={int(note_dur * 3000)}|{int(note_dur * 3000)}[d];"
        f"[4:a]adelay={int(note_dur * 4000)}|{int(note_dur * 4000)}[e];"
        f"[5:a]adelay={int(note_dur * 5000)}|{int(note_dur * 5000)}[f];"
        f"[a][b][c][d][e][f]amix=inputs=6:duration=longest,volume=1.8,"
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


def generate_brand_motif(output_path: Path, sonic_signature: str, sfx_dir: Path | None = None) -> Path:
    """Parse a sonic_signature like 'ice_clink+startup_synth' into a short brand jingle.

    Each component is generated as an SFX capped at 0.5s, concatenated into a
    motif capped at 2.0s total. Prepend-only (before the ad voice).
    """
    components = [c.strip() for c in sonic_signature.split("+") if c.strip()]
    if not components:
        raise ValueError("Empty sonic_signature")

    tmp = Path(tempfile.mkdtemp())
    parts: list[Path] = []
    try:
        total_dur = 0.0
        for comp in components:
            if total_dur >= 2.0:
                break
            part_path = tmp / f"motif_{comp}_{len(parts)}.mp3"
            generate_sfx(part_path, comp, sfx_dir)
            parts.append(part_path)
            total_dur += 0.5

        if len(parts) == 1:
            shutil.move(str(parts[0]), str(output_path))
        else:
            concat_files(parts, output_path, silence_ms=100)
            for p in parts:
                p.unlink(missing_ok=True)

        logger.info("Generated brand motif: %s (%d components)", output_path.name, len(parts))
        return output_path
    except Exception:
        for p in parts:
            p.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
