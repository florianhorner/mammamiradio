#!/usr/bin/env python3
"""Render historical motif and core-cadence audition boards.

This script does not build the installed imaging pack. The rejected Recorded
Night Drive source/rebuild path is retained only as archived lineage and cannot
be invoked. Modern Night Drive pack materialization belongs to
``complete_audio_pack_gate.py`` and ``promote_complete_audio_pack.py``; the
current approved identity is Neon Relay with Velvet Horizon's atmosphere.

Usage:
    python scripts/build_public_imaging_pack.py --prototype-motifs --source-dir /path/to/hq-derivatives
    python scripts/build_public_imaging_pack.py --core-audition --selected-motif warm_resolve \
        --selected-motif-artifact /path/to/listened/warm_resolve.mp3 \
        --source-dir /path/to/hq-derivatives --output-root /tmp/core-audition

``--prototype-motifs`` is a deliberately separate, non-release path. It accepts
three known Freesound HQ preview derivatives only to create the human selection
board. Those derivatives can never satisfy the final public-pack build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mammamiradio.audio.normalizer import (
    _reconcile_lufs,
    concat_files,
    configure_loudness_reconcile,
    crossfade_voice_over_music,
    fit_audio_oneshot,
    mix_voice_with_sting,
    normalize_ad,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOTYPE_OUTPUT_ROOT = REPO_ROOT / "tmp" / "sonic-brand-auditions" / "motif-prototypes"
DEFAULT_CORE_AUDITION_OUTPUT_ROOT = REPO_ROOT / "tmp" / "sonic-brand-auditions" / "core-audition"
FORMAT = {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}
REJECTED_REBUILD_MESSAGE = (
    "The rejected Recorded Night Drive rebuild is retired; use "
    "complete_audio_pack_gate.py and promote_complete_audio_pack.py."
)

# Immutable lineage from the first human selection board.  A direction choice
# is not the same thing as the required Mac/Sonos listening approval, so the
# Stage 2 manifest records this separately and remains release_ready=false.
MOTIF_PROTOTYPE_PACK_DIGEST = "4b7097b5c865e447aa29d250a13daf811d7cf247077a7dc62200524069db3870"
MOTIF_PROTOTYPE_SHA256 = {
    "midnight_signal": "97a3af9b763f0c78612e31a006489b0cc2e41992cbb9aaae8b755f182382d995",
    "city_pulse": "40e55a0847079d2544561cdbc944ffc2e7e9c6413d94c9df1ed558af1c3f6235",
    "warm_resolve": "41e9f28ac628198436c415493d5acb60201a561e01271a54fe7f845cff8cdd44",
}


@dataclass(frozen=True)
class SourceSpec:
    """One reviewed public master; its local filename is deliberately not shipped."""

    id: str
    filename: str
    creator: str
    title: str
    source_url: str
    source_sha256: str


@dataclass(frozen=True)
class Layer:
    """A non-synthetic clip from one reviewed source master."""

    source_id: str
    start_sec: float
    duration_sec: float
    offset_sec: float = 0.0
    gain_db: float = 0.0
    fade: bool = True


@dataclass(frozen=True)
class AssetSpec:
    """One delivered MP3 and the exact recorded-source edits that make it."""

    id: str
    path: str
    purpose: str
    kind: str
    tags: tuple[str, ...]
    duration_sec: float
    layers: tuple[Layer, ...]
    target_lufs: float = -16.0


@dataclass(frozen=True)
class MotifPrototypeSource:
    """One hash-pinned, audition-only source artifact for the motif gate.

    Freesound exposes these files as public HQ preview derivatives.  They are
    useful for choosing a direction, but are deliberately not accepted by the
    final-pack builder as replacements for the separately archived masters.
    """

    id: str
    filename: str
    creator: str
    title: str
    source_url: str
    artifact_url: str
    source_sha256: str
    provenance: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class MotifPrototypeLayer:
    """One precisely reproducible layer in a motif candidate."""

    source_id: str
    source_start_sec: float
    duration_sec: float
    output_offset_sec: float
    gain_db: float
    role: str
    dsp: tuple[str, ...]
    pitch_semitones: float = 0.0
    highpass_hz: int | None = None
    lowpass_hz: int | None = None
    fade_in_sec: float = 0.018
    fade_out_sec: float = 0.09


@dataclass(frozen=True)
class MotifPrototypeSpec:
    """A short, unselected Modern Night Drive signature candidate."""

    id: str
    label: str
    brief: str
    path: str
    duration_sec: float
    layers: tuple[MotifPrototypeLayer, ...]


@dataclass(frozen=True)
class CoreAuditionSpeech:
    """One local, audition-only Italian voice line used to judge a mix."""

    id: str
    filename: str
    text: str
    purpose: str


@dataclass(frozen=True)
class CoreAuditionCue:
    """An understated non-signature cue used by the Stage 2 audition."""

    id: str
    path: str
    purpose: str
    duration_sec: float
    layers: tuple[MotifPrototypeLayer, ...]


@dataclass(frozen=True)
class CoreGeneratedCue:
    """A deterministic locally authored transition with no recorded source."""

    id: str
    source_id: str
    path: str
    purpose: str
    duration_sec: float
    expression: str
    highpass_hz: int
    lowpass_hz: int
    fade_in_sec: float
    fade_out_sec: float


# Archived rejected-pack lineage. Nothing below this marker is a claim about
# the installed Modern Night Drive pack, and the old verifier/builder reject
# every invocation before reading a source or writing an output.
SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "cafe-kentspublicdomain",
        "cafe_kentspublicdomain.wav",
        "kentspublicdomain",
        "Freesound #324668 cafe ambience",
        "https://freesound.org/people/kentspublicdomain/sounds/324668/",
        "548d85e3bff2c44e0136a6908326202c10504e85e95cdc88b9347d5d41b26f22",
    ),
    SourceSpec(
        "cash-register-cv",
        "cash_register_cv.wav",
        "C-V",
        "Freesound #534066 cash register",
        "https://freesound.org/people/C-V/sounds/534066/",
        "6d2b0625dbd5b3ef0835885edfde8dde3e6197f55e3a5115b410dd4bdc8492fe",
    ),
    SourceSpec(
        "cassette-albertomarun",
        "cassette_albertomarun.mp3",
        "albertomarun",
        "Freesound #423871 cassette tape",
        "https://freesound.org/people/albertomarun/sounds/423871/",
        "83f3718ac0a51150a33811eb7666ae4b69d963081488ef8b4ecd80eaa5111a80",
    ),
    SourceSpec(
        "crowd-cheer-beeproductive",
        "crowd_cheer_beeproductive.wav",
        "BeeProductive",
        "Freesound #430046 crowd cheering and clapping",
        "https://freesound.org/people/BeeProductive/sounds/430046/",
        "0addb428c47fb0504ae9378724a3cd6052249e13fc5b5f47c0c6e43c20ba0ffd",
    ),
    SourceSpec(
        "crowd-laugh-mdrivet",
        "crowd_laughing_mdrivet.wav",
        "MDRivet",
        "Crowd Laughing.wav",
        "https://freesound.org/people/MDRivet/sounds/269461/",
        "a794c1465b0142071f31e4db39e72012c2d46f6e22efc76277dce843d536abb1",
    ),
    SourceSpec(
        "espresso-andra4",
        "espresso_andra4.wav",
        "Andra4",
        "Freesound #504961 espresso machine sequence",
        "https://freesound.org/people/Andra4/sounds/504961/",
        "1ea8697f81ec94d313e7ba70bee14942ea720da8816e54e7915196e1a112ca01",
    ),
    SourceSpec(
        "ice-athenspublic",
        "ice_glass_athenspublic.aiff",
        "athenspublic",
        "Freesound #341579 cocktail ice swish",
        "https://freesound.org/people/athenspublic/sounds/341579/",
        "66142b1ebb1e9126ee37138f8f7387fff562e60b882723e9c5584bcd5f625125",
    ),
    SourceSpec(
        "mandolin-gollybob",
        "mandolin_strum_gollybob.wav",
        "gollybob",
        "Mandolin Strum High G Chord.wav",
        "https://freesound.org/people/gollybob/sounds/413490/",
        "7b851a584956e53ccb1eb3b4f308854dcc55efcb2566c92013e72cf48191042c",
    ),
    SourceSpec(
        "telephone-kyles",
        "telephone_kyles.wav",
        "kyles",
        "Freesound #450042 telephone ring",
        "https://freesound.org/people/kyles/sounds/450042/",
        "dc1785ade0404b311dcc34277ae109a519aac66cb5e8b68b9062390056b7decc",
    ),
    SourceSpec(
        "trumpet-joepayne",
        "trumpet_fanfare_joepayne.mp3",
        "joepayne",
        "Clean Trumpet Fanfare .mp3",
        "https://freesound.org/people/joepayne/sounds/413201/",
        "5e0b3d1822463aba43b1ba4d34dbb6043625aebd8c3f35c49e7057d6687acc35",
    ),
    SourceSpec(
        "car-highway-yinyang",
        "car_highway_yinyang.mp3",
        "Yin_Yang_Jake007",
        "Car Passing on Highway.mp3",
        "https://freesound.org/people/Yin_Yang_Jake007/sounds/435358/",
        "d71af8f24599c64f9ae20aea1662384e4cbdc7f63666ebe6066104d4b9f81f92",
    ),
)


MOTIF_PROTOTYPE_SOURCES: tuple[MotifPrototypeSource, ...] = (
    MotifPrototypeSource(
        id="epiano-kevinklang-hq-preview",
        filename="epiano.mp3",
        creator="kevinklang",
        title="Yamaha epiano multiple C tones non-broken",
        source_url="https://freesound.org/people/kevinklang/sounds/841862/",
        artifact_url="https://cdn.freesound.org/previews/841/841862_14469752-hq.mp3",
        source_sha256="c3967d013586edec507d7611ea359b08fa3ebaa0a22196aa1e21e1f01082f4ab",
        provenance="Uploader-described Yamaha electric-piano C tones; 48 kHz mono original.",
        tags=("electric-piano", "synth", "pitched-instrument", "prototype"),
    ),
    MotifPrototypeSource(
        id="tape-trp-hq-preview",
        filename="tape.mp3",
        creator="TRP",
        title="Cassette tape hiss.flac",
        source_url="https://freesound.org/people/TRP/sounds/572531/",
        artifact_url="https://cdn.freesound.org/previews/572/572531_97550-hq.mp3",
        source_sha256="3543cf2fd8c016e5ed05f8b694d62237d259368946a8348e3abbd16f5aa38ecc",
        provenance="Direct transfer from a late-1980s cassette on a Tascam four-track; 48 kHz stereo original.",
        tags=("cassette", "tape", "hiss", "texture", "prototype"),
    ),
    MotifPrototypeSource(
        id="radio-quantumriver-hq-preview",
        filename="radio.mp3",
        creator="quantumriver",
        title="Radio tuning-static-interference",
        source_url="https://freesound.org/people/quantumriver/sounds/552160/",
        artifact_url="https://cdn.freesound.org/previews/552/552160_716433-hq.mp3",
        source_sha256="7df9903c38273be84b4bf01b01cb5a99a1964c7b13fd3938c9cf5a6698db2370",
        provenance="Direct Edirol R-09 recording of radio tuning and static; 48 kHz, 24-bit stereo original.",
        tags=("radio", "static", "tuning", "texture", "prototype"),
    ),
)


def _motif_note(
    semitones: float,
    offset_sec: float,
    duration_sec: float,
    gain_db: float,
) -> MotifPrototypeLayer:
    sign = "+" if semitones >= 0 else ""
    semitone_label = f"{semitones:g}"
    return MotifPrototypeLayer(
        source_id="epiano-kevinklang-hq-preview",
        source_start_sec=0.88,
        duration_sec=duration_sec,
        output_offset_sec=offset_sec,
        gain_db=gain_db,
        role="foreground",
        dsp=(f"pitch-shift:{sign}{semitone_label}st", "fade:18ms-in/90ms-out"),
        pitch_semitones=semitones,
        highpass_hz=90,
        lowpass_hz=8_500,
    )


MOTIF_PROTOTYPES: tuple[MotifPrototypeSpec, ...] = (
    MotifPrototypeSpec(
        id="midnight_signal",
        label="Midnight Signal",
        brief="A sparse three-note descent with a soft tape tail.",
        path="midnight_signal.mp3",
        duration_sec=1.08,
        layers=(
            _motif_note(7, 0.00, 0.46, -5.0),
            _motif_note(3, 0.27, 0.46, -6.0),
            _motif_note(0, 0.58, 0.50, -5.5),
            MotifPrototypeLayer(
                source_id="tape-trp-hq-preview",
                source_start_sec=2.20,
                duration_sec=1.08,
                output_offset_sec=0.0,
                gain_db=-31.0,
                role="texture",
                dsp=("highpass:280Hz", "lowpass:7200Hz", "fade:80ms-in/180ms-out"),
                highpass_hz=280,
                lowpass_hz=7_200,
                fade_in_sec=0.08,
                fade_out_sec=0.18,
            ),
        ),
    ),
    MotifPrototypeSpec(
        id="city_pulse",
        label="City Pulse",
        brief="A syncopated two-note call with a delayed answer and tiny radio flick.",
        path="city_pulse.mp3",
        duration_sec=0.98,
        layers=(
            _motif_note(0, 0.00, 0.30, -5.0),
            _motif_note(5, 0.18, 0.30, -6.0),
            _motif_note(0, 0.61, 0.37, -5.0),
            MotifPrototypeLayer(
                source_id="radio-quantumriver-hq-preview",
                source_start_sec=15.46,
                duration_sec=0.09,
                output_offset_sec=0.49,
                gain_db=-24.0,
                role="texture",
                dsp=("highpass:2400Hz", "lowpass:9200Hz", "fade:8ms-in/28ms-out"),
                highpass_hz=2_400,
                lowpass_hz=9_200,
                fade_in_sec=0.008,
                fade_out_sec=0.028,
            ),
        ),
    ),
    MotifPrototypeSpec(
        id="warm_resolve",
        label="Warm Resolve",
        brief="A restrained three-step rise resolving downward rather than repeating the old motif.",
        path="warm_resolve.mp3",
        duration_sec=1.12,
        layers=(
            _motif_note(0, 0.00, 0.38, -7.0),
            _motif_note(5, 0.21, 0.38, -6.5),
            _motif_note(9, 0.42, 0.38, -6.0),
            _motif_note(4, 0.72, 0.40, -5.0),
            MotifPrototypeLayer(
                source_id="tape-trp-hq-preview",
                source_start_sec=8.40,
                duration_sec=1.12,
                output_offset_sec=0.0,
                gain_db=-33.0,
                role="texture",
                dsp=("highpass:360Hz", "lowpass:6500Hz", "fade:90ms-in/220ms-out"),
                highpass_hz=360,
                lowpass_hz=6_500,
                fade_in_sec=0.09,
                fade_out_sec=0.22,
            ),
        ),
    ),
)


CORE_AUDITION_SPEECH: tuple[CoreAuditionSpeech, ...] = (
    CoreAuditionSpeech(
        id="speech.station-id",
        filename="station_id_voice.aiff",
        text="Mamma Mi Radio... da Windor a Vergen, la voce che non si spegne mai!",
        purpose="Current full station ident used by the runtime station-ID mix.",
    ),
    CoreAuditionSpeech(
        id="speech.sweeper",
        filename="sweeper_voice.aiff",
        text="Mamma Mi Radio. La notte suona italiana.",
        purpose="Representative Italian sweeper copy for the runtime sting mix.",
    ),
    CoreAuditionSpeech(
        id="speech.ad-intro",
        filename="ad_intro_voice.aiff",
        text="Una breve pausa, poi torniamo alla musica.",
        purpose="Representative host intro for the music-tail crossfade.",
    ),
    CoreAuditionSpeech(
        id="speech.spot",
        filename="spot_voice.aiff",
        text="Caffè Aurora: il gusto caldo che accompagna la notte. Disponibile vicino a te.",
        purpose="Fictional Italian spot copy used only to judge cadence and production gain.",
    ),
    CoreAuditionSpeech(
        id="speech.spot-two",
        filename="spot_two_voice.aiff",
        text="Luce Notturna: una lampada discreta per le ore più tranquille. Accendila con calma.",
        purpose="Second fictional Italian spot used to expose the optional mid-break cue once.",
    ),
    CoreAuditionSpeech(
        id="speech.ad-outro",
        filename="ad_outro_voice.aiff",
        text="E ora, di nuovo la musica su Mamma Mi Radio.",
        purpose="Representative host outro before the speech-to-music handoff.",
    ),
)


CORE_AUDITION_CUES: tuple[CoreAuditionCue, ...] = (
    CoreAuditionCue(
        id="core.ad-in",
        path="ad_in.mp3",
        purpose="Understated entry marker: a short radio aperture over low tape texture.",
        duration_sec=0.88,
        layers=(
            MotifPrototypeLayer(
                source_id="radio-quantumriver-hq-preview",
                source_start_sec=15.46,
                duration_sec=0.20,
                output_offset_sec=0.0,
                gain_db=-19.0,
                role="foreground",
                dsp=("highpass:2200Hz", "lowpass:9000Hz", "fade:8ms-in/55ms-out"),
                highpass_hz=2_200,
                lowpass_hz=9_000,
                fade_in_sec=0.008,
                fade_out_sec=0.055,
            ),
            MotifPrototypeLayer(
                source_id="tape-trp-hq-preview",
                source_start_sec=8.40,
                duration_sec=0.88,
                output_offset_sec=0.0,
                gain_db=-31.0,
                role="texture",
                dsp=("highpass:420Hz", "lowpass:6000Hz", "fade:30ms-in/190ms-out"),
                highpass_hz=420,
                lowpass_hz=6_000,
                fade_in_sec=0.03,
                fade_out_sec=0.19,
            ),
        ),
    ),
    CoreAuditionCue(
        id="core.ad-mid",
        path="ad_mid.mp3",
        purpose="Neutral single-note separator used at most once after the first spot; not the signature contour.",
        duration_sec=0.78,
        layers=(
            MotifPrototypeLayer(
                source_id="epiano-kevinklang-hq-preview",
                source_start_sec=0.88,
                duration_sec=0.34,
                output_offset_sec=0.0,
                gain_db=-15.0,
                role="foreground",
                dsp=("single-unpitched-note", "highpass:150Hz", "lowpass:4800Hz", "fade:8ms-in/150ms-out"),
                highpass_hz=150,
                lowpass_hz=4_800,
                fade_in_sec=0.008,
                fade_out_sec=0.15,
            ),
            MotifPrototypeLayer(
                source_id="radio-quantumriver-hq-preview",
                source_start_sec=12.60,
                duration_sec=0.78,
                output_offset_sec=0.0,
                gain_db=-38.0,
                role="texture",
                dsp=("highpass:2600Hz", "lowpass:7200Hz", "fade:25ms-in/180ms-out"),
                highpass_hz=2_600,
                lowpass_hz=7_200,
                fade_in_sec=0.025,
                fade_out_sec=0.18,
            ),
        ),
    ),
    CoreAuditionCue(
        id="core.ad-out",
        path="ad_out.mp3",
        purpose="Distinct closing release: tape motion followed by a narrow radio tail.",
        duration_sec=0.96,
        layers=(
            MotifPrototypeLayer(
                source_id="tape-trp-hq-preview",
                source_start_sec=12.60,
                duration_sec=0.82,
                output_offset_sec=0.0,
                gain_db=-26.0,
                role="foreground",
                dsp=("highpass:330Hz", "lowpass:5800Hz", "fade:18ms-in/230ms-out"),
                highpass_hz=330,
                lowpass_hz=5_800,
                fade_in_sec=0.018,
                fade_out_sec=0.23,
            ),
            MotifPrototypeLayer(
                source_id="radio-quantumriver-hq-preview",
                source_start_sec=13.60,
                duration_sec=0.13,
                output_offset_sec=0.68,
                gain_db=-32.0,
                role="texture",
                dsp=("highpass:3000Hz", "lowpass:8400Hz", "fade:5ms-in/55ms-out"),
                highpass_hz=3_000,
                lowpass_hz=8_400,
                fade_in_sec=0.005,
                fade_out_sec=0.055,
            ),
        ),
    ),
)


CORE_AUDITION_TRANSITIONS: tuple[CoreGeneratedCue, ...] = (
    CoreGeneratedCue(
        id="core.music-to-speech",
        source_id="generated.transition-music-to-speech",
        path="music_to_speech.mp3",
        purpose="Dry fallback handoff: a restrained electronic shutter when no music tail is available.",
        duration_sec=0.58,
        expression=("0.08*sin(2*PI*(310*t-65*t*t))*exp(-6.5*t)+0.025*sin(2*PI*(620*t-130*t*t))*exp(-8*t)"),
        highpass_hz=100,
        lowpass_hz=3_500,
        fade_in_sec=0.008,
        fade_out_sec=0.16,
    ),
    CoreGeneratedCue(
        id="core.speech-to-music",
        source_id="generated.transition-speech-to-music",
        path="speech_to_music.mp3",
        purpose="Small electronic lift prepended at unity and without a gap to a music successor.",
        duration_sec=0.66,
        expression=("0.06*sin(2*PI*(150*t+70*t*t))*exp(-1.6*t)+0.025*sin(2*PI*(225*t+105*t*t))*exp(-2*t)"),
        highpass_hz=90,
        lowpass_hz=4_200,
        fade_in_sec=0.04,
        fade_out_sec=0.18,
    ),
)


def _asset(
    asset_id: str,
    path: str,
    purpose: str,
    kind: str,
    tags: tuple[str, ...],
    duration_sec: float,
    *layers: Layer,
    target_lufs: float = -16.0,
) -> AssetSpec:
    return AssetSpec(asset_id, path, purpose, kind, tags, duration_sec, layers, target_lufs)


# These are editorial recipes, not a random SFX pool.  The station's runtime
# chooses at most one bed and two dry details from them.  All timing here is
# source timing, so a future review can reproduce or replace an individual cue
# without guessing how a procedural noise was made.
ASSETS: tuple[AssetSpec, ...] = (
    _asset(
        "identity.station-id",
        "station_id.mp3",
        "Short brass-and-mandolin station ident under a spoken name.",
        "identity",
        ("station", "trumpet", "mandolin", "warm"),
        3.2,
        Layer("trumpet-joepayne", 0.0, 3.2, gain_db=-4.0),
        Layer("mandolin-gollybob", 0.0, 2.65, offset_sec=0.28, gain_db=-7.5),
        Layer("cassette-albertomarun", 1.4, 1.8, gain_db=-26.0),
    ),
    _asset(
        "identity.sweeper",
        "sweeper.mp3",
        "Fast mandolin-and-brass sweep beneath a station sweeper.",
        "identity",
        ("station", "mandolin", "trumpet", "quick"),
        1.9,
        Layer("mandolin-gollybob", 0.0, 1.75, gain_db=-5.5),
        Layer("trumpet-joepayne", 1.15, 0.75, offset_sec=0.72, gain_db=-8.0),
    ),
    _asset(
        "identity.time-check",
        "time_check.mp3",
        "A small glass-and-string punctuation before a time check.",
        "identity",
        ("time", "ice", "mandolin", "small"),
        0.82,
        Layer("ice-athenspublic", 0.0, 0.82, gain_db=-3.5),
        Layer("mandolin-gollybob", 0.12, 0.68, offset_sec=0.10, gain_db=-15.0),
    ),
    _asset(
        "identity.ad-break",
        "bumpers/ad_break.mp3",
        "A real till-and-trumpet handoff into a fictional ad break.",
        "transition",
        ("ads", "cash-register", "trumpet", "handoff"),
        1.45,
        Layer("cash-register-cv", 0.0, 1.35, gain_db=-4.0),
        Layer("trumpet-joepayne", 0.38, 1.05, offset_sec=0.26, gain_db=-10.0),
    ),
    _asset(
        "transition.music-to-speech",
        "stingers/music_to_speech.mp3",
        "Cassette click and mandolin turn from music into speech.",
        "transition",
        ("music", "speech", "cassette", "mandolin"),
        1.55,
        Layer("cassette-albertomarun", 0.0, 1.55, gain_db=-7.0),
        Layer("mandolin-gollybob", 0.15, 1.15, offset_sec=0.36, gain_db=-13.0),
    ),
    _asset(
        "transition.speech-to-music",
        "stingers/speech_to_music.mp3",
        "Short trumpet release from speech back into music.",
        "transition",
        ("speech", "music", "trumpet", "release"),
        1.3,
        Layer("trumpet-joepayne", 2.3, 1.3, gain_db=-5.0),
        Layer("cassette-albertomarun", 5.8, 0.75, offset_sec=0.40, gain_db=-24.0),
    ),
    _asset(
        "bed.casa-notte",
        "beds/casa_notte.mp3",
        "Low continuous café room tone for ordinary spoken segments.",
        "bed",
        ("cafe", "room-tone", "spoken", "loopable"),
        16.0,
        Layer("cafe-kentspublicdomain", 8.0, 16.0, gain_db=-7.5, fade=False),
        Layer("cassette-albertomarun", 0.0, 16.0, gain_db=-30.0, fade=False),
        target_lufs=-23.0,
    ),
    _asset(
        "compat.chime",
        "sfx/chime.mp3",
        "Compatibility chime rendered from a real cocktail glass.",
        "compatibility",
        ("legacy", "ice", "chime"),
        0.82,
        Layer("ice-athenspublic", 0.0, 0.82, gain_db=-2.0),
    ),
    _asset(
        "compat.ding",
        "sfx/ding.mp3",
        "Compatibility ding rendered from a short glass tap.",
        "compatibility",
        ("legacy", "ice", "ding"),
        0.45,
        Layer("ice-athenspublic", 0.15, 0.45, gain_db=-5.5),
    ),
    _asset(
        "compat.cash-register",
        "sfx/cash_register.mp3",
        "Compatibility till cue from the recorded cash register.",
        "compatibility",
        ("legacy", "cash-register", "till"),
        0.92,
        Layer("cash-register-cv", 0.0, 0.92, gain_db=-2.0),
    ),
    _asset(
        "compat.register-hit",
        "sfx/register_hit.mp3",
        "Compatibility till impact from a different moment in the recording.",
        "compatibility",
        ("legacy", "cash-register", "hit"),
        0.52,
        Layer("cash-register-cv", 3.0, 0.52, gain_db=-2.0),
    ),
    _asset(
        "compat.sweep",
        "sfx/sweep.mp3",
        "Compatibility sweep using tape motion rather than an oscillator.",
        "compatibility",
        ("legacy", "cassette", "motion"),
        0.86,
        Layer("cassette-albertomarun", 2.0, 0.86, gain_db=-5.0),
    ),
    _asset(
        "compat.whoosh",
        "sfx/whoosh.mp3",
        "Compatibility whoosh using a real car pass.",
        "compatibility",
        ("legacy", "car", "pass"),
        0.94,
        Layer("car-highway-yinyang", 9.2, 0.94, gain_db=-7.0),
    ),
    _asset(
        "compat.tape-stop",
        "sfx/tape_stop.mp3",
        "Compatibility tape cue from a recorded cassette mechanism.",
        "compatibility",
        ("legacy", "cassette", "stop"),
        0.9,
        Layer("cassette-albertomarun", 11.0, 0.9, gain_db=-3.0),
    ),
    _asset(
        "compat.hotline-beep",
        "sfx/hotline_beep.mp3",
        "Compatibility hotline cue from a real telephone recording.",
        "compatibility",
        ("legacy", "telephone", "hotline"),
        0.86,
        Layer("telephone-kyles", 1.0, 0.86, gain_db=-8.0),
    ),
    _asset(
        "compat.mandolin-sting",
        "sfx/mandolin_sting.mp3",
        "Compatibility sting from the recorded mandolin chord.",
        "compatibility",
        ("legacy", "mandolin", "sting"),
        1.72,
        Layer("mandolin-gollybob", 0.0, 1.72, gain_db=-3.0),
    ),
    _asset(
        "compat.ice-clink",
        "sfx/ice_clink.mp3",
        "Compatibility aperitivo clink from the cocktail-ice recording.",
        "compatibility",
        ("legacy", "ice", "aperitivo"),
        1.0,
        Layer("ice-athenspublic", 0.0, 1.0, gain_db=-2.5),
    ),
    _asset(
        "compat.startup-synth",
        "sfx/startup_synth.mp3",
        "Legacy filename retained; it now contains a real trumpet flourish.",
        "compatibility",
        ("legacy", "trumpet", "flourish"),
        0.94,
        Layer("trumpet-joepayne", 0.65, 0.94, gain_db=-7.0),
    ),
    _asset(
        "ad-bed.cafe-testimonial",
        "ads/beds/cafe_testimonial.mp3",
        "Dry café bed for a suspiciously polished testimonial.",
        "ad_bed",
        ("ad", "cafe", "testimonial", "room"),
        8.0,
        Layer("cafe-kentspublicdomain", 14.0, 8.0, gain_db=-6.0, fade=False),
        target_lufs=-23.0,
    ),
    _asset(
        "ad-bed.stadium-win",
        "ads/beds/stadium_win.mp3",
        "Crowd-and-applause bed for a ridiculous victory lap.",
        "ad_bed",
        ("ad", "crowd", "applause", "victory"),
        8.0,
        Layer("crowd-cheer-beeproductive", 2.0, 8.0, gain_db=-10.0, fade=False),
        target_lufs=-24.0,
    ),
    _asset(
        "ad-bed.showroom-reveal",
        "ads/beds/showroom_reveal.mp3",
        "Cassette-and-room bed for an overlit showroom reveal.",
        "ad_bed",
        ("ad", "cassette", "showroom", "reveal"),
        7.0,
        Layer("cassette-albertomarun", 4.0, 7.0, gain_db=-13.0, fade=False),
        Layer("cafe-kentspublicdomain", 28.0, 7.0, gain_db=-20.0, fade=False),
        target_lufs=-24.0,
    ),
    _asset(
        "ad-bed.bureaucracy-stamp",
        "ads/beds/bureaucracy_stamp.mp3",
        "Quiet room tone for a deadpan official notice.",
        "ad_bed",
        ("ad", "bureaucracy", "room", "deadpan"),
        7.0,
        Layer("cafe-kentspublicdomain", 24.0, 7.0, gain_db=-13.0, fade=False),
        Layer("cassette-albertomarun", 7.0, 7.0, gain_db=-28.0, fade=False),
        target_lufs=-24.0,
    ),
    _asset(
        "ad-bed.motorway-pass",
        "ads/beds/motorway_pass.mp3",
        "Real roadside ambience for a fast motorway spot.",
        "ad_bed",
        ("ad", "motorway", "car", "road"),
        8.0,
        Layer("car-highway-yinyang", 7.0, 8.0, gain_db=-16.0, fade=False),
        target_lufs=-25.0,
    ),
    _asset(
        "ad-bed.late-night-hotline",
        "ads/beds/late_night_hotline.mp3",
        "Tape-led late-night bed with a distant phone colour.",
        "ad_bed",
        ("ad", "telephone", "cassette", "late-night"),
        8.0,
        Layer("cassette-albertomarun", 8.0, 8.0, gain_db=-14.0, fade=False),
        Layer("telephone-kyles", 4.0, 3.0, offset_sec=2.7, gain_db=-28.0),
        target_lufs=-25.0,
    ),
    _asset(
        "ad-bed.supermarket-dash",
        "ads/beds/supermarket_dash.mp3",
        "Busy café room with a faint till detail for supermarket comedy.",
        "ad_bed",
        ("ad", "supermarket", "cash-register", "busy"),
        8.0,
        Layer("cafe-kentspublicdomain", 2.0, 8.0, gain_db=-13.0, fade=False),
        Layer("cash-register-cv", 4.0, 1.2, offset_sec=3.8, gain_db=-30.0),
        target_lufs=-25.0,
    ),
    _asset(
        "ad-bed.pharmacy-whisper",
        "ads/beds/pharmacy_whisper.mp3",
        "Restrained room-and-tape bed for a whispered health claim.",
        "ad_bed",
        ("ad", "pharmacy", "whisper", "quiet"),
        7.0,
        Layer("cafe-kentspublicdomain", 31.0, 7.0, gain_db=-19.0, fade=False),
        Layer("cassette-albertomarun", 2.0, 7.0, gain_db=-29.0, fade=False),
        target_lufs=-26.0,
    ),
    _asset(
        "ad-bed.home-reveal",
        "ads/beds/home_reveal.mp3",
        "A grounded espresso-and-café bed for a domestic reveal.",
        "ad_bed",
        ("ad", "home", "espresso", "cafe"),
        8.0,
        Layer("espresso-andra4", 24.0, 8.0, gain_db=-15.0, fade=False),
        Layer("cafe-kentspublicdomain", 5.0, 8.0, gain_db=-24.0, fade=False),
        target_lufs=-25.0,
    ),
    _asset(
        "ad-cue.laugh-open",
        "ads/cues/laugh_open.mp3",
        "Short warm audience laugh to open a deliberately silly line.",
        "ad_cue",
        ("ad", "laugh", "open", "audience"),
        1.35,
        Layer("crowd-laugh-mdrivet", 4.0, 1.35, gain_db=-7.0),
    ),
    _asset(
        "ad-cue.laugh-mid",
        "ads/cues/laugh_mid.mp3",
        "Compact mid-copy audience laugh.",
        "ad_cue",
        ("ad", "laugh", "mid", "audience"),
        1.1,
        Layer("crowd-laugh-mdrivet", 20.0, 1.1, gain_db=-8.0),
    ),
    _asset(
        "ad-cue.laugh-button",
        "ads/cues/laugh_button.mp3",
        "Short laugh button after a punchline.",
        "ad_cue",
        ("ad", "laugh", "outro", "button"),
        1.5,
        Layer("crowd-laugh-mdrivet", 41.0, 1.5, gain_db=-7.5),
    ),
    _asset(
        "ad-cue.laugh-release",
        "ads/cues/laugh_release.mp3",
        "A longer laugh release for the end of a testimonial.",
        "ad_cue",
        ("ad", "laugh", "release", "audience"),
        2.0,
        Layer("crowd-laugh-mdrivet", 61.0, 2.0, gain_db=-9.0),
    ),
    _asset(
        "ad-cue.applause-open",
        "ads/cues/applause_open.mp3",
        "Immediate crowd applause for a grand opening.",
        "ad_cue",
        ("ad", "applause", "crowd", "open"),
        1.45,
        Layer("crowd-cheer-beeproductive", 0.0, 1.45, gain_db=-6.0),
    ),
    _asset(
        "ad-cue.applause-mid",
        "ads/cues/applause_mid.mp3",
        "Dry applause hit for a mid-copy win.",
        "ad_cue",
        ("ad", "applause", "crowd", "mid"),
        1.15,
        Layer("crowd-cheer-beeproductive", 5.0, 1.15, gain_db=-7.0),
    ),
    _asset(
        "ad-cue.applause-button",
        "ads/cues/applause_button.mp3",
        "Tight applause button to close a winner.",
        "ad_cue",
        ("ad", "applause", "crowd", "outro"),
        1.65,
        Layer("crowd-cheer-beeproductive", 10.0, 1.65, gain_db=-8.0),
    ),
    _asset(
        "ad-cue.applause-release",
        "ads/cues/applause_release.mp3",
        "Longer applause release, never used with another reaction cue.",
        "ad_cue",
        ("ad", "applause", "crowd", "release"),
        2.2,
        Layer("crowd-cheer-beeproductive", 13.0, 2.2, gain_db=-9.0),
    ),
    _asset(
        "ad-cue.trumpet-open",
        "ads/cues/trumpet_open.mp3",
        "A real trumpet fanfare head for a proud claim.",
        "ad_cue",
        ("ad", "trumpet", "brass", "open"),
        1.25,
        Layer("trumpet-joepayne", 0.0, 1.25, gain_db=-5.0),
    ),
    _asset(
        "ad-cue.trumpet-hit",
        "ads/cues/trumpet_hit.mp3",
        "A compact brass hit for a reveal.",
        "ad_cue",
        ("ad", "trumpet", "brass", "hit"),
        0.82,
        Layer("trumpet-joepayne", 1.5, 0.82, gain_db=-5.5),
    ),
    _asset(
        "ad-cue.trumpet-out",
        "ads/cues/trumpet_out.mp3",
        "Brass release for an ad outro.",
        "ad_cue",
        ("ad", "trumpet", "brass", "outro"),
        1.25,
        Layer("trumpet-joepayne", 2.95, 1.25, gain_db=-6.0),
    ),
    _asset(
        "ad-cue.mandolin-open",
        "ads/cues/mandolin_open.mp3",
        "A bright real mandolin opening strum.",
        "ad_cue",
        ("ad", "mandolin", "open", "warm"),
        1.35,
        Layer("mandolin-gollybob", 0.0, 1.35, gain_db=-5.0),
    ),
    _asset(
        "ad-cue.mandolin-mid",
        "ads/cues/mandolin_mid.mp3",
        "A clipped mandolin accent for a scene turn.",
        "ad_cue",
        ("ad", "mandolin", "mid", "accent"),
        0.9,
        Layer("mandolin-gollybob", 0.82, 0.9, gain_db=-6.0),
    ),
    _asset(
        "ad-cue.mandolin-out",
        "ads/cues/mandolin_out.mp3",
        "Mandolin release after a domestic punchline.",
        "ad_cue",
        ("ad", "mandolin", "outro", "release"),
        1.55,
        Layer("mandolin-gollybob", 1.0, 1.55, gain_db=-7.0),
    ),
    _asset(
        "ad-cue.espresso-hiss",
        "ads/cues/espresso_hiss.mp3",
        "Short espresso steam detail, kept dry and brief.",
        "ad_cue",
        ("ad", "espresso", "steam", "cafe"),
        1.1,
        Layer("espresso-andra4", 10.0, 1.1, gain_db=-10.0),
    ),
    _asset(
        "ad-cue.espresso-pour",
        "ads/cues/espresso_pour.mp3",
        "Recorded espresso action for a café scene.",
        "ad_cue",
        ("ad", "espresso", "pour", "cafe"),
        1.4,
        Layer("espresso-andra4", 43.0, 1.4, gain_db=-9.0),
    ),
    _asset(
        "ad-cue.espresso-button",
        "ads/cues/espresso_button.mp3",
        "Quick espresso-machine button for a scene turn.",
        "ad_cue",
        ("ad", "espresso", "button", "foley"),
        0.72,
        Layer("espresso-andra4", 79.0, 0.72, gain_db=-8.0),
    ),
    _asset(
        "ad-cue.espresso-release",
        "ads/cues/espresso_release.mp3",
        "Short espresso-machine release after copy.",
        "ad_cue",
        ("ad", "espresso", "release", "foley"),
        1.25,
        Layer("espresso-andra4", 112.0, 1.25, gain_db=-10.0),
    ),
    _asset(
        "ad-cue.cafe-clatter",
        "ads/cues/cafe_clatter.mp3",
        "A small café clatter, not an ambient loop.",
        "ad_cue",
        ("ad", "cafe", "clatter", "foley"),
        1.0,
        Layer("cafe-kentspublicdomain", 4.0, 1.0, gain_db=-10.0),
    ),
    _asset(
        "ad-cue.cafe-room-hit",
        "ads/cues/cafe_room_hit.mp3",
        "A compact room response for a scene reveal.",
        "ad_cue",
        ("ad", "cafe", "room", "accent"),
        1.15,
        Layer("cafe-kentspublicdomain", 20.0, 1.15, gain_db=-12.0),
    ),
    _asset(
        "ad-cue.cash-stamp",
        "ads/cues/cash_stamp.mp3",
        "Mechanical cash-register strike used like a bureaucratic stamp.",
        "ad_cue",
        ("ad", "cash-register", "stamp", "bureaucracy"),
        0.68,
        Layer("cash-register-cv", 2.4, 0.68, gain_db=-3.5),
    ),
    _asset(
        "ad-cue.cash-open",
        "ads/cues/cash_open.mp3",
        "Recorded till opening for a supermarket reveal.",
        "ad_cue",
        ("ad", "cash-register", "open", "supermarket"),
        1.1,
        Layer("cash-register-cv", 5.0, 1.1, gain_db=-4.0),
    ),
    _asset(
        "ad-cue.cash-close",
        "ads/cues/cash_close.mp3",
        "Short till close for an ad button.",
        "ad_cue",
        ("ad", "cash-register", "close", "button"),
        0.86,
        Layer("cash-register-cv", 8.5, 0.86, gain_db=-4.0),
    ),
    _asset(
        "ad-cue.phone-ring",
        "ads/cues/phone_ring.mp3",
        "Real phone ring for a late-night hotline opener.",
        "ad_cue",
        ("ad", "telephone", "ring", "hotline"),
        1.25,
        Layer("telephone-kyles", 2.0, 1.25, gain_db=-8.0),
    ),
    _asset(
        "ad-cue.phone-click",
        "ads/cues/phone_click.mp3",
        "Small telephone mechanism detail.",
        "ad_cue",
        ("ad", "telephone", "click", "foley"),
        0.72,
        Layer("telephone-kyles", 7.0, 0.72, gain_db=-9.0),
    ),
    _asset(
        "ad-cue.phone-out",
        "ads/cues/phone_out.mp3",
        "Telephone release for a late-night outro.",
        "ad_cue",
        ("ad", "telephone", "outro", "hotline"),
        1.1,
        Layer("telephone-kyles", 11.0, 1.1, gain_db=-9.0),
    ),
    _asset(
        "ad-cue.tape-click",
        "ads/cues/tape_click.mp3",
        "Cassette mechanism click for a scene transition.",
        "ad_cue",
        ("ad", "cassette", "click", "transition"),
        0.75,
        Layer("cassette-albertomarun", 0.5, 0.75, gain_db=-5.0),
    ),
    _asset(
        "ad-cue.tape-slap",
        "ads/cues/tape_slap.mp3",
        "A different cassette hit for a bureaucratic button.",
        "ad_cue",
        ("ad", "cassette", "slap", "button"),
        0.9,
        Layer("cassette-albertomarun", 6.0, 0.9, gain_db=-6.0),
    ),
    _asset(
        "ad-cue.tape-out",
        "ads/cues/tape_out.mp3",
        "Cassette tail used as a single dry outro detail.",
        "ad_cue",
        ("ad", "cassette", "outro", "release"),
        1.05,
        Layer("cassette-albertomarun", 15.0, 1.05, gain_db=-7.0),
    ),
    _asset(
        "ad-cue.ice-rattle",
        "ads/cues/ice_rattle.mp3",
        "A soft cocktail-ice rattle for a whispered scene.",
        "ad_cue",
        ("ad", "ice", "rattle", "whisper"),
        1.15,
        Layer("ice-athenspublic", 0.65, 1.15, gain_db=-7.5),
    ),
    _asset(
        "ad-cue.ice-button",
        "ads/cues/ice_button.mp3",
        "Tight cocktail-glass button.",
        "ad_cue",
        ("ad", "ice", "button", "aperitivo"),
        0.62,
        Layer("ice-athenspublic", 1.2, 0.62, gain_db=-5.0),
    ),
    _asset(
        "ad-cue.car-pass-open",
        "ads/cues/car_pass_open.mp3",
        "A real motorway pass to open a driving spot.",
        "ad_cue",
        ("ad", "car", "motorway", "open"),
        1.5,
        Layer("car-highway-yinyang", 3.5, 1.5, gain_db=-9.0),
    ),
    _asset(
        "ad-cue.car-pass-out",
        "ads/cues/car_pass_out.mp3",
        "Short roadway release for the end of a driving spot.",
        "ad_cue",
        ("ad", "car", "motorway", "outro"),
        1.3,
        Layer("car-highway-yinyang", 17.0, 1.3, gain_db=-10.0),
    ),
)


RECIPES: tuple[dict[str, object], ...] = (
    {
        "id": "cafe_testimonial",
        "bed": {"asset_id": "ad-bed.cafe-testimonial", "gain_db": -19.0},
        "cues": [
            {
                "anchor": "after_first_voice",
                "asset_id": "ad-cue.espresso-hiss",
                "gain_db": -13.0,
                "max_duration_sec": 1.1,
            },
            {"anchor": "outro", "asset_id": "ad-cue.laugh-button", "gain_db": -12.0, "max_duration_sec": 1.5},
        ],
    },
    {
        "id": "stadium_win",
        "bed": {"asset_id": "ad-bed.stadium-win", "gain_db": -22.0},
        "cues": [
            {"anchor": "intro", "asset_id": "ad-cue.trumpet-open", "gain_db": -8.0, "max_duration_sec": 1.25},
            {"anchor": "outro", "asset_id": "ad-cue.applause-button", "gain_db": -12.0, "max_duration_sec": 1.65},
        ],
    },
    {
        "id": "showroom_reveal",
        "bed": {"asset_id": "ad-bed.showroom-reveal", "gain_db": -21.0},
        "cues": [
            {"anchor": "intro", "asset_id": "ad-cue.ice-rattle", "gain_db": -14.0, "max_duration_sec": 1.15},
            {"anchor": "outro", "asset_id": "ad-cue.mandolin-out", "gain_db": -13.0, "max_duration_sec": 1.55},
        ],
    },
    {
        "id": "bureaucracy_stamp",
        "bed": {"asset_id": "ad-bed.bureaucracy-stamp", "gain_db": -23.0},
        "cues": [
            {
                "anchor": "after_first_voice",
                "asset_id": "ad-cue.cash-stamp",
                "gain_db": -10.0,
                "max_duration_sec": 0.68,
            },
            {"anchor": "outro", "asset_id": "ad-cue.tape-slap", "gain_db": -14.0, "max_duration_sec": 0.9},
        ],
    },
    {
        "id": "motorway_pass",
        "bed": {"asset_id": "ad-bed.motorway-pass", "gain_db": -23.0},
        "cues": [
            {"anchor": "intro", "asset_id": "ad-cue.car-pass-open", "gain_db": -13.0, "max_duration_sec": 1.5},
            {"anchor": "outro", "asset_id": "ad-cue.car-pass-out", "gain_db": -15.0, "max_duration_sec": 1.3},
        ],
    },
    {
        "id": "late_night_hotline",
        "bed": {"asset_id": "ad-bed.late-night-hotline", "gain_db": -23.0},
        "cues": [
            {"anchor": "intro", "asset_id": "ad-cue.phone-ring", "gain_db": -14.0, "max_duration_sec": 1.25},
            {"anchor": "outro", "asset_id": "ad-cue.phone-out", "gain_db": -15.0, "max_duration_sec": 1.1},
        ],
    },
    {
        "id": "supermarket_dash",
        "bed": {"asset_id": "ad-bed.supermarket-dash", "gain_db": -24.0},
        "cues": [
            {"anchor": "after_first_voice", "asset_id": "ad-cue.cash-open", "gain_db": -11.0, "max_duration_sec": 1.1},
            {"anchor": "outro", "asset_id": "ad-cue.laugh-release", "gain_db": -14.0, "max_duration_sec": 2.0},
        ],
    },
    {
        "id": "pharmacy_whisper",
        "bed": {"asset_id": "ad-bed.pharmacy-whisper", "gain_db": -27.0},
        "cues": [
            {"anchor": "intro", "asset_id": "ad-cue.ice-rattle", "gain_db": -17.0, "max_duration_sec": 1.15},
            {"anchor": "outro", "asset_id": "ad-cue.ice-button", "gain_db": -15.0, "max_duration_sec": 0.62},
        ],
    },
    {
        "id": "home_reveal",
        "bed": {"asset_id": "ad-bed.home-reveal", "gain_db": -23.0},
        "cues": [
            {
                "anchor": "after_first_voice",
                "asset_id": "ad-cue.espresso-pour",
                "gain_db": -14.0,
                "max_duration_sec": 1.4,
            },
            {"anchor": "outro", "asset_id": "ad-cue.mandolin-mid", "gain_db": -14.0, "max_duration_sec": 0.9},
        ],
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_map() -> dict[str, SourceSpec]:
    return {source.id: source for source in SOURCES}


def verify_sources(source_root: Path) -> None:
    """Reject the retired Recorded Night Drive source-verification route."""
    del source_root
    raise RuntimeError(REJECTED_REBUILD_MESSAGE)


def _motif_prototype_source_map() -> dict[str, MotifPrototypeSource]:
    return {source.id: source for source in MOTIF_PROTOTYPE_SOURCES}


def verify_motif_prototype_sources(source_root: Path) -> None:
    """Fail closed unless all three known HQ preview derivatives are intact."""
    failures: list[str] = []
    for source in MOTIF_PROTOTYPE_SOURCES:
        path = source_root / source.filename
        if not path.is_file():
            failures.append(f"missing {source.filename}")
            continue
        actual = _sha256(path)
        if actual != source.source_sha256:
            failures.append(f"SHA-256 mismatch for {source.filename}: {actual}")
    if failures:
        raise ValueError("Motif prototype sources are not intact:\n- " + "\n- ".join(failures))


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _render_motif_prototype(
    spec: MotifPrototypeSpec,
    source_root: Path,
    output_root: Path,
) -> Path:
    output_path = output_root / spec.path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp.mp3")
    temporary_path.unlink(missing_ok=True)

    sources = _motif_prototype_source_map()
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for layer in spec.layers:
        command.extend(["-i", str(source_root / sources[layer.source_id].filename)])

    filters: list[str] = []
    labels: list[str] = []
    for index, layer in enumerate(spec.layers):
        clip = (
            f"[{index}:a]atrim=start={_number(layer.source_start_sec)}:duration={_number(layer.duration_sec)},"
            "asetpts=N/SR/TB"
        )
        if layer.pitch_semitones:
            ratio = 2 ** (layer.pitch_semitones / 12)
            clip += f",asetrate={_number(48_000 * ratio)},aresample=48000,atempo={_number(1 / ratio)}"
        else:
            clip += ",aresample=48000"
        clip += ",aformat=channel_layouts=stereo"
        if layer.highpass_hz is not None:
            clip += f",highpass=f={layer.highpass_hz}"
        if layer.lowpass_hz is not None:
            clip += f",lowpass=f={layer.lowpass_hz}"
        clip += f",volume={_number(layer.gain_db)}dB"
        fade_out_start = max(layer.duration_sec - layer.fade_out_sec, 0.0)
        clip += (
            f",afade=t=in:st=0:d={_number(layer.fade_in_sec)}"
            f",afade=t=out:st={_number(fade_out_start)}:d={_number(layer.fade_out_sec)}"
        )
        if layer.output_offset_sec:
            delay_ms = round(layer.output_offset_sec * 1_000)
            clip += f",adelay={delay_ms}|{delay_ms}"
        label = f"clip{index}"
        filters.append(f"{clip}[{label}]")
        labels.append(f"[{label}]")

    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        f"apad=whole_dur={_number(spec.duration_sec)},atrim=duration={_number(spec.duration_sec)},"
        "highpass=f=45,lowpass=f=15000,loudnorm=I=-16:LRA=5:TP=-1.5,"
        "aformat=sample_rates=48000:channel_layouts=stereo[out]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-write_xing",
            "0",
            str(temporary_path),
        ]
    )
    try:
        subprocess.run(command, check=True)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def _prototype_pack_digest(candidates: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(candidates, key=lambda item: str(item["path"])):
        digest.update(str(candidate["path"]).encode())
        digest.update(b"\0")
        digest.update(str(candidate["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _motif_prototype_manifest(output_root: Path, *, generated_at: str) -> dict[str, Any]:
    sources = _motif_prototype_source_map()
    source_records = [
        {
            "id": source.id,
            "license": "CC0-1.0",
            "source_url": source.source_url,
            "artifact_url": source.artifact_url,
            "source_sha256": source.source_sha256,
            "creator": source.creator,
            "title": source.title,
            "provenance": source.provenance,
            "artifact_kind": "Freesound HQ preview derivative; not the original source master",
            "tags": list(source.tags),
        }
        for source in MOTIF_PROTOTYPE_SOURCES
    ]
    candidates: list[dict[str, object]] = []
    for spec in MOTIF_PROTOTYPES:
        candidates.append(
            {
                "id": spec.id,
                "label": spec.label,
                "brief": spec.brief,
                "path": spec.path,
                "sha256": _sha256(output_root / spec.path),
                "duration_target_sec": spec.duration_sec,
                "format": FORMAT,
                "tags": ["modern-night-drive", "electric-piano", "motif-candidate"],
                "layers": [
                    {
                        "source_id": layer.source_id,
                        "source_sha256": sources[layer.source_id].source_sha256,
                        "source_start_sec": layer.source_start_sec,
                        "duration_sec": layer.duration_sec,
                        "output_offset_sec": layer.output_offset_sec,
                        "gain_db": layer.gain_db,
                        "dsp": list(layer.dsp),
                        "license": "CC0-1.0",
                        "role": layer.role,
                    }
                    for layer in spec.layers
                ],
                "mastering_dsp": [
                    "highpass:45Hz",
                    "lowpass:15000Hz",
                    "loudnorm:I=-16,LRA=5,TP=-1.5",
                    "encode:MP3-48kHz-stereo-192kbps",
                ],
            }
        )
    pack_digest = _prototype_pack_digest(candidates)
    return {
        "schema_version": 1,
        "pack": "Modern Night Drive motif prototypes",
        "stage": "motif-selection",
        "generated_at": generated_at,
        "sources": source_records,
        "candidates": candidates,
        "quality_guard_allowlist": {"perceptual_overlaps": [], "source_core_roles": []},
        "pack_digest": pack_digest,
        "release_ready": False,
        "listening_receipt": {
            "status": "pending",
            "pack_digest": None,
            "approved_candidate_id": None,
            "surfaces": [],
            "device_classes": [],
            "reviewed_at": None,
        },
        "limitations": [
            "These audition files use hash-pinned Freesound HQ preview derivatives, not original masters.",
            "They cannot be copied into the shipped imaging pack.",
            "The final pack remains blocked until one motif is approved and original masters pass provenance review.",
        ],
    }


def render_motif_prototypes(
    source_root: Path,
    output_root: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Render the three audition-only motif candidates and their provenance ledger."""
    verify_motif_prototype_sources(source_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty prototype directory: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    for spec in MOTIF_PROTOTYPES:
        _render_motif_prototype(spec, source_root, output_root)
    stamp = generated_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest = _motif_prototype_manifest(output_root, generated_at=stamp)
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def core_ad_break_parts(
    intro_path: Path,
    spot_paths: list[Path],
    ad_in_path: Path,
    ad_out_path: Path,
    outro_path: Path,
    *,
    promo_path: Path | None = None,
    ad_mid_path: Path | None = None,
) -> list[Path]:
    """Return the runtime ad-break order with at most one mid cue.

    The producer inserts the sole mid cue after spot one.  A one-spot break
    never receives it, and longer breaks never repeat it between later spots.
    """
    if not spot_paths:
        raise ValueError("core audition requires at least one representative spot")
    parts = [intro_path]
    if promo_path is not None:
        parts.append(promo_path)
    parts.append(ad_in_path)
    for index, spot_path in enumerate(spot_paths):
        parts.append(spot_path)
        if index == 0 and len(spot_paths) > 1 and ad_mid_path is not None:
            parts.append(ad_mid_path)
    parts.extend([ad_out_path, outro_path])
    return parts


def core_cadence_parts(
    ad_break_path: Path,
    successor_path: Path,
    *,
    successor_kind: str,
    music_to_speech_path: Path | None = None,
    speech_to_music_path: Path | None = None,
    predecessor_music_path: Path | None = None,
    predecessor_has_music_tail: bool = True,
) -> list[Path]:
    """Return the runtime boundary sequence for a complete break."""
    parts = [predecessor_music_path] if predecessor_music_path is not None else []
    if predecessor_music_path is not None and not predecessor_has_music_tail:
        if music_to_speech_path is None:
            raise ValueError("dry music predecessor requires the music-to-speech cue")
        parts.append(music_to_speech_path)
    normalized_kind = successor_kind.strip().lower()
    if normalized_kind == "music":
        if speech_to_music_path is None:
            raise ValueError("music successor requires the speech-to-music cue")
        return [*parts, ad_break_path, speech_to_music_path, successor_path]
    return [*parts, ad_break_path, successor_path]


def _run_atomic_command(command: list[str], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix or ".tmp"
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp{suffix}")
    temporary_path.unlink(missing_ok=True)
    try:
        subprocess.run([*command, str(temporary_path)], check=True)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def _ffmpeg_output_args() -> list[str]:
    return [
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-write_xing",
        "0",
        "-f",
        "mp3",
    ]


def _probe_duration_sec(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _require_rendered_duration(path: Path, *, minimum_sec: float, render_id: str) -> None:
    duration_sec = _probe_duration_sec(path)
    if duration_sec < minimum_sec:
        raise RuntimeError(f"{render_id} decoded duration is {duration_sec:.3f}s; expected at least {minimum_sec:.3f}s")


def _core_cue_map() -> dict[str, CoreAuditionCue]:
    return {cue.id: cue for cue in CORE_AUDITION_CUES}


def _selected_motif(selected_motif_id: str) -> MotifPrototypeSpec:
    by_id = {candidate.id: candidate for candidate in MOTIF_PROTOTYPES}
    try:
        return by_id[selected_motif_id]
    except KeyError as exc:
        choices = ", ".join(sorted(by_id))
        raise ValueError(f"Unknown selected motif {selected_motif_id!r}; choose one of: {choices}") from exc


def _render_core_speech_source(
    spec: CoreAuditionSpeech,
    output_path: Path,
    *,
    voice_source: Path | None = None,
) -> dict[str, Any]:
    """Freeze one local voice source and return its provenance record.

    ``--voice-source`` intentionally applies to the representative spot only;
    station and sweeper copy must retain their exact declared words.  Without
    it, macOS ``say`` renders every line locally with no service upload.
    """
    if voice_source is not None and spec.id == "speech.spot":
        if not voice_source.is_file():
            raise ValueError(f"Local voice source does not exist: {voice_source}")
        original_sha256 = _sha256(voice_source)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(voice_source),
            "-vn",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s24be",
            "-f",
            "aiff",
        ]
        _run_atomic_command(command, output_path)
        origin: dict[str, Any] = {
            "kind": "caller-provided-local-file",
            "original_filename": voice_source.name,
            "original_sha256": original_sha256,
            "declared_text": None,
            "transcript_status": "not-asserted-by-builder",
            "generation": "Local FFmpeg transcode to 48 kHz stereo 24-bit AIFF; no upload.",
        }
    else:
        say_path = shutil.which("say")
        if say_path is None:
            raise OSError(
                "macOS 'say' is required for the declared Italian audition lines; run the core audition on a Mac"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        say_output_path = output_path.with_name(f".{output_path.stem}.say.aiff")
        say_output_path.unlink(missing_ok=True)
        try:
            subprocess.run(
                [
                    say_path,
                    "-v",
                    "Alice",
                    "-r",
                    "195",
                    "-o",
                    str(say_output_path),
                    spec.text,
                ],
                check=True,
            )
            _run_atomic_command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(say_output_path),
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s24be",
                    "-f",
                    "aiff",
                ],
                output_path,
            )
        finally:
            say_output_path.unlink(missing_ok=True)
        origin = {
            "kind": "local-system-tts",
            "provider": "macOS say",
            "voice": "Alice",
            "locale": "it_IT",
            "rate_words_per_minute": 195,
            "declared_text": spec.text,
            "generation": "Local system TTS, then local FFmpeg transcode to 48 kHz stereo AIFF; no upload.",
        }
    return {
        "id": spec.id,
        "license": "audition-only-local-generation",
        "creator": "Mamma Mi Radio audition tooling",
        "title": spec.purpose,
        "artifact_path": f"sources/{output_path.name}",
        "source_sha256": _sha256(output_path),
        "duration_sec": _probe_duration_sec(output_path),
        "origin": origin,
        "tags": ["speech", "italian", "audition-only"],
    }


@contextmanager
def _runtime_loudness_targets() -> Iterator[None]:
    """Apply the production startup targets while this offline board renders."""
    configure_loudness_reconcile(-16.0, -15.0, sample_rate=48_000, channels=2, bitrate=192)
    try:
        yield
    finally:
        configure_loudness_reconcile(None, None, sample_rate=48_000, channels=2, bitrate=192)


def _render_runtime_voice_sting_mix(voice_path: Path, sting_path: Path, output_path: Path) -> Path:
    """Mirror ``mix_voice_with_sting``'s production topology exactly."""
    with _runtime_loudness_targets():
        return mix_voice_with_sting(voice_path, sting_path, output_path)


def _render_runtime_music_tail_intro(music_path: Path, voice_path: Path, output_path: Path) -> Path:
    """Mirror the normal adjacent-music ad-intro crossfade."""
    return crossfade_voice_over_music(
        music_path,
        voice_path,
        output_path,
        tail_seconds=8.0,
        voice_volume=1.0,
        music_fade_volume=0.5,
        voice_delay_ms=150,
    )


def _render_context_music(output_path: Path, *, successor: bool) -> Path:
    """Render neutral synthetic program context, never candidate pack material."""
    duration = 4.2 if successor else 12.0
    if successor:
        expression = "0.16*sin(2*PI*110*t)*(1+0.10*sin(2*PI*0.21*t))+0.09*sin(2*PI*164.81*t)+0.055*sin(2*PI*220*t)"
    else:
        expression = "0.15*sin(2*PI*98*t)*(1+0.12*sin(2*PI*0.17*t))+0.08*sin(2*PI*146.83*t)+0.05*sin(2*PI*196*t)"
    return _run_atomic_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={expression}|{expression}:d={_number(duration)}:s=48000:c=stereo",
            "-af",
            (
                f"highpass=f=55,lowpass=f=5200,afade=t=in:d=0.35,"
                f"afade=t=out:st={_number(duration - 0.65)}:d=0.65,"
                "loudnorm=I=-16:LRA=7:TP=-1.5"
            ),
            *_ffmpeg_output_args(),
        ],
        output_path,
    )


