#!/usr/bin/env python3
"""Build the redistributable recorded imaging pack from verified CC0 masters.

The public add-on ships the rendered MP3 clips, not the large source masters.
This script is intentionally offline: a curator supplies a local directory of
the reviewed masters, their hashes are verified first, and FFmpeg makes only
the declared trims and mixes below.  It never downloads an asset or uses a
stock-library preview.

Usage:
    python scripts/build_public_imaging_pack.py --source-dir /path/to/masters
    python scripts/build_public_imaging_pack.py --source-dir /path/to/masters --output-root /tmp/imaging
    python scripts/build_public_imaging_pack.py --source-dir /path/to/masters --verify-sources
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
FORMAT = {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}


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
    """Fail closed when the curator points us at the wrong or altered master."""
    failures: list[str] = []
    for source in SOURCES:
        path = source_root / source.filename
        if not path.is_file():
            failures.append(f"missing {source.filename}")
            continue
        actual = _sha256(path)
        if actual != source.source_sha256:
            failures.append(f"SHA-256 mismatch for {source.filename}: {actual}")
    if failures:
        raise ValueError("Reviewed source masters are not intact:\n- " + "\n- ".join(failures))


def _number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _render_asset(spec: AssetSpec, source_root: Path, output_root: Path) -> None:
    output_path = output_root / spec.path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp.mp3")
    temporary_path.unlink(missing_ok=True)

    sources = _source_map()
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for layer in spec.layers:
        command.extend(["-i", str(source_root / sources[layer.source_id].filename)])

    filters: list[str] = []
    labels: list[str] = []
    for index, layer in enumerate(spec.layers):
        clip = (
            f"[{index}:a]atrim=start={_number(layer.start_sec)}:duration={_number(layer.duration_sec)},"
            "asetpts=N/SR/TB,aresample=48000,aformat=channel_layouts=stereo,"
            f"volume={_number(layer.gain_db)}dB"
        )
        if layer.fade:
            fade_in = min(0.018, layer.duration_sec / 6)
            fade_out = min(0.10, layer.duration_sec / 4)
            clip += (
                f",afade=t=in:st=0:d={_number(fade_in)}"
                f",afade=t=out:st={_number(max(layer.duration_sec - fade_out, 0))}:d={_number(fade_out)}"
            )
        if layer.offset_sec:
            delay_ms = round(layer.offset_sec * 1_000)
            clip += f",adelay={delay_ms}|{delay_ms}"
        label = f"clip{index}"
        filters.append(f"{clip}[{label}]")
        labels.append(f"[{label}]")

    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0,"
        f"apad=whole_dur={_number(spec.duration_sec)},atrim=duration={_number(spec.duration_sec)},"
        f"highpass=f=45,lowpass=f=16000,loudnorm=I={_number(spec.target_lufs)}:LRA=7:TP=-1.5,"
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


def _manifest(output_root: Path) -> dict[str, object]:
    source_ids_by_asset = {
        asset.id: tuple(dict.fromkeys(layer.source_id for layer in asset.layers)) for asset in ASSETS
    }
    return {
        "schema_version": 2,
        "pack": "Mamma Mi Radio — Recorded Night Drive",
        "provenance": "CC0 source recordings, clipped and mixed locally by scripts/build_public_imaging_pack.py.",
        "sources": [
            {
                "id": source.id,
                "license": "CC0-1.0",
                "source_url": source.source_url,
                "source_sha256": source.source_sha256,
                "creator": source.creator,
                "title": source.title,
                "modification": "Trimmed, gain-staged, and mixed into a Mamma Mi Radio station-imaging asset.",
            }
            for source in SOURCES
        ],
        "assets": [
            {
                "id": asset.id,
                "path": asset.path,
                "purpose": asset.purpose,
                "kind": asset.kind,
                "tags": list(asset.tags),
                "source_ids": list(source_ids_by_asset[asset.id]),
                "sha256": _sha256(output_root / asset.path),
                "format": FORMAT,
                "duration_target_sec": asset.duration_sec,
                "license": "CC0-1.0",
            }
            for asset in ASSETS
        ],
        "recipes": list(RECIPES),
    }


def build(source_root: Path, output_root: Path) -> None:
    verify_sources(source_root)
    for index, asset in enumerate(ASSETS, start=1):
        print(f"[{index:02d}/{len(ASSETS):02d}] {asset.path}")
        _render_asset(asset, source_root, output_root)
    manifest_path = output_root / "manifest.json"
    temporary_path = manifest_path.with_name(".manifest.tmp.json")
    try:
        temporary_path.write_text(
            json.dumps(_manifest(output_root), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary_path, manifest_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir", type=Path, required=True, help="Directory holding the verified, non-shipped masters"
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Rendered pack directory")
    parser.add_argument(
        "--verify-sources", action="store_true", help="Check the curated masters without writing assets"
    )
    args = parser.parse_args(argv)
    try:
        verify_sources(args.source_dir)
        if args.verify_sources:
            print(f"Reviewed CC0 masters OK: {len(SOURCES)}")
            return 0
        build(args.source_dir, args.output_root)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Built public recorded imaging pack: {args.output_root} ({len(ASSETS)} assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
