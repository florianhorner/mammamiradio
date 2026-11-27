#!/usr/bin/env python3
"""Build the digest-bound core-cadence listening gate.

This stage consumes one approved sonic-treatment board and the exact production
voices approved by that board.  It is deliberately audition-only: rendering is
local and deterministic, packaged imaging and station runtime state are never
modified, and a later pack build remains blocked until the resulting digest has
passed the Mac, small-speaker, and Wohnzimmer Sonos Arc listening gate.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mammamiradio.audio.normalizer import (
    concat_files,
    configure_loudness_reconcile,
    crossfade_voice_over_music,
    fit_audio_oneshot,
    mix_voice_with_sting,
    normalize_ad,
)

try:
    _treatment_gate = importlib.import_module("scripts.sonic_treatment_gate")
except ModuleNotFoundError as exc:  # Direct ``python scripts/core_cadence_gate.py`` invocation.
    if exc.name != "scripts":
        raise
    _treatment_gate = importlib.import_module("sonic_treatment_gate")


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "sonic-brand-auditions"
FORMAT = {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}
SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
STAGE = "core-cadence-selection"
TARGET = "Mac + small speaker + Wohnzimmer Sonos Arc"
SCOPE = (
    "Audition-only core cadence using the approved Neon Relay signature, "
    "fresh Velvet Horizon atmospheric language, and frozen current production speech."
)
SURFACES = ("station-id", "sweeper", "ad-in", "ad-out", "full-cadence")
LISTENING_PROMPT = (
    "Core gate: pass|fail; Mac: pass|fail; Small speaker: pass|fail; Sonos Arc: pass|fail; Notes: <short reason>"
)
LIMITATIONS = (
    "Audition only; board generation does not install packaged imaging assets or start or restart the station.",
    "Only the approved treatment direction is reused; all transitions and ad markers are freshly authored.",
    "Representative speech is frozen separately through the exact current production routes.",
    "Compatibility sounds and advertising recipes remain blocked until this core digest passes listening.",
)
TIMESTAMP_RE = _treatment_gate.TIMESTAMP_RE
SHA256_RE = _treatment_gate.SHA256_RE
DECISION_FIELDS = frozenset({"pack_digest", "status", "mac", "small_speaker", "sonos_arc", "rationale"})
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "pack_digest",
        "target",
        "surfaces",
        "prompt",
        "mac",
        "small_speaker",
        "sonos_arc",
        "rationale",
        "reviewed_at",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "generated_at",
        "release_ready",
        "scope",
        "upstream_treatment",
        "upstream_identity",
        "upstream_speech",
        "design_direction",
        "production_contract",
        "sources",
        "artifacts",
        "preview_groups",
        "quality_guards",
        "pack_digest",
        "listening_receipt",
        "limitations",
    }
)
FROZEN_SPEECH_IDS = ("speech.sweeper", "speech.promo", "speech.spot-a", "speech.spot-b")


@dataclass(frozen=True)
class ApprovedInputs:
    """Validated upstream treatment and voice artifacts for one render."""

    treatment_manifest: Path
    treatment_pack_digest: str
    candidate_id: str
    candidate_label: str
    signature_path: Path
    signature_sha256: str
    treatment_reviewed_at: str
    identity_manifest: Path
    identity_pack_digest: str
    voice_paths: Mapping[str, Path]
    voice_metadata: Mapping[str, Mapping[str, object]]
    speech_manifest: Path
    speech_pack_digest: str
    speech_generated_at: str
    speech_paths: Mapping[str, Path]
    speech_metadata: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class GeneratedSpec:
    """One deterministic local source used only by the listening gate."""

    id: str
    path: str
    label: str
    purpose: str
    duration_sec: float
    expression_left: str
    expression_right: str
    highpass_hz: int
    lowpass_hz: int
    fade_in_sec: float
    fade_out_sec: float
    tags: tuple[str, ...]
    foreground_source_id: str


CORE_CUES: tuple[GeneratedSpec, ...] = (
    GeneratedSpec(
        id="cue.music-to-speech",
        path="components/music_to_speech.mp3",
        label="Music to speech",
        purpose="A veiled downward handoff for the dry predecessor fallback.",
        duration_sec=0.72,
        expression_left=(
            "0.060*sin(2*PI*(392-105*t)*t)*exp(-3.2*t)"
            "+0.028*sin(2*PI*(784-210*t)*t)*exp(-5.8*t)"
            "+0.018*sin(2*PI*196*t)*exp(-2.4*t)"
        ),
        expression_right=(
            "0.058*sin(2*PI*(397-109*t)*t)*exp(-3.1*t)"
            "+0.027*sin(2*PI*(791-216*t)*t)*exp(-5.6*t)"
            "+0.019*sin(2*PI*198*t)*exp(-2.35*t)"
        ),
        highpass_hz=180,
        lowpass_hz=4_800,
        fade_in_sec=0.018,
        fade_out_sec=0.22,
        tags=("transition", "music-to-speech", "atmospheric", "midrange-anchor"),
        foreground_source_id="generated.transition.music-to-speech",
    ),
    GeneratedSpec(
        id="cue.speech-to-music",
        path="components/speech_to_music.mp3",
        label="Speech to music",
        purpose="A soft upward release placed at unity immediately before music.",
        duration_sec=0.76,
        expression_left=(
            "(1-exp(-18*t))*exp(-2.1*t)*(0.052*sin(2*PI*(220+92*t)*t)"
            "+0.030*sin(2*PI*(660+155*t)*t)+0.018*sin(2*PI*990*t))"
        ),
        expression_right=(
            "(1-exp(-17*t))*exp(-2.0*t)*(0.050*sin(2*PI*(223+95*t)*t)"
            "+0.029*sin(2*PI*(667+160*t)*t)+0.019*sin(2*PI*997*t))"
        ),
        highpass_hz=180,
        lowpass_hz=6_800,
        fade_in_sec=0.035,
        fade_out_sec=0.24,
        tags=("transition", "speech-to-music", "atmospheric", "midrange-anchor"),
        foreground_source_id="generated.transition.speech-to-music",
    ),
    GeneratedSpec(
        id="cue.ad-in",
        path="audio/ad_in.mp3",
        label="Ad in",
        purpose="A single narrow electronic aperture that opens the break.",
        duration_sec=0.84,
        expression_left=("0.085*sin(2*PI*(520+520*t)*t)*exp(-5.0*t)+0.030*sin(2*PI*1560*t)*exp(-16*t)"),
        expression_right=("0.082*sin(2*PI*(529+535*t)*t)*exp(-4.9*t)+0.031*sin(2*PI*1580*t)*exp(-15.5*t)"),
        highpass_hz=180,
        lowpass_hz=7_500,
        fade_in_sec=0.006,
        fade_out_sec=0.20,
        tags=("ad-bumper", "ad-in", "electronic", "midrange-anchor"),
        foreground_source_id="generated.ad-marker.in",
    ),
    GeneratedSpec(
        id="cue.ad-mid",
        path="components/ad_mid.mp3",
        label="Ad mid",
        purpose="One dry centered separator fitted through the runtime 0.8-second one-shot path.",
        duration_sec=0.68,
        expression_left=("0.082*sin(2*PI*880*t)*exp(-10*t)+0.034*sin(2*PI*1320*t)*exp(-14*t)"),
        expression_right=("0.080*sin(2*PI*887*t)*exp(-10.2*t)+0.035*sin(2*PI*1331*t)*exp(-13.7*t)"),
        highpass_hz=300,
        lowpass_hz=5_600,
        fade_in_sec=0.004,
        fade_out_sec=0.18,
        tags=("ad-bumper", "ad-mid", "electronic", "single-separator"),
        foreground_source_id="generated.ad-marker.mid",
    ),
    GeneratedSpec(
        id="cue.ad-out",
        path="audio/ad_out.mp3",
        label="Ad out",
        purpose="A smooth descending electronic closure distinct from the entry.",
        duration_sec=0.94,
        expression_left=("0.070*sin(2*PI*(720-245*t)*t)*exp(-2.9*t)+0.026*sin(2*PI*(1440-490*t)*t)*exp(-5.2*t)"),
        expression_right=("0.068*sin(2*PI*(728-250*t)*t)*exp(-2.8*t)+0.027*sin(2*PI*(1454-500*t)*t)*exp(-5.0*t)"),
        highpass_hz=170,
        lowpass_hz=6_400,
        fade_in_sec=0.016,
        fade_out_sec=0.28,
        tags=("ad-bumper", "ad-out", "electronic", "midrange-anchor"),
        foreground_source_id="generated.ad-marker.out",
    ),
)


CONTEXT_SPECS: tuple[GeneratedSpec, ...] = (
    GeneratedSpec(
        id="context.program-predecessor",
        path="components/program_predecessor.mp3",
        label="Program music predecessor",
        purpose="Neutral synthetic program context before the break.",
        duration_sec=10.0,
        expression_left=(
            "0.095*sin(2*PI*196*t)*(1+0.10*sin(2*PI*0.19*t))+0.060*sin(2*PI*293.66*t)+0.040*sin(2*PI*392*t)"
        ),
        expression_right=(
            "0.093*sin(2*PI*197.4*t)*(1+0.11*sin(2*PI*0.17*t))+0.058*sin(2*PI*295.5*t)+0.041*sin(2*PI*394.2*t)"
        ),
        highpass_hz=90,
        lowpass_hz=5_200,
        fade_in_sec=0.30,
        fade_out_sec=0.65,
        tags=("context", "program-music", "audition-only"),
        foreground_source_id="generated.context.program-predecessor",
    ),
    GeneratedSpec(
        id="context.program-successor",
        path="components/program_successor.mp3",
        label="Program music successor",
        purpose="Different neutral synthetic music after the break.",
        duration_sec=4.2,
        expression_left=(
            "0.090*sin(2*PI*220*t)*(1+0.08*sin(2*PI*0.23*t))+0.055*sin(2*PI*329.63*t)+0.038*sin(2*PI*440*t)"
        ),
        expression_right=(
            "0.088*sin(2*PI*221.7*t)*(1+0.09*sin(2*PI*0.21*t))+0.056*sin(2*PI*331.4*t)+0.037*sin(2*PI*442.5*t)"
        ),
        highpass_hz=95,
        lowpass_hz=5_400,
        fade_in_sec=0.18,
        fade_out_sec=0.55,
        tags=("context", "program-music", "audition-only"),
        foreground_source_id="generated.context.program-successor",
    ),
    GeneratedSpec(
        id="context.spot-bed-a",
        path="components/spot_bed_a.mp3",
        label="Representative spot bed A",
        purpose="Quiet atmospheric production bed beneath the first approved-voice spot.",
        duration_sec=2.4,
        expression_left=(
            "(1-exp(-9*t))*exp(-0.35*t)*(0.075*sin(2*PI*261.63*t)+0.048*sin(2*PI*392*t)+0.028*sin(2*PI*523.25*t))"
        ),
        expression_right=(
            "(1-exp(-8*t))*exp(-0.34*t)*(0.073*sin(2*PI*263.1*t)+0.047*sin(2*PI*394.1*t)+0.029*sin(2*PI*526.0*t))"
        ),
        highpass_hz=120,
        lowpass_hz=4_800,
        fade_in_sec=0.06,
        fade_out_sec=0.36,
        tags=("context", "spot-bed", "atmospheric", "audition-only"),
        foreground_source_id="generated.context.spot-bed-a",
    ),
    GeneratedSpec(
        id="context.spot-bed-b",
        path="components/spot_bed_b.mp3",
        label="Representative spot bed B",
        purpose="A distinct quiet atmospheric bed beneath the second approved-voice spot.",
        duration_sec=2.2,
        expression_left=(
            "0.060*sin(2*PI*(330+18*t)*t)*(0.72+0.28*sin(2*PI*2.1*t))+0.040*sin(2*PI*659.25*t)*exp(-0.45*t)"
        ),
        expression_right=(
            "0.058*sin(2*PI*(334+17*t)*t)*(0.73+0.27*sin(2*PI*2.0*t))+0.041*sin(2*PI*665.0*t)*exp(-0.44*t)"
        ),
        highpass_hz=150,
        lowpass_hz=5_600,
        fade_in_sec=0.04,
        fade_out_sec=0.32,
        tags=("context", "spot-bed", "atmospheric", "audition-only"),
        foreground_source_id="generated.context.spot-bed-b",
    ),
)

SPEC_BY_ID = {spec.id: spec for spec in (*CORE_CUES, *CONTEXT_SPECS)}
CORRELATION_ARTIFACT_IDS = (
    "render.station-bed",
    "render.sweeper-bed",
    *(spec.id for spec in CORE_CUES),
)
CORRELATION_ALLOWLIST = frozenset({frozenset({"render.station-bed", "render.sweeper-bed"})})
CORE_ROLE_ARTIFACT_IDS = (
    "preview.station-id",
    "preview.sweeper",
    "cue.ad-in",
    "cue.ad-mid",
    "cue.ad-out",
    "cue.music-to-speech",
    "cue.speech-to-music",
)
BREAK_SEQUENCES: Mapping[str, tuple[str, ...]] = {
    "render.break.adjacent.two-with-mid": (
        "render.intro-crossfade",
        "source.promo",
        "cue.ad-in",
        "render.spot-a",
        "cue.ad-mid",
        "render.spot-b",
        "cue.ad-out",
        "source.giulia-outro",
    ),
    "render.break.adjacent.two-without-mid": (
        "render.intro-crossfade",
        "source.promo",
        "cue.ad-in",
        "render.spot-a",
        "render.spot-b",
        "cue.ad-out",
        "source.giulia-outro",
    ),
    "render.break.adjacent.one-without-mid": (
        "render.intro-crossfade",
        "source.promo",
        "cue.ad-in",
        "render.spot-a",
        "cue.ad-out",
        "source.giulia-outro",
    ),
    "render.break.dry.two-with-mid": (
        "source.marco-intro",
        "source.promo",
        "cue.ad-in",
        "render.spot-a",
        "cue.ad-mid",
        "render.spot-b",
        "cue.ad-out",
        "source.giulia-outro",
    ),
    "render.break.dry.two-without-mid": (
        "source.marco-intro",
        "source.promo",
        "cue.ad-in",
        "render.spot-a",
        "render.spot-b",
        "cue.ad-out",
        "source.giulia-outro",
    ),
    "render.break.dry.one-without-mid": (
        "source.marco-intro",
        "source.promo",
        "cue.ad-in",
        "render.spot-a",
        "cue.ad-out",
        "source.giulia-outro",
    ),
}
CADENCE_SEQUENCES: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "preview.cadence.two-with-mid.music-successor": (
        "Two spots · with mid · music successor",
        (
            "render.program-preroll",
            "cue.music-to-speech",
            "render.break.dry.two-with-mid",
            "cue.speech-to-music",
            "context.program-successor",
        ),
    ),
    "preview.cadence.two-without-mid.music-successor": (
        "Two spots · without mid · music successor",
        (
            "render.break.adjacent.two-without-mid",
            "cue.speech-to-music",
            "context.program-successor",
        ),
    ),
    "preview.cadence.one-without-mid.speech-successor": (
        "One spot · without mid · spoken successor",
        (
            "render.program-preroll",
            "cue.music-to-speech",
            "render.break.dry.one-without-mid",
            "render.speech-successor",
        ),
    ),
    "preview.cadence.two-without-mid.speech-successor": (
        "Two spots · without mid · spoken successor",
        ("render.break.adjacent.two-without-mid", "render.speech-successor"),
    ),
}
CADENCE_ARTIFACT_IDS = tuple(CADENCE_SEQUENCES)
AUDIO_CONTRACT_ARTIFACT_IDS = (
    "preview.station-id",
    "preview.sweeper",
    *CORRELATION_ARTIFACT_IDS,
    *CADENCE_ARTIFACT_IDS,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _path_record(path: Path) -> tuple[str, str]:
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix(), "repo-relative"
    except ValueError:
        return str(resolved), "absolute"


def _path_from_record(value: object, kind: object, *, label: str) -> Path:
    if not isinstance(value, str) or kind not in {"repo-relative", "absolute"}:
        raise ValueError(f"{label} has an invalid path record")
    if kind == "repo-relative":
        resolved = (REPO_ROOT / value).resolve(strict=True)
        repo_root = REPO_ROOT.resolve()
        if resolved != repo_root and repo_root not in resolved.parents:
            raise ValueError(f"{label} escapes the repository")
        return resolved
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError(f"{label} absolute path is not absolute")
    return candidate.resolve(strict=True)


def _require_safe_output_root(output_root: Path) -> None:
    resolved = output_root.resolve(strict=False)
    allowed_roots = ((REPO_ROOT / "tmp").resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError("Core gates may be rendered only under workspace tmp or system temp")


def _require_dependency_chronology(generated_at: str, dependency_generated_at: str) -> None:
    """Reject a board that claims to predate one of its required inputs."""
    if not TIMESTAMP_RE.fullmatch(generated_at) or not TIMESTAMP_RE.fullmatch(dependency_generated_at):
        raise ValueError("Core or speech generated_at has an invalid timestamp")
    if generated_at < dependency_generated_at:
        raise ValueError("Core generated_at must not predate its frozen speech dependency")


def _resolve_approved_inputs(treatment_manifest: Path, speech_manifest: Path) -> ApprovedInputs:
    """Resolve the exact approved treatment and all three approved voices."""
    treatment_manifest = treatment_manifest.resolve(strict=True)
    manifest, _audio_paths = _treatment_gate.validate_treatment_gate(treatment_manifest)
    candidate_id = _treatment_gate.assert_treatment_listening_gate(treatment_manifest)
    if candidate_id != "neon_relay":
        raise ValueError("Core cadence requires the approved Neon Relay treatment")
    receipt = manifest["listening_receipt"]
    if not isinstance(receipt, Mapping):  # pragma: no cover - upstream validator invariant
        raise ValueError("Approved treatment has no listening receipt")
    reviewed_at = receipt.get("reviewed_at")
    if not isinstance(reviewed_at, str):
        raise ValueError("Approved treatment has no review timestamp")

    candidate: Mapping[str, object] | None = None
    raw_candidates = manifest.get("candidates")
    if isinstance(raw_candidates, list):
        candidate = next(
            (item for item in raw_candidates if isinstance(item, Mapping) and item.get("id") == candidate_id),
            None,
        )
    if candidate is None:
        raise ValueError("Approved treatment candidate is missing")
    if candidate.get("label") != "Neon Relay":
        raise ValueError("Approved Neon Relay treatment has an unexpected label")
    solo = candidate.get("solo")
    if not isinstance(solo, Mapping):
        raise ValueError("Approved treatment candidate has no solo artifact")
    signature_path = treatment_manifest.parent / str(solo.get("path"))
    signature_sha256 = solo.get("sha256")
    if not isinstance(signature_sha256, str) or _sha256(signature_path) != signature_sha256:
        raise ValueError("Approved treatment signature changed after listening")

    upstream = manifest.get("upstream_identity")
    if not isinstance(upstream, Mapping):
        raise ValueError("Approved treatment has no identity lineage")
    identity_manifest = _path_from_record(
        upstream.get("manifest_path"),
        upstream.get("manifest_path_kind"),
        label="identity manifest",
    )
    identity_digest, voice_paths, voice_metadata = _treatment_gate.approved_identity_clips_by_id(identity_manifest)
    required_ids = {"identity-isabella", "identity-marco", "identity-giulia"}
    if set(voice_paths) != required_ids:
        raise ValueError("Core cadence requires the exact approved Isabella, Marco, and Giulia clips")
    for voice_id in required_ids:
        metadata = voice_metadata[voice_id]
        if metadata.get("audio_sha256") != _sha256(voice_paths[voice_id]):
            raise ValueError(f"Approved voice changed after listening: {voice_id}")
    try:
        speech_gate = importlib.import_module("scripts.freeze_core_cadence_speech")
    except ModuleNotFoundError as exc:  # Direct script invocation.
        if exc.name != "scripts":
            raise
        speech_gate = importlib.import_module("freeze_core_cadence_speech")
    speech_value, speech_paths = speech_gate.validate_core_cadence_speech_manifest(speech_manifest)
    raw_clips = speech_value.get("clips")
    if not isinstance(raw_clips, list):
        raise ValueError("Core speech manifest has no clip inventory")
    speech_metadata = {str(clip.get("id")): clip for clip in raw_clips if isinstance(clip, Mapping)}
    if tuple(speech_metadata) != FROZEN_SPEECH_IDS or set(speech_paths) != set(FROZEN_SPEECH_IDS):
        raise ValueError("Core speech manifest does not contain the required ordered clips")
    speech_identity = speech_value.get("upstream_identity")
    if not isinstance(speech_identity, Mapping) or speech_identity.get("pack_digest") != identity_digest:
        raise ValueError("Core speech was not frozen from the approved identity digest")
    recorded_identity_path = _path_from_record(
        speech_identity.get("manifest_path"),
        speech_identity.get("manifest_path_kind"),
        label="speech identity manifest",
    )
    if recorded_identity_path != identity_manifest:
        raise ValueError("Core speech was frozen from a different identity manifest")
    return ApprovedInputs(
        treatment_manifest=treatment_manifest,
        treatment_pack_digest=str(manifest["pack_digest"]),
        candidate_id=candidate_id,
        candidate_label=str(candidate["label"]),
        signature_path=signature_path,
        signature_sha256=signature_sha256,
        treatment_reviewed_at=reviewed_at,
        identity_manifest=identity_manifest,
        identity_pack_digest=identity_digest,
        voice_paths=voice_paths,
        voice_metadata=voice_metadata,
        speech_manifest=speech_manifest.resolve(strict=True),
        speech_pack_digest=str(speech_value["pack_digest"]),
        speech_generated_at=str(speech_value["generated_at"]),
        speech_paths=speech_paths,
        speech_metadata=speech_metadata,
    )


def _run_ffmpeg(command: Sequence[str], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix or ".tmp"
    temporary_path = output_path.with_name(f".{output_path.stem}.{uuid4().hex}.tmp{suffix}")
    try:
        subprocess.run([*command, str(temporary_path)], check=True)
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg did not create core output: {output_path}")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def _mp3_output_args() -> list[str]:
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


def _render_generated(spec: GeneratedSpec, output_root: Path) -> Path:
    output_path = output_root / spec.path
    fade_out_start = max(spec.duration_sec - spec.fade_out_sec, 0.0)
    source = (
        f"aevalsrc=exprs='{spec.expression_left}|{spec.expression_right}':"
        f"s=48000:d={_number(spec.duration_sec)}:c=stereo"
    )
    return _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-af",
            (
                f"highpass=f={spec.highpass_hz},lowpass=f={spec.lowpass_hz},"
                f"afade=t=in:d={_number(spec.fade_in_sec)},"
                f"afade=t=out:st={_number(fade_out_start)}:d={_number(spec.fade_out_sec)},"
                "loudnorm=I=-16:LRA=6:TP=-1.5,"
                "aformat=sample_rates=48000:channel_layouts=stereo"
            ),
            *_mp3_output_args(),
        ],
        output_path,
    )


def _render_runtime_mid(spec: GeneratedSpec, output_root: Path) -> tuple[Path, str]:
    """Author the mid master, then render the exact short one-shot production path."""
    raw_root = Path(tempfile.mkdtemp(prefix="core-mid-"))
    try:
        raw_path = _render_generated(spec, raw_root)
        raw_sha256 = _sha256(raw_path)
        output_path = output_root / spec.path
        fit_audio_oneshot(
            raw_path,
            output_path,
            0.8,
            target_lufs=-16.0,
            fade_out_sec=0.08,
        )
        return output_path, raw_sha256
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)


def _render_representative_spot(
    voice_path: Path,
    bed_path: Path,
    output_path: Path,
) -> Path:
    """Mix frozen approved-voice speech with a quiet bed and use ad production DSP."""
    raw_path = output_path.with_name(f".{output_path.stem}.{uuid4().hex}.raw.mp3")
    duration_sec = _probe_audio(voice_path)[1]
    try:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(voice_path),
                "-stream_loop",
                "-1",
                "-i",
                str(bed_path),
                "-filter_complex",
                (
                    "[0:a]aresample=48000,aformat=channel_layouts=stereo[voice];"
                    f"[1:a]atrim=duration={_number(duration_sec)},asetpts=N/SR/TB,"
                    "aresample=48000,aformat=channel_layouts=stereo,volume=-30dB,"
                    "afade=t=in:d=0.08,"
                    f"afade=t=out:st={_number(max(duration_sec - 0.35, 0))}:d=0.35[bed];"
                    "[voice][bed]amix=inputs=2:duration=first:normalize=0[out]"
                ),
                "-map",
                "[out]",
                *_mp3_output_args(),
            ],
            raw_path,
        )
        with _runtime_loudness_targets():
            return normalize_ad(raw_path, output_path)
    finally:
        raw_path.unlink(missing_ok=True)


def _render_identity_bed(
    signature_path: Path,
    output_path: Path,
    *,
    duration_sec: float,
    role: str,
) -> Path:
    left, right = _identity_support_expressions(role)
    atmosphere = f"aevalsrc=exprs='{left}|{right}':s=48000:d={_number(duration_sec)}:c=stereo"
    return _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(signature_path),
            "-f",
            "lavfi",
            "-i",
            atmosphere,
            "-filter_complex",
            (
                "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[sigraw][driveraw];"
                "[sigraw]volume=-15dB[sig];"
                "[driveraw]volume=12dB,asoftclip=type=tanh:threshold=0.25:output=0.35:"
                "oversample=4,highpass=f=180,lowpass=f=2400,volume=-3dB[mid];"
                "[1:a]highpass=f=150,lowpass=f=5200,volume=4dB[air];"
                f"[sig][mid][air]amix=inputs=3:duration=longest:normalize=0,"
                f"apad=whole_dur={_number(duration_sec)},atrim=duration={_number(duration_sec)},"
                "loudnorm=I=-16:LRA=6:TP=-1.5[out]"
            ),
            "-map",
            "[out]",
            *_mp3_output_args(),
        ],
        output_path,
    )


def _identity_support_expressions(role: str) -> tuple[str, str]:
    """Return only the fresh Velvet-like atmosphere around the approved signature."""
    if role == "station":
        pad_left = "(1-exp(-7*t))*exp(-0.70*t)*(0.030*sin(2*PI*220*t)+0.018*sin(2*PI*329.63*t))"
        pad_right = "(1-exp(-6.6*t))*exp(-0.68*t)*(0.029*sin(2*PI*222*t)+0.019*sin(2*PI*332*t))"
    elif role == "sweeper":
        pad_left = "(1-exp(-10*t))*exp(-1.05*t)*(0.028*sin(2*PI*293.66*t)+0.016*sin(2*PI*440*t))"
        pad_right = "(1-exp(-9.5*t))*exp(-1.02*t)*(0.027*sin(2*PI*296*t)+0.017*sin(2*PI*443*t))"
    else:  # pragma: no cover - constant call-site invariant
        raise ValueError(f"Unknown identity-bed role: {role}")
    return pad_left, pad_right


def _identity_support_hash(role: str) -> str:
    left, right = _identity_support_expressions(role)
    duration = 3.0 if role == "station" else 2.0
    payload = {
        "role": role,
        "expression_left": left,
        "expression_right": right,
        "duration_sec": duration,
        "output_offset_sec": 0.0,
        "gain_db": 4.0,
        "filters": ["highpass:150Hz", "lowpass:5200Hz"],
        "identity_translation": {
            "derived_from_selected_signature": True,
            "filters": [
                "volume:12dB",
                "asoftclip:tanh,threshold=0.25,output=0.35,oversample=4",
                "highpass:180Hz",
                "lowpass:2400Hz",
                "volume:-3dB",
            ],
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _render_voice_excerpt(
    source_path: Path,
    output_path: Path,
    *,
    start_sec: float,
    duration_sec: float,
) -> Path:
    return _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-af",
            (
                f"atrim=start={_number(start_sec)}:duration={_number(duration_sec)},"
                "asetpts=N/SR/TB,afade=t=in:d=0.012,"
                f"afade=t=out:st={_number(max(duration_sec - 0.10, 0))}:d=0.10,"
                "loudnorm=I=-16:LRA=11:TP=-1.5"
            ),
            *_mp3_output_args(),
        ],
        output_path,
    )


def _render_preroll(source_path: Path, output_path: Path, *, duration_sec: float = 2.5) -> Path:
    return _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-sseof",
            f"-{_number(duration_sec)}",
            "-i",
            str(source_path),
            "-vn",
            *_mp3_output_args(),
        ],
        output_path,
    )


def _concat(paths: list[Path], output_path: Path, *, silence_ms: int) -> Path:
    if not paths:
        raise ValueError("Core cadence concat requires at least one part")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return concat_files(paths, output_path, silence_ms=silence_ms, loudnorm=False)


@contextmanager
def _runtime_loudness_targets() -> Iterator[None]:
    """Apply production loudness reconciliation while the offline board renders."""
    configure_loudness_reconcile(-16.0, -15.0, sample_rate=48_000, channels=2, bitrate=192)
    try:
        yield
    finally:
        configure_loudness_reconcile(None, None, sample_rate=48_000, channels=2, bitrate=192)


def _render_voice_sting_mix(voice_path: Path, sting_path: Path, output_path: Path) -> Path:
    with _runtime_loudness_targets():
        return mix_voice_with_sting(voice_path, sting_path, output_path)


def _probe_audio(path: Path) -> tuple[dict[str, object], float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bit_rate:format=duration,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    try:
        stream = payload["streams"][0]
        container = payload["format"]
        evidence = {
            "codec": str(stream["codec_name"]),
            "sample_rate_hz": int(stream["sample_rate"]),
            "channels": int(stream["channels"]),
            "bitrate_kbps": round(int(stream.get("bit_rate") or container["bit_rate"]) / 1000),
        }
        duration_sec = float(container["duration"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Incomplete ffprobe evidence for core audio: {path}") from exc
    if evidence != FORMAT or not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError(f"Core audio violates the production format: {path} ({evidence})")
    return evidence, round(duration_sec, 3)


def _source_hash(spec: GeneratedSpec) -> str:
    payload = {
        "duration_sec": spec.duration_sec,
        "expression_left": spec.expression_left,
        "expression_right": spec.expression_right,
        "highpass_hz": spec.highpass_hz,
        "lowpass_hz": spec.lowpass_hz,
        "fade_in_sec": spec.fade_in_sec,
        "fade_out_sec": spec.fade_out_sec,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _generated_layer(
    spec: GeneratedSpec,
    *,
    role: str = "foreground",
    runtime_mid_raw_sha256: str | None = None,
) -> dict[str, object]:
    dsp = [
        "generator:ffmpeg-aevalsrc",
        f"highpass:{spec.highpass_hz}Hz",
        f"lowpass:{spec.lowpass_hz}Hz",
        f"fade:{round(spec.fade_in_sec * 1000)}ms-in/{round(spec.fade_out_sec * 1000)}ms-out",
        "loudnorm:I=-16,LRA=6,TP=-1.5",
    ]
    layer: dict[str, object] = {
        "source_id": spec.foreground_source_id,
        "source_sha256": _source_hash(spec),
        "source_start_sec": 0.0,
        "duration_sec": spec.duration_sec,
        "output_offset_sec": 0.0,
        "gain_db": 0.0,
        "dsp": dsp,
        "license": "audition-only-local-generation",
        "role": role,
    }
    if runtime_mid_raw_sha256 is not None:
        layer["rendered_master_sha256"] = runtime_mid_raw_sha256
        dsp.append("fit_audio_oneshot:duration=0.8,target=-16LUFS,fade-out=0.08s")
    return layer


def _artifact_record(
    output_root: Path,
    *,
    artifact_id: str,
    path: Path,
    label: str,
    purpose: str,
    kind: str,
    tags: Sequence[str],
    foreground_source_ids: Sequence[str],
    layers: list[dict[str, object]],
) -> dict[str, object]:
    audio_format, duration_sec = _probe_audio(path)
    return {
        "id": artifact_id,
        "path": path.relative_to(output_root).as_posix(),
        "label": label,
        "purpose": purpose,
        "kind": kind,
        "tags": list(tags),
        "sha256": _sha256(path),
        "duration_sec": duration_sec,
        "format": audio_format,
        "foreground_source_ids": list(foreground_source_ids),
        "layers": layers,
    }


def _sequence_layers(
    artifact_by_id: Mapping[str, Mapping[str, object]],
    artifact_ids: Sequence[str],
    *,
    gap_sec: float,
) -> list[dict[str, object]]:
    layers: list[dict[str, object]] = []
    offset = 0.0
    for index, artifact_id in enumerate(artifact_ids):
        artifact = artifact_by_id[artifact_id]
        raw_duration = artifact.get("duration_sec")
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, int | float):
            raise ValueError(f"Artifact has no numeric duration: {artifact_id}")
        duration = float(raw_duration)
        layers.append(
            {
                "source_id": artifact_id,
                "source_sha256": artifact["sha256"],
                "source_start_sec": 0.0,
                "duration_sec": duration,
                "output_offset_sec": round(offset, 6),
                "gain_db": 0.0,
                "dsp": [
                    "concat:unity",
                    f"following-gap:{_number(gap_sec)}s" if index < len(artifact_ids) - 1 else "end",
                ],
                "license": "audition-only-composite",
                "role": "component",
            }
        )
        offset += duration
        if index < len(artifact_ids) - 1:
            offset += gap_sec
    return layers


def _flatten_foregrounds(
    artifact_by_id: Mapping[str, Mapping[str, object]],
    artifact_ids: Sequence[str],
) -> list[str]:
    result: list[str] = []
    for artifact_id in artifact_ids:
        raw_ids = artifact_by_id[artifact_id].get("foreground_source_ids")
        if not isinstance(raw_ids, list):  # pragma: no cover - local builder invariant
            raise ValueError(f"Artifact has no foreground ledger: {artifact_id}")
        result.extend(str(value) for value in raw_ids)
    return result


def _decode_pcm(
    path: Path,
    *,
    sample_rate: int = 4_000,
    audio_filter: str | None = None,
) -> tuple[float, ...]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
    ]
    if audio_filter is not None:
        command.extend(["-af", audio_filter])
    result = subprocess.run(
        [*command, "-f", "f32le", "-"],
        check=True,
        capture_output=True,
    )
    count = len(result.stdout) // 4
    return struct.unpack(f"<{count}f", result.stdout[: count * 4])


def _analysis_windows(
    samples: Sequence[float],
    *,
    window_samples: int,
    stride_samples: int,
) -> tuple[tuple[tuple[float, ...], float], ...]:
    if len(samples) < window_samples:
        return ()
    starts = list(range(0, len(samples) - window_samples + 1, stride_samples))
    last = len(samples) - window_samples
    if starts[-1] != last:
        starts.append(last)
    windows: list[tuple[tuple[float, ...], float]] = []
    for start in starts:
        segment = samples[start : start + window_samples]
        mean = sum(segment) / window_samples
        centered = tuple(value - mean for value in segment)
        norm = math.sqrt(sum(value * value for value in centered))
        if norm > 1e-9:
            windows.append((centered, norm))
    return tuple(windows)


def _max_window_correlation(first: Path, second: Path) -> float:
    window_samples = 2_000  # 500 ms at the 4 kHz analysis rate.
    stride_samples = 100  # 25 ms; catches shifted copied excerpts without an expensive full scan.
    first_windows = _analysis_windows(
        _decode_pcm(first),
        window_samples=window_samples,
        stride_samples=stride_samples,
    )
    second_windows = _analysis_windows(
        _decode_pcm(second),
        window_samples=window_samples,
        stride_samples=stride_samples,
    )
    maximum = 0.0
    for first_values, first_norm in first_windows:
        for second_values, second_norm in second_windows:
            dot = sum(a * b for a, b in zip(first_values, second_values, strict=True))
            maximum = max(maximum, abs(dot / (first_norm * second_norm)))
    return round(maximum, 6)


def _small_speaker_loss_db(path: Path) -> float:
    original = _decode_pcm(path, sample_rate=12_000)
    translated = _decode_pcm(
        path,
        sample_rate=12_000,
        audio_filter="highpass=f=180,lowpass=f=5000",
    )
    original_rms = math.sqrt(sum(value * value for value in original) / max(len(original), 1))
    translated_rms = math.sqrt(sum(value * value for value in translated) / max(len(translated), 1))
    if original_rms <= 1e-12 or translated_rms <= 1e-12:
        raise ValueError(f"Core asset has no measurable small-speaker signal: {path}")
    return round(max(0.0, 20 * math.log10(original_rms / translated_rms)), 3)


def _measure_loudness(path: Path) -> tuple[float, float]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-16:LRA=11:TP=-1.5:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    reports = re.findall(r'\{\s*"input_i".*?\}', completed.stderr, flags=re.DOTALL)
    if not reports:
        raise ValueError(f"FFmpeg returned no loudness evidence for {path}")
    report = json.loads(reports[-1])
    return round(float(report["input_i"]), 2), round(float(report["input_tp"]), 2)


def _correlation_evidence(
    artifact_by_id: Mapping[str, Mapping[str, object]],
    output_root: Path,
) -> dict[str, object]:
    threshold = 0.98
    records: list[dict[str, object]] = []
    maximum = 0.0
    for index, first_id in enumerate(CORRELATION_ARTIFACT_IDS):
        for second_id in CORRELATION_ARTIFACT_IDS[index + 1 :]:
            first = output_root / str(artifact_by_id[first_id]["path"])
            second = output_root / str(artifact_by_id[second_id]["path"])
            correlation = _max_window_correlation(first, second)
            pair = frozenset({first_id, second_id})
            allowed = pair in CORRELATION_ALLOWLIST
            records.append(
                {
                    "asset_ids": [first_id, second_id],
                    "max_abs_correlation": correlation,
                    "allowlisted": allowed,
                }
            )
            if not allowed:
                maximum = max(maximum, correlation)
            if correlation >= threshold and not allowed:
                raise ValueError(
                    f"Undocumented decoded-audio correlation >= {threshold}: {first_id} / {second_id} ({correlation})"
                )
    translation_ids = list(CORRELATION_ARTIFACT_IDS)
    translation_records = []
    for artifact_id in translation_ids:
        path = output_root / str(artifact_by_id[artifact_id]["path"])
        loss_db = _small_speaker_loss_db(path)
        translation_records.append({"asset_id": artifact_id, "loss_db": loss_db})
        if loss_db > 4.0:
            raise ValueError(f"Small-speaker proxy loses more than 4 dB: {artifact_id} ({loss_db} dB)")
    audio_contract_records = []
    for artifact_id in AUDIO_CONTRACT_ARTIFACT_IDS:
        path = output_root / str(artifact_by_id[artifact_id]["path"])
        integrated_lufs, true_peak_dbtp = _measure_loudness(path)
        audio_contract_records.append(
            {
                "asset_id": artifact_id,
                "integrated_lufs": integrated_lufs,
                "true_peak_dbtp": true_peak_dbtp,
            }
        )
        if abs(integrated_lufs - (-16.0)) > 2.0:
            raise ValueError(f"Core asset is outside -16 ±2 LUFS: {artifact_id} ({integrated_lufs})")
        if true_peak_dbtp > -1.0:
            raise ValueError(f"Core asset exceeds -1.0 dBTP: {artifact_id} ({true_peak_dbtp})")
    ad_spot_records = []
    for artifact_id in ("render.spot-a", "render.spot-b"):
        path = output_root / str(artifact_by_id[artifact_id]["path"])
        integrated_lufs, true_peak_dbtp = _measure_loudness(path)
        ad_spot_records.append(
            {
                "asset_id": artifact_id,
                "integrated_lufs": integrated_lufs,
                "true_peak_dbtp": true_peak_dbtp,
            }
        )
        if abs(integrated_lufs - (-15.0)) > 1.0:
            raise ValueError(f"Representative spot is outside -15 ±1 LUFS: {artifact_id} ({integrated_lufs})")
        if true_peak_dbtp > -1.0:
            raise ValueError(f"Representative spot exceeds -1.0 dBTP: {artifact_id} ({true_peak_dbtp})")
    return {
        "decoded_audio_correlation": {
            "threshold": threshold,
            "window_ms": 500,
            "analysis_sample_rate_hz": 4_000,
            "stride_ms": 25,
            "asset_ids": list(CORRELATION_ARTIFACT_IDS),
            "pairs": records,
            "max_observed": round(maximum, 6),
            "allowlist": [
                {
                    "asset_ids": ["render.station-bed", "render.sweeper-bed"],
                    "reason": "Both identity beds deliberately contain the approved Neon Relay signature.",
                }
            ],
        },
        "intentional_identity_overlap": {
            "asset_ids": ["preview.station-id", "preview.sweeper"],
            "source_id": "approved-treatment.signature",
            "reason": "The selected signature is intentionally limited to exactly these two identity roles.",
        },
        "small_speaker_translation": {
            "proxy": "mono highpass 180 Hz / lowpass 5 kHz",
            "max_loss_db": 4.0,
            "assets": translation_records,
        },
        "production_audio_contract": {
            "format": FORMAT,
            "target_lufs": -16.0,
            "tolerance_lu": 2.0,
            "true_peak_ceiling_dbtp": -1.0,
            "assets": audio_contract_records,
            "representative_ad_target_lufs": -15.0,
            "representative_ad_tolerance_lu": 1.0,
            "representative_ad_assets": ad_spot_records,
        },
        "max_foreground_core_roles": 2,
        "banned_semantics": ["brass", "mandolin", "trumpet"],
    }


def _core_pack_digest(manifest: Mapping[str, object]) -> str:
    material = {key: value for key, value in manifest.items() if key not in {"pack_digest", "listening_receipt"}}
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _receipt(pack_digest: str) -> dict[str, object]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": "pending",
        "pack_digest": pack_digest,
        "target": TARGET,
        "surfaces": list(SURFACES),
        "prompt": LISTENING_PROMPT,
        "mac": None,
        "small_speaker": None,
        "sonos_arc": None,
        "rationale": None,
        "reviewed_at": None,
    }


def _validate_receipt(value: object, *, pack_digest: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != RECEIPT_FIELDS:
        raise ValueError("Core listening receipt has an invalid field set")
    if value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("Core listening receipt has an unsupported schema")
    if (
        value.get("pack_digest") != pack_digest
        or value.get("target") != TARGET
        or value.get("surfaces") != list(SURFACES)
        or value.get("prompt") != LISTENING_PROMPT
    ):
        raise ValueError("Core listening receipt is stale or targets the wrong surfaces")
    status = value.get("status")
    if status == "pending":
        if any(
            value.get(field) is not None for field in ("mac", "small_speaker", "sonos_arc", "rationale", "reviewed_at")
        ):
            raise ValueError("Pending core receipt contains a decision")
        return value
    if status not in {"approved", "rejected"}:
        raise ValueError("Core listening receipt has an invalid status")
    reviewed_at = value.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        raise ValueError("Decided core receipt has no review time")
    try:
        parsed = datetime.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise ValueError("Decided core receipt has an invalid review time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Decided core receipt review time must include a timezone")
    mac = value.get("mac")
    small_speaker = value.get("small_speaker")
    sonos = value.get("sonos_arc")
    if status == "approved" and (
        mac != "pass" or small_speaker != "pass" or sonos != "pass" or value.get("rationale") != "accepted_core_cadence"
    ):
        raise ValueError("Approved core receipt must pass all three listening targets")
    if status == "rejected" and (
        mac not in {"pass", "fail"}
        or small_speaker not in {"pass", "fail"}
        or sonos not in {"pass", "fail"}
        or (mac == "pass" and small_speaker == "pass" and sonos == "pass")
        or value.get("rationale") != "rejected_core_cadence"
    ):
        raise ValueError("Rejected core receipt must identify a failed listening target")
    return value


def _readme(manifest: Mapping[str, object]) -> str:
    groups = manifest["preview_groups"]
    assert isinstance(groups, list)
    raw_artifacts = manifest["artifacts"]
    assert isinstance(raw_artifacts, list)
    artifact_by_id = {str(item["id"]): item for item in raw_artifacts if isinstance(item, Mapping)}
    direction = manifest["design_direction"]
    assert isinstance(direction, Mapping)
    raw_sources = manifest["sources"]
    assert isinstance(raw_sources, list)
    source_by_id = {str(item["id"]): item for item in raw_sources if isinstance(item, Mapping)}
    promo_character = source_by_id["speech.promo"]["character"]
    lines: list[str] = []
    for group in groups:
        assert isinstance(group, Mapping)
        lines.extend([f"### {group['label']}", "", str(group["purpose"]), ""])
        tracks = group["tracks"]
        assert isinstance(tracks, list)
        for artifact_id in tracks:
            artifact = artifact_by_id[str(artifact_id)]
            lines.append(f"- [{artifact['label']}](./{artifact['path']})")
        lines.append("")
    return "\n".join(
        [
            "# Mamma Mi Radio — core cadence approval",
            "",
            "[Open the listening board](./index.html)",
            "",
            (
                f"Selected signature: `{direction['signature']}`. "
                "The atmosphere is freshly authored from the Velvet Horizon direction; "
                "that rejected candidate audio is not reused as a second signature."
            ),
            "",
            (
                f"Frozen speech lineup: Isabella for station identity, {promo_character} for the current "
                "runtime promo, and Marco plus Giulia for the two spots. The board render itself is local "
                "and provider-free; its sources were separately frozen through their exact production routes."
            ),
            "",
            "## Five review groups",
            "",
            *lines,
            "## Procedure",
            "",
            "1. Keep Mac volume fixed. Play Station ID and Sweeper three times each.",
            "2. Replay Station ID and Sweeper on a phone, small smart speaker, or laptop speaker. "
            "Confirm the two-pulse Neon rhythm and answer remain recognisable without bass extension.",
            "3. Play Ad in, Ad out, then the single complete cadence twice.",
            (
                "4. Confirm the main cadence contains promo, two voiced spots, one mid marker, "
                "and a clean return to music."
            ),
            "5. Replay all five tracks at the same perceived level on the Wohnzimmer Sonos Arc.",
            "6. Record Mac, small-speaker, and Sonos results in the exact reply:",
            "",
            f"`{LISTENING_PROMPT}`",
            "",
            (
                "A failed result returns only this stage for revision. "
                "The compatibility and advertising pack remains unbuilt."
            ),
            "",
            f"Pack digest: `{manifest['pack_digest']}`",
            "",
            "See `manifest.json` for exact voice hashes, generated-source hashes, offsets, gains, DSP, "
            "runtime branch coverage, and correlation evidence.",
            "",
        ]
    )


def _html(manifest: Mapping[str, object]) -> str:
    raw_artifacts = manifest["artifacts"]
    raw_groups = manifest["preview_groups"]
    assert isinstance(raw_artifacts, list) and isinstance(raw_groups, list)
    artifacts = {str(item["id"]): item for item in raw_artifacts if isinstance(item, Mapping)}
    raw_sources = manifest["sources"]
    assert isinstance(raw_sources, list)
    source_by_id = {str(item["id"]): item for item in raw_sources if isinstance(item, Mapping)}
    promo_character = html.escape(str(source_by_id["speech.promo"]["character"]))
    cards: list[str] = []
    for group in raw_groups:
        assert isinstance(group, Mapping)
        players: list[str] = []
        tracks = group["tracks"]
        assert isinstance(tracks, list)
        for artifact_id in tracks:
            artifact = artifacts[str(artifact_id)]
            players.append(
                "\n".join(
                    [
                        '<div class="track">',
                        f"<h3>{html.escape(str(artifact['label']))}</h3>",
                        (
                            '<audio controls preload="metadata" '
                            f'src="{html.escape(str(artifact["path"]), quote=True)}"></audio>'
                        ),
                        "</div>",
                    ]
                )
            )
        cards.append(
            "\n".join(
                [
                    '<section class="card">',
                    f"<h2>{html.escape(str(group['label']))}</h2>",
                    f"<p>{html.escape(str(group['purpose']))}</p>",
                    *players,
                    "</section>",
                ]
            )
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mamma Mi Radio — core cadence approval</title>
<style>
:root {{ color-scheme: dark; --bg:#14110f; --surface:#251e19; --cream:#f5edd8;
  --muted:#bcae99; --sun:#f4d048; --line:rgba(245,237,216,.16); }}
html {{ min-height:100%;
  background:radial-gradient(500px 420px at 88% 8%,rgba(244,208,72,.1),transparent 70%),var(--bg); }}
body {{ max-width:960px; margin:0 auto; padding:32px 20px 64px; color:var(--cream);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.5; }}
h1,h2 {{ font-family:Georgia,"Times New Roman",serif; font-style:italic; letter-spacing:-.025em; }}
.notice {{ padding:14px 16px; border:1px solid rgba(244,208,72,.38); border-radius:8px; background:var(--surface); }}
.grid {{ display:grid; gap:16px; margin-top:24px; }}
.card {{ padding:18px; border:1px solid var(--line); border-top-color:rgba(244,208,72,.42);
  border-radius:8px; background:var(--surface); }}
.card h2 {{ margin-top:0; }}
.track {{ padding-top:10px; border-top:1px solid var(--line); margin-top:12px; }}
.track h3 {{ margin:0 0 8px; color:var(--muted); font-size:.84rem; letter-spacing:.08em; text-transform:uppercase; }}
audio {{ width:100%; }} code {{ color:var(--sun); }}
</style>
</head>
<body>
<h1>Core cadence approval</h1>
<p class="notice"><strong>Audition only.</strong> Neon Relay is the sole signature. Velvet Horizon
informs fresh atmosphere only. This board is not installed into packaged imaging and does not itself start or
restart the station; the staged mixer change remains local.</p>
<p>Frozen speech: Isabella for station identity, {promo_character} for the current runtime promo,
and Marco plus Giulia for the two representative spots.</p>
<p>Play Station ID and Sweeper three times on the Mac, then repeat them on an ordinary small
speaker. Play Ad in, Ad out, and the single complete cadence. Repeat all five tracks on the
Wohnzimmer Sonos Arc.</p>
<main class="grid">{"".join(cards)}</main>
<p>Reply: <code>{html.escape(LISTENING_PROMPT)}</code></p>
<p>Pack digest: <code>{manifest["pack_digest"]}</code></p>
</body>
</html>
"""


