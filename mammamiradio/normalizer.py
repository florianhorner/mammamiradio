"""FFmpeg-based helpers for shaping all audio into a consistent stream format."""

from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def measure_lufs(input_path: Path) -> float | None:
    """Fast integrated loudness measurement via ebur128 (~2-5s on Pi, no upsample).

    Returns integrated LUFS value, or None if measurement fails.
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # ebur128 summary prints "I:  -16.2 LUFS" in stderr
    match = re.search(r"I:\s+(-?\d+\.\d+)\s+LUFS", result.stderr or "")
    if match:
        return float(match.group(1))
    return None


# Shared FFmpeg output arguments for consistent MP3 encoding across all generators.
_MP3_OUTPUT_ARGS: list[str] = ["-ar", "48000", "-ac", "2", "-b:a", "192k", "-f", "mp3"]

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


def _fmt_num(value: float) -> str:
    """Format floats for FFmpeg expressions with bounded precision."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _gate_after(onset_sec: float) -> str:
    """Return a lavfi-safe gate expression that is 1 after onset, else 0."""
    onset = max(0.0, onset_sec)
    return f"if(gte(t\\,{_fmt_num(onset)})\\,1\\,0)"


def normalize(
    input_path: Path,
    output_path: Path,
    config=None,
    *,
    loudnorm: bool = True,
    music_eq: bool = False,
) -> Path:
    """Re-encode an input file to the station's target loudness and format.

    Set loudnorm=False for intermediate files that will receive a final loudnorm
    pass later (e.g. individual TTS lines before dialogue assembly). This runs
    ~3x faster on constrained hardware — just re-encodes to station format without
    the EBU R128 analysis pass. Silence trimming is still applied.

    Set music_eq=True for yt-dlp music tracks to apply a gentle broadcast EQ
    before the loudness pass: removes subsonic rumble, de-muds compressed audio,
    adds presence, and rolls off HF harshness from lossy re-encoding.
    """
    sample_rate = str(config.audio.sample_rate) if config else "48000"
    channels = str(config.audio.channels) if config else "2"
    bitrate = f"{config.audio.bitrate}k" if config else "192k"

    # Broadcast EQ for music: tonal consistency across yt-dlp rips.
    # Applied *before* loudnorm so the level measurement sees the equalized signal.
    # Kept gentle — this is correction, not creative processing.
    _MUSIC_EQ = (
        "highpass=f=35,"                          # sub-bass rumble from video codec leakage
        "equalizer=f=200:t=o:w=150:g=-2,"         # de-mud (compressed video audio)
        "equalizer=f=3000:t=o:w=1000:g=1.5,"      # presence / clarity
        "equalizer=f=12000:t=o:w=4000:g=-1.5,"    # tame HF harshness from MP3 re-encoding
        "acompressor=threshold=0.25:ratio=2:attack=20:release=250:makeup=1"  # gentle radio leveller
    )

    # Skip the expensive loudnorm pass if the file is already within ±1.5 LU of -16 LUFS,
    # but still re-encode to the station format (sample rate, channels, bitrate) and trim
    # trailing silence. A bare shutil.copy2 would leave the file at whatever sample rate
    # the source was (e.g. 44.1 kHz from yt-dlp), which can cause audio glitches at stream
    # boundaries. measure_lufs takes ~2-5s on Pi vs 10-75s for a full loudnorm pass.
    # When music_eq is requested, we always re-encode (EQ requires a pass regardless).
    if loudnorm and not music_eq:
        lufs = measure_lufs(input_path)
        if lufs is not None and abs(lufs - (-16.0)) <= 1.5:
            loudnorm = False  # fall through to the fast format-conversion path below
            logger.info("LUFS skip (%.1f LUFS, within tolerance): %s", lufs, input_path.name)

    if loudnorm:
        if config is not None and getattr(config, "is_addon", False) is True:
            norm_part = "dynaudnorm=f=150:g=13,alimiter=limit=0.95"
        else:
            norm_part = "loudnorm=I=-16:LRA=11:TP=-1.5"
        silence_trim = "silenceremove=start_periods=0:stop_periods=1:stop_threshold=-50dB:stop_duration=0.3"
        if music_eq:
            audio_filter = f"{_MUSIC_EQ},{norm_part},{silence_trim}"
        else:
            audio_filter = f"{norm_part},{silence_trim}"
    else:
        audio_filter = "silenceremove=start_periods=0:stop_periods=1:stop_threshold=-50dB:stop_duration=0.3"

    cmd = [
        "ffmpeg",
        "-y",
        "-threads",
        "1",
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
        audio_filter,
        "-f",
        "mp3",
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"normalize {input_path.name}")
    logger.info("Normalized%s: %s -> %s", "" if loudnorm else " (fast)", input_path.name, output_path.name)
    return output_path


