#!/usr/bin/env python3
"""Build the complete Modern Night Drive pack behind a digest-bound listening gate.

The builder is deliberately non-installing.  It consumes the exact approved
core-cadence board, writes an atomic candidate only below workspace ``tmp`` (or
the system temporary directory), and leaves ``mammamiradio/assets/imaging``
untouched.  A later promotion step may consume only a candidate whose complete
receipt is approved and current.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import importlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.normalizer import (
    loop_audio_bed,
    mix_oneshot_layers,
    mix_voice_with_bed,
    mix_with_bed,
    normalize_ad,
    probe_duration_sec,
)
from mammamiradio.hosts.ad_creative import OFFICIAL_SONIC_RECIPE_IDS

try:
    _core = importlib.import_module("scripts.core_cadence_gate")
    _validator = importlib.import_module("scripts.validate_audio_asset_pack")
except ModuleNotFoundError as exc:  # Direct ``python scripts/...`` invocation.
    if exc.name != "scripts":
        raise
    _core = importlib.import_module("core_cadence_gate")
    _validator = importlib.import_module("validate_audio_asset_pack")


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "sonic-brand-auditions"
DEFAULT_CORE_MANIFEST = DEFAULT_OUTPUT_ROOT / "core-cadence-gate-20260815T225748Z" / "manifest.json"
FORMAT = {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}
STAGE = "complete-audio-pack-selection"
SCHEMA_VERSION = 2
RECEIPT_SCHEMA_VERSION = 1
TARGET = "Mac + small speaker + Wohnzimmer Sonos Arc"
CANDIDATE_ID = "modern-night-drive-complete"
DECISION_FIELDS = {"content_digest", "status", "mac", "small_speaker", "sonos_arc", "notes"}
DECISION_RESULTS = {"pass", "fail"}
REVIEW_CLOCK_TOLERANCE_SEC = 5.0
TALK_BED_GAIN_DB = -18.0
RUNTIME_AUDIO_CONTRACT: dict[str, object] = {
    "sample_rate": 48_000,
    "channels": 2,
    "bitrate": 192,
    "lufs_target": -16.0,
    "ad_lufs_target": -15.0,
    "broadcast_chain": False,
    "bed_volume_db": TALK_BED_GAIN_DB,
    "imaging_assets_dir": "",
}
ASSET_TARGET_LUFS = -16.0
ASSET_TOLERANCE_LU = 2.0
PROGRAM_PREVIEW_TARGET_LUFS = -16.0
PROGRAM_PREVIEW_TOLERANCE_LU = 2.0
ADVERTISING_PREVIEW_TARGET_LUFS = -15.0
ADVERTISING_PREVIEW_TOLERANCE_LU = 1.0
TRUE_PEAK_CEILING_DBTP = -1.0
AUDIO_QUALITY_CONTRACT: dict[str, object] = {
    "measurement": "ffmpeg-loudnorm-input",
    "asset_target_lufs": ASSET_TARGET_LUFS,
    "asset_tolerance_lu": ASSET_TOLERANCE_LU,
    "program_preview_target_lufs": PROGRAM_PREVIEW_TARGET_LUFS,
    "program_preview_tolerance_lu": PROGRAM_PREVIEW_TOLERANCE_LU,
    "advertising_preview_target_lufs": ADVERTISING_PREVIEW_TARGET_LUFS,
    "advertising_preview_tolerance_lu": ADVERTISING_PREVIEW_TOLERANCE_LU,
    "true_peak_ceiling_dbtp": TRUE_PEAK_CEILING_DBTP,
}
LISTENING_PROMPT = (
    "Complete pack: pass|fail; Mac: pass|fail; Small speaker: pass|fail; Sonos Arc: pass|fail; Notes: <short reason>"
)
REQUIRED_SURFACES = (
    "station-id",
    "sweeper",
    "ad-in",
    "ad-out",
    "full-cadence",
    "time-check",
    "casa-notte-context",
    "compatibility-board",
    "advertising-board",
)
REQUIRED_DEVICE_CLASSES = ("mac", "small-speaker", "target-speaker", "sonos-arc")
RECIPE_IDS = (
    "cafe_testimonial",
    "stadium_win",
    "showroom_reveal",
    "bureaucracy_stamp",
    "motorway_pass",
    "late_night_hotline",
    "supermarket_dash",
    "pharmacy_whisper",
    "home_reveal",
)
EXPECTED_OFFICIAL_BRAND_CONTRACT: dict[str, tuple[str, str]] = {
    "Prezzoforte": ("supermarket", "supermarket_dash"),
    "Sacchettino": ("supermarket", "supermarket_dash"),
    "Borsello & Figli": ("supermarket", "supermarket_dash"),
    "Velocino": ("cars", "motorway_pass"),
    "Stradabella": ("cars", "motorway_pass"),
    "Motoretto": ("cars", "motorway_pass"),
    "TeleCuore": ("telecom", "late_night_hotline"),
    "Fibra Magica": ("telecom", "late_night_hotline"),
    "Prontissimo Mobile": ("telecom", "late_night_hotline"),
    "Bancone SpA": ("banking", "bureaucracy_stamp"),
    "Risparmiana": ("banking", "bureaucracy_stamp"),
    "Pastaforte": ("food", "cafe_testimonial"),
    "Cioccolozzo": ("food", "cafe_testimonial"),
    "Caffè Turbino": ("food", "cafe_testimonial"),
    "ModaBloc": ("fashion", "showroom_reveal"),
    "Dolorfin": ("pharma", "pharmacy_whisper"),
    "Capellissimo": ("pharma", "pharmacy_whisper"),
    "Intestix": ("pharma", "pharmacy_whisper"),
    "Negroni as a Service": ("tech", "late_night_hotline"),
    "Mausoleo del Presidentissimo": ("tourism", "bureaucracy_stamp"),
    "Spaghetti Carbonara su Stecco": ("food", "cafe_testimonial"),
    "Caffè Misterioso": ("food", "cafe_testimonial"),
    "Autolavaggio Supremo": ("services", "showroom_reveal"),
    "Profumo di Nonna": ("beauty", "home_reveal"),
    "Scarpe Volanti": ("fashion", "stadium_win"),
    "Gelato Infinito": ("food", "cafe_testimonial"),
    "Pizzeria del Futuro": ("food", "showroom_reveal"),
    "Assicurazione Fantasma": ("finance", "late_night_hotline"),
    "Palestra Domani": ("fitness", "stadium_win"),
    "Cravatte Titaniche": ("fashion", "showroom_reveal"),
    "Clinica Dentale Sorriso Forzato": ("health", "pharmacy_whisper"),
    "Vino Quantistico": ("food", "cafe_testimonial"),
    "Taxi Poetico": ("services", "motorway_pass"),
    "Notaio Espresso": ("services", "bureaucracy_stamp"),
    "Sveglia Vendicativa": ("tech", "showroom_reveal"),
    "Ascensore Sentimentale": ("home", "home_reveal"),
    "Tappeto Testimone": ("home", "home_reveal"),
    "Gelateria Meteo": ("food", "cafe_testimonial"),
    "Frigo Severo": ("home", "home_reveal"),
    "Frigo Generoso": ("home", "home_reveal"),
}
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "generated_at",
        "release_ready",
        "design_direction",
        "upstream_core",
        "production_contract",
        "sources",
        "assets",
        "recipes",
        "previews",
        "quality_guard_allowlist",
        "pack_digest",
        "limitations",
        "board_files",
        "content_digest",
        "listening_receipt",
    }
)
_ASSET_FIELDS = frozenset(
    {
        "id",
        "path",
        "purpose",
        "kind",
        "tags",
        "source_ids",
        "sha256",
        "format",
        "duration_target_sec",
        "audio_quality",
        "license",
        "layers",
    }
)
_LAYER_FIELDS = frozenset(
    {
        "source_id",
        "source_sha256",
        "source_start_sec",
        "duration_sec",
        "output_offset_sec",
        "gain_db",
        "dsp",
        "license",
        "role",
    }
)
_PREVIEW_FIELDS = frozenset(
    {
        "id",
        "label",
        "group",
        "path",
        "sha256",
        "duration_sec",
        "format",
        "audio_quality",
    }
)
_LIMITATIONS = (
    "Candidate only; generation does not install assets or start or restart the station.",
    "Exact approved voiced core previews are listening references, not runtime underlays.",
    "Promotion remains blocked until this complete content digest has an approved receipt.",
)
FORBIDDEN_SONIC_TERMS = frozenset({"brass", "trumpet"})
# Repo-owned code whose behavior is exercised while rendering or revalidating a
# complete candidate.  Keep this as the executable-input closure, not merely the
# modules imported directly by this script.
GENERATOR_DEPENDENCIES = (
    "scripts/complete_audio_pack_gate.py",
    "scripts/core_cadence_gate.py",
    "scripts/freeze_core_cadence_speech.py",
    "scripts/sonic_treatment_gate.py",
    "scripts/audition_tts_voices.py",
    "scripts/validate_audio_asset_pack.py",
    "mammamiradio/audio/admission.py",
    "mammamiradio/audio/imaging.py",
    "mammamiradio/audio/imaging_schema.py",
    "mammamiradio/audio/normalizer.py",
    "mammamiradio/audio/synth_cache.py",
    "mammamiradio/audio/tts.py",
    "mammamiradio/audio/voice_catalog.py",
    "mammamiradio/core/config.py",
    "mammamiradio/core/models.py",
    "mammamiradio/hosts/ad_creative.py",
    "mammamiradio/hosts/fallbacks.py",
    "mammamiradio/main.py",
    "mammamiradio/scheduling/producer.py",
    "radio.toml",
    "ha-addon/mammamiradio/radio.toml",
)


@dataclass(frozen=True)
class SynthAsset:
    """One deterministic project-authored master and its delivery role."""

    asset_id: str
    path: str
    kind: str
    title: str
    duration_sec: float
    root_hz: float
    answer_hz: float
    sweep_hz: float
    delay_sec: float
    decay: float
    highpass_hz: int
    lowpass_hz: int
    tags: tuple[str, ...]
    role: str = "foreground"


@dataclass(frozen=True)
class BedAsset:
    """One restrained electronic bed with a distinct harmonic cadence."""

    asset_id: str
    path: str
    title: str
    root_hz: float
    third_ratio: float
    fifth_ratio: float
    pulse_hz: float
    duration_sec: float = 8.0


@dataclass(frozen=True)
class RecipeDefinition:
    """One complete advertising world with two exclusive foreground cues."""

    recipe_id: str
    bed_gain_db: float
    cue_one: SynthAsset
    cue_two: SynthAsset


def _cue(
    recipe_id: str,
    slug: str,
    title: str,
    root_hz: float,
    answer_hz: float,
    *,
    duration_sec: float,
    sweep_hz: float = 0.0,
    delay_sec: float = 0.16,
    decay: float = 7.0,
) -> SynthAsset:
    return SynthAsset(
        asset_id=f"ad-cue.{slug}",
        path=f"ads/cues/{slug}.mp3",
        kind="ad_cue",
        title=title,
        duration_sec=duration_sec,
        root_hz=root_hz,
        answer_hz=answer_hz,
        sweep_hz=sweep_hz,
        delay_sec=delay_sec,
        decay=decay,
        highpass_hz=170,
        lowpass_hz=6_200,
        tags=("electronic", "foreground-cue", recipe_id, "modern-night-drive"),
    )


COMPATIBILITY_ASSETS: tuple[SynthAsset, ...] = (
    SynthAsset(
        "compat.chime",
        "sfx/chime.mp3",
        "compatibility",
        "Glass resolve",
        0.62,
        698,
        1175,
        80,
        0.17,
        7.5,
        180,
        6500,
        ("electronic", "glass", "resolve"),
    ),
    SynthAsset(
        "compat.ding",
        "sfx/ding.mp3",
        "compatibility",
        "Soft signal",
        0.50,
        988,
        740,
        -35,
        0.11,
        12.0,
        250,
        5900,
        ("electronic", "signal", "compact"),
    ),
    SynthAsset(
        "compat.cash-register",
        "sfx/cash_register.mp3",
        "compatibility",
        "Transaction shimmer",
        0.56,
        620,
        1510,
        120,
        0.09,
        10.5,
        220,
        6800,
        ("electronic", "transaction", "shimmer"),
    ),
    SynthAsset(
        "compat.register-hit",
        "sfx/register_hit.mp3",
        "compatibility",
        "Checkout relay",
        0.44,
        770,
        1295,
        -90,
        0.19,
        13.0,
        230,
        6200,
        ("electronic", "relay", "checkout"),
    ),
    SynthAsset(
        "compat.sweep",
        "sfx/sweep.mp3",
        "compatibility",
        "Night rise",
        0.72,
        280,
        910,
        460,
        0.28,
        4.8,
        140,
        5400,
        ("electronic", "rise", "transition"),
    ),
    SynthAsset(
        "compat.whoosh",
        "sfx/whoosh.mp3",
        "compatibility",
        "Airline glide",
        0.78,
        410,
        1760,
        760,
        0.50,
        4.2,
        190,
        7200,
        ("electronic", "glide", "air"),
    ),
    SynthAsset(
        "compat.tape-stop",
        "sfx/tape_stop.mp3",
        "compatibility",
        "Tape descent",
        0.64,
        1180,
        205,
        -690,
        0.22,
        5.3,
        120,
        4800,
        ("electronic", "tape", "descent"),
    ),
    SynthAsset(
        "compat.hotline-beep",
        "sfx/hotline_beep.mp3",
        "compatibility",
        "Private line",
        0.50,
        1336,
        941,
        0,
        0.13,
        15.0,
        400,
        4200,
        ("electronic", "hotline", "signal"),
    ),
    SynthAsset(
        "compat.mandolin-sting",
        "sfx/mandolin_sting.mp3",
        "compatibility",
        "Legacy relay alias",
        0.55,
        510,
        1040,
        40,
        0.18,
        10.8,
        180,
        6200,
        ("electronic", "relay", "legacy-alias"),
    ),
    SynthAsset(
        "compat.ice-clink",
        "sfx/ice_clink.mp3",
        "compatibility",
        "Crystal edge",
        0.46,
        1860,
        2830,
        -120,
        0.075,
        17.0,
        700,
        7600,
        ("electronic", "crystal", "edge"),
    ),
    SynthAsset(
        "compat.startup-synth",
        "sfx/startup_synth.mp3",
        "compatibility",
        "Night console synthesizer bloom",
        0.86,
        220,
        880,
        360,
        0.27,
        4.4,
        120,
        6800,
        ("synth", "synthesizer", "electronic", "startup"),
    ),
)


TIME_CHECK = SynthAsset(
    "identity.time-check",
    "time_check.mp3",
    "identity",
    "Clock relay",
    0.52,
    1046,
    784,
    -85,
    0.18,
    10.5,
    260,
    5900,
    ("time-check", "electronic", "relay"),
)


CASA_NOTTE = BedAsset(
    "bed.casa-notte", "beds/casa_notte.mp3", "Casa Notte atmosphere", 146.83, 1.498, 2.003, 0.137, 12.0
)


RECIPE_BEDS: tuple[BedAsset, ...] = (
    BedAsset(
        "ad-bed.cafe-testimonial", "ads/beds/cafe_testimonial.mp3", "After-hours cafe glass", 174.61, 1.498, 2.000, 0.31
    ),
    BedAsset("ad-bed.stadium-win", "ads/beds/stadium_win.mp3", "Measured arena lift", 196.00, 1.587, 2.245, 0.47),
    BedAsset("ad-bed.showroom-reveal", "ads/beds/showroom_reveal.mp3", "Velvet showroom", 220.00, 1.260, 1.498, 0.19),
    BedAsset(
        "ad-bed.bureaucracy-stamp",
        "ads/beds/bureaucracy_stamp.mp3",
        "Midnight document room",
        164.81,
        1.335,
        1.782,
        0.23,
    ),
    BedAsset("ad-bed.motorway-pass", "ads/beds/motorway_pass.mp3", "Wet motorway pulse", 130.81, 1.498, 2.245, 0.53),
    BedAsset(
        "ad-bed.late-night-hotline", "ads/beds/late_night_hotline.mp3", "Private-line haze", 185.00, 1.250, 1.681, 0.11
    ),
    BedAsset(
        "ad-bed.supermarket-dash", "ads/beds/supermarket_dash.mp3", "Neon aisle motion", 207.65, 1.335, 2.000, 0.61
    ),
    BedAsset(
        "ad-bed.pharmacy-whisper", "ads/beds/pharmacy_whisper.mp3", "Clean counter air", 155.56, 1.260, 1.888, 0.17
    ),
    BedAsset("ad-bed.home-reveal", "ads/beds/home_reveal.mp3", "Apartment window glow", 146.83, 1.587, 2.119, 0.29),
)


RECIPE_DEFINITIONS: tuple[RecipeDefinition, ...] = (
    RecipeDefinition(
        "cafe_testimonial",
        -23.0,
        _cue(
            "cafe_testimonial",
            "espresso_glint",
            "Espresso glint",
            820,
            1810,
            duration_sec=0.58,
            delay_sec=0.12,
            decay=11.0,
        ),
        _cue("cafe_testimonial", "room_smile", "Room smile", 560, 940, duration_sec=0.72, sweep_hz=70, decay=6.8),
    ),
    RecipeDefinition(
        "stadium_win",
        -24.0,
        _cue(
            "stadium_win", "crowd_lift", "Crowd lift abstraction", 330, 990, duration_sec=0.78, sweep_hz=210, decay=5.1
        ),
        _cue("stadium_win", "score_flash", "Score flash", 740, 1480, duration_sec=0.52, sweep_hz=-80, decay=10.0),
    ),
    RecipeDefinition(
        "showroom_reveal",
        -24.0,
        _cue("showroom_reveal", "glass_open", "Glass open", 620, 1245, duration_sec=0.66, sweep_hz=95, decay=7.5),
        _cue("showroom_reveal", "velvet_drop", "Velvet drop", 470, 705, duration_sec=0.76, sweep_hz=-130, decay=5.9),
    ),
    RecipeDefinition(
        "bureaucracy_stamp",
        -25.0,
        _cue(
            "bureaucracy_stamp",
            "paper_tick",
            "Paper tick abstraction",
            910,
            1365,
            duration_sec=0.43,
            delay_sec=0.08,
            decay=15.0,
        ),
        _cue("bureaucracy_stamp", "seal_close", "Seal close", 390, 585, duration_sec=0.69, sweep_hz=-65, decay=7.2),
    ),
    RecipeDefinition(
        "motorway_pass",
        -24.0,
        _cue("motorway_pass", "lane_rise", "Lane rise", 260, 780, duration_sec=0.82, sweep_hz=390, decay=4.4),
        _cue(
            "motorway_pass", "tail_light", "Tail-light resolve", 680, 340, duration_sec=0.64, sweep_hz=-180, decay=6.7
        ),
    ),
    RecipeDefinition(
        "late_night_hotline",
        -25.0,
        _cue(
            "late_night_hotline",
            "line_open",
            "Private line open",
            941,
            1336,
            duration_sec=0.50,
            delay_sec=0.14,
            decay=14.5,
        ),
        _cue(
            "late_night_hotline",
            "line_release",
            "Private line release",
            610,
            915,
            duration_sec=0.67,
            sweep_hz=-55,
            decay=7.8,
        ),
    ),
    RecipeDefinition(
        "supermarket_dash",
        -24.0,
        _cue(
            "supermarket_dash",
            "scanner_ping",
            "Scanner ping abstraction",
            1120,
            1680,
            duration_sec=0.42,
            delay_sec=0.10,
            decay=15.0,
        ),
        _cue(
            "supermarket_dash",
            "checkout_glide",
            "Checkout glide",
            520,
            1040,
            duration_sec=0.73,
            sweep_hz=140,
            decay=6.1,
        ),
    ),
    RecipeDefinition(
        "pharmacy_whisper",
        -26.0,
        _cue("pharmacy_whisper", "soft_dose", "Soft dose signal", 660, 990, duration_sec=0.57, sweep_hz=30, decay=9.2),
        _cue(
            "pharmacy_whisper", "clean_release", "Clean release", 440, 1320, duration_sec=0.70, sweep_hz=-45, decay=7.0
        ),
    ),
    RecipeDefinition(
        "home_reveal",
        -24.0,
        _cue("home_reveal", "door_glow", "Door glow", 350, 700, duration_sec=0.68, sweep_hz=85, decay=7.1),
        _cue("home_reveal", "lamp_resolve", "Lamp resolve", 590, 885, duration_sec=0.74, sweep_hz=-70, decay=6.5),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _require_safe_output_root(output_root: Path) -> None:
    resolved = output_root.resolve(strict=False)
    allowed = ((REPO_ROOT / "tmp").resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise ValueError("Complete-pack gates may be rendered only under workspace tmp or system temp")
    packaged = (REPO_ROOT / "mammamiradio" / "assets" / "imaging").resolve()
    if resolved == packaged or packaged in resolved.parents:
        raise ValueError("Complete-pack generation cannot write packaged imaging assets")


def _core_artifacts(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, list):
        raise ValueError("Approved core manifest has no artifact inventory")
    artifacts = {str(item.get("id")): item for item in raw if isinstance(item, Mapping)}
    if len(artifacts) != len(raw):
        raise ValueError("Approved core manifest has duplicate or invalid artifacts")
    return artifacts


def _upstream_core_lineage(
    manifest: Mapping[str, object],
    artifact_id: str,
) -> dict[str, object]:
    """Return an immutable copy of one approved artifact's original lineage.

    A delivered complete-pack asset is an exact composite artifact, so its
    public delivery layer truthfully points at that retained composite master.
    The approved core's component layers are not standalone retained audio
    masters: their source hashes bind authored treatments, synthesis specs, or
    frozen speech.  Keep those original records here instead of relabelling
    them as public CC0 delivery sources.
    """
    artifact = _core_artifacts(manifest).get(artifact_id)
    if artifact is None:
        raise ValueError(f"Approved core artifact is missing: {artifact_id}")
    raw_layers = artifact.get("layers")
    if (
        not isinstance(raw_layers, list)
        or not raw_layers
        or not all(isinstance(layer, Mapping) for layer in raw_layers)
    ):
        raise ValueError(f"Approved core artifact has no exact layer lineage: {artifact_id}")

    raw_sources = manifest.get("sources")
    if not isinstance(raw_sources, list) or not all(isinstance(source, Mapping) for source in raw_sources):
        raise ValueError("Approved core manifest has no exact source ledger")
    source_by_id: dict[str, Mapping[str, object]] = {}
    for raw_source in raw_sources:
        source_id = raw_source.get("id")
        if not isinstance(source_id, str) or not source_id or source_id in source_by_id:
            raise ValueError("Approved core manifest has duplicate or invalid sources")
        source_by_id[source_id] = raw_source

    referenced_ids: set[str] = set()
    for raw_layer in raw_layers:
        source_id = raw_layer.get("source_id")
        source_sha256 = raw_layer.get("source_sha256")
        source = source_by_id.get(str(source_id)) if isinstance(source_id, str) else None
        if source is None or source.get("source_sha256") != source_sha256:
            raise ValueError(f"Approved core artifact layer has stale source lineage: {artifact_id}")
        referenced_ids.add(source_id)

    # Preserve the upstream source-ledger order as well as every source field.
    referenced_sources = [source for source in raw_sources if str(source.get("id")) in referenced_ids]
    return {
        "artifact": copy.deepcopy(dict(artifact)),
        "sources": copy.deepcopy([dict(source) for source in referenced_sources]),
    }


def _artifact_path(
    core_manifest_path: Path,
    artifacts: Mapping[str, Mapping[str, object]],
    artifact_id: str,
) -> Path:
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise ValueError(f"Approved core artifact is missing: {artifact_id}")
    relative = artifact.get("path")
    expected_sha = artifact.get("sha256")
    if not isinstance(relative, str) or not relative or not isinstance(expected_sha, str):
        raise ValueError(f"Approved core artifact has no delivered path/hash: {artifact_id}")
    path = (core_manifest_path.parent / relative).resolve(strict=True)
    if core_manifest_path.parent.resolve() not in path.parents:
        raise ValueError(f"Approved core artifact escapes its board: {artifact_id}")
    if _sha256(path) != expected_sha:
        raise ValueError(f"Approved core artifact changed after listening: {artifact_id}")
    return path


def _copy(path: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)
    return destination


def _synth_expression(spec: SynthAsset, *, right: bool) -> str:
    detune = 1.009 if right else 1.0
    root = spec.root_hz * detune
    answer = spec.answer_hz * (1.006 if right else 1.0)
    sweep = spec.sweep_hz * (1.02 if right else 1.0)
    delay = spec.delay_sec + (0.007 if right else 0.0)
    # Soft additive tones and a delayed answer keep cues sleek and midrange-led.
    return (
        f"0.060*sin(2*PI*({root:.5f}+{sweep:.5f}*t)*t)*exp(-{spec.decay:.5f}*t)"
        f"+0.021*sin(2*PI*({root * 2.002:.5f}+{sweep * 0.51:.5f}*t)*t)*exp(-{spec.decay * 1.35:.5f}*t)"
        f"+0.034*sin(2*PI*{answer:.5f}*(t-{delay:.5f}))*"
        f"exp(-{spec.decay * 0.86:.5f}*(t-{delay:.5f}))*gt(t\\,{delay:.5f})"
    )


def _render_synth_master(spec: SynthAsset, source_master: Path) -> Path:
    generated = _core.GeneratedSpec(
        id=f"source.{spec.asset_id}",
        path=source_master.name,
        label=spec.title,
        purpose="Deterministic project-authored Modern Night Drive source master.",
        duration_sec=spec.duration_sec,
        expression_left=_synth_expression(spec, right=False),
        expression_right=_synth_expression(spec, right=True),
        highpass_hz=spec.highpass_hz,
        lowpass_hz=spec.lowpass_hz,
        fade_in_sec=min(0.018, spec.duration_sec / 8),
        fade_out_sec=min(0.22, spec.duration_sec / 3),
        tags=spec.tags,
        foreground_source_id=f"project.{spec.asset_id}",
    )
    rendered = _core._render_generated(generated, source_master.parent)
    if rendered != source_master:
        raise RuntimeError(f"Unexpected source-master path for {spec.asset_id}")
    return rendered


def _bed_expression(spec: BedAsset, *, right: bool) -> str:
    detune = 1.008 if right else 1.0
    root = spec.root_hz * detune
    third = spec.root_hz * spec.third_ratio * (1.004 if right else 1.0)
    fifth = spec.root_hz * spec.fifth_ratio * (1.011 if right else 1.0)
    pulse = spec.pulse_hz * (0.97 if right else 1.0)
    return (
        f"(1-exp(-3.2*t))*(0.047*sin(2*PI*{root:.5f}*t)"
        f"+0.031*sin(2*PI*{third:.5f}*t)+0.019*sin(2*PI*{fifth:.5f}*t))"
        f"*(0.78+0.22*sin(2*PI*{pulse:.5f}*t))"
        f"+0.010*sin(2*PI*{root * 0.5:.5f}*t)*(0.70+0.30*sin(2*PI*{pulse * 0.5:.5f}*t))"
    )


def _render_bed_master(spec: BedAsset, source_master: Path) -> Path:
    generated = _core.GeneratedSpec(
        id=f"source.{spec.asset_id}",
        path=source_master.name,
        label=spec.title,
        purpose="Deterministic project-authored Velvet Horizon atmospheric master.",
        duration_sec=spec.duration_sec,
        expression_left=_bed_expression(spec, right=False),
        expression_right=_bed_expression(spec, right=True),
        highpass_hz=85,
        lowpass_hz=4_800,
        fade_in_sec=0.55,
        fade_out_sec=0.85,
        tags=("electronic", "atmospheric", "velvet-horizon", "music-bed"),
        foreground_source_id=f"project.{spec.asset_id}",
    )
    rendered = _core._render_generated(generated, source_master.parent)
    if rendered != source_master:
        raise RuntimeError(f"Unexpected source-master path for {spec.asset_id}")
    return rendered


def _git_blob(revision: str, relative_path: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise ValueError(f"Complete-pack generator dependency is not committed: {relative_path}")
    return result.stdout


def _tool_version(command: str) -> str:
    result = subprocess.run(
        [command, "-version"],
        check=True,
        capture_output=True,
        text=True,
    )
    first_line = result.stdout.splitlines()[0].strip() if result.stdout else ""
    if not first_line:
        raise ValueError(f"Complete-pack generator could not identify {command}")
    return first_line


def _assert_runtime_audio_contract() -> None:
    """Keep offline previews and recipes identical to shipped runtime inputs."""
    definition_ids = tuple(definition.recipe_id for definition in RECIPE_DEFINITIONS)
    if definition_ids != RECIPE_IDS:
        raise ValueError("Complete-pack recipe definitions differ from the exact runtime recipe IDs")
    if frozenset(RECIPE_IDS) != OFFICIAL_SONIC_RECIPE_IDS:
        raise ValueError("Complete-pack recipe IDs differ from the official runtime recipe registry")

    configured_brand_contracts: list[dict[str, tuple[str, str]]] = []
    for relative_path in ("radio.toml", "ha-addon/mammamiradio/radio.toml"):
        try:
            config = tomllib.loads((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Complete-pack runtime config is unreadable: {relative_path}") from exc
        audio = config.get("audio")
        imaging = config.get("imaging")
        if not isinstance(audio, Mapping) or not isinstance(imaging, Mapping):
            raise ValueError(f"Complete-pack runtime config lacks audio/imaging tables: {relative_path}")
        ads = config.get("ads")
        brands = ads.get("brands") if isinstance(ads, Mapping) else None
        if not isinstance(brands, list) or not brands:
            raise ValueError(f"Complete-pack runtime config lacks official ad brands: {relative_path}")
        configured_contract: dict[str, tuple[str, str]] = {}
        for index, brand in enumerate(brands):
            if not isinstance(brand, Mapping):
                raise ValueError(f"Complete-pack runtime ad brand {index} is invalid: {relative_path}")
            name = brand.get("name")
            category = brand.get("category")
            recipe_id = brand.get("sonic_recipe")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(category, str)
                or not category
                or not isinstance(recipe_id, str)
                or not recipe_id
            ):
                raise ValueError(
                    f"Complete-pack runtime ad brand {index} lacks a category or recipe identity: {relative_path}"
                )
            if name in configured_contract:
                raise ValueError(f"Complete-pack runtime ad brand name repeats: {relative_path}")
            configured_contract[name] = (category, recipe_id)
        if frozenset(recipe_id for _category, recipe_id in configured_contract.values()) != frozenset(RECIPE_IDS):
            raise ValueError(f"Complete-pack runtime recipe set differs from the exact nine recipes: {relative_path}")
        if configured_contract != EXPECTED_OFFICIAL_BRAND_CONTRACT:
            raise ValueError(
                f"Complete-pack runtime brand contract differs from the canonical 40-brand map: {relative_path}"
            )
        configured_brand_contracts.append(configured_contract)
        actual = {
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
            "bitrate": audio.get("bitrate"),
            "lufs_target": audio.get("lufs_target"),
            "ad_lufs_target": audio.get("ad_lufs_target"),
            "broadcast_chain": audio.get("broadcast_chain"),
            "bed_volume_db": imaging.get("bed_volume_db"),
            "imaging_assets_dir": imaging.get("assets_dir"),
        }
        if actual != RUNTIME_AUDIO_CONTRACT:
            raise ValueError(
                f"Complete-pack preview settings differ from the shipped runtime contract: {relative_path}"
            )
    if (
        configured_brand_contracts[0] != configured_brand_contracts[1]
    ):  # pragma: no cover - canonical checks imply parity
        raise ValueError("Complete-pack runtime brand contracts differ between shipped configs")


@lru_cache(maxsize=1)
def _pinned_generator_evidence() -> dict[str, object]:
    """Bind generated masters to every committed input that can affect them."""
    _assert_runtime_audio_contract()
    relative_script = Path(__file__).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not revision or len(revision) != 40:
        raise ValueError("Complete-pack generator has no full Git revision")
    dependency_records: list[dict[str, str]] = []
    for relative_path in GENERATOR_DEPENDENCIES:
        local_path = REPO_ROOT / relative_path
        try:
            local_bytes = local_path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Complete-pack generator dependency is unreadable: {relative_path}") from exc
        committed_bytes = _git_blob(revision, relative_path)
        if local_bytes != committed_bytes:
            raise ValueError(
                "Complete-pack generator dependencies must be committed unchanged before a review candidate "
                f"is rendered: {relative_path}"
            )
        dependency_records.append(
            {
                "path": relative_path,
                "url": f"https://github.com/florianhorner/mammamiradio/blob/{revision}/{relative_path}",
                "sha256": hashlib.sha256(committed_bytes).hexdigest(),
            }
        )
    primary = next(record for record in dependency_records if record["path"] == relative_script)
    return {
        "kind": "git-committed-ffmpeg-generator",
        "revision": revision,
        "path": relative_script,
        "url": primary["url"],
        "sha256": primary["sha256"],
        "dependencies": dependency_records,
        "runtime": {
            "python": platform.python_version(),
            "ffmpeg": _tool_version("ffmpeg"),
        },
    }


def _source_record(
    source_id: str,
    title: str,
    master_path: Path,
    pack_dir: Path,
    tags: Sequence[str],
    *,
    modification: str,
) -> dict[str, object]:
    generator = _pinned_generator_evidence()
    return {
        "id": source_id,
        "license": "CC0-1.0",
        "source_url": str(generator["url"]),
        "source_sha256": _sha256(master_path),
        "creator": "Mammami Radio project",
        "title": title,
        "modification": modification,
        "original_path": os.path.relpath(master_path, pack_dir),
        "tags": list(tags),
        "provenance_type": "project-authored-deterministic-master",
        "generator": generator,
    }


def _dependency_hashes(generator: Mapping[str, object], *, label: str) -> dict[str, str]:
    raw_dependencies = generator.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise ValueError(f"{label} has no exact generator dependency ledger")
    dependencies: dict[str, str] = {}
    for raw in raw_dependencies:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} contains an invalid generator dependency")
        path = raw.get("path")
        sha256 = raw.get("sha256")
        if not isinstance(path, str) or not isinstance(sha256, str) or path in dependencies:
            raise ValueError(f"{label} contains an invalid or repeated generator dependency")
        dependencies[path] = sha256
    if set(dependencies) != set(GENERATOR_DEPENDENCIES):
        raise ValueError(f"{label} does not pin the exact runtime dependency set")
    return dependencies


def _validate_current_generator_evidence(raw_sources: Sequence[object]) -> None:
    """Invalidate a listening candidate when its current runtime seams drift."""
    recorded_generator: Mapping[str, object] | None = None
    for index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, Mapping) or not isinstance(raw_source.get("generator"), Mapping):
            raise ValueError(f"Complete-pack source {index} has no generated-master evidence")
        generator = raw_source["generator"]
        assert isinstance(generator, Mapping)
        if recorded_generator is None:
            recorded_generator = generator
        elif generator != recorded_generator:
            raise ValueError("Complete-pack sources do not share one exact generator evidence record")
    if recorded_generator is None:
        raise ValueError("Complete pack has no generator evidence")
    recorded_hashes = _dependency_hashes(recorded_generator, label="Complete-pack recorded generator")
    if recorded_generator.get("path") != "scripts/complete_audio_pack_gate.py":
        raise ValueError("Complete-pack recorded generator has the wrong primary path")
    if recorded_generator.get("sha256") != recorded_hashes["scripts/complete_audio_pack_gate.py"]:
        raise ValueError("Complete-pack recorded generator primary hash differs from its dependency ledger")

    cache_clear = getattr(_pinned_generator_evidence, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    current_generator = _pinned_generator_evidence()
    current_hashes = _dependency_hashes(current_generator, label="Current complete-pack generator")
    if current_hashes != recorded_hashes:
        raise ValueError("Complete-pack runtime dependencies changed after the listening candidate was rendered")


def _audio_quality_evidence(
    path: Path,
    *,
    quality_class: str,
    target_lufs: float,
    tolerance_lu: float,
) -> dict[str, object]:
    """Measure and fail closed on the production loudness/true-peak contract."""
    integrated_lufs, true_peak_dbtp = _core._measure_loudness(path)
    if not math.isfinite(integrated_lufs) or not math.isfinite(true_peak_dbtp):
        raise ValueError(f"Audio quality evidence is non-finite: {path}")
    true_peak_ceiling = TRUE_PEAK_CEILING_DBTP
    if abs(integrated_lufs - target_lufs) > tolerance_lu:
        raise ValueError(f"Audio is outside {target_lufs:g} ±{tolerance_lu:g} LUFS: {path} ({integrated_lufs:g})")
    if true_peak_dbtp > true_peak_ceiling:
        raise ValueError(f"Audio exceeds {true_peak_ceiling:g} dBTP: {path} ({true_peak_dbtp:g})")
    return {
        "class": quality_class,
        "measurement": AUDIO_QUALITY_CONTRACT["measurement"],
        "integrated_lufs": integrated_lufs,
        "target_lufs": target_lufs,
        "tolerance_lu": tolerance_lu,
        "true_peak_dbtp": true_peak_dbtp,
        "true_peak_ceiling_dbtp": true_peak_ceiling,
        "exception": None,
    }


def _asset_record(
    asset_id: str,
    relative_path: str,
    kind: str,
    tags: Sequence[str],
    source: Mapping[str, object],
    pack_dir: Path,
    *,
    role: str,
    dsp: Sequence[str],
    upstream_lineage: Mapping[str, object] | None = None,
) -> dict[str, object]:
    path = pack_dir / relative_path
    evidence, duration = _core._probe_audio(path)
    if evidence != FORMAT:
        raise ValueError(f"Pack asset violates the production format: {relative_path}")
    record: dict[str, object] = {
        "id": asset_id,
        "path": relative_path,
        "purpose": f"Modern Night Drive {kind.replace('_', ' ')} asset.",
        "kind": kind,
        "tags": list(tags),
        "source_ids": [source["id"]],
        "sha256": _sha256(path),
        "format": FORMAT,
        "duration_target_sec": duration,
        "audio_quality": _audio_quality_evidence(
            path,
            quality_class="asset",
            target_lufs=ASSET_TARGET_LUFS,
            tolerance_lu=ASSET_TOLERANCE_LU,
        ),
        "license": "CC0-1.0",
        "layers": [
            {
                "source_id": source["id"],
                "source_sha256": source["source_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": duration,
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": list(dsp),
                "license": "CC0-1.0",
                "role": role,
            }
        ],
    }
    if upstream_lineage is not None:
        record["upstream_lineage"] = copy.deepcopy(dict(upstream_lineage))
    return record


def _deliver_master(master_path: Path, pack_dir: Path, relative_path: str) -> Path:
    return _copy(master_path, pack_dir / relative_path)


def _generic_pack_digest(assets: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for asset in sorted(assets, key=lambda item: (str(item["path"]), str(item["id"]))):
        digest.update(str(asset["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(asset["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _resolve_frozen_spot_speech(
    core_manifest: Mapping[str, object],
) -> tuple[dict[str, Path], dict[str, Mapping[str, object]]]:
    upstream = core_manifest.get("upstream_speech")
    if not isinstance(upstream, Mapping):
        raise ValueError("Approved core manifest has no frozen-speech lineage")
    speech_manifest = _core._path_from_record(
        upstream.get("manifest_path"),
        upstream.get("manifest_path_kind"),
        label="complete-pack speech manifest",
    )
    try:
        speech_gate = importlib.import_module("scripts.freeze_core_cadence_speech")
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        speech_gate = importlib.import_module("freeze_core_cadence_speech")
    manifest, paths = speech_gate.validate_core_cadence_speech_manifest(speech_manifest)
    raw_clips = manifest.get("clips")
    if not isinstance(raw_clips, list):
        raise ValueError("Frozen-speech manifest has no clips")
    metadata = {str(item.get("id")): item for item in raw_clips if isinstance(item, Mapping)}
    required = {"speech.spot-a", "speech.spot-b"}
    if not required <= set(paths) or not required <= set(metadata):
        raise ValueError("Complete recipe board requires both approved frozen spot voices")
    return {key: paths[key] for key in required}, metadata


def _render_recipe_preview(
    voice_path: Path,
    bed_path: Path,
    bed_gain_db: float,
    cues: tuple[tuple[Path, str, float, float], tuple[Path, str, float, float]],
    output_path: Path,
) -> Path:
    duration = probe_duration_sec(voice_path)
    if duration is None or duration <= 0:
        raise ValueError(f"Frozen spot speech is not probeable: {voice_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = output_path.parent / f".{output_path.stem}.{uuid4().hex}.work"
    scratch.mkdir()
    looped_bed = scratch / "bed.mp3"
    bed_mix = scratch / "bed-mix.mp3"
    cue_mix = scratch / "cue-mix.mp3"
    try:
        loop_audio_bed(bed_path, looped_bed, duration)
        mix_with_bed(voice_path, looped_bed, bed_mix, 10 ** (bed_gain_db / 20.0))
        layers: list[tuple[Path, float, float, float]] = []
        for cue_path, anchor, gain_db, max_duration_sec in cues:
            if anchor == "intro":
                offset = 0.0
            elif anchor == "outro":
                offset = max(0.0, duration - max_duration_sec)
            else:  # pragma: no cover - constant recipe invariant
                raise ValueError(f"Unsupported preview cue anchor: {anchor}")
            layers.append((cue_path, offset, gain_db, max_duration_sec))
        mix_oneshot_layers(bed_mix, layers, cue_mix)
        with _core._runtime_loudness_targets():
            normalize_ad(cue_mix, output_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return output_path


def _render_casa_notte_preview(voice_path: Path, bed_path: Path, output_path: Path) -> Path:
    """Exercise the exact packaged talk-bed fallback with approved frozen speech."""
    duration = probe_duration_sec(voice_path)
    if duration is None or duration <= 0:
        raise ValueError(f"Frozen spot speech is not probeable: {voice_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = output_path.parent / f".{output_path.stem}.{uuid4().hex}.work"
    scratch.mkdir()
    looped_bed = scratch / "talk-bed.mp3"
    try:
        assets_dir = bed_path.parent.parent
        if bed_path != assets_dir / "beds" / "casa_notte.mp3":
            raise ValueError("Casa Notte preview must exercise the packaged fallback path")
        fallback_beds = sorted(path for path in (assets_dir / "beds").glob("*.mp3") if path.is_file())
        if fallback_beds != [bed_path]:
            raise ValueError("Casa Notte must be the complete pack's only packaged talk-bed fallback")
        imaging = ImagingLibrary(
            [],
            scratch,
            bed_volume_db=TALK_BED_GAIN_DB,
            assets_dir=assets_dir,
            cache_dir=None,
        )
        imaging.pick_talk_bed(duration, looped_bed)
        with _core._runtime_loudness_targets():
            mix_voice_with_bed(voice_path, looped_bed, output_path, TALK_BED_GAIN_DB)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return output_path


def _runtime_imaging_library(pack_dir: Path, scratch_dir: Path) -> ImagingLibrary:
    return ImagingLibrary(
        [],
        scratch_dir,
        bed_volume_db=TALK_BED_GAIN_DB,
        assets_dir=pack_dir,
        cache_dir=None,
    )


def _render_runtime_mid_from_pack(pack_dir: Path, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    imaging = _runtime_imaging_library(pack_dir, output_path.parent)
    return imaging.pick_ad_bumper(output_path, duration_sec=0.8, role="mid")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _preview_record(
    run_root: Path,
    preview_path: Path,
    preview_id: str,
    label: str,
    group: str,
    *,
    render: Mapping[str, object] | None = None,
) -> dict[str, object]:
    evidence, duration = _core._probe_audio(preview_path)
    if evidence != FORMAT:
        raise ValueError(f"Preview violates the production format: {preview_path}")
    advertising = group == "advertising"
    target_lufs = ADVERTISING_PREVIEW_TARGET_LUFS if advertising else PROGRAM_PREVIEW_TARGET_LUFS
    tolerance_lu = ADVERTISING_PREVIEW_TOLERANCE_LU if advertising else PROGRAM_PREVIEW_TOLERANCE_LU
    record: dict[str, object] = {
        "id": preview_id,
        "label": label,
        "group": group,
        "path": preview_path.relative_to(run_root).as_posix(),
        "sha256": _sha256(preview_path),
        "duration_sec": duration,
        "format": evidence,
        "audio_quality": _audio_quality_evidence(
            preview_path,
            quality_class="advertising-preview" if advertising else "program-preview",
            target_lufs=target_lufs,
            tolerance_lu=tolerance_lu,
        ),
    }
    if render is not None:
        record["render"] = dict(render)
    return record


def _receipt(pack_digest: str, content_digest: str) -> dict[str, object]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "pending",
        "pack_digest": pack_digest,
        "content_digest": content_digest,
        "target": TARGET,
        "surfaces": list(REQUIRED_SURFACES),
        "device_classes": list(REQUIRED_DEVICE_CLASSES),
        "prompt": LISTENING_PROMPT,
        "mac": None,
        "small_speaker": None,
        "sonos_arc": None,
        "notes": None,
        "reviewed_at": None,
        "approved_candidate_id": None,
    }


def _content_digest_payload(manifest: Mapping[str, object]) -> dict[str, object]:
    excluded = {"content_digest", "listening_receipt", "release_ready"}
    return {key: value for key, value in manifest.items() if key not in excluded}


def _quality_guard_allowlist() -> dict[str, object]:
    return {
        "perceptual_overlaps": [
            {
                "asset_ids": ["identity.station-id", "identity.sweeper"],
                "reason": "The approved Neon Relay signature is intentionally exclusive to these two identity roles.",
            }
        ],
        "source_core_roles": [],
    }


def _design_direction_contract() -> dict[str, object]:
    return {
        "signature": "neon_relay",
        "atmospheric_character": "velvet_horizon",
        "signature_roles": ["identity.station-id", "identity.sweeper"],
        "foreground_policy": "one unique project-authored source per advertising cue",
        "small_speaker_policy": "recognition remains above 170 Hz",
    }


def _build_manifest(
    *,
    generated_at: str,
    core_manifest_path: Path,
    core_digest: str,
    sources: list[dict[str, object]],
    assets: list[dict[str, object]],
    recipes: list[dict[str, object]],
    previews: list[dict[str, object]],
    mid_proof: Mapping[str, object],
) -> dict[str, object]:
    core_path, core_path_kind = _core._path_record(core_manifest_path)
    pack_digest = _generic_pack_digest(assets)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "generated_at": generated_at,
        "release_ready": False,
        "design_direction": _design_direction_contract(),
        "upstream_core": {
            "manifest_path": core_path,
            "manifest_path_kind": core_path_kind,
            "pack_digest": core_digest,
        },
        "production_contract": {
            "format": FORMAT,
            "runtime_audio": dict(RUNTIME_AUDIO_CONTRACT),
            "audio_quality": dict(AUDIO_QUALITY_CONTRACT),
            "station_and_sweeper_are_runtime_underlays": True,
            "ad_bumper_roles": ["in", "mid", "out"],
            "mid_runtime_fit": dict(mid_proof),
            "recipe_preview_helpers": [
                "loop_audio_bed",
                "mix_with_bed",
                "mix_oneshot_layers",
                "normalize_ad",
            ],
            "talk_bed_preview_helpers": [
                "ImagingLibrary.pick_talk_bed",
                "mix_voice_with_bed",
            ],
        },
        "sources": sources,
        "assets": assets,
        "recipes": recipes,
        "previews": previews,
        "quality_guard_allowlist": _quality_guard_allowlist(),
        "pack_digest": pack_digest,
        "limitations": list(_LIMITATIONS),
    }


def _board_html(manifest: Mapping[str, object]) -> str:
    raw_previews = manifest.get("previews")
    if not isinstance(raw_previews, list):
        raise ValueError("Complete pack has no preview inventory")
    groups = (
        ("approved-core", "Approved core — unchanged"),
        ("runtime-support", "Runtime support — time check and fallback bed"),
        ("compatibility", "Compatibility sounds"),
        ("advertising", "Advertising recipes"),
    )
    sections: list[str] = []
    for group_id, heading in groups:
        cards: list[str] = []
        for preview in raw_previews:
            if not isinstance(preview, Mapping) or preview.get("group") != group_id:
                continue
            label = html.escape(str(preview["label"]))
            path = html.escape(str(preview["path"]), quote=True)
            full_digest = html.escape(str(preview["sha256"]), quote=True)
            digest = full_digest[:12]
            cards.append(
                "<article><h3>"
                + label
                + "</h3><audio controls preload='none' src='"
                + path
                + "?sha256="
                + full_digest
                + "'></audio><p>SHA-256 "
                + digest
                + "…</p></article>"
            )
        sections.append(f"<section><h2>{html.escape(heading)}</h2><div class='grid'>{''.join(cards)}</div></section>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Modern Night Drive — complete pack gate</title>
<style>
:root{{--ink:#eef3f6;--muted:#9dadb7;--panel:#14212a;--line:#29414e;--accent:#63d7d2}}
*{{box-sizing:border-box}}
body{{margin:0;background:#081116;color:var(--ink);font:16px/1.45 -apple-system,BlinkMacSystemFont,
"Segoe UI",sans-serif}}
main{{max-width:1160px;margin:auto;padding:40px 22px 80px}}h1{{font-size:clamp(2rem,5vw,4.5rem);margin:.15em 0}}
.lede{{max-width:820px;color:var(--muted)}}section{{margin-top:42px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}}
article{{background:linear-gradient(145deg,#162730,#101a21);border:1px solid var(--line);
border-radius:16px;padding:18px}}
h2{{color:var(--accent)}}h3{{margin-top:0}}audio{{width:100%}}p{{color:var(--muted);font-size:.88rem;word-break:break-word}}
code{{color:var(--accent)}}
</style></head><body><main><p>Stage 3 listening gate</p><h1>Neon Relay × Velvet Horizon</h1>
<p class="lede">The approved core remains byte-for-byte unchanged. Review every compatibility cue and all nine
runtime-faithful advertising worlds on Mac, a small speaker, and the Wohnzimmer Sonos Arc.</p>
<p>Content digest: <code id="content-digest">loading from validated manifest…</code></p>{"".join(sections)}
<script>fetch('pack/manifest.json',{{cache:'no-store'}}).then(r=>r.json()).then(m=>{{
document.getElementById('content-digest').textContent=m.content_digest;
}}).catch(()=>{{document.getElementById('content-digest').textContent='manifest unavailable';}});</script>
</main></body></html>"""