def _source_inventory(inputs: ApprovedInputs) -> list[dict[str, object]]:
    """Return the exact source ledger used by both builder and validator."""
    voice_records = []
    for voice_id in ("identity-isabella", "identity-marco", "identity-giulia"):
        metadata = inputs.voice_metadata[voice_id]
        voice_records.append(
            {
                "id": voice_id,
                "kind": "approved-production-voice",
                "character": metadata["character"],
                "source_sha256": metadata["audio_sha256"],
                "duration_sec": metadata["duration_sec"],
                "declared_text": metadata["text"],
                "license": "production-voice-config",
            }
        )
    frozen_speech_records = []
    for speech_id in FROZEN_SPEECH_IDS:
        metadata = inputs.speech_metadata[speech_id]
        frozen_speech_records.append(
            {
                "id": speech_id,
                "kind": (
                    "frozen-runtime-route-speech"
                    if metadata.get("source_kind") == "runtime-promo-selection"
                    else "frozen-approved-route-speech"
                ),
                "source_sha256": metadata["audio_sha256"],
                "character": metadata["character"],
                "duration_sec": metadata["duration_sec"],
                "declared_text": metadata["text"],
                "text_sha256": metadata["text_sha256"],
                "source_voice_id": metadata["source_voice_id"],
                "provider": metadata["provider"],
                "voice": metadata["voice"],
                "model": metadata["model"],
                "settings": metadata["settings"],
                "prosody": metadata["prosody"],
                "license": "production-voice-config",
            }
        )
    generated_sources = [
        {
            "id": spec.foreground_source_id,
            "kind": "deterministic-local-synthesis",
            "source_sha256": _source_hash(spec),
            "duration_sec": spec.duration_sec,
            "generator": "FFmpeg aevalsrc",
            "expression_left": spec.expression_left,
            "expression_right": spec.expression_right,
            "license": "audition-only-local-generation",
            "tags": list(spec.tags),
        }
        for spec in (*CORE_CUES, *CONTEXT_SPECS)
    ]
    return [
        {
            "id": "approved-treatment.signature",
            "kind": "approved-treatment-artifact",
            "source_sha256": inputs.signature_sha256,
            "candidate_id": inputs.candidate_id,
            "license": "audition-only-approved-direction",
            "tags": ["signature", inputs.candidate_id],
        },
        {
            "id": "generated.identity-support.station",
            "kind": "deterministic-local-synthesis",
            "source_sha256": _identity_support_hash("station"),
            "expression_left": _identity_support_expressions("station")[0],
            "expression_right": _identity_support_expressions("station")[1],
            "license": "audition-only-local-generation",
            "tags": ["texture", "atmospheric", "station-id", "midrange-anchor"],
        },
        {
            "id": "generated.identity-support.sweeper",
            "kind": "deterministic-local-synthesis",
            "source_sha256": _identity_support_hash("sweeper"),
            "expression_left": _identity_support_expressions("sweeper")[0],
            "expression_right": _identity_support_expressions("sweeper")[1],
            "license": "audition-only-local-generation",
            "tags": ["texture", "atmospheric", "sweeper", "midrange-anchor"],
        },
        *voice_records,
        *frozen_speech_records,
        *generated_sources,
    ]