def _render_music_preroll(music_path: Path, output_path: Path, *, duration_sec: float = 2.5) -> Path:
    """Expose the real tail-replay seam immediately before the intro crossfade."""
    return _run_atomic_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-sseof",
            f"-{_number(duration_sec)}",
            "-i",
            str(music_path),
            "-vn",
            *_ffmpeg_output_args(),
        ],
        output_path,
    )


def _render_identity_role_bed(
    signature_path: Path,
    tape_path: Path,
    output_path: Path,
    *,
    duration_sec: float,
    pad_hz: float,
) -> Path:
    """Play the selected contour once, then carry a quiet non-repeating tail."""
    return _run_atomic_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(signature_path),
            "-stream_loop",
            "-1",
            "-ss",
            "8.4",
            "-i",
            str(tape_path),
            "-f",
            "lavfi",
            "-i",
            (
                f"aevalsrc=0.035*sin(2*PI*{_number(pad_hz)}*t)*exp(-0.75*t)|"
                f"0.035*sin(2*PI*{_number(pad_hz)}*t)*exp(-0.75*t):"
                f"d={_number(duration_sec)}:s=48000:c=stereo"
            ),
            "-filter_complex",
            (
                "[0:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.0[sig];"
                f"[1:a]atrim=duration={_number(duration_sec)},asetpts=N/SR/TB,aresample=48000,"
                "aformat=channel_layouts=stereo,highpass=f=360,lowpass=f=6200,volume=-33dB,"
                f"afade=t=out:st={_number(duration_sec - 0.35)}:d=0.35[tape];"
                f"[2:a]adelay=720|720,volume=-12dB,afade=t=out:st={_number(duration_sec - 0.45)}:d=0.45[pad];"
                "[sig][tape][pad]amix=inputs=3:duration=longest:normalize=0,"
                f"apad=whole_dur={_number(duration_sec)},atrim=duration={_number(duration_sec)},"
                "highpass=f=45,lowpass=f=15000,loudnorm=I=-16:LRA=5:TP=-1.5[out]"
            ),
            "-map",
            "[out]",
            *_ffmpeg_output_args(),
        ],
        output_path,
    )


