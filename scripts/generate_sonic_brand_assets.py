#!/usr/bin/env python3
"""Render the bundled Mamma Mi Radio Italian-night-drive sonic asset pack.

Every asset is synthesized from source-defined FFmpeg filters. No sound
libraries, recorded samples, network access, or proprietary generators are
used. Re-running this script recreates the pack and its accompanying review
manifest from the same checked-in recipe.

Usage:
    python3 scripts/generate_sonic_brand_assets.py
    python3 scripts/generate_sonic_brand_assets.py --validate-only
    python3 scripts/generate_sonic_brand_assets.py --output-root /tmp/mmr-pack
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
SAMPLE_RATE_HZ = 48_000
CHANNELS = 2
BITRATE_KBPS = 192
LICENSE = "Apache-2.0"
PROVENANCE = (
    "Original deterministic FFmpeg procedural synthesis by "
    "scripts/generate_sonic_brand_assets.py; no downloaded or external samples."
)


@dataclass(frozen=True)
class AssetSpec:
    path: str
    purpose: str
    duration_target_sec: float


@dataclass(frozen=True)
class Layer:
    """One deterministic FFmpeg source plus its shaping filter chain."""

    source: str
    filters: str


ASSETS: tuple[AssetSpec, ...] = (
    AssetSpec("station_id.mp3", "Three-second master sonic logo for full station ident voice tags.", 3.0),
    AssetSpec("sweeper.mp3", "Short house sting beneath between-song station sweepers.", 2.0),
    AssetSpec("time_check.mp3", "Half-second warm clock punctuation before time checks.", 0.5),
    AssetSpec("bumpers/ad_break.mp3", "Compact break opener for fictional-ad handoffs.", 1.2),
    AssetSpec("stingers/music_to_speech.mp3", "Night-drive transition from music into speech.", 1.5),
    AssetSpec("stingers/speech_to_music.mp3", "Night-drive release from speech back into music.", 1.0),
    AssetSpec("beds/casa_notte.mp3", "Loopable non-drone Rhodes-and-mandolin bed for Casa-notte speech.", 16.0),
    AssetSpec("sfx/chime.mp3", "Warm two-note glass-and-Rhodes confirmation chime.", 0.62),
    AssetSpec("sfx/ding.mp3", "Small rounded brass bell punctuation.", 0.38),
    AssetSpec("sfx/cash_register.mp3", "Bright analogue till bell with a short mechanical tick.", 0.52),
    AssetSpec("sfx/register_hit.mp3", "Low wooden-metal till impact, distinct from the register bell.", 0.28),
    AssetSpec("sfx/sweep.mp3", "Clean ascending radio dial sweep.", 0.72),
    AssetSpec("sfx/whoosh.mp3", "Soft filtered air whoosh for motion and handoff.", 0.80),
    AssetSpec("sfx/tape_stop.mp3", "Short descending analogue tape-stop release.", 0.62),
    AssetSpec("sfx/hotline_beep.mp3", "Italian late-night hotline dual-tone beep.", 0.28),
    AssetSpec("sfx/mandolin_sting.mp3", "Plucked four-note Mediterranean mandolin-style sting.", 0.72),
    AssetSpec("sfx/ice_clink.mp3", "Small inharmonic aperitivo ice-glass clink.", 0.46),
    AssetSpec("sfx/startup_synth.mp3", "Ascending analogue synth bloom for faux-tech ads.", 0.85),
)


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _tone(
    frequency_hz: float,
    duration_sec: float,
    *,
    delay_ms: int = 0,
    volume: float = 0.16,
    left: float = 0.92,
    right: float = 0.78,
) -> Layer:
    """A short decaying pseudo-stereo pluck made from a sine partial."""
    release = min(0.12, duration_sec / 3)
    filters = [
        f"volume={_number(volume)}",
        "afade=t=in:d=0.004",
        f"afade=t=out:st={_number(max(duration_sec - release, 0))}:d={_number(release)}",
        f"pan=stereo|c0={_number(left)}*c0|c1={_number(right)}*c0",
    ]
    if delay_ms:
        filters.append(f"adelay={delay_ms}|{delay_ms}")
    return Layer(
        source=(
            f"sine=frequency={_number(frequency_hz)}:sample_rate={SAMPLE_RATE_HZ}:duration={_number(duration_sec)}"
        ),
        filters=",".join(filters),
    )


def _noise(
    color: str,
    duration_sec: float,
    *,
    seed: int,
    volume: float,
    highpass_hz: int = 40,
    lowpass_hz: int = 12_000,
    delay_ms: int = 0,
) -> Layer:
    """A seeded, deterministic grain texture for tape, air, click, or room character."""
    profiles = {
        "white": ((1_733.0, 2_381.0, 3_371.0, 4_643.0), (0.19, 0.14, 0.10, 0.07)),
        "pink": ((89.0, 233.0, 577.0, 1_327.0), (0.22, 0.16, 0.10, 0.06)),
        "brown": ((47.0, 73.0, 119.0, 191.0), (0.25, 0.17, 0.11, 0.07)),
    }
    frequencies, amplitudes = profiles.get(color, profiles["pink"])
    detune = ((seed % 13) - 6) * 0.7
    expression = "+".join(
        f"{_number(amplitudes[index])}*sin(2*PI*{_number(frequency + detune)}*t)"
        for index, frequency in enumerate(frequencies)
    )
    fade = min(0.08, duration_sec / 4)
    filters = [
        "aformat=channel_layouts=stereo",
        f"highpass=f={highpass_hz}",
        f"lowpass=f={lowpass_hz}",
        f"volume={_number(volume)}",
        f"afade=t=in:d={_number(min(0.01, fade))}",
        f"afade=t=out:st={_number(max(duration_sec - fade, 0))}:d={_number(fade)}",
    ]
    if delay_ms:
        filters.append(f"adelay={delay_ms}|{delay_ms}")
    return Layer(
        source=f"aevalsrc={expression}|{expression}:d={_number(duration_sec)}:s={SAMPLE_RATE_HZ}:c=stereo",
        filters=",".join(filters),
    )


def _chirp(
    start_hz: float,
    end_hz: float,
    duration_sec: float,
    *,
    volume: float,
    delay_ms: int = 0,
    highpass_hz: int = 40,
    lowpass_hz: int = 12_000,
) -> Layer:
    """A logarithmic frequency sweep, matching the station's native FFmpeg idiom."""
    ratio = end_hz / start_hz
    start = _number(start_hz)
    duration = _number(duration_sec)
    ratio_s = _number(ratio)
    expression = f"0.22*sin(2*PI*{start}*{duration}/log({ratio_s})*(({ratio_s})^(t/{duration})-1))"
    fade = min(0.08, duration_sec / 4)
    filters = [
        f"highpass=f={highpass_hz}",
        f"lowpass=f={lowpass_hz}",
        f"volume={_number(volume)}",
        f"afade=t=in:d={_number(fade)}",
        f"afade=t=out:st={_number(max(duration_sec - fade, 0))}:d={_number(fade)}",
    ]
    if delay_ms:
        filters.append(f"adelay={delay_ms}|{delay_ms}")
    return Layer(
        source=f"aevalsrc={expression}|{expression}:d={duration}:s={SAMPLE_RATE_HZ}:c=stereo",
        filters=",".join(filters),
    )