def _render_board(run_root: Path, manifest: Mapping[str, object]) -> None:
    (run_root / "index.html").write_text(_board_html(manifest), encoding="utf-8")


def _handoff_text() -> str:
    return """# Modern Night Drive complete-pack listening gate

The candidate content digest is displayed from the validated `pack/manifest.json`
when the board opens.

This candidate does **not** modify packaged imaging, start the station, deploy,
or restart Home Assistant. The station ID and sweeper pack members are the
approved runtime underlays; the voiced versions on the board are references.

## Listen

From the candidate board directory:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory .
```

Open <http://127.0.0.1:8765/> and review, in order:

1. Approved core (unchanged reference)
2. Runtime time-check and Casa Notte fallback-bed context
3. All 11 compatibility sounds
4. All nine advertising recipe previews

Listen on the Mac, a small speaker, and the Wohnzimmer Sonos Arc. Reply exactly:

`Complete pack: pass|fail; Mac: pass|fail; Small speaker: pass|fail; Sonos Arc: pass|fail; Notes: <short reason>`

Promotion remains blocked until that decision is recorded against the digest shown by the board.
"""


def _write_handoff(run_root: Path) -> None:
    (run_root / "README.md").write_text(_handoff_text(), encoding="utf-8")


def _board_file_records(run_root: Path) -> list[dict[str, str]]:
    return [{"path": filename, "sha256": _sha256(run_root / filename)} for filename in ("index.html", "README.md")]