def concat_files(
    paths: list[Path],
    output_path: Path,
    silence_ms: int = 300,
    loudnorm: bool = True,
) -> Path:
    """Concatenate rendered parts into a single MP3 segment.

    When silence_ms > 0, short silence gaps are inserted between each part
    using the FFmpeg anullsrc filter for a more produced feel.

    Set loudnorm=False when all inputs are already normalized to skip the
    expensive EBU R128 loudness pass (~1-3s saved per concat).
    """
    if len(paths) == 0:
        raise ValueError("concat_files: paths list must not be empty")
    if len(paths) == 1:
        return paths[0]

    inputs = []
    filter_parts = []
    silence_dur = silence_ms / 1000.0

    if silence_ms > 0 and len(paths) > 1:
        stream_idx = 0
        for i, p in enumerate(paths):
            inputs.extend(["-i", str(p)])
            filter_parts.append(f"[{stream_idx}:a]")
            stream_idx += 1
            if i < len(paths) - 1:
                inputs.extend(["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo,atrim=duration={silence_dur}"])
                filter_parts.append(f"[{stream_idx}:a]")
                stream_idx += 1
        total_streams = len(paths) + len(paths) - 1
    else:
        for i, p in enumerate(paths):
            inputs.extend(["-i", str(p)])
            filter_parts.append(f"[{i}:a]")
        total_streams = len(paths)

    norm_filter = ",loudnorm=I=-16:LRA=11:TP=-1.5" if loudnorm else ""
    filter_str = "".join(filter_parts) + f"concat=n={total_streams}:v=0:a=1{norm_filter}[out]"

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
        f"afade=t=in:d={fade},afade=t=out:st={duration_sec - fade}:d={fade},volume=0.35",
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


def _generate_cash_register(output_path: Path, duration_sec: float = 0.35) -> Path:
    """Layered cash register: bell strike + mechanical clatter + noise burst.

    Tones combined into single aevalsrc with exponential decay envelopes.
    Noise stays as separate input (can't be expressed in aevalsrc).
    """
    d = duration_sec
    # Bell tones with exponential decay envelopes in one expression:
    # 1200Hz bell (fast decay) + 1507Hz detuned bell (0.7x vol) + 2400Hz ring (0.4x, shorter)
    tones_expr = "1.0*sin(2*PI*1200*t)*exp(-8*t)+0.7*sin(2*PI*1507*t)*exp(-10*t)+0.4*sin(2*PI*2400*t)*exp(-20*t)"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={tones_expr}|{tones_expr}:d={d}:s=48000:c=stereo",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=d={d * 0.15}:c=pink:r=48000:a=0.3",
        "-filter_complex",
        f"[1:a]afade=t=in:d=0.001,afade=t=out:st=0.01:d={d * 0.15 - 0.01}[click];"
        f"[0:a][click]amix=inputs=2:duration=longest,"
        f"aecho=0.8:0.5:15|30:0.2|0.1,volume=2.0[out]",
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        "-t",
        str(d),
        str(output_path),
    ]
    _run_ffmpeg(cmd, "cash register SFX")
    return output_path


def _generate_whoosh(output_path: Path, duration_sec: float = 0.6) -> Path:
    """Filtered pink noise whoosh — bandpass sweeps up for a rush of air."""
    d = duration_sec
    fade = min(0.08, d / 4)
    # Pink noise through a rising bandpass filter gives a natural whoosh
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=d={d}:c=pink:r=48000:a=0.5",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=d={d}:c=white:r=48000:a=0.15",
        "-filter_complex",
        # Pink noise with rising bandpass (low → high sweep feel)
        f"[0:a]highpass=f=200:t=q:w=0.7,lowpass=f=4000:t=q:w=0.5,"
        f"afade=t=in:d={fade},afade=t=out:st={d - fade * 2}:d={fade * 2}[pink];"
        # Gentle white noise layer for airiness
        f"[1:a]highpass=f=2000,lowpass=f=8000,"
        f"afade=t=in:d={d * 0.3},afade=t=out:st={d * 0.5}:d={d * 0.4}[air];"
        f"[pink][air]amix=inputs=2:duration=first,volume=1.5[out]",
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, "whoosh SFX")
    return output_path


def _generate_mandolin_sting(output_path: Path, duration_sec: float = 0.5) -> Path:
    """Plucked-string sting: fast attack, exponential decay with harmonics.

    All 3 arpeggio notes (E4, A4, C#5) with octave harmonics combined into
    a single aevalsrc. Staggered onsets via time-shifted decay envelopes.
    6 inputs → 1.
    """
    d = duration_sec
    # Each note: fundamental + octave harmonic, plucked envelope (exp decay),
    # staggered onset at 0ms, 80ms, 160ms
    # Note 1: E4(330) + E5(660), onset=0
    # Note 2: A4(440) + A5(880), onset=0.08
    # Note 3: C#5(554) + C#6(1108), onset=0.16
    expr = (
        # Note 1 (E4+E5) — immediate onset
        "1.0*sin(2*PI*330*t)*exp(-12*t)"
        "+0.5*sin(2*PI*660*t)*exp(-12*t)"
        # Note 2 (A4+A5) — onset at 0.08s
        f"+1.0*sin(2*PI*440*t)*exp(-12*(t-0.08))*{_gate_after(0.08)}"
        f"+0.5*sin(2*PI*880*t)*exp(-12*(t-0.08))*{_gate_after(0.08)}"
        # Note 3 (C#5+C#6) — onset at 0.16s
        f"+1.0*sin(2*PI*554*t)*exp(-15*(t-0.16))*{_gate_after(0.16)}"
        f"+0.5*sin(2*PI*1108*t)*exp(-15*(t-0.16))*{_gate_after(0.16)}"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={expr}|{expr}:d={d}:s=48000:c=stereo",
        "-af",
        "aecho=0.6:0.4:20:0.15,volume=2.5",
        *_MP3_OUTPUT_ARGS,
        "-t",
        str(d),
        str(output_path),
    ]
    _run_ffmpeg(cmd, "mandolin sting SFX")
    return output_path