def _render_mix(
    output_path: Path,
    duration_sec: float,
    layers: list[Layer],
    master_filters: list[str],
    *,
    target_lufs: float = -16.0,
    seamless_loop: bool = False,
) -> None:
    """Render a shaped, bounded MP3 atomically from deterministic source layers.

    One-shot imaging cues get short boundary fades so they enter and leave the
    station cleanly. A source intended for raw runtime looping must opt out:
    fading its tail creates a measurable dip every time FFmpeg repeats it.
    """
    if not layers:
        raise ValueError(f"{output_path.name} needs at least one layer")

    # ``atrim`` caps a long mix but cannot extend a short one. A silent, fixed-
    # duration source keeps one-shot effects at their declared window so their
    # tails and downstream scheduling stay deterministic.
    layers = [
        *layers,
        Layer(
            source=f"anullsrc=r={SAMPLE_RATE_HZ}:cl=stereo:d={_number(duration_sec)}",
            filters="anull",
        ),
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp.mp3")
    temporary_path.unlink(missing_ok=True)

    # Keep one filter/encoder execution path. These assets are generated offline,
    # so a stable, inspectable render recipe matters more than parallel speed.
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-fflags",
        "+bitexact",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
    ]
    for layer in layers:
        command.extend(["-f", "lavfi", "-i", layer.source])

    filter_parts: list[str] = []
    labels: list[str] = []
    for index, layer in enumerate(layers):
        label = f"layer{index}"
        filter_parts.append(f"[{index}:a]{layer.filters}[{label}]")
        labels.append(f"[{label}]")

    filter_parts.append(
        f"{''.join(labels)}amix=inputs={len(layers)}:duration=longest:dropout_transition=0:normalize=0[mix]"
    )
    final_chain = [
        *master_filters,
        f"loudnorm=I={_number(target_lufs)}:LRA=7:TP=-1.5",
        f"atrim=0:{_number(duration_sec)}",
    ]
    if not seamless_loop:
        fade = min(0.10, duration_sec / 4)
        final_chain.extend(
            [
                f"afade=t=in:d={_number(min(0.012, fade))}",
                f"afade=t=out:st={_number(max(duration_sec - fade, 0))}:d={_number(fade)}",
            ]
        )
    final_chain.append(f"aformat=sample_rates={SAMPLE_RATE_HZ}:channel_layouts=stereo")
    filter_parts.append(f"[mix]{','.join(final_chain)}[out]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[out]",
            "-ar",
            str(SAMPLE_RATE_HZ),
            "-ac",
            str(CHANNELS),
            "-b:a",
            f"{BITRATE_KBPS}k",
            "-write_xing",
            "0",
            "-f",
            "mp3",
            str(temporary_path),
        ]
    )
    try:
        subprocess.run(command, check=True)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _warm_master(*, volume: float = 0.86, air_hz: int = 11_500) -> list[str]:
    return [
        "highpass=f=48",
        f"lowpass=f={air_hz}",
        f"volume={_number(volume)}",
    ]