CORE_PACK_ASSETS: tuple[tuple[str, str, str, str, tuple[str, ...], str], ...] = (
    (
        "render.station-bed",
        "identity.station-id",
        "station_id.mp3",
        "identity",
        ("station", "neon-relay", "runtime-underlay"),
        "foreground",
    ),
    (
        "render.sweeper-bed",
        "identity.sweeper",
        "sweeper.mp3",
        "identity",
        ("sweeper", "neon-relay", "runtime-underlay"),
        "foreground",
    ),
    (
        "cue.music-to-speech",
        "transition.music-to-speech",
        "stingers/music_to_speech.mp3",
        "transition",
        ("music-to-speech", "electronic", "understated"),
        "foreground",
    ),
    (
        "cue.speech-to-music",
        "transition.speech-to-music",
        "stingers/speech_to_music.mp3",
        "transition",
        ("speech-to-music", "electronic", "understated"),
        "foreground",
    ),
    ("cue.ad-in", "bumper.ad-in", "bumpers/ad_in.mp3", "transition", ("ad-bumper", "in", "electronic"), "foreground"),
    (
        "cue.ad-out",
        "bumper.ad-out",
        "bumpers/ad_out.mp3",
        "transition",
        ("ad-bumper", "out", "electronic"),
        "foreground",
    ),
)