def _render_core_cue(cue: CoreAuditionCue, source_root: Path, output_root: Path) -> Path:
    return _render_motif_prototype(
        MotifPrototypeSpec(
            id=cue.id,
            label=cue.id,
            brief=cue.purpose,
            path=cue.path,
            duration_sec=cue.duration_sec,
            layers=cue.layers,
        ),
        source_root,
        output_root,
    )


def _render_generated_core_cue(cue: CoreGeneratedCue, output_root: Path) -> Path:
    output_path = output_root / cue.path
    fade_out_start = max(cue.duration_sec - cue.fade_out_sec, 0.0)
    rendered = _run_atomic_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (f"aevalsrc={cue.expression}|{cue.expression}:d={_number(cue.duration_sec)}:s=48000:c=stereo"),
            "-af",
            (
                f"highpass=f={cue.highpass_hz},lowpass=f={cue.lowpass_hz},"
                f"afade=t=in:d={_number(cue.fade_in_sec)},"
                f"afade=t=out:st={_number(fade_out_start)}:d={_number(cue.fade_out_sec)},"
                "loudnorm=I=-16:LRA=5:TP=-1.5"
            ),
            *_ffmpeg_output_args(),
        ],
        output_path,
    )
    with _runtime_loudness_targets():
        if not _reconcile_lufs(rendered):
            raise RuntimeError(f"Could not reconcile generated core cue loudness: {cue.id}")
    return rendered