def _render_station_id(path: Path) -> None:
    layers = [
        _tone(261.63, 0.52, volume=0.20, left=0.98, right=0.72),
        _tone(329.63, 0.48, delay_ms=310, volume=0.18, left=0.76, right=0.98),
        _tone(392.00, 0.46, delay_ms=620, volume=0.17, left=0.98, right=0.74),
        _tone(523.25, 0.82, delay_ms=930, volume=0.19, left=0.78, right=0.98),
        _tone(130.81, 2.90, volume=0.045, left=0.92, right=0.92),
        _tone(196.00, 2.90, volume=0.032, left=0.83, right=0.95),
        _noise("pink", 3.0, seed=101, volume=0.009, highpass_hz=190, lowpass_hz=4_700),
    ]
    _render_mix(path, 3.0, layers, _warm_master(volume=0.82, air_hz=10_800))


def _render_sweeper(path: Path) -> None:
    layers = [
        _tone(392.00, 0.34, volume=0.18, left=0.98, right=0.70),
        _tone(523.25, 0.38, delay_ms=240, volume=0.17, left=0.74, right=0.98),
        _tone(659.25, 0.62, delay_ms=500, volume=0.16, left=0.97, right=0.76),
        _tone(196.00, 1.92, volume=0.032, left=0.90, right=0.90),
        _noise("pink", 2.0, seed=102, volume=0.006, highpass_hz=220, lowpass_hz=4_500),
    ]
    _render_mix(path, 2.0, layers, _warm_master(volume=0.78, air_hz=10_200))