def _generate_ice_clink(output_path: Path, duration_sec: float = 0.3) -> Path:
    """Ice clink: layered high-frequency tones with fast decay + noise transient.

    Tones combined into single aevalsrc with exponential decay. 4 inputs → 2.
    """
    d = duration_sec
    # Three glass-like tones with fast exponential decay, single expression
    tones_expr = "1.0*sin(2*PI*2400*t)*exp(-15*t)+0.6*sin(2*PI*3200*t)*exp(-20*t)+0.3*sin(2*PI*4800*t)*exp(-25*t)"
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={tones_expr}|{tones_expr}:d={d}:s=48000:c=stereo",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=d={d * 0.08}:c=white:r=48000:a=0.2",
        "-filter_complex",
        f"[1:a]afade=t=in:d=0.001,afade=t=out:st=0.005:d={d * 0.08 - 0.005}[click];"
        f"[0:a][click]amix=inputs=2:duration=longest,"
        f"aecho=0.8:0.6:8|16:0.3|0.15,volume=2.5[out]",
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        "-t",
        str(d),
        str(output_path),
    ]
    _run_ffmpeg(cmd, "ice clink SFX")
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

    def _simple_fallback() -> Path:
        if sfx_type in ("sweep", "whoosh", "startup_synth"):
            return generate_sweep(output_path, start_hz=320, end_hz=1100, duration_sec=0.35)
        if sfx_type == "tape_stop":
            return generate_sweep(output_path, start_hz=1400, end_hz=120, duration_sec=0.3)
        if sfx_type in ("cash_register", "register_hit", "ice_clink", "mandolin_sting"):
            return generate_tone(output_path, freq_hz=1047, duration_sec=0.18)
        if sfx_type == "hotline_beep":
            return generate_tone(output_path, freq_hz=1336, duration_sec=0.18)
        return generate_tone(output_path, freq_hz=880, duration_sec=0.25)

    try:
        # Synthetic fallbacks — richer audio using layered lavfi filters
        if sfx_type in ("chime", "ding"):
            return generate_tone(output_path, freq_hz=880, duration_sec=0.4)
        if sfx_type in ("cash_register", "register_hit"):
            return _generate_cash_register(output_path)
        if sfx_type in ("sweep", "whoosh"):
            return _generate_whoosh(output_path)
        if sfx_type == "tape_stop":
            # Descending sweep — tape stopping effect
            return generate_sweep(output_path, start_hz=2000, end_hz=80, duration_sec=0.5)
        if sfx_type == "hotline_beep":
            # Short dual-tone DTMF-like beep
            return generate_tone(output_path, freq_hz=1336, duration_sec=0.2)
        if sfx_type == "mandolin_sting":
            return _generate_mandolin_sting(output_path)
        if sfx_type == "ice_clink":
            return _generate_ice_clink(output_path)
        if sfx_type == "startup_synth":
            # Ascending sweep with synth bloom character
            return generate_sweep(output_path, start_hz=200, end_hz=1200, duration_sec=0.6)
        logger.warning("Unknown SFX type '%s', using default chime", sfx_type)
        return generate_tone(output_path, freq_hz=880, duration_sec=0.4)
    except Exception as exc:
        logger.warning("Synthetic SFX '%s' failed, using simple fallback: %s", sfx_type, exc)
        return _simple_fallback()