def _render_representative_spot(
    voice_path: Path,
    tape_path: Path,
    output_path: Path,
) -> Path:
    duration = _probe_duration_sec(voice_path)
    raw_path = output_path.with_name(f".{output_path.stem}.raw.mp3")
    raw_path.unlink(missing_ok=True)
    try:
        _run_atomic_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(voice_path),
                "-stream_loop",
                "-1",
                "-ss",
                "8.4",
                "-i",
                str(tape_path),
                "-filter_complex",
                (
                    f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume=1.0[voice];"
                    f"[1:a]atrim=duration={_number(duration)},asetpts=N/SR/TB,aresample=48000,"
                    "aformat=channel_layouts=stereo,highpass=f=360,lowpass=f=6000,"
                    "volume=-30dB,afade=t=in:d=0.08,"
                    f"afade=t=out:st={_number(max(duration - 0.35, 0))}:d=0.35[bed];"
                    "[voice][bed]amix=inputs=2:duration=first:normalize=0[out]"
                ),
                "-map",
                "[out]",
                *_ffmpeg_output_args(),
            ],
            raw_path,
        )
        with _runtime_loudness_targets():
            return normalize_ad(raw_path, output_path)
    finally:
        raw_path.unlink(missing_ok=True)


def _render_dry_voice(voice_path: Path, output_path: Path) -> Path:
    return _run_atomic_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(voice_path),
            "-vn",
            "-af",
            "aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:LRA=11:TP=-1.5",
            *_ffmpeg_output_args(),
        ],
        output_path,
    )