def _render_time_check(path: Path) -> None:
    layers = [
        _tone(1046.50, 0.24, volume=0.19, left=0.96, right=0.79),
        _tone(1567.98, 0.23, delay_ms=120, volume=0.12, left=0.77, right=0.98),
        _noise("pink", 0.10, seed=103, volume=0.012, highpass_hz=1_500, lowpass_hz=7_500, delay_ms=5),
    ]
    _render_mix(path, 0.5, layers, _warm_master(volume=0.74, air_hz=12_000))


def _render_ad_break(path: Path) -> None:
    layers = [
        _tone(164.81, 1.15, volume=0.065, left=0.92, right=0.92),
        _tone(329.63, 0.28, volume=0.18, left=0.98, right=0.74),
        _tone(440.00, 0.27, delay_ms=180, volume=0.16, left=0.75, right=0.98),
        _tone(659.25, 0.34, delay_ms=350, volume=0.17, left=0.98, right=0.74),
        _tone(783.99, 0.42, delay_ms=580, volume=0.13, left=0.77, right=0.98),
        _noise("brown", 0.32, seed=104, volume=0.022, highpass_hz=180, lowpass_hz=2_400),
    ]
    _render_mix(path, 1.2, layers, _warm_master(volume=0.88, air_hz=11_500))


def _render_music_to_speech(path: Path) -> None:
    layers = [
        _chirp(190.0, 1_120.0, 0.78, volume=0.20, highpass_hz=130, lowpass_hz=7_800),
        _noise("pink", 1.25, seed=105, volume=0.026, highpass_hz=550, lowpass_hz=7_200),
        _tone(523.25, 0.38, delay_ms=730, volume=0.14, left=0.76, right=0.98),
        _tone(659.25, 0.42, delay_ms=900, volume=0.13, left=0.98, right=0.72),
        _tone(130.81, 1.42, volume=0.032, left=0.90, right=0.90),
    ]
    _render_mix(path, 1.5, layers, _warm_master(volume=0.82, air_hz=10_000))


def _render_speech_to_music(path: Path) -> None:
    layers = [
        _chirp(940.0, 180.0, 0.48, volume=0.17, highpass_hz=75, lowpass_hz=6_400),
        _tone(261.63, 0.48, delay_ms=350, volume=0.16, left=0.98, right=0.74),
        _tone(392.00, 0.54, delay_ms=480, volume=0.14, left=0.75, right=0.98),
        _noise("brown", 0.62, seed=106, volume=0.017, highpass_hz=90, lowpass_hz=2_100),
    ]
    _render_mix(path, 1.0, layers, _warm_master(volume=0.80, air_hz=9_700))


def _render_casa_notte(path: Path) -> None:
    # Four fixed four-beat bars: audible motion, not a static drone. The
    # sustaining layers deliberately outlast the 16-second trimmed window, so
    # their releases cannot create a level dip at the raw loop boundary. Their
    # integer-cycle frequencies and zero-detune texture start/end at the same
    # phase across the window, keeping -stream_loop suitable for long renders.
    loop_duration_sec = 16.0
    sustain_duration_sec = loop_duration_sec + 0.2
    notes = (261.63, 329.63, 392.00, 523.25, 293.66, 369.99, 440.00, 587.33)
    layers: list[Layer] = [
        _tone(65.5, sustain_duration_sec, volume=0.020, left=0.92, right=0.92),
        _tone(98.0, sustain_duration_sec, volume=0.014, left=0.86, right=0.95),
        _noise("pink", sustain_duration_sec, seed=110, volume=0.0045, highpass_hz=220, lowpass_hz=4_200),
    ]
    for beat in range(16):
        frequency = notes[beat % len(notes)]
        accent = 0.105 if beat % 4 == 0 else 0.078
        layers.append(
            _tone(
                frequency,
                0.56,
                delay_ms=beat * 1_000,
                volume=accent,
                left=0.98 if beat % 2 == 0 else 0.76,
                right=0.76 if beat % 2 == 0 else 0.98,
            )
        )
    for bar in range(4):
        layers.append(_tone((130.81, 146.83, 164.81, 146.83)[bar], 1.25, delay_ms=bar * 4_000, volume=0.045))
    _render_mix(
        path,
        loop_duration_sec,
        layers,
        _warm_master(volume=0.64, air_hz=8_800),
        target_lufs=-24.0,
        seamless_loop=True,
    )