def _preview_groups() -> list[dict[str, object]]:
    return [
        {
            "id": "station-id",
            "label": "Station ID",
            "purpose": "Full approved Isabella ident with Neon Relay and a restrained atmospheric tail.",
            "tracks": ["preview.station-id"],
        },
        {
            "id": "sweeper",
            "label": "Sweeper",
            "purpose": "Fresh approved-route Isabella rendering of one configured sweeper line.",
            "tracks": ["preview.sweeper"],
        },
        {
            "id": "ad-in",
            "label": "Ad in",
            "purpose": "Distinct break-entry marker with no station signature.",
            "tracks": ["cue.ad-in"],
        },
        {
            "id": "ad-out",
            "label": "Ad out",
            "purpose": "Distinct break-exit marker with no station signature.",
            "tracks": ["cue.ad-out"],
        },
        {
            "id": "full-cadence",
            "label": "Complete cadence",
            "purpose": "Main dry-predecessor branch with promo, two voiced spots, one mid marker, and music return.",
            "tracks": ["preview.cadence.two-with-mid.music-successor"],
        },
    ]


def _build_manifest(
    output_root: Path,
    *,
    inputs: ApprovedInputs,
    generated_at: str,
    artifacts: list[dict[str, object]],
) -> dict[str, Any]:
    treatment_path, treatment_kind = _path_record(inputs.treatment_manifest)
    identity_path, identity_kind = _path_record(inputs.identity_manifest)
    speech_path, speech_kind = _path_record(inputs.speech_manifest)
    sources = _source_inventory(inputs)
    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}
    quality_guards = _correlation_evidence(artifact_by_id, output_root)
    preview_groups = _preview_groups()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "generated_at": generated_at,
        "release_ready": False,
        "scope": SCOPE,
        "upstream_treatment": {
            "manifest_path": treatment_path,
            "manifest_path_kind": treatment_kind,
            "pack_digest": inputs.treatment_pack_digest,
            "candidate_id": inputs.candidate_id,
            "candidate_label": inputs.candidate_label,
            "candidate_sha256": inputs.signature_sha256,
            "reviewed_at": inputs.treatment_reviewed_at,
        },
        "upstream_identity": {
            "manifest_path": identity_path,
            "manifest_path_kind": identity_kind,
            "pack_digest": inputs.identity_pack_digest,
            "voice_sha256": {
                voice_id: inputs.voice_metadata[voice_id]["audio_sha256"]
                for voice_id in ("identity-isabella", "identity-marco", "identity-giulia")
            },
        },
        "upstream_speech": {
            "manifest_path": speech_path,
            "manifest_path_kind": speech_kind,
            "pack_digest": inputs.speech_pack_digest,
            "generated_at": inputs.speech_generated_at,
            "clip_sha256": {
                speech_id: inputs.speech_metadata[speech_id]["audio_sha256"] for speech_id in FROZEN_SPEECH_IDS
            },
        },
        "design_direction": {
            "signature": inputs.candidate_id,
            "signature_roles": ["station-id", "sweeper"],
            "atmospheric_reference": "velvet_horizon",
            "atmospheric_reference_usage": "freshly-authored texture and transitions; candidate audio not reused",
            "small_speaker_invariant": "identity anchored above 180 Hz; bass reinforces but never carries recognition",
        },
        "production_contract": {
            "format": FORMAT,
            "station_voice_mix": {
                "helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
                "sting_gain_linear": 0.15,
                "voice_gain_linear": 1.2,
                "voice_delay_ms": 400,
                "amix_normalize": False,
                "filter_complex_threads": 1,
            },
            "adjacent_music_intro": {
                "helper": "mammamiradio.audio.normalizer.crossfade_voice_over_music",
                "tail_sec": 8.0,
                "music_gain_linear": 0.5,
                "voice_gain_linear": 1.0,
                "voice_delay_ms": 150,
                "music_to_speech_suppressed": True,
            },
            "dry_predecessor": {"preroll_sec": 2.5, "music_to_speech_gain_db": 0.0},
            "ad_break": {"gap_ms": 300, "final_loudnorm": False, "max_mid_bumpers": 1},
            "promo": {
                "branch": "render-success",
                "position": "after-intro-before-ad-in",
                "text": "A word from our sponsors, amici.",
            },
            "representative_spots": {
                "count": 2,
                "helper": "mammamiradio.audio.normalizer.normalize_ad",
                "target_lufs": -15.0,
                "bed_gain_db": -30.0,
            },
            "mid_bumper": {
                "helper": "mammamiradio.audio.normalizer.fit_audio_oneshot",
                "duration_sec": 0.8,
                "target_lufs": -16.0,
                "fade_out_sec": 0.08,
            },
            "music_successor": {"speech_to_music_gain_db": 0.0, "gap_ms": 0},
        },
        "sources": sources,
        "artifacts": artifacts,
        "preview_groups": preview_groups,
        "quality_guards": quality_guards,
        "limitations": list(LIMITATIONS),
    }
    manifest["pack_digest"] = _core_pack_digest(manifest)
    manifest["listening_receipt"] = _receipt(manifest["pack_digest"])
    return manifest