CORE_PREVIEW_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("preview.station-id", "Station ID"),
    ("preview.sweeper", "Sweeper"),
    ("cue.ad-in", "Ad in"),
    ("cue.ad-out", "Ad out"),
    ("preview.cadence.two-with-mid.music-successor", "Full cadence — two spots with mid"),
    ("preview.cadence.two-without-mid.music-successor", "Full cadence — two spots without mid"),
    ("preview.cadence.one-without-mid.speech-successor", "Full cadence — speech successor"),
)


def _append_delivered_asset(
    *,
    pack_dir: Path,
    source_master: Path,
    asset_id: str,
    relative_path: str,
    kind: str,
    title: str,
    tags: Sequence[str],
    role: str,
    modification: str,
    sources: list[dict[str, object]],
    assets: list[dict[str, object]],
    dsp: Sequence[str],
    upstream_lineage: Mapping[str, object] | None = None,
) -> None:
    source_id = _source_id_for_asset(asset_id)
    source = _source_record(
        source_id,
        title,
        source_master,
        pack_dir,
        tags,
        modification=modification,
    )
    _deliver_master(source_master, pack_dir, relative_path)
    sources.append(source)
    assets.append(
        _asset_record(
            asset_id,
            relative_path,
            kind,
            tags,
            source,
            pack_dir,
            role=role,
            dsp=dsp,
            upstream_lineage=upstream_lineage,
        )
    )