def _render_chime(path: Path) -> None:
    _render_mix(
        path,
        0.62,
        [
            _tone(1046.50, 0.42, volume=0.18, left=0.98, right=0.74),
            _tone(1567.98, 0.50, delay_ms=90, volume=0.11, left=0.76, right=0.98),
            _noise("pink", 0.08, seed=108, volume=0.009, highpass_hz=1_200, lowpass_hz=7_000),
        ],
        _warm_master(volume=0.74, air_hz=12_000),
    )


def _render_ding(path: Path) -> None:
    _render_mix(
        path,
        0.38,
        [
            _tone(1318.51, 0.30, volume=0.16, left=0.95, right=0.80),
            _tone(1760.00, 0.25, delay_ms=8, volume=0.060, left=0.80, right=0.96),
        ],
        _warm_master(volume=0.70, air_hz=12_500),
    )


def _render_cash_register(path: Path) -> None:
    _render_mix(
        path,
        0.52,
        [
            _noise("white", 0.045, seed=109, volume=0.045, highpass_hz=1_600, lowpass_hz=9_000),
            _tone(1280.00, 0.34, delay_ms=18, volume=0.16, left=0.98, right=0.72),
            _tone(1900.00, 0.28, delay_ms=34, volume=0.085, left=0.75, right=0.98),
            _tone(2400.00, 0.19, delay_ms=66, volume=0.045, left=0.98, right=0.77),
        ],
        _warm_master(volume=0.76, air_hz=13_500),
    )


def _render_register_hit(path: Path) -> None:
    _render_mix(
        path,
        0.28,
        [
            _tone(94.00, 0.060, volume=0.070, left=0.91, right=0.91),
            _tone(184.00, 0.18, delay_ms=8, volume=0.14, left=0.96, right=0.82),
            _tone(740.00, 0.12, delay_ms=15, volume=0.055, left=0.80, right=0.98),
            _tone(2_180.00, 0.050, delay_ms=4, volume=0.020, left=0.98, right=0.78),
        ],
        _warm_master(volume=0.68, air_hz=7_500),
    )


def _render_sweep(path: Path) -> None:
    _render_mix(
        path,
        0.72,
        [
            _chirp(190.0, 1_850.0, 0.66, volume=0.22, highpass_hz=110, lowpass_hz=9_200),
            _noise("pink", 0.60, seed=111, volume=0.011, highpass_hz=500, lowpass_hz=7_300),
        ],
        _warm_master(volume=0.72, air_hz=10_500),
    )


def _render_whoosh(path: Path) -> None:
    _render_mix(
        path,
        0.80,
        [
            _noise("pink", 0.74, seed=112, volume=0.055, highpass_hz=650, lowpass_hz=6_500),
            _chirp(300.0, 780.0, 0.60, volume=0.060, highpass_hz=240, lowpass_hz=2_200),
        ],
        ["highpass=f=180", "lowpass=f=8000", "volume=0.76"],
    )