def _render_in_place(
    treatment_manifest: Path,
    speech_manifest: Path,
    output_root: Path,
    *,
    generated_at: str,
) -> dict[str, Any]:
    inputs = _resolve_approved_inputs(treatment_manifest, speech_manifest)
    _require_dependency_chronology(generated_at, inputs.speech_generated_at)
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "audio").mkdir()
    (output_root / "components").mkdir()
    artifacts: list[dict[str, object]] = []

    signature_path = output_root / "components" / f"signature_{inputs.candidate_id}.mp3"
    shutil.copy2(inputs.signature_path, signature_path)
    station_bed = _render_identity_bed(
        signature_path,
        output_root / "components" / "station_bed.mp3",
        duration_sec=3.0,
        role="station",
    )
    sweeper_bed = _render_identity_bed(
        signature_path,
        output_root / "components" / "sweeper_bed.mp3",
        duration_sec=2.0,
        role="sweeper",
    )
    speech_successor = _render_voice_excerpt(
        inputs.voice_paths["identity-isabella"],
        output_root / "components" / "speech_successor.mp3",
        start_sec=1.514,
        duration_sec=3.22,
    )
    station_id = output_root / "audio" / "station_id.mp3"
    sweeper = output_root / "audio" / "sweeper.mp3"
    _render_voice_sting_mix(inputs.voice_paths["identity-isabella"], station_bed, station_id)
    _render_voice_sting_mix(inputs.speech_paths["speech.sweeper"], sweeper_bed, sweeper)

    runtime_mid_raw_sha256: str | None = None
    for spec in (*CORE_CUES, *CONTEXT_SPECS):
        if spec.id == "cue.ad-mid":
            rendered, runtime_mid_raw_sha256 = _render_runtime_mid(spec, output_root)
        else:
            rendered = _render_generated(spec, output_root)
        is_spot_bed = "spot-bed" in spec.tags
        artifacts.append(
            _artifact_record(
                output_root,
                artifact_id=spec.id,
                path=rendered,
                label=spec.label,
                purpose=spec.purpose,
                kind="core-cue" if spec in CORE_CUES else "audition-context",
                tags=spec.tags,
                foreground_source_ids=[] if is_spot_bed else [spec.foreground_source_id],
                layers=[
                    _generated_layer(
                        spec,
                        role="texture" if is_spot_bed else "foreground",
                        runtime_mid_raw_sha256=(runtime_mid_raw_sha256 if spec.id == "cue.ad-mid" else None),
                    )
                ],
            )
        )

    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}
    spot_records: list[dict[str, object]] = []
    for suffix in ("a", "b"):
        speech_id = f"speech.spot-{suffix}"
        bed_id = f"context.spot-bed-{suffix}"
        bed_path = output_root / str(artifact_by_id[bed_id]["path"])
        spot_path = _render_representative_spot(
            inputs.speech_paths[speech_id],
            bed_path,
            output_root / "components" / f"spot_{suffix}.mp3",
        )
        speech_metadata = inputs.speech_metadata[speech_id]
        speech_sha256 = _sha256(inputs.speech_paths[speech_id])
        record = _artifact_record(
            output_root,
            artifact_id=f"render.spot-{suffix}",
            path=spot_path,
            label=f"Representative voiced spot {suffix.upper()}",
            purpose="Approved-voice representative speech through the production ad normalization path.",
            kind="audition-context",
            tags=("context", "voiced-spot", "production-ad-dsp", "audition-only"),
            foreground_source_ids=[speech_id],
            layers=[
                {
                    "source_id": speech_id,
                    "source_sha256": speech_sha256,
                    "source_start_sec": 0.0,
                    "duration_sec": speech_metadata["duration_sec"],
                    "output_offset_sec": 0.0,
                    "gain_db": 0.0,
                    "dsp": [
                        "mix:voice-unity",
                        "normalize_ad:compressor,presence,air,highpass,loudnorm",
                        "runtime-reconcile:-15LUFS",
                    ],
                    "license": "production-voice-config",
                    "role": "voice",
                },
                {
                    "source_id": bed_id,
                    "source_sha256": artifact_by_id[bed_id]["sha256"],
                    "source_start_sec": 0.0,
                    "duration_sec": speech_metadata["duration_sec"],
                    "output_offset_sec": 0.0,
                    "gain_db": -30.0,
                    "dsp": ["loop-to-voice-duration", "fade:80ms-in/350ms-out"],
                    "license": "audition-only-composite",
                    "role": "texture",
                },
            ],
        )
        spot_records.append(record)
    artifacts.extend(spot_records)
    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}
    signature_record = _artifact_record(
        output_root,
        artifact_id="component.signature",
        path=signature_path,
        label=f"Approved {inputs.candidate_label} signature",
        purpose="Exact digest-bound treatment solo copied from the approved board.",
        kind="approved-signature",
        tags=("signature", inputs.candidate_id),
        foreground_source_ids=["approved-treatment.signature"],
        layers=[
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": _probe_audio(signature_path)[1],
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": ["copy:exact-approved-artifact"],
                "license": "audition-only-approved-direction",
                "role": "foreground",
            }
        ],
    )
    station_bed_record = _artifact_record(
        output_root,
        artifact_id="render.station-bed",
        path=station_bed,
        label="Station identity bed",
        purpose="Neon Relay once with fresh atmospheric and midrange support.",
        kind="internal-render",
        tags=("identity", "station-id", "atmospheric"),
        foreground_source_ids=["approved-treatment.signature"],
        layers=[
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": _probe_audio(signature_path)[1],
                "output_offset_sec": 0.0,
                "gain_db": -15.0,
                "dsp": ["copy:exact-approved-artifact", "identity-bed-gain:-15dB"],
                "license": "audition-only-approved-direction",
                "role": "foreground",
            },
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": _probe_audio(signature_path)[1],
                "output_offset_sec": 0.0,
                "gain_db": 9.0,
                "dsp": [
                    "identity-derived-midrange",
                    "volume:12dB",
                    "asoftclip:tanh,threshold=0.25,output=0.35,oversample=4",
                    "highpass:180Hz",
                    "lowpass:2400Hz",
                    "volume:-3dB",
                ],
                "license": "audition-only-approved-direction",
                "role": "texture",
            },
            {
                "source_id": "generated.identity-support.station",
                "source_sha256": _identity_support_hash("station"),
                "source_start_sec": 0.0,
                "duration_sec": 3.0,
                "output_offset_sec": 0.0,
                "gain_db": 4.0,
                "dsp": ["highpass:150Hz", "lowpass:5200Hz", "loudnorm:I=-16,LRA=6,TP=-1.5"],
                "license": "audition-only-local-generation",
                "role": "texture",
            },
        ],
    )
    sweeper_bed_record = _artifact_record(
        output_root,
        artifact_id="render.sweeper-bed",
        path=sweeper_bed,
        label="Sweeper identity bed",
        purpose="Neon Relay once with shorter atmospheric and midrange support.",
        kind="internal-render",
        tags=("identity", "sweeper", "atmospheric"),
        foreground_source_ids=["approved-treatment.signature"],
        layers=[
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": _probe_audio(signature_path)[1],
                "output_offset_sec": 0.0,
                "gain_db": -15.0,
                "dsp": ["copy:exact-approved-artifact", "identity-bed-gain:-15dB"],
                "license": "audition-only-approved-direction",
                "role": "foreground",
            },
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": _probe_audio(signature_path)[1],
                "output_offset_sec": 0.0,
                "gain_db": 9.0,
                "dsp": [
                    "identity-derived-midrange",
                    "volume:12dB",
                    "asoftclip:tanh,threshold=0.25,output=0.35,oversample=4",
                    "highpass:180Hz",
                    "lowpass:2400Hz",
                    "volume:-3dB",
                ],
                "license": "audition-only-approved-direction",
                "role": "texture",
            },
            {
                "source_id": "generated.identity-support.sweeper",
                "source_sha256": _identity_support_hash("sweeper"),
                "source_start_sec": 0.0,
                "duration_sec": 2.0,
                "output_offset_sec": 0.0,
                "gain_db": 4.0,
                "dsp": ["highpass:150Hz", "lowpass:5200Hz", "loudnorm:I=-16,LRA=6,TP=-1.5"],
                "license": "audition-only-local-generation",
                "role": "texture",
            },
        ],
    )
    speech_successor_record = _artifact_record(
        output_root,
        artifact_id="render.speech-successor",
        path=speech_successor,
        label="Spoken successor",
        purpose="Approved Isabella excerpt used to test a non-music successor.",
        kind="audition-context",
        tags=("voice", "spoken-successor", "approved-excerpt"),
        foreground_source_ids=["identity-isabella"],
        layers=[
            {
                "source_id": "identity-isabella",
                "source_sha256": inputs.voice_metadata["identity-isabella"]["audio_sha256"],
                "source_start_sec": 1.514,
                "duration_sec": 3.22,
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": ["trim", "fade:12ms-in/100ms-out", "loudnorm:I=-16,LRA=11,TP=-1.5"],
                "license": "production-voice-config",
                "role": "foreground",
            }
        ],
    )
    artifacts.extend([signature_record, station_bed_record, sweeper_bed_record, speech_successor_record])
    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}

    station_record = _artifact_record(
        output_root,
        artifact_id="preview.station-id",
        path=station_id,
        label="Station ID",
        purpose="Full approved Isabella station ID with the selected signature.",
        kind="preview",
        tags=("preview", "identity", "station-id"),
        foreground_source_ids=["approved-treatment.signature", "identity-isabella"],
        layers=[
            {
                "source_id": "render.station-bed",
                "source_sha256": station_bed_record["sha256"],
                "source_start_sec": 0.0,
                "duration_sec": station_bed_record["duration_sec"],
                "output_offset_sec": 0.0,
                "gain_db": round(20 * math.log10(0.15), 6),
                "dsp": ["mix_voice_with_sting:bed", "amix:duration=longest,normalize=0"],
                "license": "audition-only-composite",
                "role": "foreground",
            },
            {
                "source_id": "identity-isabella",
                "source_sha256": inputs.voice_metadata["identity-isabella"]["audio_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.voice_metadata["identity-isabella"]["duration_sec"],
                "output_offset_sec": 0.4,
                "gain_db": round(20 * math.log10(1.2), 6),
                "dsp": [
                    "mix_voice_with_sting:voice",
                    "amix:duration=longest,normalize=0",
                    "loudnorm:I=-16,LRA=11,TP=-1.5",
                ],
                "license": "production-voice-config",
                "role": "voice",
            },
        ],
    )
    sweeper_record = _artifact_record(
        output_root,
        artifact_id="preview.sweeper",
        path=sweeper,
        label="Sweeper",
        purpose="Short Isabella station-name sweep with the selected signature once.",
        kind="preview",
        tags=("preview", "identity", "sweeper"),
        foreground_source_ids=["approved-treatment.signature", "speech.sweeper"],
        layers=[
            {
                "source_id": "render.sweeper-bed",
                "source_sha256": sweeper_bed_record["sha256"],
                "source_start_sec": 0.0,
                "duration_sec": sweeper_bed_record["duration_sec"],
                "output_offset_sec": 0.0,
                "gain_db": round(20 * math.log10(0.15), 6),
                "dsp": ["mix_voice_with_sting:bed", "amix:duration=longest,normalize=0"],
                "license": "audition-only-composite",
                "role": "foreground",
            },
            {
                "source_id": "speech.sweeper",
                "source_sha256": _sha256(inputs.speech_paths["speech.sweeper"]),
                "source_start_sec": 0.0,
                "duration_sec": inputs.speech_metadata["speech.sweeper"]["duration_sec"],
                "output_offset_sec": 0.4,
                "gain_db": round(20 * math.log10(1.2), 6),
                "dsp": [
                    "mix_voice_with_sting:voice",
                    "amix:duration=longest,normalize=0",
                    "loudnorm:I=-16,LRA=11,TP=-1.5",
                ],
                "license": "production-voice-config",
                "role": "voice",
            },
        ],
    )
    artifacts.extend([station_record, sweeper_record])
    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}

    predecessor = output_root / str(artifact_by_id["context.program-predecessor"]["path"])
    preroll = _render_preroll(predecessor, output_root / "components" / "program_preroll.mp3")
    intro_crossfade = output_root / "components" / "intro_crossfade.mp3"
    crossfade_voice_over_music(
        predecessor,
        inputs.voice_paths["identity-marco"],
        intro_crossfade,
        tail_seconds=8.0,
        voice_volume=1.0,
        music_fade_volume=0.5,
        voice_delay_ms=150,
    )
    preroll_record = _artifact_record(
        output_root,
        artifact_id="render.program-preroll",
        path=preroll,
        label="Dry predecessor preroll",
        purpose="Last 2.5 seconds of the program predecessor before the fallback transition.",
        kind="audition-context",
        tags=("context", "program-preroll"),
        foreground_source_ids=["generated.context.program-predecessor"],
        layers=[
            {
                "source_id": "context.program-predecessor",
                "source_sha256": artifact_by_id["context.program-predecessor"]["sha256"],
                "source_start_sec": 7.5,
                "duration_sec": 2.5,
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": ["runtime-tail-excerpt"],
                "license": "audition-only-composite",
                "role": "foreground",
            }
        ],
    )
    intro_crossfade_record = _artifact_record(
        output_root,
        artifact_id="render.intro-crossfade",
        path=intro_crossfade,
        label="Adjacent-music intro",
        purpose="Production crossfade of approved Marco over the music tail.",
        kind="internal-render",
        tags=("context", "ad-intro", "adjacent-music"),
        foreground_source_ids=["generated.context.program-predecessor", "identity-marco"],
        layers=[
            {
                "source_id": "context.program-predecessor",
                "source_sha256": artifact_by_id["context.program-predecessor"]["sha256"],
                "source_start_sec": 2.0,
                "duration_sec": 8.0,
                "output_offset_sec": 0.0,
                "gain_db": round(20 * math.log10(0.5), 6),
                "dsp": ["crossfade_voice_over_music:tail", "fade-out:8s"],
                "license": "audition-only-composite",
                "role": "foreground",
            },
            {
                "source_id": "identity-marco",
                "source_sha256": inputs.voice_metadata["identity-marco"]["audio_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.voice_metadata["identity-marco"]["duration_sec"],
                "output_offset_sec": 0.15,
                "gain_db": 0.0,
                "dsp": ["crossfade_voice_over_music:voice", "loudnorm:I=-16,LRA=11,TP=-1.5"],
                "license": "production-voice-config",
                "role": "voice",
            },
        ],
    )
    artifacts.extend([preroll_record, intro_crossfade_record])
    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}

    dry_intro_record = {
        "id": "source.marco-intro",
        "path": None,
        "label": "Approved Marco break intro",
        "purpose": "External approved voice source used directly in cadence assembly.",
        "kind": "source-alias",
        "tags": ["voice", "ad-intro", "approved"],
        "sha256": inputs.voice_metadata["identity-marco"]["audio_sha256"],
        "duration_sec": inputs.voice_metadata["identity-marco"]["duration_sec"],
        "format": FORMAT,
        "foreground_source_ids": ["identity-marco"],
        "layers": [],
    }
    outro_record = {
        "id": "source.giulia-outro",
        "path": None,
        "label": "Approved Giulia break outro",
        "purpose": "External approved voice source used directly in cadence assembly.",
        "kind": "source-alias",
        "tags": ["voice", "ad-outro", "approved"],
        "sha256": inputs.voice_metadata["identity-giulia"]["audio_sha256"],
        "duration_sec": inputs.voice_metadata["identity-giulia"]["duration_sec"],
        "format": FORMAT,
        "foreground_source_ids": ["identity-giulia"],
        "layers": [],
    }
    promo_record = {
        "id": "source.promo",
        "path": None,
        "label": "Normal-mode sponsor disclosure",
        "purpose": "Frozen current-runtime-route rendering of the normal promo tag.",
        "kind": "source-alias",
        "tags": ["voice", "promo", "runtime-route"],
        "sha256": _sha256(inputs.speech_paths["speech.promo"]),
        "duration_sec": inputs.speech_metadata["speech.promo"]["duration_sec"],
        "format": FORMAT,
        "foreground_source_ids": ["speech.promo"],
        "layers": [],
    }
    artifacts.extend([dry_intro_record, promo_record, outro_record])
    artifact_by_id = {str(artifact["id"]): artifact for artifact in artifacts}

    sequence_paths: dict[str, Path] = {
        **{
            artifact_id: output_root / str(artifact_by_id[artifact_id]["path"])
            for artifact_id in artifact_by_id
            if artifact_by_id[artifact_id]["path"] is not None
        },
        "source.marco-intro": inputs.voice_paths["identity-marco"],
        "source.promo": inputs.speech_paths["speech.promo"],
        "source.giulia-outro": inputs.voice_paths["identity-giulia"],
    }
    for artifact_id, sequence in BREAK_SEQUENCES.items():
        output_path = output_root / "components" / f"{artifact_id.removeprefix('render.break.').replace('.', '_')}.mp3"
        _concat([sequence_paths[item] for item in sequence], output_path, silence_ms=300)
        record = _artifact_record(
            output_root,
            artifact_id=artifact_id,
            path=output_path,
            label=artifact_id.replace("render.break.", "Break ").replace(".", " ").title(),
            purpose="Runtime-order ad-break assembly at 300 ms gaps and without final loudness pass.",
            kind="internal-render",
            tags=("ad-break", "runtime-order", "audition-only"),
            foreground_source_ids=_flatten_foregrounds(artifact_by_id, sequence),
            layers=_sequence_layers(artifact_by_id, sequence, gap_sec=0.3),
        )
        artifacts.append(record)
        artifact_by_id[artifact_id] = record
        sequence_paths[artifact_id] = output_path

    for artifact_id, (label, sequence) in CADENCE_SEQUENCES.items():
        filename = artifact_id.removeprefix("preview.cadence.").replace(".", "_") + ".mp3"
        output_path = output_root / "audio" / filename
        _concat([sequence_paths[item] for item in sequence], output_path, silence_ms=0)
        foregrounds = _flatten_foregrounds(artifact_by_id, sequence)
        if len(foregrounds) != len(set(foregrounds)):
            repeated = sorted({source for source in foregrounds if foregrounds.count(source) > 1})
            raise ValueError(f"Foreground source repeats within {artifact_id}: {', '.join(repeated)}")
        record = _artifact_record(
            output_root,
            artifact_id=artifact_id,
            path=output_path,
            label=label,
            purpose="Complete runtime-faithful cadence branch.",
            kind=("preview" if artifact_id == "preview.cadence.two-with-mid.music-successor" else "validation-render"),
            tags=(
                ("preview", "full-cadence", "runtime-branch")
                if artifact_id == "preview.cadence.two-with-mid.music-successor"
                else ("internal", "branch-coverage", "runtime-branch")
            ),
            foreground_source_ids=foregrounds,
            layers=_sequence_layers(artifact_by_id, sequence, gap_sec=0.0),
        )
        artifacts.append(record)
        artifact_by_id[artifact_id] = record
        sequence_paths[artifact_id] = output_path

    manifest = _build_manifest(
        output_root,
        inputs=inputs,
        generated_at=generated_at,
        artifacts=artifacts,
    )
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "README.md").write_text(_readme(manifest), encoding="utf-8")
    (output_root / "index.html").write_text(_html(manifest), encoding="utf-8")
    (output_root / ".ready").write_text(f"{manifest['pack_digest']}\n", encoding="ascii")
    return manifest