def generate_music_bed(output_path: Path, mood: str, duration_sec: float) -> Path:
    """Generate a synthetic music bed for an ad based on mood.

    Each mood uses a distinct waveform texture (via harmonics) and a distinct
    effects chain so beds sound different from each other.  The heavy aecho
    that caused all beds to drone identically has been removed; moods now use
    aphaser, equalizer, or short room reverb to add space without beating.
    """
    fade_out = min(1.5, duration_sec / 3)
    d = duration_sec

    # ── helpers ────────────────────────────────────────────────────────────────
    def _piano(f: float) -> str:
        """Harmonic series that sounds like a soft struck string (no pure sine)."""
        return (
            f"sin(2*PI*{f}*t)"
            f"+0.7*sin(2*PI*{f*2}*t)"
            f"+0.25*sin(2*PI*{f*3}*t)"
            f"+0.1*sin(2*PI*{f*4}*t)"
        )

    def _hollow(f: float) -> str:
        """Odd harmonics only — clarinet / organ quality."""
        return (
            f"sin(2*PI*{f}*t)"
            f"+0.33*sin(2*PI*{f*3}*t)"
            f"+0.2*sin(2*PI*{f*5}*t)"
            f"+0.14*sin(2*PI*{f*7}*t)"
        )

    def _saw(f: float) -> str:
        """All harmonics — bright / buzzy sawtooth character."""
        return (
            f"sin(2*PI*{f}*t)"
            f"+0.5*sin(2*PI*{f*2}*t)"
            f"+0.33*sin(2*PI*{f*3}*t)"
            f"+0.25*sin(2*PI*{f*4}*t)"
            f"+0.2*sin(2*PI*{f*5}*t)"
        )

    def _pad_expr(root: float, third: float, fifth: float, wave_fn) -> str:
        """Chord built from three voices using the given waveform."""
        r = wave_fn(root)
        t = wave_fn(third)
        fi = wave_fn(fifth)
        return f"0.5*({r})+0.4*({t})+0.35*({fi})"

    def _fades(d: float, fo: float) -> str:
        return f"afade=t=in:d=0.6,afade=t=out:st={d - fo}:d={fo}"

    def _run(expr: str, af: str, label: str) -> Path:
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"aevalsrc={expr}|{expr}:d={d}:s=48000:c=stereo",
            "-af", af, *_MP3_OUTPUT_ARGS, str(output_path),
        ]
        _run_ffmpeg(cmd, f"music bed ({label})")
        logger.info("Generated music bed: %s (%s, %.1fs)", output_path.name, label, d)
        return output_path

    # ── suspicious_jazz: walking bass with piano harmonics, tiny room reverb ──
    if mood == "suspicious_jazz":
        root, third, fifth = 220.0, 277.0, 370.0
        flat7 = root * 1.8
        bass_expr = (
            f"0.35*sin(2*PI*{root*0.5}*t)*max(0,1-2*mod(t,2))"
            f"+0.35*sin(2*PI*{fifth*0.5}*t)*max(0,1-2*abs(mod(t,2)-0.5))"
            f"+0.35*sin(2*PI*{flat7*0.5}*t)*max(0,1-2*abs(mod(t,2)-1.0))"
            f"+0.35*sin(2*PI*{third*0.5}*t)*max(0,1-2*abs(mod(t,2)-1.5))"
        )
        pad = _pad_expr(root, third, fifth, _piano)
        expr = f"0.5*({pad})+0.7*({bass_expr})"
        af = (
            f"aecho=0.6:0.3:25|55:0.04|0.02,"  # tiny room reverb only
            f"{_fades(d, fade_out)},"
            f"volume=0.14"
        )
        return _run(expr, af, "suspicious_jazz")

    # ── discount_techno: sawtooth pulse with fast rhythmic tremolo ────────────
    if mood == "discount_techno":
        root, third, fifth = 110.0, 165.0, 220.0
        expr = _pad_expr(root, third, fifth, _saw)
        af = (
            f"tremolo=f=8:d=0.55,"         # fast tremolo → rhythmic pulse, not drone
            f"highpass=f=180,"             # cut muddy bass
            f"equalizer=f=3000:t=o:w=800:g=3,"  # presence boost
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "discount_techno")

    # ── tarantella_pop: bright saw chord, phaser for movement ────────────────
    if mood == "tarantella_pop":
        root, third, fifth = 523.0, 659.0, 784.0
        expr = _pad_expr(root, third, fifth, _saw)
        af = (
            f"aphaser=in_gain=0.4:out_gain=0.74:delay=3.0:decay=0.4:speed=0.5:type=t,"
            f"highpass=f=250,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "tarantella_pop")

    # ── upbeat: bright piano chord, fast phaser ───────────────────────────────
    if mood == "upbeat":
        root, third, fifth = 330.0, 440.0, 523.0
        expr = _pad_expr(root, third, fifth, _piano)
        af = (
            f"aphaser=in_gain=0.4:out_gain=0.74:delay=2.0:decay=0.3:speed=0.8:type=t,"
            f"highpass=f=200,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "upbeat")

    # ── shopping_channel: bright hollow organ, quick tremolo ─────────────────
    if mood == "shopping_channel":
        root, third, fifth = 400.0, 600.0, 800.0
        expr = _pad_expr(root, third, fifth, _hollow)
        af = (
            f"tremolo=f=5:d=0.3,"
            f"highpass=f=200,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "shopping_channel")

    # ── lounge: warm piano pad, gentle slow phaser ───────────────────────────
    if mood == "lounge":
        root, third, fifth = 220.0, 330.0, 440.0
        expr = _pad_expr(root, third, fifth, _piano)
        af = (
            f"aphaser=in_gain=0.4:out_gain=0.74:delay=4.0:decay=0.5:speed=0.25:type=t,"
            f"lowpass=f=1800,"
            f"{_fades(d, fade_out)},"
            f"volume=0.14"
        )
        return _run(expr, af, "lounge")

    # ── cheap_synth_romance: hollow mid-range, slow phaser ───────────────────
    if mood == "cheap_synth_romance":
        root, third, fifth = 293.0, 370.0, 440.0
        expr = _pad_expr(root, third, fifth, _hollow)
        af = (
            f"aphaser=in_gain=0.5:out_gain=0.74:delay=5.0:decay=0.6:speed=0.2:type=t,"
            f"lowpass=f=1400,"
            f"{_fades(d, fade_out)},"
            f"volume=0.14"
        )
        return _run(expr, af, "cheap_synth_romance")

    # ── luxury_spa: very soft hollow, minimal movement ───────────────────────
    if mood == "luxury_spa":
        root, third, fifth = 250.0, 315.0, 375.0
        expr = _pad_expr(root, third, fifth, _hollow)
        af = (
            f"aphaser=in_gain=0.3:out_gain=0.6:delay=6.0:decay=0.5:speed=0.15:type=t,"
            f"lowpass=f=900,"
            f"{_fades(d, fade_out)},"
            f"volume=0.12"
        )
        return _run(expr, af, "luxury_spa")

    # ── mysterious: low hollow chord, very slow phaser ───────────────────────
    if mood == "mysterious":
        root, third, fifth = 100.0, 126.0, 150.0
        expr = _pad_expr(root, third, fifth, _hollow)
        af = (
            f"aphaser=in_gain=0.4:out_gain=0.6:delay=8.0:decay=0.6:speed=0.1:type=t,"
            f"lowpass=f=700,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "mysterious")

    # ── dramatic: low piano swell, tremolo for gravitas ──────────────────────
    if mood == "dramatic":
        root, third, fifth = 80.0, 100.0, 120.0
        expr = _pad_expr(root, third, fifth, _piano)
        af = (
            f"tremolo=f=0.8:d=0.35,"
            f"lowpass=f=600,"
            f"{_fades(d, fade_out)},"
            f"volume=0.14"
        )
        return _run(expr, af, "dramatic")

    # ── epic / overblown_epic: very low, rich harmonics, slow swell ──────────
    if mood in ("epic", "overblown_epic"):
        root = 55.0 if mood == "overblown_epic" else 65.0
        third, fifth = root * 1.25, root * 1.5
        expr = _pad_expr(root, third, fifth, _saw)
        af = (
            f"tremolo=f=0.5:d=0.3,"
            f"lowpass=f=500,"
            f"{_fades(d, fade_out)},"
            f"volume=0.14"
        )
        return _run(expr, af, mood)

    # ── showroom: mid piano, slight phaser for polish ────────────────────────
    if mood == "showroom":
        root, third, fifth = 300.0, 375.0, 450.0
        expr = _pad_expr(root, third, fifth, _piano)
        af = (
            f"aphaser=in_gain=0.35:out_gain=0.7:delay=3.0:decay=0.4:speed=0.35:type=t,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "showroom")

    # ── stadium: low hollow chord, strong tremolo ─────────────────────────────
    if mood == "stadium":
        root, third, fifth = 100.0, 150.0, 200.0
        expr = _pad_expr(root, third, fifth, _hollow)
        af = (
            f"tremolo=f=1.2:d=0.4,"
            f"lowpass=f=800,"
            f"{_fades(d, fade_out)},"
            f"volume=0.14"
        )
        return _run(expr, af, "stadium")

    # ── motorway: low rumble + sawtooth texture ───────────────────────────────
    if mood == "motorway":
        root, third, fifth = 55.0, 82.0, 110.0
        expr = _pad_expr(root, third, fifth, _saw)
        af = (
            f"tremolo=f=1.5:d=0.2,"
            f"lowpass=f=400,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "motorway")

    # ── beach / cafe: light piano at mid frequencies ──────────────────────────
    if mood in ("beach", "cafe"):
        root = 174.0 if mood == "cafe" else 196.0
        third, fifth = root * 1.25, root * 1.5
        expr = _pad_expr(root, third, fifth, _piano)
        af = (
            f"aphaser=in_gain=0.35:out_gain=0.7:delay=3.5:decay=0.4:speed=0.3:type=t,"
            f"highpass=f=120,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, mood)

    # ── occult_basement: lowest hollow, barely moving ────────────────────────
    if mood == "occult_basement":
        root, third, fifth = 50.0, 63.0, 75.0
        expr = _pad_expr(root, third, fifth, _hollow)
        af = (
            f"tremolo=f=0.4:d=0.45,"
            f"lowpass=f=350,"
            f"{_fades(d, fade_out)},"
            f"volume=0.13"
        )
        return _run(expr, af, "occult_basement")

    # ── default fallback: warm lounge pad ────────────────────────────────────
    root, third, fifth = 220.0, 330.0, 440.0
    expr = _pad_expr(root, third, fifth, _piano)
    af = (
        f"aphaser=in_gain=0.4:out_gain=0.74:delay=4.0:decay=0.5:speed=0.25:type=t,"
        f"lowpass=f=1800,"
        f"{_fades(d, fade_out)},"
        f"volume=0.14"
    )
    return _run(expr, af, mood)


def generate_foley_loop(output_path: Path, environment: str, duration_sec: float) -> Path:
    """Generate a short ambient foley texture for the given ad environment.

    Unlike the tonal music bed, foley uses noise-based synthesis to create
    recognisable environmental textures: crowd chatter, road/engine noise,
    ocean wash, water trickle, etc.  Sits at very low volume under the voice
    and music bed so the spot feels alive without masking the copy.

    Returns *output_path* unchanged (even on failure) so callers can test
    ``output_path.exists()`` to decide whether to mix it in.
    """
    d = duration_sec
    fade_out = min(1.0, d / 4)
    fades = f"afade=t=in:d=0.5,afade=t=out:st={d - fade_out}:d={fade_out}"

    def _noise(color: str, af: str) -> None:
        """Run ffmpeg with an anoisesrc lavfi input."""
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anoisesrc=color={color}:r=48000:d={d}",
            "-af", af,
            *_MP3_OUTPUT_ARGS,
            str(output_path),
        ]
        _run_ffmpeg(cmd, f"foley loop ({environment})")

    def _expr(expr: str, af: str) -> None:
        """Run ffmpeg with an aevalsrc lavfi input (for harmonic textures)."""
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"aevalsrc={expr}|{expr}:d={d}:s=48000:c=stereo",
            "-af", af,
            *_MP3_OUTPUT_ARGS,
            str(output_path),
        ]
        _run_ffmpeg(cmd, f"foley loop ({environment})")

    try:
        if environment == "cafe":
            # Distant café: bandpass pink noise → room chatter texture.
            # Very slow tremolo → natural energy ebb in a busy room.
            _noise(
                "pink",
                f"bandpass=f=1100:w=900,tremolo=f=0.07:d=0.12,volume=0.055,{fades}",
            )

        elif environment == "motorway":
            # Engine harmonics (aevalsrc) + fast piston tremolo → car in motion.
            engine = (
                "0.28*sin(2*PI*82*t)"
                "+0.18*sin(2*PI*164*t)"
                "+0.10*sin(2*PI*246*t)"
                "+0.06*sin(2*PI*328*t)"
            )
            _expr(
                engine,
                f"tremolo=f=13:d=0.28,lowpass=f=380,volume=0.11,{fades}",
            )

        elif environment == "beach":
            # Ocean wash: pink noise lowpass + very slow tremolo (wave rhythm ~6 s/cycle).
            _noise(
                "pink",
                f"lowpass=f=650,highpass=f=55,tremolo=f=0.16:d=0.62,volume=0.08,{fades}",
            )

        elif environment == "stadium":
            # Crowd roar: pink noise bandpass + short aecho for stadium reverb.
            _noise(
                "pink",
                f"bandpass=f=750:w=650,aecho=0.7:0.35:30|65:0.07|0.03,volume=0.09,{fades}",
            )

        elif environment == "luxury_spa":
            # Water trickle: highpass pink noise + slow tremolo.
            _noise(
                "pink",
                f"highpass=f=1400,lowpass=f=6500,tremolo=f=0.32:d=0.48,volume=0.05,{fades}",
            )

        elif environment == "showroom":
            # Subtle room tone: very quiet broadband noise, mostly sub-perceptual.
            _noise(
                "pink",
                f"highpass=f=180,lowpass=f=3500,volume=0.04,{fades}",
            )

        elif environment == "shopping_channel":
            # Bright studio energy: white noise bandpass → phones/audience shimmer.
            _noise(
                "white",
                f"bandpass=f=2200:w=1600,volume=0.05,{fades}",
            )

        else:
            # Unknown environment — nothing to generate.
            return output_path

        logger.info("Generated foley loop: %s (%s, %.1fs)", output_path.name, environment, d)

    except Exception as exc:
        logger.warning("Foley loop generation failed (%s): %s — skipping", environment, exc)
        # Do not create the file; caller checks output_path.exists()

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
        f"[1:a]volume={volume_scale}[bed];[0:a][bed]amix=inputs=2:duration=first:dropout_transition=2,loudnorm=I=-16:LRA=11:TP=-1.5[out]",
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
    """Generate a short radio bumper jingle with pad, velocity variation, and reverb.

    Ascending C-E-G-C6 arpeggio over a sustained C-major pad, with echo/reverb
    tail and per-note velocity shaping. All tones in a single aevalsrc expression
    with time-shifted plucked envelopes (8 inputs → 1).
    """
    fade = min(0.1, duration_sec / 4)
    nd = duration_sec / 6  # note duration
    d = duration_sec

    # Melody notes with velocities and staggered onsets
    # Each note: vel * sin(freq*t) * exp(-decay*(t-onset)) * gate(t>=onset)
    # Notes: C5(523), E5(659), G5(784), C6(1047), G5(784), E5(659)
    melody_parts = [
        f"0.7*sin(2*PI*523*t)*exp(-{1.0 / nd * 3}*(t-0))*{_gate_after(0.0)}",
        f"0.85*sin(2*PI*659*t)*exp(-{1.0 / nd * 3}*(t-{nd}))*{_gate_after(nd)}",
        f"1.0*sin(2*PI*784*t)*exp(-{1.0 / nd * 3}*(t-{nd * 2}))*{_gate_after(nd * 2)}",
        f"1.0*sin(2*PI*1047*t)*exp(-{1.0 / nd * 3}*(t-{nd * 3}))*{_gate_after(nd * 3)}",
        f"0.8*sin(2*PI*784*t)*exp(-{1.0 / nd * 3}*(t-{nd * 4}))*{_gate_after(nd * 4)}",
        f"0.6*sin(2*PI*659*t)*exp(-{1.0 / (nd * 1.5) * 3}*(t-{nd * 5}))*{_gate_after(nd * 5)}",
    ]
    # Pad: sustained C3(131) + G3(196) with gentle tremolo approximated by AM
    pad_parts = [
        "0.15*sin(2*PI*131*t)*(1+0.2*sin(2*PI*3*t))",
        "0.12*sin(2*PI*196*t)*(1+0.15*sin(2*PI*2.5*t))",
    ]
    expr = "+".join(melody_parts + pad_parts)

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={expr}|{expr}:d={d}:s=48000:c=stereo",
        "-af",
        f"volume=0.45,aecho=0.8:0.7:30|60|90:0.25|0.15|0.08,afade=t=in:d={fade},afade=t=out:st={d - fade}:d={fade}",
        *_MP3_OUTPUT_ARGS,
        "-t",
        str(d),
        str(output_path),
    ]
    try:
        _run_ffmpeg(cmd, "bumper jingle")
    except subprocess.CalledProcessError:
        logger.warning("Bumper synthesis fallback: using simpler jingle after aevalsrc failure")
        fallback_cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=523:sample_rate=48000:duration={_fmt_num(d)}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=784:sample_rate=48000:duration={_fmt_num(d)}",
            "-filter_complex",
            (
                "[0:a]volume=0.8[a0];"
                "[1:a]volume=0.55,adelay=80|80[a1];"
                "[a0][a1]amix=inputs=2:duration=first,"
                f"aecho=0.8:0.6:35|70:0.20|0.10,"
                f"afade=t=in:d={_fmt_num(fade)},"
                f"afade=t=out:st={_fmt_num(d - fade)}:d={_fmt_num(fade)}"
            ),
            *_MP3_OUTPUT_ARGS,
            "-t",
            str(d),
            str(output_path),
        ]
        _run_ffmpeg(fallback_cmd, "bumper jingle fallback")
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


def crossfade_voice_over_music(
    music_path: Path,
    voice_path: Path,
    output_path: Path,
    tail_seconds: float = 8.0,
    voice_volume: float = 1.0,
    music_fade_volume: float = 0.5,
) -> Path:
    """Overlay voice on the tail of a music track, fading music down underneath.

    Takes the last `tail_seconds` of the music, fades it to `music_fade_volume`,
    and mixes the voice on top. The result is a "DJ talking over the outro" effect.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-sseof",
        f"-{tail_seconds}",
        "-i",
        str(music_path),
        "-i",
        str(voice_path),
        "-filter_complex",
        f"[0:a]afade=t=out:st=0:d={tail_seconds},volume={music_fade_volume}[music];"
        f"[1:a]volume={voice_volume},adelay=1500|1500[voice];"
        f"[music][voice]amix=inputs=2:duration=longest:dropout_transition=2,"
        f"loudnorm=I=-16:LRA=11:TP=-1.5[out]",
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, "crossfade voice over music")
    logger.info("Crossfade voice over music -> %s", output_path.name)
    return output_path


def generate_station_id_bed(
    output_path: Path,
    duration_sec: float = 3.0,
    motif_notes: list[int] | None = None,
) -> Path:
    """Generate a short musical sting for station ID: ascending chord + reverb tail.

    Uses the Rhodes-style motif notes from [sonic_brand].motif_notes in radio.toml.
    The last note sustains longer for a reverb tail effect.
    """
    notes = (motif_notes or [523, 659, 784, 1047])[:8]  # cap at 8 notes (ffmpeg label limit)
    note_dur = duration_sec / len(notes)
    fade = min(0.15, duration_sec / 5)

    # Build lavfi inputs — last note sustains longer
    inputs: list[str] = []
    for i, freq in enumerate(notes):
        dur = duration_sec * 0.6 if i == len(notes) - 1 else note_dur
        inputs.extend(["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}"])

    # Build filter: stagger each note with adelay, mix, add echo
    labels = "abcdefgh"[: len(notes)]
    filter_parts = []
    for i, label in enumerate(labels):
        delay_ms = int(note_dur * 400 * i)
        filter_parts.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[{label}]")

    mix_inputs = "".join(f"[{ch}]" for ch in labels)
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(notes)}:duration=longest,volume=0.4,"
        f"aecho=0.8:0.7:40|80:0.3|0.2,"
        f"afade=t=in:d={fade},afade=t=out:st={duration_sec - fade * 2}:d={fade * 2}[out]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        "-t",
        str(duration_sec),
        str(output_path),
    ]
    _run_ffmpeg(cmd, "station ID bed")
    logger.info("Generated station ID bed: %s", output_path.name)
    return output_path


def mix_voice_with_sting(
    voice_path: Path,
    sting_path: Path,
    output_path: Path,
) -> Path:
    """Mix a voice tag centered over a musical sting, with the sting quieter underneath."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(sting_path),
        "-i",
        str(voice_path),
        "-filter_complex",
        "[0:a]volume=0.15[bed];"
        "[1:a]adelay=400|400,volume=1.2[voice];"
        "[bed][voice]amix=inputs=2:duration=longest:dropout_transition=1,"
        "loudnorm=I=-16:LRA=11:TP=-1.5[out]",
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, "mix voice with sting")
    logger.info("Mixed voice + sting -> %s", output_path.name)
    return output_path


def normalize_ad(input_path: Path, output_path: Path) -> Path:
    """Broadcast-style processing for ad audio — loud, bright, and punchy.

    Chain: heavy compressor (fast attack squashes transients, low threshold
    catches everything) → presence boost at 3kHz for clarity → air boost at
    8kHz for sparkle → bass shelf cut to avoid muddiness under compression →
    aggressive loudnorm (I=-14, LRA=7 for minimal dynamic range, TP=-1.0 for
    maximum loudness before clipping).

    Compared to music normalize() (I=-16, LRA=11, TP=-1.5), ads are 2 LUFS
    louder, much narrower dynamic range, and brighter. This is the standard
    broadcast approach — ads should pop without being jarring.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-af",
        # Heavy broadcast compressor: low threshold, high ratio, fast attack
        "acompressor=threshold=-24dB:ratio=8:attack=3:release=40:makeup=6,"
        # Presence/clarity boost + air
        "treble=gain=4:frequency=3000,"
        "treble=gain=2:frequency=8000,"
        # Cut mud below 120Hz (ads don't need sub-bass)
        "highpass=f=120:t=q:w=0.7,"
        # EBU R128 loudness — louder and tighter than music
        "loudnorm=I=-14:LRA=7:TP=-1.0",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"normalize_ad {input_path.name}")
    logger.info("Ad broadcast processing: %s -> %s", input_path.name, output_path.name)
    return output_path


def mix_ad_with_bed(voiceover_path: Path, output_path: Path) -> Path:
    """Mix an ad voiceover with a gentle ambient bed so the spot isn't dry voice-only.

    The bed is a warm 220Hz+330Hz+440Hz sine chord with a slow 0.5Hz volume pulse,
    generated to exactly match the voiceover length, then mixed at -18dB under the
    voiceover. Output gets the same EBU R128 loudnorm pass as normalize_ad.

    NOTE: synthesize_ad() already applies a mood-based bed via generate_station_id_bed().
    Only call this function on raw voiceovers that bypassed synthesize_ad processing.
    """
    # Get voiceover duration so the aevalsrc bed is trimmed exactly.
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(voiceover_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        duration = float(result.stdout.strip()) if result.returncode == 0 else 30.0
    except ValueError:
        duration = 30.0

    # Warm sine bed: three harmonics with a slow 0.5Hz LFO breathing envelope.
    bed_expr = "0.03*sin(2*PI*220*t)+0.02*sin(2*PI*330*t)+0.01*sin(2*PI*440*t)"
    # Multiply by breathing envelope: oscillates between 0.6 and 1.0 at 0.5Hz
    bed_with_lfo = f"({bed_expr})*(0.8+0.2*sin(2*PI*0.5*t))"

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(voiceover_path),
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={bed_with_lfo}|{bed_with_lfo}:d={duration:.3f}:s=48000:c=stereo",
        "-filter_complex",
        # bed at -18dB (volume≈0.126), voiceover at unity, then loudnorm
        "[1:a]volume=0.126[bed];[0:a][bed]amix=inputs=2:duration=first[mixed];[mixed]loudnorm=I=-14:LRA=7:TP=-1.0[out]",
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, f"mix_ad_with_bed {voiceover_path.name}")
    logger.info("Ad bed mix: %s -> %s", voiceover_path.name, output_path.name)
    return output_path


def mix_quiet_bleed(
    base_path: Path,
    bleed_path: Path,
    output_path: Path,
    bleed_volume_db: float = -22.0,
    bleed_duration_sec: float = 4.0,
) -> Path:
    """Mix a very quiet snippet of *bleed_path* under the start of *base_path*.

    Creates the "studio mic left on" atmosphere — faint Italian voices under
    a music segment.  The bleed is faded in/out so it never pops.
    """
    fade = min(0.8, bleed_duration_sec / 3)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(base_path),
        "-i",
        str(bleed_path),
        "-filter_complex",
        (
            f"[1:a]atrim=0:{bleed_duration_sec},"
            f"volume={bleed_volume_db}dB,"
            f"afade=t=in:d={fade},afade=t=out:st={bleed_duration_sec - fade}:d={fade}[bleed];"
            "[0:a][bleed]amix=inputs=2:duration=first:dropout_transition=0,"
            "loudnorm=I=-16:LRA=11:TP=-1.5[out]"
        ),
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, "studio bleed mix")
    return output_path


def mix_oneshot_sfx(
    base_path: Path,
    sfx_path: Path,
    output_path: Path,
    offset_sec: float = 0.0,
    sfx_volume_db: float = -18.0,
) -> Path:
    """Mix a short one-shot SFX into *base_path* at *offset_sec*."""
    delay_ms = int(offset_sec * 1000)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(base_path),
        "-i",
        str(sfx_path),
        "-filter_complex",
        (
            f"[1:a]volume={sfx_volume_db}dB,adelay={delay_ms}|{delay_ms}[sfx];"
            "[0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0,"
            "loudnorm=I=-16:LRA=11:TP=-1.5[out]"
        ),
        "-map",
        "[out]",
        *_MP3_OUTPUT_ARGS,
        str(output_path),
    ]
    _run_ffmpeg(cmd, "oneshot SFX mix")
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