def _render_tape_stop(path: Path) -> None:
    _render_mix(
        path,
        0.62,
        [
            _chirp(1_600.0, 78.0, 0.54, volume=0.20, highpass_hz=65, lowpass_hz=6_300),
            _noise("brown", 0.45, seed=113, volume=0.026, highpass_hz=70, lowpass_hz=1_500),
        ],
        ["highpass=f=55", "lowpass=f=6800", "volume=0.70"],
    )


def _render_hotline_beep(path: Path) -> None:
    _render_mix(
        path,
        0.28,
        [
            _tone(770.00, 0.18, delay_ms=20, volume=0.14, left=0.94, right=0.83),
            _tone(1336.00, 0.18, delay_ms=20, volume=0.13, left=0.83, right=0.96),
        ],
        ["highpass=f=350", "lowpass=f=3500", "volume=0.74"],
    )


def _render_mandolin_sting(path: Path) -> None:
    _render_mix(
        path,
        0.72,
        [
            _noise("white", 0.025, seed=114, volume=0.030, highpass_hz=1_800, lowpass_hz=9_000),
            _tone(659.25, 0.25, delay_ms=0, volume=0.16, left=0.98, right=0.72),
            _tone(783.99, 0.24, delay_ms=105, volume=0.15, left=0.76, right=0.98),
            _tone(987.77, 0.25, delay_ms=210, volume=0.13, left=0.98, right=0.74),
            _tone(1318.51, 0.38, delay_ms=315, volume=0.12, left=0.78, right=0.98),
        ],
        _warm_master(volume=0.74, air_hz=12_800),
    )


def _render_ice_clink(path: Path) -> None:
    _render_mix(
        path,
        0.46,
        [
            _noise("white", 0.018, seed=115, volume=0.040, highpass_hz=2_200, lowpass_hz=11_000),
            _tone(1497.00, 0.30, delay_ms=8, volume=0.10, left=0.98, right=0.76),
            _tone(2337.00, 0.25, delay_ms=17, volume=0.075, left=0.74, right=0.98),
            _tone(3371.00, 0.18, delay_ms=28, volume=0.042, left=0.98, right=0.80),
        ],
        _warm_master(volume=0.68, air_hz=14_000),
    )


def _render_startup_synth(path: Path) -> None:
    _render_mix(
        path,
        0.85,
        [
            _chirp(170.0, 1_240.0, 0.72, volume=0.16, highpass_hz=80, lowpass_hz=8_800),
            _tone(110.00, 0.74, volume=0.050, left=0.92, right=0.92),
            _tone(440.00, 0.26, delay_ms=430, volume=0.10, left=0.98, right=0.72),
            _tone(659.25, 0.32, delay_ms=520, volume=0.090, left=0.75, right=0.98),
        ],
        ["highpass=f=65", "lowpass=f=10000", "volume=0.76"],
    )


BUILDERS = {
    "station_id.mp3": _render_station_id,
    "sweeper.mp3": _render_sweeper,
    "time_check.mp3": _render_time_check,
    "bumpers/ad_break.mp3": _render_ad_break,
    "stingers/music_to_speech.mp3": _render_music_to_speech,
    "stingers/speech_to_music.mp3": _render_speech_to_music,
    "beds/casa_notte.mp3": _render_casa_notte,
    "sfx/chime.mp3": _render_chime,
    "sfx/ding.mp3": _render_ding,
    "sfx/cash_register.mp3": _render_cash_register,
    "sfx/register_hit.mp3": _render_register_hit,
    "sfx/sweep.mp3": _render_sweep,
    "sfx/whoosh.mp3": _render_whoosh,
    "sfx/tape_stop.mp3": _render_tape_stop,
    "sfx/hotline_beep.mp3": _render_hotline_beep,
    "sfx/mandolin_sting.mp3": _render_mandolin_sting,
    "sfx/ice_clink.mp3": _render_ice_clink,
    "sfx/startup_synth.mp3": _render_startup_synth,
}


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "pack": "Mamma Mi Radio — Italian Night Drive",
        "format": {
            "codec": "mp3",
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "channels": CHANNELS,
            "bitrate_kbps": BITRATE_KBPS,
        },
        "license": LICENSE,
        "provenance": PROVENANCE,
        "assets": [
            {
                **asdict(spec),
                "license": LICENSE,
                "provenance": PROVENANCE,
            }
            for spec in ASSETS
        ],
    }