LAYER_FIELDS = frozenset(
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


def _expected_layers(
    artifact_id: str,
    artifact_by_id: Mapping[str, Mapping[str, object]],
    inputs: ApprovedInputs,
) -> list[dict[str, object]]:
    """Reconstruct the exact provenance contract independently of manifest claims."""
    spec = SPEC_BY_ID.get(artifact_id)
    if spec is not None:
        is_spot_bed = "spot-bed" in spec.tags
        if artifact_id == "cue.ad-mid":
            raw_layers = artifact_by_id[artifact_id].get("layers")
            if not isinstance(raw_layers, list) or len(raw_layers) != 1 or not isinstance(raw_layers[0], Mapping):
                raise ValueError("Runtime mid cue has no source-master provenance")
            raw_sha256 = raw_layers[0].get("rendered_master_sha256")
            if not isinstance(raw_sha256, str) or not SHA256_RE.fullmatch(raw_sha256):
                raise ValueError("Runtime mid cue has no valid rendered-master hash")
            return [_generated_layer(spec, runtime_mid_raw_sha256=raw_sha256)]
        return [_generated_layer(spec, role="texture" if is_spot_bed else "foreground")]

    signature_duration = _probe_audio(inputs.signature_path)[1]
    if artifact_id == "component.signature":
        return [
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": signature_duration,
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": ["copy:exact-approved-artifact"],
                "license": "audition-only-approved-direction",
                "role": "foreground",
            }
        ]
    if artifact_id in {"render.station-bed", "render.sweeper-bed"}:
        role = "station" if artifact_id.endswith("station-bed") else "sweeper"
        duration = 3.0 if role == "station" else 2.0
        return [
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": signature_duration,
                "output_offset_sec": 0.0,
                "gain_db": -15.0,
                "dsp": ["copy:exact-approved-artifact", "identity-bed-gain:-15dB"],
                "license": "audition-only-approved-direction",
                "role": "foreground",
            },
            {
                "source_id": "approved-treatment.signature",
                "source_sha256": inputs.signature_sha256,
                "source_start_sec": 0.0,
                "duration_sec": signature_duration,
                "output_offset_sec": 0.0,
                "gain_db": 9.0,
                "dsp": [
                    "identity-derived-midrange",
                    "volume:12dB",
                    "asoftclip:tanh,threshold=0.25,output=0.35,oversample=4",
                    "highpass:180Hz",
                    "lowpass:2400Hz",
                    "volume:-3dB",
                ],
                "license": "audition-only-approved-direction",
                "role": "texture",
            },
            {
                "source_id": f"generated.identity-support.{role}",
                "source_sha256": _identity_support_hash(role),
                "source_start_sec": 0.0,
                "duration_sec": duration,
                "output_offset_sec": 0.0,
                "gain_db": 4.0,
                "dsp": ["highpass:150Hz", "lowpass:5200Hz", "loudnorm:I=-16,LRA=6,TP=-1.5"],
                "license": "audition-only-local-generation",
                "role": "texture",
            },
        ]
    if artifact_id == "render.speech-successor":
        return [
            {
                "source_id": "identity-isabella",
                "source_sha256": inputs.voice_metadata["identity-isabella"]["audio_sha256"],
                "source_start_sec": 1.514,
                "duration_sec": 3.22,
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": ["trim", "fade:12ms-in/100ms-out", "loudnorm:I=-16,LRA=11,TP=-1.5"],
                "license": "production-voice-config",
                "role": "foreground",
            }
        ]
    if artifact_id in {"render.spot-a", "render.spot-b"}:
        suffix = artifact_id[-1]
        speech_id = f"speech.spot-{suffix}"
        bed_id = f"context.spot-bed-{suffix}"
        return [
            {
                "source_id": speech_id,
                "source_sha256": inputs.speech_metadata[speech_id]["audio_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.speech_metadata[speech_id]["duration_sec"],
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": [
                    "mix:voice-unity",
                    "normalize_ad:compressor,presence,air,highpass,loudnorm",
                    "runtime-reconcile:-15LUFS",
                ],
                "license": "production-voice-config",
                "role": "voice",
            },
            {
                "source_id": bed_id,
                "source_sha256": artifact_by_id[bed_id]["sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.speech_metadata[speech_id]["duration_sec"],
                "output_offset_sec": 0.0,
                "gain_db": -30.0,
                "dsp": ["loop-to-voice-duration", "fade:80ms-in/350ms-out"],
                "license": "audition-only-composite",
                "role": "texture",
            },
        ]
    if artifact_id == "preview.station-id":
        bed = artifact_by_id["render.station-bed"]
        return [
            {
                "source_id": "render.station-bed",
                "source_sha256": bed["sha256"],
                "source_start_sec": 0.0,
                "duration_sec": bed["duration_sec"],
                "output_offset_sec": 0.0,
                "gain_db": round(20 * math.log10(0.15), 6),
                "dsp": ["mix_voice_with_sting:bed", "amix:duration=longest,normalize=0"],
                "license": "audition-only-composite",
                "role": "foreground",
            },
            {
                "source_id": "identity-isabella",
                "source_sha256": inputs.voice_metadata["identity-isabella"]["audio_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.voice_metadata["identity-isabella"]["duration_sec"],
                "output_offset_sec": 0.4,
                "gain_db": round(20 * math.log10(1.2), 6),
                "dsp": [
                    "mix_voice_with_sting:voice",
                    "amix:duration=longest,normalize=0",
                    "loudnorm:I=-16,LRA=11,TP=-1.5",
                ],
                "license": "production-voice-config",
                "role": "voice",
            },
        ]
    if artifact_id == "preview.sweeper":
        bed = artifact_by_id["render.sweeper-bed"]
        return [
            {
                "source_id": "render.sweeper-bed",
                "source_sha256": bed["sha256"],
                "source_start_sec": 0.0,
                "duration_sec": bed["duration_sec"],
                "output_offset_sec": 0.0,
                "gain_db": round(20 * math.log10(0.15), 6),
                "dsp": ["mix_voice_with_sting:bed", "amix:duration=longest,normalize=0"],
                "license": "audition-only-composite",
                "role": "foreground",
            },
            {
                "source_id": "speech.sweeper",
                "source_sha256": inputs.speech_metadata["speech.sweeper"]["audio_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.speech_metadata["speech.sweeper"]["duration_sec"],
                "output_offset_sec": 0.4,
                "gain_db": round(20 * math.log10(1.2), 6),
                "dsp": [
                    "mix_voice_with_sting:voice",
                    "amix:duration=longest,normalize=0",
                    "loudnorm:I=-16,LRA=11,TP=-1.5",
                ],
                "license": "production-voice-config",
                "role": "voice",
            },
        ]
    if artifact_id == "render.program-preroll":
        source = artifact_by_id["context.program-predecessor"]
        return [
            {
                "source_id": "context.program-predecessor",
                "source_sha256": source["sha256"],
                "source_start_sec": 7.5,
                "duration_sec": 2.5,
                "output_offset_sec": 0.0,
                "gain_db": 0.0,
                "dsp": ["runtime-tail-excerpt"],
                "license": "audition-only-composite",
                "role": "foreground",
            }
        ]
    if artifact_id == "render.intro-crossfade":
        predecessor = artifact_by_id["context.program-predecessor"]
        return [
            {
                "source_id": "context.program-predecessor",
                "source_sha256": predecessor["sha256"],
                "source_start_sec": 2.0,
                "duration_sec": 8.0,
                "output_offset_sec": 0.0,
                "gain_db": round(20 * math.log10(0.5), 6),
                "dsp": ["crossfade_voice_over_music:tail", "fade-out:8s"],
                "license": "audition-only-composite",
                "role": "foreground",
            },
            {
                "source_id": "identity-marco",
                "source_sha256": inputs.voice_metadata["identity-marco"]["audio_sha256"],
                "source_start_sec": 0.0,
                "duration_sec": inputs.voice_metadata["identity-marco"]["duration_sec"],
                "output_offset_sec": 0.15,
                "gain_db": 0.0,
                "dsp": ["crossfade_voice_over_music:voice", "loudnorm:I=-16,LRA=11,TP=-1.5"],
                "license": "production-voice-config",
                "role": "voice",
            },
        ]
    if artifact_id in BREAK_SEQUENCES:
        return _sequence_layers(artifact_by_id, BREAK_SEQUENCES[artifact_id], gap_sec=0.3)
    if artifact_id in CADENCE_SEQUENCES:
        return _sequence_layers(artifact_by_id, CADENCE_SEQUENCES[artifact_id][1], gap_sec=0.0)
    if artifact_id in {"source.marco-intro", "source.promo", "source.giulia-outro"}:
        return []
    raise ValueError(f"No provenance contract exists for core artifact: {artifact_id}")


def _flatten_layer_sources(
    artifact_id: str,
    artifact_by_id: Mapping[str, Mapping[str, object]],
    *,
    stack: tuple[str, ...] = (),
) -> list[str]:
    if artifact_id in stack:
        raise ValueError(f"Core provenance contains an artifact cycle: {artifact_id}")
    artifact = artifact_by_id[artifact_id]
    if artifact.get("kind") == "source-alias":
        raw = artifact.get("foreground_source_ids")
        if not isinstance(raw, list):
            raise ValueError(f"Source alias has no foreground ledger: {artifact_id}")
        return [str(value) for value in raw]
    layers = artifact.get("layers")
    if not isinstance(layers, list):
        raise ValueError(f"Core artifact has no layer ledger: {artifact_id}")
    flattened: list[str] = []
    for layer in layers:
        if not isinstance(layer, Mapping) or layer.get("role") not in {"foreground", "voice", "component"}:
            continue
        source_id = str(layer.get("source_id"))
        if source_id in artifact_by_id:
            flattened.extend(_flatten_layer_sources(source_id, artifact_by_id, stack=(*stack, artifact_id)))
        else:
            flattened.append(source_id)
    return flattened


def _validate_provenance(
    manifest: Mapping[str, object],
    artifact_by_id: Mapping[str, Mapping[str, object]],
    inputs: ApprovedInputs,
) -> None:
    sources = manifest.get("sources")
    expected_sources = _source_inventory(inputs)
    if sources != expected_sources:
        raise ValueError("Core source inventory or source provenance is stale")
    source_by_id = {str(source["id"]): source for source in expected_sources}
    if len(source_by_id) != len(expected_sources):
        raise ValueError("Core source inventory repeats IDs")

    alias_sources = {
        "source.marco-intro": "identity-marco",
        "source.promo": "speech.promo",
        "source.giulia-outro": "identity-giulia",
    }
    for artifact_id, artifact in artifact_by_id.items():
        layers = artifact.get("layers")
        if not isinstance(layers, list):
            raise ValueError(f"Core artifact has no layer ledger: {artifact_id}")
        expected_layers = _expected_layers(artifact_id, artifact_by_id, inputs)
        if layers != expected_layers:
            raise ValueError(f"Core artifact layer provenance is stale: {artifact_id}")
        prior_offset = -1.0
        for layer in layers:
            assert isinstance(layer, Mapping)
            allowed_fields = LAYER_FIELDS | ({"rendered_master_sha256"} if artifact_id == "cue.ad-mid" else set())
            if set(layer) != allowed_fields:
                raise ValueError(f"Core artifact layer has an invalid field set: {artifact_id}")
            source_id = layer.get("source_id")
            source_sha256 = layer.get("source_sha256")
            if not isinstance(source_id, str) or (source_id not in source_by_id and source_id not in artifact_by_id):
                raise ValueError(f"Core layer references an unknown source: {artifact_id}")
            expected_sha256 = (
                source_by_id[source_id]["source_sha256"]
                if source_id in source_by_id
                else artifact_by_id[source_id]["sha256"]
            )
            if source_sha256 != expected_sha256:
                raise ValueError(f"Core layer source hash is stale: {artifact_id}")
            for field in ("source_start_sec", "duration_sec", "output_offset_sec", "gain_db"):
                number = layer.get(field)
                if isinstance(number, bool) or not isinstance(number, int | float) or not math.isfinite(float(number)):
                    raise ValueError(f"Core layer has invalid numeric provenance: {artifact_id}.{field}")
            if float(layer["source_start_sec"]) < 0 or float(layer["duration_sec"]) <= 0:
                raise ValueError(f"Core layer has an invalid source excerpt: {artifact_id}")
            offset = float(layer["output_offset_sec"])
            if offset < 0 or offset < prior_offset:
                raise ValueError(f"Core layer offsets are not monotonic: {artifact_id}")
            prior_offset = offset
            dsp = layer.get("dsp")
            if not isinstance(dsp, list) or not dsp or not all(isinstance(item, str) and item for item in dsp):
                raise ValueError(f"Core layer has invalid DSP provenance: {artifact_id}")
            if not isinstance(layer.get("license"), str) or not str(layer["license"]).strip():
                raise ValueError(f"Core layer has no license provenance: {artifact_id}")
            if layer.get("role") not in {"foreground", "voice", "texture", "component"}:
                raise ValueError(f"Core layer has an invalid role: {artifact_id}")

        raw_foregrounds = artifact.get("foreground_source_ids")
        if not isinstance(raw_foregrounds, list):
            raise ValueError(f"Core artifact has no foreground ledger: {artifact_id}")
        if artifact_id in alias_sources:
            alias_source = source_by_id[alias_sources[artifact_id]]
            if (
                raw_foregrounds != [alias_sources[artifact_id]]
                or artifact.get("sha256") != alias_source["source_sha256"]
                or artifact.get("duration_sec") != alias_source["duration_sec"]
                or artifact.get("format") != FORMAT
            ):
                raise ValueError(f"Core source alias is misbound: {artifact_id}")
        elif raw_foregrounds != _flatten_layer_sources(artifact_id, artifact_by_id):
            raise ValueError(f"Core foreground ledger disagrees with its layers: {artifact_id}")


def _validate_semantics(manifest: Mapping[str, object]) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Core manifest has no artifact inventory")
    artifact_by_id = {str(item.get("id")): item for item in artifacts if isinstance(item, Mapping)}
    expected_ids = {
        *(spec.id for spec in (*CORE_CUES, *CONTEXT_SPECS)),
        "component.signature",
        "render.station-bed",
        "render.sweeper-bed",
        "render.speech-successor",
        "render.spot-a",
        "render.spot-b",
        "preview.station-id",
        "preview.sweeper",
        "render.program-preroll",
        "render.intro-crossfade",
        "source.marco-intro",
        "source.promo",
        "source.giulia-outro",
        *BREAK_SEQUENCES,
        *CADENCE_ARTIFACT_IDS,
    }
    if set(artifact_by_id) != expected_ids or len(artifact_by_id) != len(artifacts):
        raise ValueError("Core artifact inventory is incomplete or repeats IDs")

    direction = manifest.get("design_direction")
    if not isinstance(direction, Mapping):
        raise ValueError("Core manifest has no design direction")
    upstream_treatment = manifest.get("upstream_treatment")
    if not isinstance(upstream_treatment, Mapping):
        raise ValueError("Core manifest has no upstream treatment")
    expected_direction = {
        "signature": "neon_relay",
        "signature_roles": ["station-id", "sweeper"],
        "atmospheric_reference": "velvet_horizon",
        "atmospheric_reference_usage": "freshly-authored texture and transitions; candidate audio not reused",
        "small_speaker_invariant": "identity anchored above 180 Hz; bass reinforces but never carries recognition",
    }
    if direction != expected_direction:
        raise ValueError("Core design direction is stale or semantically inconsistent")
    if (
        upstream_treatment.get("candidate_id") != "neon_relay"
        or upstream_treatment.get("candidate_label") != "Neon Relay"
    ):
        raise ValueError("Core treatment is not the approved Neon Relay direction")

    banned = {"brass", "mandolin", "trumpet"}
    for artifact in artifacts:
        assert isinstance(artifact, Mapping)
        searchable = " ".join(
            [str(artifact.get("purpose", "")), *(str(tag) for tag in artifact.get("tags", []))]
        ).lower()
        if any(word in searchable for word in banned):
            raise ValueError(f"Banned semantic material appears in {artifact.get('id')}")

    signature_roles = []
    foreground_counts: dict[str, int] = {}
    for artifact_id in CORE_ROLE_ARTIFACT_IDS:
        raw_ids = artifact_by_id[artifact_id].get("foreground_source_ids")
        if not isinstance(raw_ids, list):
            raise ValueError(f"Core role has no foreground ledger: {artifact_id}")
        for source_id in raw_ids:
            foreground_counts[str(source_id)] = foreground_counts.get(str(source_id), 0) + 1
            if source_id == "approved-treatment.signature":
                signature_roles.append(artifact_id)
    if signature_roles != ["preview.station-id", "preview.sweeper"]:
        raise ValueError("Selected signature must appear only in station ID and sweeper")
    if max(foreground_counts.values(), default=0) > 2:
        raise ValueError("One foreground source appears in more than two core roles")
    mid_duration = artifact_by_id["cue.ad-mid"].get("duration_sec")
    if (
        isinstance(mid_duration, bool)
        or not isinstance(mid_duration, int | float)
        or not 0.8 <= float(mid_duration) <= 0.86
    ):
        raise ValueError("Runtime-fitted ad-mid output is not the requested 0.8-second one-shot")
    for artifact_id in CADENCE_ARTIFACT_IDS:
        foregrounds = artifact_by_id[artifact_id].get("foreground_source_ids")
        if not isinstance(foregrounds, list) or len(foregrounds) != len(set(foregrounds)):
            raise ValueError(f"Foreground source repeats within {artifact_id}")


def validate_core_cadence_gate(
    manifest_path: Path,
    *,
    require_ready: bool = True,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    """Validate upstream receipts, artifacts, listening surfaces, and guards."""
    if manifest_path.is_symlink():
        raise ValueError("Core manifest must not be a symlink")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Core manifest is not readable JSON: {manifest_path}") from exc
    if not isinstance(raw_manifest, dict) or set(raw_manifest) != MANIFEST_FIELDS:
        raise ValueError("Core manifest has an invalid field set")
    manifest: dict[str, Any] = raw_manifest
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("stage") != STAGE:
        raise ValueError("Core manifest has the wrong schema or stage")
    if manifest.get("release_ready") is not False:
        raise ValueError("Core audition must never be release-ready")
    if not isinstance(manifest.get("generated_at"), str) or not TIMESTAMP_RE.fullmatch(manifest["generated_at"]):
        raise ValueError("Core manifest has an invalid generation time")
    if manifest.get("scope") != SCOPE or manifest.get("limitations") != list(LIMITATIONS):
        raise ValueError("Core manifest scope or limitations are stale")

    upstream_treatment = manifest.get("upstream_treatment")
    if not isinstance(upstream_treatment, Mapping):
        raise ValueError("Core manifest has no treatment lineage")
    treatment_manifest = _path_from_record(
        upstream_treatment.get("manifest_path"),
        upstream_treatment.get("manifest_path_kind"),
        label="treatment manifest",
    )
    upstream_speech = manifest.get("upstream_speech")
    if not isinstance(upstream_speech, Mapping):
        raise ValueError("Core manifest has no frozen speech lineage")
    speech_manifest = _path_from_record(
        upstream_speech.get("manifest_path"),
        upstream_speech.get("manifest_path_kind"),
        label="core speech manifest",
    )
    inputs = _resolve_approved_inputs(treatment_manifest, speech_manifest)
    _require_dependency_chronology(manifest["generated_at"], inputs.speech_generated_at)
    if upstream_treatment != {
        "manifest_path": _path_record(inputs.treatment_manifest)[0],
        "manifest_path_kind": _path_record(inputs.treatment_manifest)[1],
        "pack_digest": inputs.treatment_pack_digest,
        "candidate_id": inputs.candidate_id,
        "candidate_label": inputs.candidate_label,
        "candidate_sha256": inputs.signature_sha256,
        "reviewed_at": inputs.treatment_reviewed_at,
    }:
        raise ValueError("Core treatment lineage is stale")
    upstream_identity = manifest.get("upstream_identity")
    expected_identity = {
        "manifest_path": _path_record(inputs.identity_manifest)[0],
        "manifest_path_kind": _path_record(inputs.identity_manifest)[1],
        "pack_digest": inputs.identity_pack_digest,
        "voice_sha256": {
            voice_id: inputs.voice_metadata[voice_id]["audio_sha256"]
            for voice_id in ("identity-isabella", "identity-marco", "identity-giulia")
        },
    }
    if upstream_identity != expected_identity:
        raise ValueError("Core identity lineage is stale")
    expected_speech = {
        "manifest_path": _path_record(inputs.speech_manifest)[0],
        "manifest_path_kind": _path_record(inputs.speech_manifest)[1],
        "pack_digest": inputs.speech_pack_digest,
        "generated_at": inputs.speech_generated_at,
        "clip_sha256": {
            speech_id: inputs.speech_metadata[speech_id]["audio_sha256"] for speech_id in FROZEN_SPEECH_IDS
        },
    }
    if upstream_speech != expected_speech:
        raise ValueError("Core frozen-speech lineage is stale")
    expected_contract = {
        "format": FORMAT,
        "station_voice_mix": {
            "helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
            "sting_gain_linear": 0.15,
            "voice_gain_linear": 1.2,
            "voice_delay_ms": 400,
            "amix_normalize": False,
            "filter_complex_threads": 1,
        },
        "adjacent_music_intro": {
            "helper": "mammamiradio.audio.normalizer.crossfade_voice_over_music",
            "tail_sec": 8.0,
            "music_gain_linear": 0.5,
            "voice_gain_linear": 1.0,
            "voice_delay_ms": 150,
            "music_to_speech_suppressed": True,
        },
        "dry_predecessor": {"preroll_sec": 2.5, "music_to_speech_gain_db": 0.0},
        "ad_break": {"gap_ms": 300, "final_loudnorm": False, "max_mid_bumpers": 1},
        "promo": {
            "branch": "render-success",
            "position": "after-intro-before-ad-in",
            "text": "A word from our sponsors, amici.",
        },
        "representative_spots": {
            "count": 2,
            "helper": "mammamiradio.audio.normalizer.normalize_ad",
            "target_lufs": -15.0,
            "bed_gain_db": -30.0,
        },
        "mid_bumper": {
            "helper": "mammamiradio.audio.normalizer.fit_audio_oneshot",
            "duration_sec": 0.8,
            "target_lufs": -16.0,
            "fade_out_sec": 0.08,
        },
        "music_successor": {"speech_to_music_gain_db": 0.0, "gap_ms": 0},
    }
    if manifest.get("production_contract") != expected_contract:
        raise ValueError("Core production contract is stale")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Core manifest has no artifacts")
    audio_paths: list[Path] = []
    output_hashes: dict[str, str] = {}
    artifact_by_id: dict[str, Mapping[str, object]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise ValueError("Core manifest contains invalid artifact metadata")
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, str) or artifact_id in artifact_by_id:
            raise ValueError("Core manifest repeats or omits an artifact ID")
        artifact_by_id[artifact_id] = artifact
        relative_path = artifact.get("path")
        if relative_path is None:
            if artifact.get("kind") != "source-alias":
                raise ValueError(f"Core artifact has no path: {artifact_id}")
            continue
        if not isinstance(relative_path, str):
            raise ValueError(f"Core artifact has an invalid path: {artifact_id}")
        path = manifest_path.parent / relative_path
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Core artifact is missing or unsafe: {artifact_id}")
        actual_hash = _sha256(path)
        if artifact.get("sha256") != actual_hash:
            raise ValueError(f"Core artifact hash differs from the manifest: {artifact_id}")
        if actual_hash in output_hashes:
            raise ValueError(f"Exact duplicate core outputs: {output_hashes[actual_hash]} and {artifact_id}")
        output_hashes[actual_hash] = artifact_id
        audio_format, duration_sec = _probe_audio(path)
        if artifact.get("format") != audio_format or artifact.get("duration_sec") != duration_sec:
            raise ValueError(f"Core artifact probe evidence differs from the manifest: {artifact_id}")
        audio_paths.append(path)
    _validate_provenance(manifest, artifact_by_id, inputs)
    _validate_semantics(manifest)

    expected_quality = _correlation_evidence(artifact_by_id, manifest_path.parent)
    if manifest.get("quality_guards") != expected_quality:
        raise ValueError("Core quality-guard evidence is stale")
    preview_groups = manifest.get("preview_groups")
    if preview_groups != _preview_groups():
        raise ValueError("Core listening surface inventory is stale")
    assert isinstance(preview_groups, list)
    visible_tracks = 0
    for group in preview_groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("tracks"), list):
            raise ValueError("Core listening group is invalid")
        visible_tracks += len(group["tracks"])
        for artifact_id in group["tracks"]:
            if artifact_id not in artifact_by_id:
                raise ValueError("Core listening group references an unknown artifact")
    if visible_tracks != 5:
        raise ValueError("Core listening board must expose exactly five audio tracks")

    pack_digest = manifest.get("pack_digest")
    if not isinstance(pack_digest, str) or not SHA256_RE.fullmatch(pack_digest):
        raise ValueError("Core manifest has an invalid pack digest")
    if pack_digest != _core_pack_digest(manifest):
        raise ValueError("Core manifest pack_digest is stale or invalid")
    _validate_receipt(manifest.get("listening_receipt"), pack_digest=pack_digest)

    expected_files = {
        "manifest.json",
        "README.md",
        "index.html",
        *(path.relative_to(manifest_path.parent).as_posix() for path in audio_paths),
    }
    ready_path = manifest_path.parent / ".ready"
    if require_ready or ready_path.exists() or ready_path.is_symlink():
        expected_files.add(".ready")
    actual_files = {
        path.relative_to(manifest_path.parent).as_posix()
        for path in manifest_path.parent.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != expected_files:
        raise ValueError("Core board inventory differs from the manifest")
    expected_surfaces = {"README.md": _readme(manifest), "index.html": _html(manifest)}
    for filename, expected in expected_surfaces.items():
        path = manifest_path.parent / filename
        if path.is_symlink() or path.read_text(encoding="utf-8") != expected:
            raise ValueError(f"Core listening surface differs from the validated manifest: {filename}")
    if require_ready and (ready_path.is_symlink() or ready_path.read_text(encoding="ascii").strip() != pack_digest):
        raise ValueError("Core ready marker does not match pack_digest")
    return manifest, tuple(audio_paths)


def render_core_cadence_gate(
    treatment_manifest: Path,
    speech_manifest: Path,
    output_root: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Render and atomically publish the five-group core listening board."""
    _require_safe_output_root(output_root)
    if treatment_manifest.is_symlink():
        raise ValueError("Treatment manifest must not be a symlink")
    if speech_manifest.is_symlink():
        raise ValueError("Core speech manifest must not be a symlink")
    if os.path.lexists(output_root):
        raise FileExistsError(f"Refusing to overwrite core cadence gate: {output_root}")
    inputs = _resolve_approved_inputs(treatment_manifest, speech_manifest)
    stamp = generated_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("generated_at must use YYYYMMDDTHHMMSSZ format")
    _require_dependency_chronology(stamp, inputs.speech_generated_at)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    claim_path = output_root.parent / f".{output_root.name}.claim"
    try:
        claim_fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise FileExistsError(f"Core cadence gate is already being rendered: {output_root}") from None
    os.close(claim_fd)
    staging_root: Path | None = None
    try:
        staging_root = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
        manifest = _render_in_place(
            treatment_manifest,
            speech_manifest,
            staging_root,
            generated_at=stamp,
        )
        validate_core_cadence_gate(staging_root / "manifest.json")
        if os.path.lexists(output_root):
            raise FileExistsError(f"Core cadence gate appeared during render: {output_root}")
        os.replace(staging_root, output_root)
        return manifest
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        claim_path.unlink(missing_ok=True)


def _evidence_snapshot(
    manifest_path: Path,
    manifest: Mapping[str, object],
    audio_paths: Sequence[Path],
) -> dict[str, str]:
    upstream_treatment = manifest.get("upstream_treatment")
    upstream_identity = manifest.get("upstream_identity")
    upstream_speech = manifest.get("upstream_speech")
    if (
        not isinstance(upstream_treatment, Mapping)
        or not isinstance(upstream_identity, Mapping)
        or not isinstance(upstream_speech, Mapping)
    ):
        raise ValueError("Core manifest has incomplete upstream evidence")
    treatment_path = _path_from_record(
        upstream_treatment.get("manifest_path"),
        upstream_treatment.get("manifest_path_kind"),
        label="treatment manifest",
    )
    identity_path = _path_from_record(
        upstream_identity.get("manifest_path"),
        upstream_identity.get("manifest_path_kind"),
        label="identity manifest",
    )
    speech_path = _path_from_record(
        upstream_speech.get("manifest_path"),
        upstream_speech.get("manifest_path_kind"),
        label="core speech manifest",
    )
    paths = [
        manifest_path.parent / "README.md",
        manifest_path.parent / "index.html",
        manifest_path.parent / ".ready",
        treatment_path,
        identity_path,
        speech_path,
        *audio_paths,
    ]
    return {str(path.resolve(strict=True)): _sha256(path) for path in paths}


def write_core_listening_receipt_from_decision(
    *,
    manifest_path: Path,
    decision_path: Path,
    reviewed_at: str | None = None,
) -> Path:
    """Record one digest-bound human core decision under an exclusive lock."""
    lock_path = manifest_path.parent.parent / f".{manifest_path.parent.name}.core-listening-receipt.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Core listening receipt is already being decided") from None
    try:
        original_bytes = manifest_path.read_bytes()
        manifest, audio_paths = validate_core_cadence_gate(manifest_path)
        original_evidence = _evidence_snapshot(manifest_path, manifest, audio_paths)
        current = manifest.get("listening_receipt")
        if not isinstance(current, Mapping) or current.get("status") != "pending":
            raise ValueError("Core listening receipt has already been decided")
        try:
            raw_decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Core decision is not readable JSON: {decision_path}") from exc
        if not isinstance(raw_decision, Mapping) or set(raw_decision) != DECISION_FIELDS:
            raise ValueError("Core decision has an invalid field set")
        pack_digest = str(manifest["pack_digest"])
        if raw_decision.get("pack_digest") != pack_digest:
            raise ValueError("Core decision pack_digest does not match the current board")
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": raw_decision.get("status"),
            "pack_digest": pack_digest,
            "target": TARGET,
            "surfaces": list(SURFACES),
            "prompt": LISTENING_PROMPT,
            "mac": raw_decision.get("mac"),
            "small_speaker": raw_decision.get("small_speaker"),
            "sonos_arc": raw_decision.get("sonos_arc"),
            "rationale": raw_decision.get("rationale"),
            "reviewed_at": reviewed_at or datetime.now(UTC).isoformat(),
        }
        _validate_receipt(receipt, pack_digest=pack_digest)
        current_manifest, current_audio_paths = validate_core_cadence_gate(manifest_path)
        if manifest_path.read_bytes() != original_bytes:
            raise ValueError("Core manifest changed while its decision was being recorded")
        if _evidence_snapshot(manifest_path, current_manifest, current_audio_paths) != original_evidence:
            raise ValueError("Core board changed while its decision was being recorded")
        manifest["listening_receipt"] = receipt
        temporary_path = manifest_path.parent.parent / (
            f".{manifest_path.parent.name}.{manifest_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, manifest_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        try:
            decided_manifest, decided_audio_paths = validate_core_cadence_gate(manifest_path)
            if _evidence_snapshot(manifest_path, decided_manifest, decided_audio_paths) != original_evidence:
                raise ValueError("Core board changed while its decision was being committed")
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            rollback_path = manifest_path.parent.parent / (
                f".{manifest_path.parent.name}.{manifest_path.name}.{uuid4().hex}.rollback"
            )
            try:
                rollback_path.write_bytes(original_bytes)
                os.replace(rollback_path, manifest_path)
            finally:
                rollback_path.unlink(missing_ok=True)
            raise
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
    return manifest_path


def assert_core_listening_gate(manifest_path: Path) -> str:
    """Return the approved pack digest or fail before complete-pack generation."""
    manifest, _audio_paths = validate_core_cadence_gate(manifest_path)
    receipt = manifest.get("listening_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("status") != "approved":
        raise ValueError("Core listening gate is not approved for this pack digest")
    return str(manifest["pack_digest"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--treatment-manifest", type=Path, required=True)
    parser.add_argument("--speech-manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--generated-at")
    args = parser.parse_args(argv)
    stamp = args.generated_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = args.output_root or DEFAULT_OUTPUT_ROOT / f"core-cadence-gate-{stamp}"
    try:
        manifest = render_core_cadence_gate(
            args.treatment_manifest,
            args.speech_manifest,
            output_root,
            generated_at=stamp,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Core cadence gate: {output_root}")
    print(f"Manifest: {output_root / 'manifest.json'}")
    print(f"Listening page: {output_root / 'index.html'}")
    print(f"Handoff: {output_root / 'README.md'}")
    print(f"Pack digest: {manifest['pack_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