def _render_complete_pack_in_place(
    core_manifest_path: Path,
    run_root: Path,
    *,
    generated_at: str,
) -> dict[str, object]:
    core_manifest_path = core_manifest_path.resolve(strict=True)
    core_digest = _core.assert_core_listening_gate(core_manifest_path)
    core_manifest, _core_audio = _core.validate_core_cadence_gate(core_manifest_path)
    artifacts = _core_artifacts(core_manifest)
    pack_dir = run_root / "pack"
    masters_dir = pack_dir / "provenance" / "source-masters"
    preview_root = run_root / "previews"
    pack_dir.mkdir(parents=True)
    masters_dir.mkdir(parents=True)
    preview_root.mkdir()

    sources: list[dict[str, object]] = []
    assets: list[dict[str, object]] = []
    previews: list[dict[str, object]] = []

    # Runtime station/sweeper members are exact approved underlays, never the
    # already-voiced listening finals.
    for artifact_id, asset_id, relative_path, kind, tags, role in CORE_PACK_ASSETS:
        approved = _artifact_path(core_manifest_path, artifacts, artifact_id)
        source_master = _copy(approved, masters_dir / f"{asset_id}.mp3")
        _append_delivered_asset(
            pack_dir=pack_dir,
            source_master=source_master,
            asset_id=asset_id,
            relative_path=relative_path,
            kind=kind,
            title=f"Approved core artifact {artifact_id}",
            tags=tags,
            role=role,
            modification="Exact byte copy from the digest-approved project-authored core cadence.",
            sources=sources,
            assets=assets,
            dsp=("copy:exact-approved-core-artifact",),
            upstream_lineage=_upstream_core_lineage(core_manifest, artifact_id),
        )

    # The shipped middle member is the authored raw master. Runtime performs
    # one 0.8-second fit; that result must equal the approved core cue exactly.
    mid_spec = next(spec for spec in _core.CORE_CUES if spec.id == "cue.ad-mid")
    raw_mid_spec = _core.GeneratedSpec(
        id=mid_spec.id,
        path="bumper.ad-mid.mp3",
        label=mid_spec.label,
        purpose=mid_spec.purpose,
        duration_sec=mid_spec.duration_sec,
        expression_left=mid_spec.expression_left,
        expression_right=mid_spec.expression_right,
        highpass_hz=mid_spec.highpass_hz,
        lowpass_hz=mid_spec.lowpass_hz,
        fade_in_sec=mid_spec.fade_in_sec,
        fade_out_sec=mid_spec.fade_out_sec,
        tags=mid_spec.tags,
        foreground_source_id=mid_spec.foreground_source_id,
    )
    raw_mid = _core._render_generated(raw_mid_spec, masters_dir)
    expected_mid_artifact = artifacts["cue.ad-mid"]
    raw_mid_expected = None
    layers = expected_mid_artifact.get("layers")
    if isinstance(layers, list) and layers and isinstance(layers[0], Mapping):
        raw_mid_expected = layers[0].get("rendered_master_sha256")
    if not isinstance(raw_mid_expected, str) or _sha256(raw_mid) != raw_mid_expected:
        raise ValueError("Regenerated ad-mid raw master differs from the approved authoring render")
    _append_delivered_asset(
        pack_dir=pack_dir,
        source_master=raw_mid,
        asset_id="bumper.ad-mid",
        relative_path="bumpers/ad_mid.mp3",
        kind="transition",
        title="Project-authored electronic mid-break separator",
        tags=("ad-bumper", "mid", "electronic", "single-separator"),
        role="foreground",
        modification="Deterministic local synthesis retained raw for one runtime fit.",
        sources=sources,
        assets=assets,
        dsp=("generator:ffmpeg-aevalsrc", "runtime-fit:deferred"),
        upstream_lineage=_upstream_core_lineage(core_manifest, "cue.ad-mid"),
    )
    runtime_mid = preview_root / "core" / "ad_mid_runtime.mp3"
    runtime_mid.parent.mkdir(parents=True)
    _render_runtime_mid_from_pack(pack_dir, runtime_mid)
    expected_runtime_mid = str(expected_mid_artifact["sha256"])
    actual_runtime_mid = _sha256(runtime_mid)
    if actual_runtime_mid != expected_runtime_mid:
        raise ValueError("Runtime-fitted ad-mid differs from the approved core output")

    # Remaining core and compatibility masters are all project-authored.
    time_master = _render_synth_master(TIME_CHECK, masters_dir / f"{TIME_CHECK.asset_id}.mp3")
    _append_delivered_asset(
        pack_dir=pack_dir,
        source_master=time_master,
        asset_id=TIME_CHECK.asset_id,
        relative_path=TIME_CHECK.path,
        kind=TIME_CHECK.kind,
        title=TIME_CHECK.title,
        tags=TIME_CHECK.tags,
        role=TIME_CHECK.role,
        modification="Deterministic local electronic time-check synthesis.",
        sources=sources,
        assets=assets,
        dsp=("generator:ffmpeg-aevalsrc", "loudnorm:-16LUFS"),
    )

    casa_master = _render_bed_master(CASA_NOTTE, masters_dir / f"{CASA_NOTTE.asset_id}.mp3")
    _append_delivered_asset(
        pack_dir=pack_dir,
        source_master=casa_master,
        asset_id=CASA_NOTTE.asset_id,
        relative_path=CASA_NOTTE.path,
        kind="bed",
        title=CASA_NOTTE.title,
        tags=("electronic", "atmospheric", "velvet-horizon", "casa-notte"),
        role="texture",
        modification="Deterministic local atmospheric synthesis.",
        sources=sources,
        assets=assets,
        dsp=("generator:ffmpeg-aevalsrc", "highpass:85Hz", "lowpass:4800Hz", "loudnorm:-16LUFS"),
    )

    for spec in COMPATIBILITY_ASSETS:
        master = _render_synth_master(spec, masters_dir / f"{spec.asset_id}.mp3")
        _append_delivered_asset(
            pack_dir=pack_dir,
            source_master=master,
            asset_id=spec.asset_id,
            relative_path=spec.path,
            kind=spec.kind,
            title=spec.title,
            tags=spec.tags,
            role=spec.role,
            modification="Deterministic local electronic compatibility synthesis.",
            sources=sources,
            assets=assets,
            dsp=(
                "generator:ffmpeg-aevalsrc",
                f"highpass:{spec.highpass_hz}Hz",
                f"lowpass:{spec.lowpass_hz}Hz",
                "loudnorm:-16LUFS",
            ),
        )

    bed_by_recipe: dict[str, BedAsset] = {}
    for bed in RECIPE_BEDS:
        recipe_id = bed.asset_id.removeprefix("ad-bed.").replace("-", "_")
        bed_by_recipe[recipe_id] = bed
        master = _render_bed_master(bed, masters_dir / f"{bed.asset_id}.mp3")
        _append_delivered_asset(
            pack_dir=pack_dir,
            source_master=master,
            asset_id=bed.asset_id,
            relative_path=bed.path,
            kind="ad_bed",
            title=bed.title,
            tags=("electronic", "atmospheric", "advertising-bed", recipe_id),
            role="texture",
            modification="Deterministic local advertising-bed synthesis.",
            sources=sources,
            assets=assets,
            dsp=("generator:ffmpeg-aevalsrc", "highpass:85Hz", "lowpass:4800Hz", "loudnorm:-16LUFS"),
        )

    cue_specs = tuple(cue for recipe in RECIPE_DEFINITIONS for cue in (recipe.cue_one, recipe.cue_two))
    for spec in cue_specs:
        master = _render_synth_master(spec, masters_dir / f"{spec.asset_id}.mp3")
        _append_delivered_asset(
            pack_dir=pack_dir,
            source_master=master,
            asset_id=spec.asset_id,
            relative_path=spec.path,
            kind=spec.kind,
            title=spec.title,
            tags=spec.tags,
            role=spec.role,
            modification="Deterministic local recipe-foreground synthesis.",
            sources=sources,
            assets=assets,
            dsp=(
                "generator:ffmpeg-aevalsrc",
                f"highpass:{spec.highpass_hz}Hz",
                f"lowpass:{spec.lowpass_hz}Hz",
                "loudnorm:-16LUFS",
            ),
        )

    recipes = _expected_recipes()

    # Exact core finals remain references only; none are installed as underlays.
    for artifact_id, label in CORE_PREVIEW_ARTIFACTS:
        approved = _artifact_path(core_manifest_path, artifacts, artifact_id)
        destination = _copy(approved, preview_root / "core" / f"{artifact_id.replace('.', '_')}.mp3")
        previews.append(_preview_record(run_root, destination, f"core.{artifact_id}", label, "approved-core"))
    previews.append(
        _preview_record(run_root, runtime_mid, "core.runtime-ad-mid", "Ad mid — runtime fit", "approved-core")
    )

    speech_paths, _speech_metadata = _resolve_frozen_spot_speech(core_manifest)
    previews.append(
        _preview_record(
            run_root,
            pack_dir / TIME_CHECK.path,
            "preview.identity.time-check",
            "Time check — runtime sting",
            "runtime-support",
        )
    )
    casa_preview = preview_root / "runtime" / "casa_notte_with_voice.mp3"
    casa_voice = speech_paths["speech.spot-a"]
    _render_casa_notte_preview(casa_voice, pack_dir / CASA_NOTTE.path, casa_preview)
    previews.append(
        _preview_record(
            run_root,
            casa_preview,
            "preview.bed.casa-notte-context",
            "Casa Notte — packaged fallback bed with approved voice",
            "runtime-support",
            render={
                "voice_id": "speech.spot-a",
                "voice_sha256": _sha256(casa_voice),
                "bed_asset_id": CASA_NOTTE.asset_id,
                "bed_gain_db": TALK_BED_GAIN_DB,
                "helpers": ["ImagingLibrary.pick_talk_bed", "mix_voice_with_bed"],
            },
        )
    )

    for spec in COMPATIBILITY_ASSETS:
        path = pack_dir / spec.path
        previews.append(_preview_record(run_root, path, f"preview.{spec.asset_id}", spec.title, "compatibility"))

    asset_by_id = {str(asset["id"]): asset for asset in assets}
    for index, definition in enumerate(RECIPE_DEFINITIONS):
        bed = bed_by_recipe[definition.recipe_id]
        voice_id = "speech.spot-a" if index % 2 == 0 else "speech.spot-b"
        preview_path = preview_root / "recipes" / f"{definition.recipe_id}.mp3"
        _render_recipe_preview(
            speech_paths[voice_id],
            pack_dir / bed.path,
            definition.bed_gain_db,
            (
                (pack_dir / definition.cue_one.path, "intro", -13.0, definition.cue_one.duration_sec),
                (pack_dir / definition.cue_two.path, "outro", -12.5, definition.cue_two.duration_sec),
            ),
            preview_path,
        )
        previews.append(
            _preview_record(
                run_root,
                preview_path,
                f"preview.recipe.{definition.recipe_id}",
                definition.recipe_id.replace("_", " ").title(),
                "advertising",
                render={
                    "voice_id": voice_id,
                    "voice_sha256": _sha256(speech_paths[voice_id]),
                    "bed_asset_id": bed.asset_id,
                    "bed_gain_db": definition.bed_gain_db,
                    "cues": [
                        {
                            "anchor": "intro",
                            "asset_id": definition.cue_one.asset_id,
                            "gain_db": -13.0,
                            "max_duration_sec": definition.cue_one.duration_sec,
                        },
                        {
                            "anchor": "outro",
                            "asset_id": definition.cue_two.asset_id,
                            "gain_db": -12.5,
                            "max_duration_sec": definition.cue_two.duration_sec,
                        },
                    ],
                    "helpers": ["loop_audio_bed", "mix_with_bed", "mix_oneshot_layers", "normalize_ad"],
                },
            )
        )
        if definition.cue_one.asset_id not in asset_by_id or definition.cue_two.asset_id not in asset_by_id:
            raise ValueError(f"Recipe preview references missing cue assets: {definition.recipe_id}")

    mid_proof = {
        "raw_pack_path": "bumpers/ad_mid.mp3",
        "raw_sha256": _sha256(pack_dir / "bumpers" / "ad_mid.mp3"),
        "helper": "mammamiradio.audio.imaging.ImagingLibrary.pick_ad_bumper",
        "duration_sec": 0.8,
        "target_lufs": -16.0,
        "fade_out_sec": 0.08,
        "runtime_output_sha256": actual_runtime_mid,
        "approved_core_output_sha256": expected_runtime_mid,
    }
    manifest = _build_manifest(
        generated_at=generated_at,
        core_manifest_path=core_manifest_path,
        core_digest=core_digest,
        sources=sources,
        assets=assets,
        recipes=recipes,
        previews=previews,
        mid_proof=mid_proof,
    )
    _render_board(run_root, manifest)
    _write_handoff(run_root)
    manifest["board_files"] = _board_file_records(run_root)
    content_digest = _json_digest(_content_digest_payload(manifest))
    manifest["content_digest"] = content_digest
    manifest["listening_receipt"] = _receipt(str(manifest["pack_digest"]), content_digest)
    _write_json(pack_dir / "manifest.json", manifest)
    report = _validator.validate_audio_asset_pack(pack_dir)
    _validator.write_attribution(report)
    return manifest


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Complete-pack manifest is not readable JSON: {manifest_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Complete-pack manifest root must be an object")
    return value


def _expected_asset_ids() -> set[str]:
    return {
        *(asset_id for _artifact, asset_id, _path, _kind, _tags, _role in CORE_PACK_ASSETS),
        "bumper.ad-mid",
        TIME_CHECK.asset_id,
        CASA_NOTTE.asset_id,
        *(spec.asset_id for spec in COMPATIBILITY_ASSETS),
        *(bed.asset_id for bed in RECIPE_BEDS),
        *(cue.asset_id for definition in RECIPE_DEFINITIONS for cue in (definition.cue_one, definition.cue_two)),
    }


def _expected_asset_contract() -> dict[str, tuple[str, str]]:
    contract = {
        asset_id: (relative_path, kind) for _artifact, asset_id, relative_path, kind, _tags, _role in CORE_PACK_ASSETS
    }
    contract["bumper.ad-mid"] = ("bumpers/ad_mid.mp3", "transition")
    contract[TIME_CHECK.asset_id] = (TIME_CHECK.path, TIME_CHECK.kind)
    contract[CASA_NOTTE.asset_id] = (CASA_NOTTE.path, "bed")
    contract.update({spec.asset_id: (spec.path, spec.kind) for spec in COMPATIBILITY_ASSETS})
    contract.update({bed.asset_id: (bed.path, "ad_bed") for bed in RECIPE_BEDS})
    contract.update(
        {
            cue.asset_id: (cue.path, cue.kind)
            for definition in RECIPE_DEFINITIONS
            for cue in (definition.cue_one, definition.cue_two)
        }
    )
    return contract


def _upstream_artifact_id_for_asset(asset_id: str) -> str | None:
    for artifact_id, core_asset_id, *_rest in CORE_PACK_ASSETS:
        if core_asset_id == asset_id:
            return artifact_id
    if asset_id == "bumper.ad-mid":
        return "cue.ad-mid"
    return None


def _expected_asset_metadata_contract() -> dict[str, dict[str, object]]:
    """Return exact provenance-bearing metadata for every delivered asset."""
    contract: dict[str, dict[str, object]] = {}

    def add(
        asset_id: str,
        kind: str,
        tags: Sequence[str],
        role: str,
        dsp: Sequence[str],
    ) -> None:
        source_id = _source_id_for_asset(asset_id)
        contract[asset_id] = {
            "purpose": f"Modern Night Drive {kind.replace('_', ' ')} asset.",
            "tags": list(tags),
            "source_id": source_id,
            "role": role,
            "dsp": list(dsp),
            "license": "CC0-1.0",
        }

    for _artifact_id, asset_id, _path, kind, tags, role in CORE_PACK_ASSETS:
        add(asset_id, kind, tags, role, ("copy:exact-approved-core-artifact",))
    add(
        "bumper.ad-mid",
        "transition",
        ("ad-bumper", "mid", "electronic", "single-separator"),
        "foreground",
        ("generator:ffmpeg-aevalsrc", "runtime-fit:deferred"),
    )
    add(
        TIME_CHECK.asset_id,
        TIME_CHECK.kind,
        TIME_CHECK.tags,
        TIME_CHECK.role,
        ("generator:ffmpeg-aevalsrc", "loudnorm:-16LUFS"),
    )
    add(
        CASA_NOTTE.asset_id,
        "bed",
        ("electronic", "atmospheric", "velvet-horizon", "casa-notte"),
        "texture",
        ("generator:ffmpeg-aevalsrc", "highpass:85Hz", "lowpass:4800Hz", "loudnorm:-16LUFS"),
    )
    for spec in COMPATIBILITY_ASSETS:
        add(
            spec.asset_id,
            spec.kind,
            spec.tags,
            spec.role,
            (
                "generator:ffmpeg-aevalsrc",
                f"highpass:{spec.highpass_hz}Hz",
                f"lowpass:{spec.lowpass_hz}Hz",
                "loudnorm:-16LUFS",
            ),
        )
    for bed in RECIPE_BEDS:
        recipe_id = bed.asset_id.removeprefix("ad-bed.").replace("-", "_")
        add(
            bed.asset_id,
            "ad_bed",
            ("electronic", "atmospheric", "advertising-bed", recipe_id),
            "texture",
            ("generator:ffmpeg-aevalsrc", "highpass:85Hz", "lowpass:4800Hz", "loudnorm:-16LUFS"),
        )
    for definition in RECIPE_DEFINITIONS:
        for cue in (definition.cue_one, definition.cue_two):
            add(
                cue.asset_id,
                cue.kind,
                cue.tags,
                cue.role,
                (
                    "generator:ffmpeg-aevalsrc",
                    f"highpass:{cue.highpass_hz}Hz",
                    f"lowpass:{cue.lowpass_hz}Hz",
                    "loudnorm:-16LUFS",
                ),
            )
    return contract


def _source_id_for_asset(asset_id: str) -> str:
    """Return the immutable public source identity for one delivered asset."""
    if asset_id == "compat.mandolin-sting":
        return "project.compat.legacy-relay"
    return f"project.{asset_id}"


def _expected_source_metadata_contract(
    generator: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Return the exact authored source record for every retained master.

    Audio hashes are validated against live master and delivery bytes later.
    Everything else, including the pack-internal master path, is static authoring
    provenance and must not be inferred from a mutable manifest record.
    """
    asset_metadata = _expected_asset_metadata_contract()
    contract: dict[str, dict[str, object]] = {}

    def add(asset_id: str, title: str, modification: str) -> None:
        source_id = _source_id_for_asset(asset_id)
        tags = asset_metadata[asset_id]["tags"]
        assert isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)
        contract[source_id] = {
            "id": source_id,
            "license": "CC0-1.0",
            "source_url": generator.get("url"),
            "creator": "Mammami Radio project",
            "title": title,
            "modification": modification,
            "original_path": f"provenance/source-masters/{asset_id}.mp3",
            "tags": list(tags),
            "provenance_type": "project-authored-deterministic-master",
            "generator": generator,
        }

    for artifact_id, asset_id, _path, _kind, _tags, _role in CORE_PACK_ASSETS:
        add(
            asset_id,
            f"Approved core artifact {artifact_id}",
            "Exact byte copy from the digest-approved project-authored core cadence.",
        )
    add(
        "bumper.ad-mid",
        "Project-authored electronic mid-break separator",
        "Deterministic local synthesis retained raw for one runtime fit.",
    )
    add(
        TIME_CHECK.asset_id,
        TIME_CHECK.title,
        "Deterministic local electronic time-check synthesis.",
    )
    add(
        CASA_NOTTE.asset_id,
        CASA_NOTTE.title,
        "Deterministic local atmospheric synthesis.",
    )
    for spec in COMPATIBILITY_ASSETS:
        add(
            spec.asset_id,
            spec.title,
            "Deterministic local electronic compatibility synthesis.",
        )
    for bed in RECIPE_BEDS:
        add(
            bed.asset_id,
            bed.title,
            "Deterministic local advertising-bed synthesis.",
        )
    for definition in RECIPE_DEFINITIONS:
        for cue in (definition.cue_one, definition.cue_two):
            add(
                cue.asset_id,
                cue.title,
                "Deterministic local recipe-foreground synthesis.",
            )
    return contract


def _expected_recipes() -> list[dict[str, object]]:
    bed_by_recipe = {bed.asset_id.removeprefix("ad-bed.").replace("-", "_"): bed for bed in RECIPE_BEDS}
    return [
        {
            "id": definition.recipe_id,
            "bed": {
                "asset_id": bed_by_recipe[definition.recipe_id].asset_id,
                "gain_db": definition.bed_gain_db,
            },
            "cues": [
                {
                    "anchor": "intro",
                    "asset_id": definition.cue_one.asset_id,
                    "gain_db": -13.0,
                    "max_duration_sec": definition.cue_one.duration_sec,
                },
                {
                    "anchor": "outro",
                    "asset_id": definition.cue_two.asset_id,
                    "gain_db": -12.5,
                    "max_duration_sec": definition.cue_two.duration_sec,
                },
            ],
        }
        for definition in RECIPE_DEFINITIONS
    ]


def _expected_preview_contract() -> dict[str, tuple[str, str, str, bool]]:
    contract = {
        f"core.{artifact_id}": (
            f"previews/core/{artifact_id.replace('.', '_')}.mp3",
            "approved-core",
            label,
            False,
        )
        for artifact_id, label in CORE_PREVIEW_ARTIFACTS
    }
    contract["core.runtime-ad-mid"] = (
        "previews/core/ad_mid_runtime.mp3",
        "approved-core",
        "Ad mid — runtime fit",
        False,
    )
    contract["preview.identity.time-check"] = (
        f"pack/{TIME_CHECK.path}",
        "runtime-support",
        "Time check — runtime sting",
        False,
    )
    contract["preview.bed.casa-notte-context"] = (
        "previews/runtime/casa_notte_with_voice.mp3",
        "runtime-support",
        "Casa Notte — packaged fallback bed with approved voice",
        True,
    )
    contract.update(
        {
            f"preview.{spec.asset_id}": (f"pack/{spec.path}", "compatibility", spec.title, False)
            for spec in COMPATIBILITY_ASSETS
        }
    )
    contract.update(
        {
            f"preview.recipe.{definition.recipe_id}": (
                f"previews/recipes/{definition.recipe_id}.mp3",
                "advertising",
                definition.recipe_id.replace("_", " ").title(),
                True,
            )
            for definition in RECIPE_DEFINITIONS
        }
    )
    return contract


def _expected_casa_preview_render(voice_path: Path) -> dict[str, object]:
    return {
        "voice_id": "speech.spot-a",
        "voice_sha256": _sha256(voice_path),
        "bed_asset_id": CASA_NOTTE.asset_id,
        "bed_gain_db": TALK_BED_GAIN_DB,
        "helpers": ["ImagingLibrary.pick_talk_bed", "mix_voice_with_bed"],
    }


def _expected_recipe_preview_render(
    definition: RecipeDefinition,
    *,
    voice_id: str,
    voice_path: Path,
    bed: BedAsset,
) -> dict[str, object]:
    return {
        "voice_id": voice_id,
        "voice_sha256": _sha256(voice_path),
        "bed_asset_id": bed.asset_id,
        "bed_gain_db": definition.bed_gain_db,
        "cues": [
            {
                "anchor": "intro",
                "asset_id": definition.cue_one.asset_id,
                "gain_db": -13.0,
                "max_duration_sec": definition.cue_one.duration_sec,
            },
            {
                "anchor": "outro",
                "asset_id": definition.cue_two.asset_id,
                "gain_db": -12.5,
                "max_duration_sec": definition.cue_two.duration_sec,
            },
        ],
        "helpers": ["loop_audio_bed", "mix_with_bed", "mix_oneshot_layers", "normalize_ad"],
    }


def _generated_at(value: object, *, now: datetime | None = None) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Complete pack has no compact UTC generation timestamp")
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("Complete-pack generated_at must use YYYYMMDDTHHMMSSZ") from exc
    current = now or datetime.now(UTC)
    if parsed.timestamp() > current.timestamp() + REVIEW_CLOCK_TOLERANCE_SEC:
        raise ValueError("Complete-pack generation timestamp is in the future")
    return parsed


def _reviewed_at(value: object, *, generated_at: datetime, now: datetime | None = None) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Decided complete pack has no review timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Decided complete pack has an invalid review timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Decided complete-pack timestamp must include a timezone")
    parsed_utc = parsed.astimezone(UTC)
    current = now or datetime.now(UTC)
    if parsed_utc < generated_at:
        raise ValueError("Complete-pack review predates candidate generation")
    if parsed_utc.timestamp() > current.timestamp() + REVIEW_CLOCK_TOLERANCE_SEC:
        raise ValueError("Complete-pack review timestamp is in the future")
    return parsed_utc


def _validate_receipt(manifest: Mapping[str, object], *, now: datetime | None = None) -> None:
    receipt = manifest.get("listening_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("Complete pack has no listening receipt")
    expected_fields = {
        "schema_version",
        "status",
        "pack_digest",
        "content_digest",
        "target",
        "surfaces",
        "device_classes",
        "prompt",
        "mac",
        "small_speaker",
        "sonos_arc",
        "notes",
        "reviewed_at",
        "approved_candidate_id",
    }
    if set(receipt) != expected_fields:
        raise ValueError("Complete-pack listening receipt has an invalid field set")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("Complete-pack listening receipt has the wrong schema")
    if receipt.get("pack_digest") != manifest.get("pack_digest"):
        raise ValueError("Complete-pack listening receipt is stale for the asset digest")
    if receipt.get("content_digest") != manifest.get("content_digest"):
        raise ValueError("Complete-pack listening receipt is stale for the content digest")
    if receipt.get("target") != TARGET or receipt.get("prompt") != LISTENING_PROMPT:
        raise ValueError("Complete-pack listening receipt has stale instructions")
    if receipt.get("surfaces") != list(REQUIRED_SURFACES):
        raise ValueError("Complete-pack listening receipt has stale surfaces")
    if receipt.get("device_classes") != list(REQUIRED_DEVICE_CLASSES):
        raise ValueError("Complete-pack listening receipt has stale device classes")
    generated = _generated_at(manifest.get("generated_at"), now=now)
    status = receipt.get("status")
    if status == "pending":
        if manifest.get("release_ready") is not False:
            raise ValueError("Pending complete pack cannot be release-ready")
        for field in ("mac", "small_speaker", "sonos_arc", "notes", "reviewed_at", "approved_candidate_id"):
            if receipt.get(field) is not None:
                raise ValueError(f"Pending receipt has a decided {field} result")
        return
    if status not in {"approved", "rejected"}:
        raise ValueError("Complete-pack listening receipt status must be pending, approved, or rejected")
    for field in ("mac", "small_speaker", "sonos_arc"):
        if receipt.get(field) not in DECISION_RESULTS:
            raise ValueError(f"Decided complete pack has an invalid {field} result")
    notes = receipt.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("Decided complete pack requires listening notes")
    _reviewed_at(receipt.get("reviewed_at"), generated_at=generated, now=now)
    if status == "approved":
        if manifest.get("release_ready") is not True:
            raise ValueError("Approved complete pack must be marked release-ready")
        if receipt.get("approved_candidate_id") != CANDIDATE_ID:
            raise ValueError("Approved receipt has the wrong candidate id")
        for field in ("mac", "small_speaker", "sonos_arc"):
            if receipt.get(field) != "pass":
                raise ValueError(f"Approved complete pack requires {field}=pass")
        return
    if manifest.get("release_ready") is not False:
        raise ValueError("Rejected complete pack cannot be release-ready")
    if receipt.get("approved_candidate_id") is not None:
        raise ValueError("Rejected complete pack cannot name an approved candidate")
    if all(receipt.get(field) == "pass" for field in ("mac", "small_speaker", "sonos_arc")):
        raise ValueError("Rejected complete pack must identify a failed listening target")


def validate_complete_audio_pack(manifest_path: Path) -> tuple[dict[str, Any], tuple[Path, ...]]:
    """Validate provenance, exact upstream core, previews, recipes, and receipt."""
    if manifest_path.is_symlink():
        raise ValueError("Complete-pack manifest must not be a symlink")
    manifest_path = manifest_path.resolve(strict=True)
    _assert_runtime_audio_contract()
    manifest = _read_manifest(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("stage") != STAGE:
        raise ValueError("Complete-pack manifest has the wrong schema or stage")
    if manifest.get("quality_guard_allowlist") != _quality_guard_allowlist():
        raise ValueError("Complete-pack perceptual-overlap allowlist differs from the reviewed contract")
    expected_content = _json_digest(_content_digest_payload(manifest))
    if manifest.get("content_digest") != expected_content:
        raise ValueError("Complete-pack content digest differs from manifest semantics")
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("Complete-pack manifest has an invalid top-level field set")
    if manifest.get("design_direction") != _design_direction_contract():
        raise ValueError("Complete-pack design direction differs from the approved contract")
    if manifest.get("limitations") != list(_LIMITATIONS):
        raise ValueError("Complete-pack limitations differ from the approved contract")
    pack_dir = manifest_path.parent
    run_root = pack_dir.parent
    report = _validator.validate_audio_asset_pack(
        pack_dir,
        require_listening_approval=manifest.get("listening_receipt", {}).get("status") == "approved"
        if isinstance(manifest.get("listening_receipt"), Mapping)
        else False,
    )
    _validator.check_attribution(report)

    upstream = manifest.get("upstream_core")
    if not isinstance(upstream, Mapping):
        raise ValueError("Complete pack has no approved-core lineage")
    core_manifest_path = _core._path_from_record(
        upstream.get("manifest_path"), upstream.get("manifest_path_kind"), label="complete-pack core manifest"
    )
    core_digest = _core.assert_core_listening_gate(core_manifest_path)
    if upstream.get("pack_digest") != core_digest:
        raise ValueError("Complete-pack approved-core digest is stale")
    core_manifest, _core_paths = _core.validate_core_cadence_gate(core_manifest_path)
    artifacts = _core_artifacts(core_manifest)

    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list) or not all(isinstance(item, Mapping) for item in raw_assets):
        raise ValueError("Complete pack has an invalid asset inventory")
    assets = {str(item["id"]): item for item in raw_assets}
    if set(assets) != _expected_asset_ids() or len(assets) != len(raw_assets):
        missing = sorted(_expected_asset_ids() - set(assets))
        extra = sorted(set(assets) - _expected_asset_ids())
        raise ValueError(f"Complete-pack asset inventory differs (missing={missing}, extra={extra})")
    delivered_contract = {
        asset_id: (str(asset.get("path")), str(asset.get("kind"))) for asset_id, asset in assets.items()
    }
    if delivered_contract != _expected_asset_contract():
        raise ValueError("Complete-pack asset path/kind contract differs from the runtime inventory")
    expected_metadata = _expected_asset_metadata_contract()
    for asset_id, asset in assets.items():
        expected_asset_metadata = expected_metadata[asset_id]
        upstream_artifact_id = _upstream_artifact_id_for_asset(asset_id)
        expected_asset_fields = _ASSET_FIELDS | ({"upstream_lineage"} if upstream_artifact_id is not None else set())
        if set(asset) != expected_asset_fields:
            raise ValueError(f"Complete-pack asset has an invalid field set: {asset_id}")
        layers = asset.get("layers")
        if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], Mapping):
            raise ValueError(f"Complete-pack asset must have one exact provenance layer: {asset_id}")
        layer = layers[0]
        if set(layer) != _LAYER_FIELDS:
            raise ValueError(f"Complete-pack asset layer has an invalid field set: {asset_id}")
        actual = {
            "purpose": asset.get("purpose"),
            "tags": asset.get("tags"),
            "source_id": layer.get("source_id"),
            "role": layer.get("role"),
            "dsp": layer.get("dsp"),
            "license": asset.get("license"),
            "layer_license": layer.get("license"),
            "source_ids": asset.get("source_ids"),
        }
        exact = {
            **expected_asset_metadata,
            "layer_license": expected_asset_metadata["license"],
            "source_ids": [expected_asset_metadata["source_id"]],
        }
        if actual != exact:
            raise ValueError(f"Complete-pack asset provenance metadata differs from its authoring contract: {asset_id}")
        actual_lineage = asset.get("upstream_lineage")
        if upstream_artifact_id is None:
            if actual_lineage is not None:
                raise ValueError(f"Complete-pack generated asset has unexpected upstream lineage: {asset_id}")
        elif actual_lineage != _upstream_core_lineage(core_manifest, upstream_artifact_id):
            raise ValueError(f"Complete-pack upstream core lineage differs from approved artifact: {asset_id}")
    for asset_id, asset in assets.items():
        asset_path = pack_dir / str(asset["path"])
        format_evidence, measured_duration = _core._probe_audio(asset_path)
        if (
            asset.get("format") != format_evidence
            or format_evidence != FORMAT
            or asset.get("duration_target_sec") != measured_duration
        ):
            raise ValueError(f"Complete-pack asset format or duration evidence is stale: {asset_id}")
        expected_quality = _audio_quality_evidence(
            asset_path,
            quality_class="asset",
            target_lufs=ASSET_TARGET_LUFS,
            tolerance_lu=ASSET_TOLERANCE_LU,
        )
        if asset.get("audio_quality") != expected_quality:
            raise ValueError(f"Complete-pack asset loudness or true-peak evidence is stale: {asset_id}")

    # Exact approved runtime underlays and markers may not drift between gates.
    for artifact_id, asset_id, relative_path, _kind, _tags, _role in CORE_PACK_ASSETS:
        expected = _artifact_path(core_manifest_path, artifacts, artifact_id)
        delivered = pack_dir / relative_path
        if _sha256(delivered) != _sha256(expected) or assets[asset_id].get("sha256") != _sha256(expected):
            raise ValueError(f"Complete-pack core asset differs from approved artifact: {asset_id}")

    production = manifest.get("production_contract")
    if not isinstance(production, Mapping) or production.get("station_and_sweeper_are_runtime_underlays") is not True:
        raise ValueError("Complete pack does not declare the runtime-underlay identity contract")
    if set(production) != {
        "format",
        "runtime_audio",
        "audio_quality",
        "station_and_sweeper_are_runtime_underlays",
        "ad_bumper_roles",
        "mid_runtime_fit",
        "recipe_preview_helpers",
        "talk_bed_preview_helpers",
    }:
        raise ValueError("Complete pack has a stale production-contract field set")
    if production.get("format") != FORMAT or production.get("ad_bumper_roles") != ["in", "mid", "out"]:
        raise ValueError("Complete pack has stale format or bumper-role settings")
    if production.get("runtime_audio") != RUNTIME_AUDIO_CONTRACT:
        raise ValueError("Complete pack has stale runtime audio settings")
    if production.get("audio_quality") != AUDIO_QUALITY_CONTRACT:
        raise ValueError("Complete pack has stale loudness or true-peak settings")
    if production.get("recipe_preview_helpers") != [
        "loop_audio_bed",
        "mix_with_bed",
        "mix_oneshot_layers",
        "normalize_ad",
    ] or production.get("talk_bed_preview_helpers") != [
        "ImagingLibrary.pick_talk_bed",
        "mix_voice_with_bed",
    ]:
        raise ValueError("Complete pack has stale runtime preview helper settings")
    mid_proof = production.get("mid_runtime_fit")
    if not isinstance(mid_proof, Mapping):
        raise ValueError("Complete pack has no runtime ad-mid proof")
    raw_mid = pack_dir / "bumpers" / "ad_mid.mp3"
    if mid_proof.get("raw_sha256") != _sha256(raw_mid):
        raise ValueError("Complete-pack raw ad-mid hash is stale")
    approved_mid = _artifact_path(core_manifest_path, artifacts, "cue.ad-mid")
    with tempfile.TemporaryDirectory(prefix="complete-mid-validate-") as temp_dir:
        fitted = Path(temp_dir) / "ad_mid.mp3"
        _render_runtime_mid_from_pack(pack_dir, fitted)
        fitted_sha = _sha256(fitted)
    if fitted_sha != _sha256(approved_mid) or mid_proof.get("runtime_output_sha256") != fitted_sha:
        raise ValueError("Complete-pack ad-mid does not reproduce the approved runtime fit")
    expected_mid_proof = {
        "raw_pack_path": "bumpers/ad_mid.mp3",
        "raw_sha256": _sha256(raw_mid),
        "helper": "mammamiradio.audio.imaging.ImagingLibrary.pick_ad_bumper",
        "duration_sec": 0.8,
        "target_lufs": -16.0,
        "fade_out_sec": 0.08,
        "runtime_output_sha256": fitted_sha,
        "approved_core_output_sha256": _sha256(approved_mid),
    }
    if dict(mid_proof) != expected_mid_proof:
        raise ValueError("Complete-pack ad-mid runtime proof has stale helper settings")

    raw_recipes = manifest.get("recipes")
    if raw_recipes != _expected_recipes():
        raise ValueError("Complete pack does not contain the exact nine recipe definitions")
    assert isinstance(raw_recipes, list)
    cue_ids: list[str] = []
    for recipe in raw_recipes:
        assert isinstance(recipe, Mapping)
        raw_cues = recipe.get("cues")
        if not isinstance(raw_cues, list) or len(raw_cues) != 2:
            raise ValueError(f"Recipe must contain exactly two foreground cues: {recipe.get('id')}")
        recipe_cues = [str(cue.get("asset_id")) for cue in raw_cues if isinstance(cue, Mapping)]
        if len(recipe_cues) != 2 or len(set(recipe_cues)) != 2:
            raise ValueError(f"Recipe repeats a foreground cue: {recipe.get('id')}")
        cue_ids.extend(recipe_cues)
    if len(cue_ids) != len(set(cue_ids)):
        raise ValueError("One advertising foreground cue appears in more than one recipe")
    cue_source_ids: dict[str, str] = {}
    for cue_id in cue_ids:
        cue_asset = assets[cue_id]
        layers = cue_asset.get("layers")
        if not isinstance(layers, list) or len(layers) != 1 or not isinstance(layers[0], Mapping):
            raise ValueError(f"Advertising cue must have exactly one foreground layer: {cue_id}")
        layer = layers[0]
        source_id = layer.get("source_id")
        if layer.get("role") != "foreground" or not isinstance(source_id, str) or not source_id.startswith("project."):
            raise ValueError(f"Advertising cue lacks one project-authored foreground source: {cue_id}")
        if cue_asset.get("source_ids") != [source_id]:
            raise ValueError(f"Advertising cue source ledger differs from its foreground layer: {cue_id}")
        cue_source_ids[cue_id] = source_id
    if len(set(cue_source_ids.values())) != len(cue_source_ids):
        raise ValueError("One project-authored foreground source appears in more than one advertising cue")

    # No rejected sonic material may be relabeled in provenance. The legacy
    # compatibility filename/id is the sole unavoidable historical symbol.
    raw_sources = manifest.get("sources")
    if (
        not isinstance(raw_sources, list)
        or not raw_sources
        or not all(isinstance(source, Mapping) for source in raw_sources)
    ):
        raise ValueError("Complete pack has no source ledger")
    first_generator = raw_sources[0].get("generator")
    if not isinstance(first_generator, Mapping):
        raise ValueError("Complete-pack source ledger has no shared generator evidence")
    source_by_id = {str(source.get("id")): source for source in raw_sources}
    expected_sources = _expected_source_metadata_contract(first_generator)
    if set(source_by_id) != set(expected_sources) or len(source_by_id) != len(raw_sources):
        missing = sorted(set(expected_sources) - set(source_by_id))
        extra = sorted(set(source_by_id) - set(expected_sources))
        raise ValueError(f"Complete-pack source inventory differs (missing={missing}, extra={extra})")
    for asset_id, asset in assets.items():
        source_ids = asset.get("source_ids")
        layers = asset.get("layers")
        if (
            not isinstance(source_ids, list)
            or len(source_ids) != 1
            or not isinstance(source_ids[0], str)
            or not isinstance(layers, list)
            or len(layers) != 1
            or not isinstance(layers[0], Mapping)
        ):
            raise ValueError(f"Complete-pack asset must have one retained source master: {asset_id}")
        source_id = source_ids[0]
        source = source_by_id.get(source_id)
        layer = layers[0]
        if source is None or not isinstance(source.get("original_path"), str):
            raise ValueError(f"Complete-pack asset source master is missing: {asset_id}")
        if (
            source.get("id") != source_id
            or source.get("tags") != expected_metadata[asset_id]["tags"]
            or source.get("license") != expected_metadata[asset_id]["license"]
            or source.get("provenance_type") != "project-authored-deterministic-master"
        ):
            raise ValueError(f"Complete-pack retained source metadata differs from its asset contract: {asset_id}")
        delivered_path = pack_dir / str(asset["path"])
        raw_master_path = pack_dir / str(source["original_path"])
        if raw_master_path.is_symlink():
            raise ValueError("Complete-pack source master must not be a symlink")
        master_path = raw_master_path.resolve(strict=True)
        if pack_dir.resolve() not in master_path.parents:
            raise ValueError("Complete-pack source master escapes the promotable pack")
        delivered_sha = _sha256(delivered_path)
        master_sha = _sha256(master_path)
        if not (
            delivered_sha
            == master_sha
            == asset.get("sha256")
            == source.get("source_sha256")
            == layer.get("source_sha256")
        ):
            raise ValueError(f"Complete-pack asset differs from its retained source master: {asset_id}")
        if (
            layer.get("source_id") != source_id
            or layer.get("source_start_sec") != 0.0
            or layer.get("output_offset_sec") != 0.0
            or layer.get("gain_db") != 0.0
            or layer.get("duration_sec") != asset.get("duration_target_sec")
        ):
            raise ValueError(f"Complete-pack asset layer does not describe an exact source-master copy: {asset_id}")
    asset_id_by_source = {str(metadata["source_id"]): asset_id for asset_id, metadata in expected_metadata.items()}
    for source_id, expected_source in expected_sources.items():
        asset_id = asset_id_by_source[source_id]
        source = source_by_id[source_id]
        if set(source) != {*expected_source, "source_sha256"} or any(
            source.get(field) != value for field, value in expected_source.items()
        ):
            raise ValueError(f"Complete-pack retained source metadata differs from its authoring contract: {asset_id}")
    for cue_id, source_id in cue_source_ids.items():
        source = source_by_id.get(source_id)
        if source is None or source.get("provenance_type") != "project-authored-deterministic-master":
            raise ValueError(f"Advertising cue source is not project-authored deterministic material: {cue_id}")
    _validate_current_generator_evidence(raw_sources)
    semantic_blob = " ".join(
        str(value)
        for source in raw_sources
        if isinstance(source, Mapping)
        for key, value in source.items()
        if key != "original_path"
    ).casefold()
    forbidden = sorted(term for term in FORBIDDEN_SONIC_TERMS if term in semantic_blob)
    if forbidden:
        raise ValueError(f"Complete-pack provenance retains rejected sonic terms: {', '.join(forbidden)}")

    raw_previews = manifest.get("previews")
    if not isinstance(raw_previews, list):
        raise ValueError("Complete pack has no board preview ledger")
    preview_paths: list[Path] = []
    preview_ids: set[str] = set()
    preview_by_id: dict[str, Mapping[str, object]] = {}
    preview_path_by_id: dict[str, Path] = {}
    preview_contract: dict[str, tuple[str, str]] = {}
    expected_previews = _expected_preview_contract()
    for preview in raw_previews:
        if not isinstance(preview, Mapping):
            raise ValueError("Complete-pack preview ledger contains a non-object")
        preview_id = str(preview.get("id"))
        if preview_id in preview_ids:
            raise ValueError(f"Complete-pack preview id repeats: {preview_id}")
        preview_ids.add(preview_id)
        relative = preview.get("path")
        if not isinstance(relative, str):
            raise ValueError(f"Complete-pack preview has no path: {preview_id}")
        raw_path = run_root / relative
        if raw_path.is_symlink():
            raise ValueError(f"Complete-pack preview must not be a symlink: {preview_id}")
        path = raw_path.resolve(strict=True)
        if run_root.resolve() not in path.parents:
            raise ValueError(f"Complete-pack preview escapes the board: {preview_id}")
        if preview.get("sha256") != _sha256(path):
            raise ValueError(f"Complete-pack preview changed after digesting: {preview_id}")
        evidence, duration = _core._probe_audio(path)
        if preview.get("format") != evidence or evidence != FORMAT or preview.get("duration_sec") != duration:
            raise ValueError(f"Complete-pack preview format evidence is stale: {preview_id}")
        preview_paths.append(path)
        group = str(preview.get("group"))
        preview_contract[preview_id] = (relative, group)
        preview_by_id[preview_id] = preview
        preview_path_by_id[preview_id] = path
    expected_inventory = {
        preview_id: (relative, group)
        for preview_id, (relative, group, _label, _render_required) in expected_previews.items()
    }
    if preview_contract != expected_inventory:
        raise ValueError("Complete-pack board has the wrong exact preview inventory")
    for preview_id, (_relative, group, label, render_required) in expected_previews.items():
        preview = preview_by_id[preview_id]
        expected_preview_fields = _PREVIEW_FIELDS | ({"render"} if render_required else set())
        if set(preview) != expected_preview_fields:
            raise ValueError(f"Complete-pack preview has an invalid field set: {preview_id}")
        if preview.get("label") != label:
            raise ValueError(f"Complete-pack preview label differs from its listening contract: {preview_id}")
        advertising = group == "advertising"
        expected_quality = _audio_quality_evidence(
            preview_path_by_id[preview_id],
            quality_class="advertising-preview" if advertising else "program-preview",
            target_lufs=ADVERTISING_PREVIEW_TARGET_LUFS if advertising else PROGRAM_PREVIEW_TARGET_LUFS,
            tolerance_lu=ADVERTISING_PREVIEW_TOLERANCE_LU if advertising else PROGRAM_PREVIEW_TOLERANCE_LU,
        )
        if preview.get("audio_quality") != expected_quality:
            raise ValueError(f"Complete-pack preview loudness or true-peak evidence is stale: {preview_id}")

    for artifact_id, _label in CORE_PREVIEW_ARTIFACTS:
        preview = preview_by_id[f"core.{artifact_id}"]
        expected = _artifact_path(core_manifest_path, artifacts, artifact_id)
        if preview.get("sha256") != _sha256(expected):
            raise ValueError(f"Approved core preview differs from its listened artifact: {artifact_id}")
    if preview_by_id["core.runtime-ad-mid"].get("sha256") != fitted_sha:
        raise ValueError("Complete-pack runtime ad-mid preview differs from the proven runtime fit")

    speech_paths, _speech_metadata = _resolve_frozen_spot_speech(core_manifest)
    casa_record = preview_by_id["preview.bed.casa-notte-context"]
    if casa_record.get("render") != _expected_casa_preview_render(speech_paths["speech.spot-a"]):
        raise ValueError("Casa Notte listening preview has stale runtime render evidence")
    bed_by_recipe = {bed.asset_id.removeprefix("ad-bed.").replace("-", "_"): bed for bed in RECIPE_BEDS}
    with tempfile.TemporaryDirectory(prefix="complete-preview-validate-") as temp_dir:
        scratch = Path(temp_dir)
        runtime_imaging = _runtime_imaging_library(pack_dir, scratch)
        rendered_time_check = scratch / "time_check.mp3"
        runtime_imaging.pick_time_check_sting(rendered_time_check)
        if _sha256(rendered_time_check) != str(preview_by_id["preview.identity.time-check"]["sha256"]):
            raise ValueError("Time-check listening preview differs from the public runtime selection")
        rendered_casa = scratch / "casa_notte.mp3"
        _render_casa_notte_preview(
            speech_paths["speech.spot-a"],
            pack_dir / CASA_NOTTE.path,
            rendered_casa,
        )
        if _sha256(rendered_casa) != str(casa_record["sha256"]):
            raise ValueError("Casa Notte listening preview differs from the runtime-faithful render")
        for index, definition in enumerate(RECIPE_DEFINITIONS):
            voice_id = "speech.spot-a" if index % 2 == 0 else "speech.spot-b"
            bed = bed_by_recipe[definition.recipe_id]
            preview_id = f"preview.recipe.{definition.recipe_id}"
            preview = preview_by_id[preview_id]
            expected_render = _expected_recipe_preview_render(
                definition,
                voice_id=voice_id,
                voice_path=speech_paths[voice_id],
                bed=bed,
            )
            if preview.get("render") != expected_render:
                raise ValueError(f"Recipe preview has stale runtime render evidence: {definition.recipe_id}")
            resolved = runtime_imaging.resolve_ad_recipe(definition.recipe_id, f"complete-gate-{index}")
            if resolved is None or resolved.bed_path != pack_dir / bed.path or len(resolved.cues) != 2:
                raise ValueError(f"Recipe does not resolve through the public runtime seam: {definition.recipe_id}")
            first_cue, second_cue = resolved.cues
            expected_resolved_cues = (
                (pack_dir / definition.cue_one.path, "intro", -13.0, definition.cue_one.duration_sec),
                (pack_dir / definition.cue_two.path, "outro", -12.5, definition.cue_two.duration_sec),
            )
            actual_resolved_cues = (
                (first_cue.asset_path, first_cue.anchor, first_cue.gain_db, first_cue.max_duration_sec),
                (second_cue.asset_path, second_cue.anchor, second_cue.gain_db, second_cue.max_duration_sec),
            )
            if resolved.bed_gain_db != definition.bed_gain_db or actual_resolved_cues != expected_resolved_cues:
                raise ValueError(
                    f"Recipe runtime resolution differs from its approved definition: {definition.recipe_id}"
                )
            rendered = scratch / f"{definition.recipe_id}.mp3"
            _render_recipe_preview(
                speech_paths[voice_id],
                resolved.bed_path,
                resolved.bed_gain_db,
                actual_resolved_cues,
                rendered,
            )
            if _sha256(rendered) != str(preview["sha256"]):
                raise ValueError(
                    f"Recipe listening preview differs from the runtime-faithful render: {definition.recipe_id}"
                )

    expected_board_files = _board_file_records(run_root)
    if manifest.get("board_files") != expected_board_files:
        raise ValueError("Complete-pack listening-surface hash ledger is stale")
    expected_surfaces = {"index.html": _board_html(manifest), "README.md": _handoff_text()}
    for filename, expected_text in expected_surfaces.items():
        path = run_root / filename
        if path.is_symlink() or path.read_text(encoding="utf-8") != expected_text:
            raise ValueError(f"Complete-pack listening surface differs from the manifest: {filename}")
    ready_path = run_root / ".ready"
    if ready_path.is_symlink() or ready_path.read_text(encoding="ascii") != f"{manifest['content_digest']}\n":
        raise ValueError("Complete-pack ready marker does not match the content digest")

    expected_files = {
        "pack/manifest.json",
        "pack/ATTRIBUTION.md",
        ".ready",
        *(str(item["path"]) for item in expected_board_files),
        *(f"pack/{asset['path']}" for asset in raw_assets),
        *(str(preview["path"]) for preview in raw_previews),
    }
    for source in raw_sources:
        if not isinstance(source, Mapping) or not isinstance(source.get("original_path"), str):
            raise ValueError("Complete-pack source ledger has no local master path")
        raw_source_path = pack_dir / str(source["original_path"])
        if raw_source_path.is_symlink():
            raise ValueError("Complete-pack source master must not be a symlink")
        source_path = raw_source_path.resolve(strict=True)
        if pack_dir.resolve() not in source_path.parents:
            raise ValueError("Complete-pack source master escapes the promotable pack")
        expected_files.add(source_path.relative_to(run_root).as_posix())
    expected_dirs = {
        parent.as_posix()
        for relative in expected_files
        for parent in Path(relative).parents
        if parent.as_posix() != "."
    }
    inventory_entries = tuple(run_root.rglob("*"))
    symlinks = [path for path in inventory_entries if path.is_symlink()]
    if symlinks:
        raise ValueError("Complete-pack board inventory cannot contain symlinks")
    special_entries = [path for path in inventory_entries if not path.is_file() and not path.is_dir()]
    if special_entries:
        raise ValueError("Complete-pack board inventory cannot contain special filesystem entries")
    actual_files = {path.relative_to(run_root).as_posix() for path in inventory_entries if path.is_file()}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise ValueError(f"Complete-pack board inventory differs (missing={missing}, extra={extra})")
    actual_dirs = {path.relative_to(run_root).as_posix() for path in inventory_entries if path.is_dir()}
    if actual_dirs != expected_dirs:
        missing = sorted(expected_dirs - actual_dirs)
        extra = sorted(actual_dirs - expected_dirs)
        raise ValueError(f"Complete-pack board directory inventory differs (missing={missing}, extra={extra})")
    _validate_receipt(manifest)
    return manifest, tuple(preview_paths)


def render_complete_audio_pack(
    core_manifest_path: Path,
    output_root: Path,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Atomically render and validate one non-installing complete-pack candidate."""
    _generated_at(generated_at)
    _require_safe_output_root(output_root)
    output_root = output_root.resolve(strict=False)
    if output_root.exists():
        raise FileExistsError(f"Complete-pack output already exists: {output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)
    staging = output_root.parent / f".{output_root.name}.{uuid4().hex}.staging"
    _require_safe_output_root(staging)
    try:
        staging.mkdir()
        manifest = _render_complete_pack_in_place(
            core_manifest_path,
            staging,
            generated_at=generated_at,
        )
        (staging / ".ready").write_text(str(manifest["content_digest"]) + "\n", encoding="utf-8")
        validate_complete_audio_pack(staging / "pack" / "manifest.json")
        os.replace(staging, output_root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _complete_evidence_snapshot(manifest_path: Path) -> dict[str, str]:
    run_root = manifest_path.parent.parent
    snapshot: dict[str, str] = {}
    for path in run_root.rglob("*"):
        relative = path.relative_to(run_root).as_posix()
        if path.is_symlink():
            raise ValueError("Complete-pack evidence cannot contain symlinks")
        if path.is_file():
            if path != manifest_path:
                snapshot[relative] = _sha256(path)
        elif path.is_dir():
            snapshot[f"{relative}/"] = "directory"
        else:
            raise ValueError("Complete-pack evidence cannot contain special filesystem entries")
    return snapshot


def record_complete_listening_receipt(
    manifest_path: Path,
    decision_path: Path,
    *,
    reviewed_at: str | None = None,
) -> Path:
    """Atomically record one digest-bound approved or rejected listening decision."""
    if manifest_path.is_symlink():
        raise ValueError("Complete-pack manifest must not be a symlink")
    manifest_path = manifest_path.resolve(strict=True)
    run_root = manifest_path.parent.parent
    lock_path = run_root.parent / f".{run_root.name}.complete-listening-receipt.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("Complete-pack listening receipt is already being decided") from exc
    try:
        original = manifest_path.read_bytes()
        manifest, _preview_paths = validate_complete_audio_pack(manifest_path)
        original_evidence = _complete_evidence_snapshot(manifest_path)
        receipt = manifest.get("listening_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("status") != "pending":
            raise ValueError("Complete-pack listening receipt has already been decided")
        try:
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Complete-pack decision is not readable JSON: {decision_path}") from exc
        if not isinstance(decision, dict) or set(decision) != DECISION_FIELDS:
            raise ValueError("Complete-pack decision has an invalid field set")
        if decision.get("content_digest") != manifest.get("content_digest"):
            raise ValueError("Complete-pack decision targets a stale content digest")
        status = decision.get("status")
        if status not in {"approved", "rejected"}:
            raise ValueError("Complete-pack decision status must be approved or rejected")
        notes = decision.get("notes")
        if not isinstance(notes, str) or not notes.strip():
            raise ValueError("Complete-pack decision requires listening notes")
        decided_receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": status,
            "pack_digest": manifest["pack_digest"],
            "content_digest": manifest["content_digest"],
            "approved_candidate_id": CANDIDATE_ID if status == "approved" else None,
            "target": TARGET,
            "surfaces": list(REQUIRED_SURFACES),
            "device_classes": list(REQUIRED_DEVICE_CLASSES),
            "prompt": LISTENING_PROMPT,
            "mac": decision.get("mac"),
            "small_speaker": decision.get("small_speaker"),
            "sonos_arc": decision.get("sonos_arc"),
            "notes": notes.strip(),
            "reviewed_at": reviewed_at or datetime.now(UTC).isoformat(),
        }
        manifest["release_ready"] = status == "approved"
        manifest["listening_receipt"] = decided_receipt
        _validate_receipt(manifest)

        current_manifest, _current_previews = validate_complete_audio_pack(manifest_path)
        if manifest_path.read_bytes() != original:
            raise ValueError("Complete-pack manifest changed while its decision was being recorded")
        if _complete_evidence_snapshot(manifest_path) != original_evidence:
            raise ValueError("Complete-pack board changed while its decision was being recorded")
        if current_manifest.get("content_digest") != manifest.get("content_digest"):
            raise ValueError("Complete-pack content digest changed while its decision was being recorded")

        temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
        try:
            _write_json(temporary, manifest)
            os.replace(temporary, manifest_path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            validate_complete_audio_pack(manifest_path)
            if _complete_evidence_snapshot(manifest_path) != original_evidence:
                raise ValueError("Complete-pack board changed while its decision was being committed")
        except BaseException:
            rollback = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.rollback")
            try:
                rollback.write_bytes(original)
                os.replace(rollback, manifest_path)
            finally:
                rollback.unlink(missing_ok=True)
            raise
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
    return manifest_path


def assert_complete_listening_gate(manifest_path: Path) -> str:
    manifest, _previews = validate_complete_audio_pack(manifest_path)
    receipt = manifest.get("listening_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("status") != "approved":
        raise ValueError("Complete-pack listening gate is not approved for this content digest")
    return str(manifest["content_digest"])


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Render an atomic pending complete-pack candidate")
    build.add_argument("--core-manifest", type=Path, default=DEFAULT_CORE_MANIFEST)
    build.add_argument("--output-root", type=Path)
    build.add_argument("--generated-at")
    validate = subparsers.add_parser("validate", help="Validate one complete-pack candidate")
    validate.add_argument("manifest", type=Path)
    receipt = subparsers.add_parser("record-receipt", help="Record an approved human listening decision")
    receipt.add_argument("manifest", type=Path)
    receipt.add_argument("decision", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "validate":
            manifest, previews = validate_complete_audio_pack(args.manifest)
            print(f"Complete audio pack OK: {len(manifest['assets'])} assets, {len(manifest['recipes'])} recipes")
            print(f"Content digest: {manifest['content_digest']}")
            print(f"Previews: {len(previews)}")
            return 0
        if args.command == "record-receipt":
            record_complete_listening_receipt(args.manifest, args.decision)
            print(f"Complete-pack listening receipt: {args.manifest}")
            return 0
        stamp = args.generated_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_root = args.output_root or DEFAULT_OUTPUT_ROOT / f"complete-pack-gate-{stamp}"
        manifest = render_complete_audio_pack(args.core_manifest, output_root, generated_at=stamp)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Complete pack gate: {output_root}")
    print(f"Manifest: {output_root / 'pack' / 'manifest.json'}")
    print(f"Listening page: {output_root / 'index.html'}")
    print(f"Handoff: {output_root / 'README.md'}")
    print(f"Content digest: {manifest['content_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