def _probe(path: Path) -> tuple[str, int, int, int, float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bit_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    duration = float(payload["format"]["duration"])
    return (
        str(stream.get("codec_name", "")),
        int(stream.get("sample_rate", 0)),
        int(stream.get("channels", 0)),
        int(stream.get("bit_rate", 0)),
        duration,
    )


def _max_volume_db(path: Path) -> float:
    """Return the decoded peak level so a valid-but-inaudible asset cannot ship."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    matches = re.findall(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    if not matches:
        raise RuntimeError(f"{path.name}: volumedetect did not report a peak")
    return float(matches[-1])


def validate(output_root: Path) -> None:
    """Fail clearly if a pack member is missing or violates its delivery contract."""
    manifest_path = output_root / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_paths = {spec.path for spec in ASSETS}
    manifest_paths = {str(item.get("path", "")) for item in manifest.get("assets", [])}
    if manifest_paths != expected_paths:
        raise RuntimeError(f"manifest path set differs from pack: {sorted(manifest_paths ^ expected_paths)}")

    for spec in ASSETS:
        asset_path = output_root / spec.path
        if not asset_path.is_file():
            raise RuntimeError(f"missing asset: {asset_path}")
        codec, sample_rate, channels, bit_rate, duration = _probe(asset_path)
        tolerance = max(0.12, spec.duration_target_sec * 0.10)
        if codec != "mp3":
            raise RuntimeError(f"{spec.path}: expected MP3, got {codec or 'unknown'}")
        if sample_rate != SAMPLE_RATE_HZ or channels != CHANNELS:
            raise RuntimeError(
                f"{spec.path}: expected {SAMPLE_RATE_HZ}Hz stereo, got {sample_rate}Hz/{channels} channels"
            )
        if abs(bit_rate - BITRATE_KBPS * 1000) > 2_000:
            raise RuntimeError(f"{spec.path}: expected {BITRATE_KBPS}kbps, got {bit_rate / 1000:.1f}kbps")
        if abs(duration - spec.duration_target_sec) > tolerance:
            raise RuntimeError(
                f"{spec.path}: duration {duration:.3f}s misses target "
                f"{spec.duration_target_sec:.3f}s (±{tolerance:.3f}s)"
            )
        max_volume = _max_volume_db(asset_path)
        # A target-loudness render can have a lower peak on short, rounded cues;
        # -18 dBFS still gives the downstream station/ad masters enough signal to
        # work with, while rejecting a quietly broken render such as the old
        # -28 dBFS pack that would disappear below voice.
        min_peak = -20.0 if spec.path == "beds/casa_notte.mp3" else -18.0
        if not min_peak <= max_volume <= -0.5:
            raise RuntimeError(
                f"{spec.path}: peak {max_volume:.1f}dB is outside the audible-safe range [{min_peak:.1f}, -0.5]dB"
            )
        print(
            f"OK {spec.path}: {duration:.3f}s | {sample_rate}Hz | {channels}ch | "
            f"{bit_rate / 1000:.0f}kbps | peak {max_volume:.1f}dB"
        )


def generate(output_root: Path) -> None:
    for spec in ASSETS:
        BUILDERS[spec.path](output_root / spec.path)
        print(f"Rendered {spec.path}")
    (output_root / "manifest.json").write_text(
        json.dumps(_manifest(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {output_root / 'manifest.json'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Check an already-rendered pack without rewriting it.",
    )
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    if args.validate_only:
        validate(output_root)
    else:
        generate(output_root)
        validate(output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