def _concat_core_parts(paths: list[Path], output_path: Path, *, silence_ms: int) -> Path:
    if not paths:
        raise ValueError("core audition concat requires at least one input")
    return concat_files(paths, output_path, silence_ms=silence_ms, loudnorm=False)


def _core_layer_record(layer: MotifPrototypeLayer) -> dict[str, Any]:
    source = _motif_prototype_source_map()[layer.source_id]
    return {
        "source_id": layer.source_id,
        "source_sha256": source.source_sha256,
        "source_start_sec": layer.source_start_sec,
        "duration_sec": layer.duration_sec,
        "output_offset_sec": layer.output_offset_sec,
        "gain_db": layer.gain_db,
        "dsp": list(layer.dsp),
        "license": "CC0-1.0",
        "role": layer.role,
    }


def _generated_core_source_hash(cue: CoreGeneratedCue) -> str:
    payload = json.dumps(
        {
            "duration_sec": cue.duration_sec,
            "expression": cue.expression,
            "fade_in_sec": cue.fade_in_sec,
            "fade_out_sec": cue.fade_out_sec,
            "highpass_hz": cue.highpass_hz,
            "lowpass_hz": cue.lowpass_hz,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _generated_core_layer_record(cue: CoreGeneratedCue) -> dict[str, Any]:
    return {
        "source_id": cue.source_id,
        "source_sha256": _generated_core_source_hash(cue),
        "source_start_sec": 0.0,
        "duration_sec": cue.duration_sec,
        "output_offset_sec": 0.0,
        "gain_db": 0.0,
        "dsp": [
            f"highpass:{cue.highpass_hz}Hz",
            f"lowpass:{cue.lowpass_hz}Hz",
            f"fade:{round(cue.fade_in_sec * 1_000)}ms-in/{round(cue.fade_out_sec * 1_000)}ms-out",
            "loudnorm:I=-16,LRA=5,TP=-1.5",
            "runtime-reconcile:-16LUFS",
        ],
        "license": "audition-only-local-generation",
        "role": "foreground",
    }


def _component_source_id(path: Path) -> str:
    if path.stem == "program_music_successor":
        return "context.program-music-successor"
    return f"render.{path.stem.replace('_', '-')}"


def _component_records(paths: list[Path], *, gap_sec: float) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0.0
    for index, path in enumerate(paths):
        duration = _probe_duration_sec(path)
        records.append(
            {
                "source_id": _component_source_id(path),
                "source_sha256": _sha256(path),
                "source_start_sec": 0.0,
                "duration_sec": duration,
                "output_offset_sec": round(offset, 6),
                "gain_db": 0.0,
                "dsp": ["concat:unity", f"following-gap:{_number(gap_sec)}s" if index < len(paths) - 1 else "end"],
                "license": "audition-only-composite",
                "role": "component",
            }
        )
        offset += duration
        if index < len(paths) - 1:
            offset += gap_sec
    return records


def _preview_record(
    output_root: Path,
    *,
    preview_id: str,
    label: str,
    path: str,
    purpose: str,
    layers: list[dict[str, Any]],
    mastering_dsp: list[str],
) -> dict[str, Any]:
    audio_path = output_root / path
    return {
        "id": preview_id,
        "label": label,
        "path": path,
        "purpose": purpose,
        "sha256": _sha256(audio_path),
        "duration_sec": _probe_duration_sec(audio_path),
        "format": FORMAT,
        "layers": layers,
        "mastering_dsp": mastering_dsp,
    }


def _core_pack_digest(previews: list[dict[str, Any]], *, component_ledger_digest: str) -> str:
    digest = hashlib.sha256()
    for preview in sorted(previews, key=lambda item: str(item["path"])):
        digest.update(str(preview["path"]).encode())
        digest.update(b"\0")
        digest.update(str(preview["sha256"]).encode("ascii"))
        digest.update(b"\n")
    digest.update(b"component-ledger\0")
    digest.update(component_ledger_digest.encode("ascii"))
    digest.update(b"\n")
    return digest.hexdigest()


def _core_audition_manifest(
    output_root: Path,
    *,
    selected_motif: MotifPrototypeSpec,
    speech_sources: list[dict[str, Any]],
    generated_at: str,
    render_paths: dict[str, Path],
    ad_break_parts: list[Path],
    cadence_parts: list[Path],
) -> dict[str, Any]:
    prototype_sources = _motif_prototype_source_map()
    selected_signature_path = render_paths["selected_signature"]
    selected_signature_source = {
        "id": f"selected-motif.{selected_motif.id}",
        "license": "CC0-1.0 audition derivative",
        "creator": "Mamma Mi Radio audition tooling",
        "title": f"Selected {selected_motif.label} contour",
        "artifact_path": selected_signature_path.relative_to(output_root).as_posix(),
        "source_sha256": _sha256(selected_signature_path),
        "selection_candidate_sha256": MOTIF_PROTOTYPE_SHA256[selected_motif.id],
        "selection_pack_digest": MOTIF_PROTOTYPE_PACK_DIGEST,
        "layers": [_core_layer_record(layer) for layer in selected_motif.layers],
        "tags": ["selected-motif", selected_motif.id, "audition-only"],
    }
    source_records: list[dict[str, Any]] = [
        {
            "id": source.id,
            "license": "CC0-1.0",
            "source_url": source.source_url,
            "artifact_url": source.artifact_url,
            "source_sha256": source.source_sha256,
            "creator": source.creator,
            "title": source.title,
            "provenance": source.provenance,
            "artifact_kind": "Freesound HQ preview derivative; cannot ship as a final master",
            "tags": list(source.tags),
        }
        for source in MOTIF_PROTOTYPE_SOURCES
    ]
    source_records.extend(speech_sources)
    source_records.append(selected_signature_source)
    for role, frequency, duration in (("station", 110.0, 3.0), ("sweeper", 146.83, 2.0)):
        expression = f"0.035*sin(2*PI*{_number(frequency)}*t)*exp(-0.75*t)"
        source_records.append(
            {
                "id": f"generated.identity-{role}-tail-pad",
                "license": "audition-only-local-generation",
                "creator": "Mamma Mi Radio audition tooling",
                "title": f"Non-repeating {role} identity tail pad",
                "source_sha256": hashlib.sha256(expression.encode()).hexdigest(),
                "generation": {
                    "engine": "FFmpeg aevalsrc",
                    "expression": expression,
                    "duration_sec": duration,
                    "sample_rate_hz": 48_000,
                    "channels": 2,
                },
                "tags": ["identity-tail", "procedural-pad", "audition-only"],
            }
        )
    for cue in CORE_AUDITION_TRANSITIONS:
        source_records.append(
            {
                "id": cue.source_id,
                "license": "audition-only-local-generation",
                "creator": "Mamma Mi Radio audition tooling",
                "title": cue.purpose,
                "source_sha256": _generated_core_source_hash(cue),
                "generation": {
                    "engine": "FFmpeg aevalsrc",
                    "expression": cue.expression,
                    "duration_sec": cue.duration_sec,
                    "sample_rate_hz": 48_000,
                    "channels": 2,
                },
                "tags": ["transition", "electronic", "procedural", "audition-only"],
            }
        )
    for source_id, path, title, expression in (
        (
            "context.program-music-tail",
            render_paths["program_music_tail"],
            "Synthetic program-music context used to expose the dry music-to-speech fallback",
            "98Hz + 146.83Hz + 196Hz slow-modulated pad",
        ),
        (
            "context.program-music-successor",
            render_paths["program_music_successor"],
            "Synthetic music successor used only to hear the return to programming",
            "110Hz + 164.81Hz + 220Hz slow-modulated pad",
        ),
    ):
        source_records.append(
            {
                "id": source_id,
                "license": "audition-only-local-generation",
                "creator": "Mamma Mi Radio audition tooling",
                "title": title,
                "artifact_path": path.relative_to(output_root).as_posix(),
                "source_sha256": _sha256(path),
                "generation": {
                    "engine": "FFmpeg aevalsrc",
                    "expression_summary": expression,
                    "mastering": "48 kHz stereo MP3, loudnorm I=-16 LRA=7 TP=-1.5",
                },
                "tags": ["context-only", "synthetic-program-music", "not-pack-material"],
            }
        )

    speech_by_id = {str(record["id"]): record for record in speech_sources}

    def speech_layer(source_id: str, *, offset: float, gain_linear: float) -> dict[str, Any]:
        source = speech_by_id[source_id]
        return {
            "source_id": source_id,
            "source_sha256": source["source_sha256"],
            "source_start_sec": 0.0,
            "duration_sec": source["duration_sec"],
            "output_offset_sec": offset,
            "gain_linear": gain_linear,
            "gain_db": round(20 * math.log10(gain_linear), 6),
            "dsp": [f"delay:{round(offset * 1_000)}ms" if offset else "delay:0ms"],
            "license": source["license"],
            "role": "voice",
        }

    def identity_bed_layer(render_key: str) -> dict[str, Any]:
        path = render_paths[render_key]
        return {
            "source_id": f"render.{render_key.replace('_', '-')}",
            "source_sha256": _sha256(path),
            "source_start_sec": 0.0,
            "duration_sec": _probe_duration_sec(path),
            "output_offset_sec": 0.0,
            "gain_linear": 0.15,
            "gain_db": -16.478175,
            "dsp": ["runtime-mix_voice_with_sting:bed"],
            "license": "CC0-1.0 audition composite",
            "role": "foreground-signature",
        }

    station_layers = [
        identity_bed_layer("station_bed"),
        speech_layer("speech.station-id", offset=0.4, gain_linear=1.2),
    ]
    sweeper_layers = [
        identity_bed_layer("sweeper_bed"),
        speech_layer("speech.sweeper", offset=0.4, gain_linear=1.2),
    ]
    cue_map = _core_cue_map()
    transition_map = {cue.id: cue for cue in CORE_AUDITION_TRANSITIONS}
    ad_in_layers = [_core_layer_record(layer) for layer in cue_map["core.ad-in"].layers]
    ad_out_layers = [_core_layer_record(layer) for layer in cue_map["core.ad-out"].layers]

    previews = [
        _preview_record(
            output_root,
            preview_id="station-id",
            label="Station ID",
            path="station_id.mp3",
            purpose=f"{selected_motif.label} under the representative full station ident.",
            layers=station_layers,
            mastering_dsp=[
                "runtime topology: sting volume=0.15",
                "runtime topology: voice delay=400ms volume=1.2",
                "amix:duration=longest,dropout_transition=1",
                "loudnorm:I=-16,LRA=11,TP=-1.5",
            ],
        ),
        _preview_record(
            output_root,
            preview_id="sweeper",
            label="Sweeper",
            path="sweeper.mp3",
            purpose=f"{selected_motif.label} under a longer representative Italian sweeper.",
            layers=sweeper_layers,
            mastering_dsp=[
                "runtime topology: sting volume=0.15",
                "runtime topology: voice delay=400ms volume=1.2",
                "amix:duration=longest,dropout_transition=1",
                "loudnorm:I=-16,LRA=11,TP=-1.5",
            ],
        ),
        _preview_record(
            output_root,
            preview_id="ad-in",
            label="Ad in",
            path="ad_in.mp3",
            purpose=cue_map["core.ad-in"].purpose,
            layers=ad_in_layers,
            mastering_dsp=["highpass:45Hz", "lowpass:15000Hz", "loudnorm:I=-16,LRA=5,TP=-1.5"],
        ),
        _preview_record(
            output_root,
            preview_id="ad-out",
            label="Ad out",
            path="ad_out.mp3",
            purpose=cue_map["core.ad-out"].purpose,
            layers=ad_out_layers,
            mastering_dsp=["highpass:45Hz", "lowpass:15000Hz", "loudnorm:I=-16,LRA=5,TP=-1.5"],
        ),
        _preview_record(
            output_root,
            preview_id="full-cadence",
            label="Full music → spot → music cadence",
            path="full_cadence.mp3",
            purpose="One runtime-order break with representative Italian speech and a music successor.",
            layers=_component_records(cadence_parts, gap_sec=0.0),
            mastering_dsp=[
                "pre-roll: last 2.5s of predecessor music",
                "music-to-speech cue: dry fallback because no adjacent tail file is available",
                "intro: dry representative voice at the production speech target",
                "two spots: one optional mid-break cue is exposed exactly once",
                "ad break: 300ms silence between every part, no final loudnorm",
                "speech-to-music cue: unity concat with no gap before music successor",
                "final cadence: unity concat with no additional loudnorm",
            ],
        ),
    ]
    internal_renders = [
        {
            "id": "render.station-bed",
            "path": render_paths["station_bed"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["station_bed"]),
            "layers": [
                {
                    "source_id": selected_signature_source["id"],
                    "source_sha256": selected_signature_source["source_sha256"],
                    "source_start_sec": 0.0,
                    "duration_sec": _probe_duration_sec(selected_signature_path),
                    "output_offset_sec": 0.0,
                    "gain_db": 0.0,
                    "dsp": ["play-once", "no-contour-repeat"],
                    "license": selected_signature_source["license"],
                    "role": "foreground-signature",
                },
                {
                    "source_id": "tape-trp-hq-preview",
                    "source_sha256": prototype_sources["tape-trp-hq-preview"].source_sha256,
                    "source_start_sec": 8.4,
                    "duration_sec": 3.0,
                    "output_offset_sec": 0.0,
                    "gain_db": -33.0,
                    "dsp": ["highpass:360Hz", "lowpass:6200Hz", "fade-out:350ms"],
                    "license": "CC0-1.0",
                    "role": "texture",
                },
                {
                    "source_id": "generated.identity-station-tail-pad",
                    "source_sha256": next(
                        source["source_sha256"]
                        for source in source_records
                        if source["id"] == "generated.identity-station-tail-pad"
                    ),
                    "source_start_sec": 0.0,
                    "duration_sec": 2.28,
                    "output_offset_sec": 0.72,
                    "gain_db": -12.0,
                    "dsp": ["aevalsrc:0.035*sin(2*PI*110*t)*exp(-0.75*t)", "fade-out:450ms"],
                    "license": "audition-only-local-generation",
                    "role": "texture",
                },
            ],
        },
        {
            "id": "render.sweeper-bed",
            "path": render_paths["sweeper_bed"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["sweeper_bed"]),
            "layers": [
                {
                    "source_id": selected_signature_source["id"],
                    "source_sha256": selected_signature_source["source_sha256"],
                    "source_start_sec": 0.0,
                    "duration_sec": _probe_duration_sec(selected_signature_path),
                    "output_offset_sec": 0.0,
                    "gain_db": 0.0,
                    "dsp": ["play-once", "no-contour-repeat"],
                    "license": selected_signature_source["license"],
                    "role": "foreground-signature",
                },
                {
                    "source_id": "tape-trp-hq-preview",
                    "source_sha256": prototype_sources["tape-trp-hq-preview"].source_sha256,
                    "source_start_sec": 8.4,
                    "duration_sec": 2.0,
                    "output_offset_sec": 0.0,
                    "gain_db": -33.0,
                    "dsp": ["highpass:360Hz", "lowpass:6200Hz", "fade-out:350ms"],
                    "license": "CC0-1.0",
                    "role": "texture",
                },
                {
                    "source_id": "generated.identity-sweeper-tail-pad",
                    "source_sha256": next(
                        source["source_sha256"]
                        for source in source_records
                        if source["id"] == "generated.identity-sweeper-tail-pad"
                    ),
                    "source_start_sec": 0.0,
                    "duration_sec": 1.28,
                    "output_offset_sec": 0.72,
                    "gain_db": -12.0,
                    "dsp": ["aevalsrc:0.035*sin(2*PI*146.83*t)*exp(-0.75*t)", "fade-out:450ms"],
                    "license": "audition-only-local-generation",
                    "role": "texture",
                },
            ],
        },
        {
            "id": "render.ad-in",
            "path": render_paths["ad_in"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["ad_in"]),
            "layers": [_core_layer_record(layer) for layer in cue_map["core.ad-in"].layers],
            "usage": "Role-specific entry bumper used once at the start of an advertising break.",
        },
        {
            "id": "render.ad-mid",
            "path": render_paths["ad_mid"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["ad_mid"]),
            "authored_source_path": render_paths["ad_mid_source"].relative_to(output_root).as_posix(),
            "authored_source_sha256": _sha256(render_paths["ad_mid_source"]),
            "layers": [_core_layer_record(layer) for layer in cue_map["core.ad-mid"].layers],
            "runtime_shape": ["fit_audio_oneshot", "duration:0.8s", "target:-16LUFS", "fade-out:80ms"],
            "usage": "Optional once after spot one in a multi-spot break; exposed once in this board cadence.",
        },
        {
            "id": "render.ad-out",
            "path": render_paths["ad_out"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["ad_out"]),
            "layers": [_core_layer_record(layer) for layer in cue_map["core.ad-out"].layers],
            "usage": "Role-specific exit bumper used once at the end of an advertising break.",
        },
        {
            "id": "render.music-to-speech",
            "path": render_paths["music_to_speech"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["music_to_speech"]),
            "layers": [_generated_core_layer_record(transition_map["core.music-to-speech"])],
            "usage": "Dry-intro fallback exposed once in the board cadence because no adjacent tail file is available.",
        },
        {
            "id": "render.speech-to-music",
            "path": render_paths["speech_to_music"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["speech_to_music"]),
            "layers": [_generated_core_layer_record(transition_map["core.speech-to-music"])],
            "usage": "Prepended at unity and with no gap to the music successor.",
        },
        {
            "id": "render.program-music-preroll",
            "path": render_paths["program_music_preroll"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["program_music_preroll"]),
            "layers": [
                {
                    "source_id": "context.program-music-tail",
                    "source_sha256": _sha256(render_paths["program_music_tail"]),
                    "source_start_sec": max(_probe_duration_sec(render_paths["program_music_tail"]) - 2.5, 0.0),
                    "duration_sec": min(2.5, _probe_duration_sec(render_paths["program_music_tail"])),
                    "output_offset_sec": 0.0,
                    "gain_db": 0.0,
                    "dsp": ["last-2.5s", "exposes-tail-replay-seam"],
                    "license": "audition-only-local-generation",
                    "role": "music-context",
                }
            ],
        },
        {
            "id": "render.ad-intro",
            "path": render_paths["ad_intro"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["ad_intro"]),
            "layers": [speech_layer("speech.ad-intro", offset=0.0, gain_linear=1.0)],
            "mastering_dsp": ["aresample:48000", "stereo", "loudnorm:I=-16,LRA=11,TP=-1.5"],
            "usage": "Dry-intro fallback used when no adjacent predecessor tail can be opened.",
        },
        {
            "id": "render.representative-spot",
            "path": render_paths["spot"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["spot"]),
            "layers": [
                speech_layer("speech.spot", offset=0.0, gain_linear=1.0),
                {
                    "source_id": "tape-trp-hq-preview",
                    "source_sha256": prototype_sources["tape-trp-hq-preview"].source_sha256,
                    "source_start_sec": 8.4,
                    "duration_sec": speech_by_id["speech.spot"]["duration_sec"],
                    "output_offset_sec": 0.0,
                    "gain_db": -30.0,
                    "dsp": ["loop-if-needed", "highpass:360Hz", "lowpass:6000Hz"],
                    "license": "CC0-1.0",
                    "role": "texture",
                },
            ],
        },
        {
            "id": "render.representative-spot-two",
            "path": render_paths["spot_two"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["spot_two"]),
            "layers": [
                speech_layer("speech.spot-two", offset=0.0, gain_linear=1.0),
                {
                    "source_id": "tape-trp-hq-preview",
                    "source_sha256": prototype_sources["tape-trp-hq-preview"].source_sha256,
                    "source_start_sec": 8.4,
                    "duration_sec": speech_by_id["speech.spot-two"]["duration_sec"],
                    "output_offset_sec": 0.0,
                    "gain_db": -30.0,
                    "dsp": ["loop-if-needed", "highpass:360Hz", "lowpass:6000Hz"],
                    "license": "CC0-1.0",
                    "role": "texture",
                },
            ],
        },
        {
            "id": "render.ad-outro",
            "path": render_paths["ad_outro"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["ad_outro"]),
            "layers": [speech_layer("speech.ad-outro", offset=0.0, gain_linear=1.0)],
            "mastering_dsp": ["aresample:48000", "stereo", "loudnorm:I=-16,LRA=11,TP=-1.5"],
            "usage": "Representative host outro immediately before the speech-to-music cue.",
        },
        {
            "id": "render.ad-break",
            "path": render_paths["ad_break"].relative_to(output_root).as_posix(),
            "sha256": _sha256(render_paths["ad_break"]),
            "layers": _component_records(ad_break_parts, gap_sec=0.3),
            "usage": "Audition break: dry intro, ad-in, spot one, ad-mid, spot two, ad-out, outro.",
        },
    ]
    component_ledger_digest = hashlib.sha256(
        json.dumps(
            {"sources": source_records, "internal_renders": internal_renders},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    previews[-1]["component_ledger_digest"] = component_ledger_digest
    pack_digest = _core_pack_digest(previews, component_ledger_digest=component_ledger_digest)
    return {
        "schema_version": 1,
        "pack": "Modern Night Drive core audition",
        "stage": "core-cadence",
        "generated_at": generated_at,
        "selected_motif_id": selected_motif.id,
        "selected_motif_label": selected_motif.label,
        "motif_selection": {
            "schema_version": 1,
            "status": "selected",
            "candidate_id": selected_motif.id,
            "prototype_pack_digest": MOTIF_PROTOTYPE_PACK_DIGEST,
            "candidate_sha256": MOTIF_PROTOTYPE_SHA256[selected_motif.id],
            "recorded_at": generated_at,
        },
        "sources": source_records,
        "previews": previews,
        "internal_renders": internal_renders,
        "component_ledger_digest": component_ledger_digest,
        "runtime_contract": {
            "station_voice_mix": {"sting_gain_linear": 0.15, "voice_gain_linear": 1.2, "voice_delay_ms": 400},
            "dry_music_intro": {
                "board_preroll_sec": 2.5,
                "music_to_speech_gain_db": 0.0,
                "voice_gain_db": 0.0,
                "music_to_speech_suppressed": False,
            },
            "adjacent_music_intro_preserved": {
                "tail_sec": 8.0,
                "music_gain_linear": 0.5,
                "voice_gain_linear": 1.0,
                "voice_delay_ms": 150,
                "music_to_speech_suppressed": True,
                "board_usage": "covered by regression tests; dry fallback chosen for the audible board",
            },
            "ad_break": {"gap_ms": 300, "final_loudnorm": False, "max_mid_bumpers": 1},
            "music_successor": {"speech_to_music_gain_db": 0.0, "gap_ms": 0},
        },
        "quality_guard_allowlist": {
            "perceptual_overlaps": [
                {
                    "asset_ids": ["station-id", "sweeper"],
                    "reason": (
                        f"Both identity roles intentionally state {selected_motif.label} once; "
                        "their 3s/2s tails diverge and the contour appears nowhere else."
                    ),
                }
            ],
            "source_core_roles": [],
        },
        "pack_digest": pack_digest,
        "release_ready": False,
        "listening_receipt": {
            "status": "pending",
            "pack_digest": None,
            "approved_candidate_id": None,
            "surfaces": [],
            "device_classes": [],
            "reviewed_at": None,
        },
        "limitations": [
            "This is an audition board, not a shippable asset pack.",
            "Freesound HQ preview derivatives cannot be promoted into the final pack.",
            "The selected motif direction has no recorded Mac or target-speaker approval yet.",
            "Synthetic program music and local representative speech are context only.",
            "No trumpet or mandolin material is used.",
        ],
    }


def _core_audition_readme(manifest: dict[str, Any]) -> str:
    preview_lines = [
        f"- [{preview['label']}](./{preview['path']}) — {preview['purpose']}" for preview in manifest["previews"]
    ]
    return "\n".join(
        [
            "# Modern Night Drive — core cadence approval",
            "",
            "[Open the listening board](./index.html)",
            "",
            (
                f"Selected direction: {manifest['selected_motif_label']} "
                f"(`{manifest['selected_motif_id']}`). This records a direction choice only; "
            ),
            "Mac and Wohnzimmer Sonos Arc approval are still pending.",
            "",
            "## Five previews",
            "",
            *preview_lines,
            "",
            "## Procedure",
            "",
            "1. Keep Mac volume fixed. Play Station ID and Sweeper three times each.",
            (
                f"2. Confirm the {manifest['selected_motif_label']} contour stays restrained and is used only "
                "on those two identity surfaces."
            ),
            (
                "3. Play Ad in, Ad out, then the full cadence twice. Listen for distinct entry/exit cues, "
                "clear Italian speech, and no trumpet or mandolin character."
            ),
            "4. Replay all five previews at the same perceived level on the Wohnzimmer Sonos Arc.",
            "5. Reply exactly:",
            "",
            "```text",
            "Core gate: pass|fail",
            "Mac: pass|fail",
            "Sonos Arc: pass|fail",
            "Notes: <short reason>",
            "```",
            "",
            (
                "A failed result returns only this core stage for revision. "
                "Compatibility sounds and advertising recipes remain unbuilt."
            ),
            "",
            "## Runtime fidelity",
            "",
            (
                "- Station ID and sweeper use the production `mix_voice_with_sting` gains: "
                "sting 0.15, voice 1.2, voice delayed 400 ms."
            ),
            (
                "- The full cadence uses the production dry fallback: predecessor-music pre-roll, the "
                "music-to-speech cue, dry intro, two spots with one mid cue, ad-out, outro, then the "
                "speech-to-music cue and music successor. This exposes every core cue exactly once."
            ),
            (
                "- Break parts have 300 ms gaps and no final loudness pass. The normal adjacent-tail "
                "crossfade remains covered by regression tests; speech-to-music is prepended at unity with "
                "no gap to the music successor."
            ),
            "",
            f"Pack digest: `{manifest['pack_digest']}`",
            "",
            (
                "See `manifest.json` for every source hash, excerpt, offset, gain, DSP operation, license, "
                "and selection-lineage field."
            ),
            "",
        ]
    )


def _core_audition_html(manifest: dict[str, Any]) -> str:
    cards = []
    for preview in manifest["previews"]:
        cards.append(
            "\n".join(
                [
                    '<article class="card">',
                    f"<h2>{preview['label']}</h2>",
                    f"<p>{preview['purpose']}</p>",
                    f'<audio controls preload="metadata" src="{preview["path"]}"></audio>',
                    "</article>",
                ]
            )
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Modern Night Drive core cadence approval</title>
<style>
:root {{ color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
body {{ margin: 0 auto; max-width: 880px; padding: 32px 20px 64px; background: #111318; color: #f4efe7; }}
h1 {{ letter-spacing: -0.03em; }}
.notice {{ padding: 14px 16px; border: 1px solid #7b6b50; border-radius: 12px; background: #211e19; }}
.grid {{ display: grid; gap: 16px; margin-top: 24px; }}
.card {{ padding: 18px; border: 1px solid #343844; border-radius: 14px; background: #191c23; }}
.card h2 {{ margin-top: 0; }}
audio {{ width: 100%; }}
code {{ color: #efc27a; }}
</style>
</head>
<body>
<h1>Modern Night Drive — core cadence approval</h1>
<p class="notice"><strong>Audition only.</strong> {manifest["selected_motif_label"]} is direction-selected,
but no Mac or Wohnzimmer Sonos Arc pass is recorded. Nothing here is release-ready.</p>
<p>Keep volume fixed. Hear the two identity surfaces three times, then Ad in, Ad out,
and the full cadence twice. The cadence exposes every bumper and boundary transition
once. Replay all five on the target speaker.</p>
<main class="grid">
{"".join(cards)}
</main>
<p>Reply: <code>Core gate: pass|fail; Mac: pass|fail; Sonos Arc: pass|fail; Notes: ...</code></p>
<p>Pack digest: <code>{manifest["pack_digest"]}</code></p>
</body>
</html>
"""


def render_core_audition(
    source_root: Path,
    output_root: Path,
    *,
    selected_motif_id: str,
    selected_motif_artifact: Path,
    voice_source: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Render the five-surface Stage 2 board and publish it atomically."""
    verify_motif_prototype_sources(source_root)
    selected_motif = _selected_motif(selected_motif_id)
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Refusing to overwrite non-empty core audition directory: {output_root}")
    if not selected_motif_artifact.is_file():
        raise ValueError(f"Selected motif artifact does not exist: {selected_motif_artifact}")
    selected_signature_sha256 = _sha256(selected_motif_artifact)
    expected_signature_sha256 = MOTIF_PROTOTYPE_SHA256[selected_motif.id]
    if selected_signature_sha256 != expected_signature_sha256:
        raise ValueError(
            f"Selected motif artifact does not match selection lineage: {selected_signature_sha256} "
            f"!= {expected_signature_sha256}"
        )

    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
    try:
        manifest = _render_core_audition_in_place(
            source_root,
            staging_root,
            selected_motif_id=selected_motif_id,
            selected_motif_artifact=selected_motif_artifact,
            voice_source=voice_source,
            generated_at=generated_at,
        )
        if output_root.exists():
            output_root.rmdir()
        os.replace(staging_root, output_root)
        return manifest
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _render_core_audition_in_place(
    source_root: Path,
    output_root: Path,
    *,
    selected_motif_id: str,
    selected_motif_artifact: Path,
    voice_source: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Render one validated core board into a private staging directory."""
    selected_motif = _selected_motif(selected_motif_id)
    output_root.mkdir(parents=True, exist_ok=True)
    source_output_root = output_root / "sources"
    component_root = output_root / "components"
    source_output_root.mkdir()
    component_root.mkdir()

    selected_signature_path = source_output_root / selected_motif.path
    shutil.copyfile(selected_motif_artifact, selected_signature_path)
    tape_path = source_root / _motif_prototype_source_map()["tape-trp-hq-preview"].filename
    station_bed_path = _render_identity_role_bed(
        selected_signature_path,
        tape_path,
        component_root / "station_bed.mp3",
        duration_sec=3.0,
        pad_hz=110.0,
    )
    sweeper_bed_path = _render_identity_role_bed(
        selected_signature_path,
        tape_path,
        component_root / "sweeper_bed.mp3",
        duration_sec=2.0,
        pad_hz=146.83,
    )
    speech_records: list[dict[str, Any]] = []
    speech_paths: dict[str, Path] = {}
    for speech in CORE_AUDITION_SPEECH:
        speech_path = source_output_root / speech.filename
        speech_paths[speech.id] = speech_path
        speech_records.append(_render_core_speech_source(speech, speech_path, voice_source=voice_source))

    station_id_path = output_root / "station_id.mp3"
    sweeper_path = output_root / "sweeper.mp3"
    _render_runtime_voice_sting_mix(speech_paths["speech.station-id"], station_bed_path, station_id_path)
    _render_runtime_voice_sting_mix(speech_paths["speech.sweeper"], sweeper_bed_path, sweeper_path)

    cue_paths: dict[str, Path] = {}
    ad_mid_source_path: Path | None = None
    for cue in CORE_AUDITION_CUES:
        if cue.id == "core.ad-mid":
            raw_root = component_root / "raw"
            ad_mid_source_path = _render_core_cue(cue, source_root, raw_root)
            _require_rendered_duration(
                ad_mid_source_path,
                minimum_sec=cue.duration_sec - 0.05,
                render_id=cue.id,
            )
            cue_paths[cue.id] = fit_audio_oneshot(
                ad_mid_source_path,
                component_root / "ad_mid.mp3",
                0.8,
                target_lufs=-16.0,
                fade_out_sec=0.08,
            )
            continue
        cue_root = output_root if cue.id in {"core.ad-in", "core.ad-out"} else component_root
        cue_paths[cue.id] = _render_core_cue(cue, source_root, cue_root)
    if ad_mid_source_path is None:  # pragma: no cover - constant inventory invariant
        raise RuntimeError("core ad-mid cue is missing")
    for transition in CORE_AUDITION_TRANSITIONS:
        cue_paths[transition.id] = _render_generated_core_cue(transition, component_root)

    program_music_tail = _render_context_music(component_root / "program_music_tail.mp3", successor=False)
    program_music_preroll = _render_music_preroll(program_music_tail, component_root / "program_music_preroll.mp3")
    program_music_successor = _render_context_music(component_root / "program_music_successor.mp3", successor=True)
    intro_path = _render_dry_voice(speech_paths["speech.ad-intro"], component_root / "ad_intro.mp3")
    spot_path = _render_representative_spot(
        speech_paths["speech.spot"],
        tape_path,
        component_root / "representative_spot.mp3",
    )
    spot_two_path = _render_representative_spot(
        speech_paths["speech.spot-two"],
        tape_path,
        component_root / "representative_spot_two.mp3",
    )
    outro_path = _render_dry_voice(speech_paths["speech.ad-outro"], component_root / "ad_outro.mp3")
    break_parts = core_ad_break_parts(
        intro_path,
        [spot_path, spot_two_path],
        cue_paths["core.ad-in"],
        cue_paths["core.ad-out"],
        outro_path,
        ad_mid_path=cue_paths["core.ad-mid"],
    )
    ad_break_path = _concat_core_parts(break_parts, component_root / "ad_break.mp3", silence_ms=300)
    cadence_sequence = core_cadence_parts(
        ad_break_path,
        program_music_successor,
        successor_kind="music",
        music_to_speech_path=cue_paths["core.music-to-speech"],
        speech_to_music_path=cue_paths["core.speech-to-music"],
        predecessor_music_path=program_music_preroll,
        predecessor_has_music_tail=False,
    )
    full_cadence_path = _concat_core_parts(cadence_sequence, output_root / "full_cadence.mp3", silence_ms=0)

    render_paths = {
        "selected_signature": selected_signature_path,
        "station_bed": station_bed_path,
        "sweeper_bed": sweeper_bed_path,
        "ad_in": cue_paths["core.ad-in"],
        "ad_mid": cue_paths["core.ad-mid"],
        "ad_mid_source": ad_mid_source_path,
        "ad_out": cue_paths["core.ad-out"],
        "music_to_speech": cue_paths["core.music-to-speech"],
        "speech_to_music": cue_paths["core.speech-to-music"],
        "program_music_tail": program_music_tail,
        "program_music_preroll": program_music_preroll,
        "program_music_successor": program_music_successor,
        "ad_intro": intro_path,
        "spot": spot_path,
        "spot_two": spot_two_path,
        "ad_outro": outro_path,
        "ad_break": ad_break_path,
        "full_cadence": full_cadence_path,
    }
    stamp = generated_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest = _core_audition_manifest(
        output_root,
        selected_motif=selected_motif,
        speech_sources=speech_records,
        generated_at=stamp,
        render_paths=render_paths,
        ad_break_parts=break_parts,
        cadence_parts=cadence_sequence,
    )
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "README.md").write_text(_core_audition_readme(manifest), encoding="utf-8")
    (output_root / "index.html").write_text(_core_audition_html(manifest), encoding="utf-8")
    return manifest


def _render_asset(spec: AssetSpec, source_root: Path, output_root: Path) -> None:
    """Reject direct rendering through the archived pack definition."""
    del spec, source_root, output_root
    raise RuntimeError(REJECTED_REBUILD_MESSAGE)


def _manifest(output_root: Path) -> dict[str, object]:
    """Reject manifest creation through the archived pack definition."""
    del output_root
    raise RuntimeError(REJECTED_REBUILD_MESSAGE)


def build(source_root: Path, output_root: Path) -> None:
    """Reject the retired Recorded Night Drive materialization route."""
    del source_root, output_root
    raise RuntimeError(REJECTED_REBUILD_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Directory holding the hash-pinned HQ derivatives used by audition modes",
    )
    parser.add_argument("--output-root", type=Path, help="Rendered pack or motif-prototype directory")
    parser.add_argument(
        "--verify-sources",
        action="store_true",
        help="Verify audition-lineage HQ derivatives without rendering",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prototype-motifs",
        action="store_true",
        help="Render only the three audition candidates; these HQ derivatives can never become the public pack",
    )
    mode.add_argument(
        "--core-audition",
        action="store_true",
        help="Render the five-surface Stage 2 board; never writes the shipped pack",
    )
    parser.add_argument(
        "--selected-motif",
        choices=tuple(candidate.id for candidate in MOTIF_PROTOTYPES),
        help="Direction chosen at the motif gate (required with --core-audition)",
    )
    parser.add_argument(
        "--selected-motif-artifact",
        type=Path,
        help="Exact listened candidate MP3 (required with --core-audition; SHA-bound to motif selection)",
    )
    parser.add_argument(
        "--voice-source",
        type=Path,
        help="Optional local representative spot voice; other declared lines use macOS say locally",
    )
    args = parser.parse_args(argv)
    try:
        if not args.prototype_motifs and not args.core_audition:
            raise ValueError(REJECTED_REBUILD_MESSAGE)
        default_output_root = (
            DEFAULT_PROTOTYPE_OUTPUT_ROOT if args.prototype_motifs else DEFAULT_CORE_AUDITION_OUTPUT_ROOT
        )
        output_root = args.output_root or default_output_root
        if args.core_audition:
            if args.selected_motif is None:
                raise ValueError("--selected-motif is required with --core-audition")
            if args.selected_motif_artifact is None and not args.verify_sources:
                raise ValueError("--selected-motif-artifact is required with --core-audition")
            verify_motif_prototype_sources(args.source_dir)
            if args.verify_sources:
                print(f"Core audition sources OK: {len(MOTIF_PROTOTYPE_SOURCES)}")
                return 0
            manifest = render_core_audition(
                args.source_dir,
                output_root,
                selected_motif_id=args.selected_motif,
                selected_motif_artifact=args.selected_motif_artifact,
                voice_source=args.voice_source,
            )
            print(f"Built Modern Night Drive core audition: {output_root} ({len(manifest['previews'])} previews)")
            return 0
        if args.prototype_motifs:
            verify_motif_prototype_sources(args.source_dir)
            if args.verify_sources:
                print(f"Motif prototype sources OK: {len(MOTIF_PROTOTYPE_SOURCES)}")
                return 0
            manifest = render_motif_prototypes(args.source_dir, output_root)
            print(
                f"Built Modern Night Drive motif prototypes: {output_root} ({len(manifest['candidates'])} candidates)"
            )
            return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable audition mode")


if __name__ == "__main__":
    raise SystemExit(main())
