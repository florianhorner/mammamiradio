#!/usr/bin/env python3
"""Freeze fresh, approved-voice speech sources for the core cadence gate.

This is a provider-facing build step, not a runtime TTS path. It binds Isabella,
Marco, and Giulia to an approved voice-identity manifest, resolves the promo
voice through the current runtime selector, and calls only their exact direct
Azure or ElevenLabs routes. Generic TTS fallback is deliberately unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from mammamiradio.audio import tts as tts_module
from mammamiradio.core.config import load_config
from mammamiradio.scheduling.producer import _safe_ad_promo_voice

try:
    _identity_audition = importlib.import_module("scripts.audition_tts_voices")
except ModuleNotFoundError as exc:  # Direct ``python scripts/...`` invocation.
    if exc.name != "scripts":
        raise
    _identity_audition = importlib.import_module("audition_tts_voices")
approved_identity_clips_by_id = _identity_audition.approved_identity_clips_by_id

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONFIG_PATH = REPO_ROOT / "radio.toml"
RUNTIME_PROMO_SELECTOR = "mammamiradio.scheduling.producer._safe_ad_promo_voice"
ELEVENLABS_V2_MODEL = "eleven_multilingual_v2"
MAX_FUTURE_SKEW_SECONDS = 300
SCHEMA_VERSION = 1
STAGE = "core-cadence-speech-freeze"
FORMAT = {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}
LOCAL_POSTPROCESS = "direct provider helper normalization (loudnorm=True)"
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "generated_at",
        "release_ready",
        "upstream_identity",
        "runtime_promo",
        "provider_contract",
        "clips",
        "pack_digest",
    }
)
UPSTREAM_FIELDS = frozenset({"manifest_path", "manifest_path_kind", "pack_digest", "voice_audio_sha256"})
RUNTIME_PROMO_FIELDS = frozenset(
    {
        "config_path",
        "config_path_kind",
        "config_sha256",
        "selector",
        "selected_voice_name",
        "route",
    }
)
CLIP_FIELDS = frozenset(
    {
        "id",
        "character",
        "purpose",
        "text",
        "text_sha256",
        "source_kind",
        "source_voice_id",
        "source_voice_audio_sha256",
        "provider",
        "voice",
        "model",
        "settings",
        "prosody",
        "path",
        "audio_sha256",
        "duration_sec",
        "format",
        "local_postprocess",
    }
)
PROSODY_FIELDS = frozenset({"requested_rate", "requested_pitch", "applied", "reason"})


@dataclass(frozen=True)
class SpeechSpec:
    """One freshly rendered production-voice source."""

    id: str
    filename: str
    source_voice_id: str | None
    character: str
    purpose: str
    text: str
    runtime_rate: str | None = None
    runtime_pitch: str | None = None


SPEECH_SPECS: tuple[SpeechSpec, ...] = (
    SpeechSpec(
        id="speech.sweeper",
        filename="01-isabella-sweeper.mp3",
        source_voice_id="identity-isabella",
        character="Isabella",
        purpose="Configured short station sweeper for the core cadence preview.",
        text="Sei su Mamma Mi Radio.",
    ),
    SpeechSpec(
        id="speech.promo",
        filename="02-runtime-promo.mp3",
        source_voice_id=None,
        character="Runtime promo voice",
        purpose="Normal-mode break-level sponsor tag using the runtime promo prosody request.",
        text="A word from our sponsors, amici.",
        runtime_rate="+40%",
        runtime_pitch="-10Hz",
    ),
    SpeechSpec(
        id="speech.spot-a",
        filename="03-marco-spot-a.mp3",
        source_voice_id="identity-marco",
        character="Marco",
        purpose="Fictional first spot for a representative two-spot break.",
        text="Caffè Aurora: il gusto caldo che accompagna la notte. Disponibile vicino a te.",
    ),
    SpeechSpec(
        id="speech.spot-b",
        filename="04-giulia-spot-b.mp3",
        source_voice_id="identity-giulia",
        character="Giulia",
        purpose="Fictional second spot for the with-mid-bumper cadence branch.",
        text=("Luce Notturna: una lampada discreta per le ore più tranquille. Accendila con calma."),
    ),
)

REQUIRED_CLIP_IDS = tuple(spec.id for spec in SPEECH_SPECS)
REQUIRED_SOURCE_IDS = frozenset(spec.source_voice_id for spec in SPEECH_SPECS if spec.source_voice_id is not None)
EXPECTED_PROVIDERS = {
    "identity-isabella": "azure",
    "identity-marco": "elevenlabs",
    "identity-giulia": "elevenlabs",
}


@dataclass(frozen=True)
class ApprovedIdentity:
    """Immutable material resolved from one approved identity board."""

    manifest_path: Path
    pack_digest: str
    routes: Mapping[str, Mapping[str, object]]
    audio_sha256: Mapping[str, str]


@dataclass(frozen=True)
class RuntimePromo:
    """Exact runtime-selected break-level promo voice and config evidence."""

    config_path: Path
    config_sha256: str
    selector: str
    selected_voice_name: str
    route: Mapping[str, object]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_copy(value: object) -> Any:
    """Return a JSON-safe deep copy of validated manifest material."""
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_copy(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"Unsupported JSON provenance value: {type(value).__name__}")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], field: str) -> None:
    if set(value) != expected:
        detail = ", ".join(sorted(expected.symmetric_difference(value)))
        raise ValueError(f"{field} has an invalid field set: {detail}")


def _stamp(value: str | None, *, now: datetime | None = None) -> str:
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("timestamp reference must include a timezone")
    reference = reference.astimezone(UTC)
    stamp = value or reference.strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("generated_at must use YYYYMMDDTHHMMSSZ format")
    parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if parsed > reference + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        raise ValueError(f"generated_at must not be more than {MAX_FUTURE_SKEW_SECONDS} seconds in the future")
    return stamp


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
        repository = REPO_ROOT.resolve()
        if resolved != repository and repository not in resolved.parents:
            raise ValueError(f"{label} escapes the repository")
        return resolved
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError(f"{label} absolute path is not absolute")
    return candidate.resolve(strict=True)


def _require_safe_output_dir(output_dir: Path) -> None:
    """Keep provider output away from packaged assets and runtime state."""
    resolved = output_dir.resolve(strict=False)
    allowed_roots = ((REPO_ROOT / "tmp").resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved != root and root in resolved.parents for root in allowed_roots):
        raise ValueError("Speech freezes may be written only below workspace tmp or system temp")


def _effective_route(metadata: Mapping[str, object], source_voice_id: str) -> dict[str, object]:
    route = _mapping(metadata.get("route"), f"{source_voice_id}.route")
    if route.get("fallback_used") is not False:
        raise ValueError(f"{source_voice_id} was not approved on a no-fallback route")
    requested = _mapping(route.get("requested"), f"{source_voice_id}.route.requested")
    effective = _mapping(route.get("effective"), f"{source_voice_id}.route.effective")
    if requested != effective:
        raise ValueError(f"{source_voice_id} requested and effective routes differ")
    if set(effective) != {"provider", "voice", "model", "settings"}:
        raise ValueError(f"{source_voice_id} effective route has an invalid field set")

    provider = effective.get("provider")
    voice = effective.get("voice")
    model = effective.get("model")
    settings = effective.get("settings")
    if provider != EXPECTED_PROVIDERS[source_voice_id]:
        raise ValueError(f"{source_voice_id} no longer uses its approved provider")
    if not isinstance(voice, str) or not voice or not isinstance(model, str) or not model:
        raise ValueError(f"{source_voice_id} effective route has no voice or model")
    if not isinstance(settings, Mapping):
        raise ValueError(f"{source_voice_id} effective route settings must be an object")
    if provider == "azure":
        if model != "azure_speech":
            raise ValueError("Isabella no longer uses the approved Azure model route")
        if not isinstance(settings.get("rate"), str) or not isinstance(settings.get("pitch"), str):
            raise ValueError("Isabella's approved Azure route has no exact rate or pitch")
        if settings.get("provider_output_format") != "audio-24khz-160kbitrate-mono-mp3":
            raise ValueError("Isabella's approved Azure provider format changed")
    return _json_copy(effective)


def _resolve_approved_identity(identity_manifest: Path) -> ApprovedIdentity:
    if identity_manifest.is_symlink():
        raise ValueError("Identity manifest must not be a symlink")
    identity_manifest = identity_manifest.resolve(strict=True)
    pack_digest, paths_by_id, metadata_by_id = approved_identity_clips_by_id(identity_manifest)
    if not isinstance(pack_digest, str) or not SHA256_RE.fullmatch(pack_digest):
        raise ValueError("Approved identity board has no valid pack digest")
    if set(paths_by_id) != REQUIRED_SOURCE_IDS or set(metadata_by_id) != REQUIRED_SOURCE_IDS:
        raise ValueError("Speech freeze requires exactly the approved Isabella, Marco, and Giulia voices")

    routes: dict[str, Mapping[str, object]] = {}
    audio_sha256: dict[str, str] = {}
    for source_voice_id in sorted(REQUIRED_SOURCE_IDS):
        path = paths_by_id[source_voice_id]
        metadata = _mapping(metadata_by_id[source_voice_id], source_voice_id)
        if metadata.get("id") != source_voice_id:
            raise ValueError(f"Approved identity metadata is misbound for {source_voice_id}")
        digest = metadata.get("audio_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValueError(f"Approved identity voice has no valid hash: {source_voice_id}")
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise ValueError(f"Approved identity voice changed after listening: {source_voice_id}")
        routes[source_voice_id] = _effective_route(metadata, source_voice_id)
        audio_sha256[source_voice_id] = digest
    return ApprovedIdentity(identity_manifest, pack_digest, routes, audio_sha256)


def _resolve_runtime_promo() -> RuntimePromo:
    """Resolve the same safe house voice selected by the running producer."""
    if RUNTIME_CONFIG_PATH.is_symlink():
        raise ValueError("Runtime radio config must not be a symlink")
    config_path = RUNTIME_CONFIG_PATH.resolve(strict=True)
    config = load_config(str(config_path))
    selected = _safe_ad_promo_voice(config.ads.voices)
    if selected is None:
        raise ValueError("Runtime config has no safe ad-promo voice")

    provider = (selected.engine or "edge").strip().lower()
    if provider != "elevenlabs":
        raise ValueError("Runtime ad-promo voice must use the direct ElevenLabs route")
    voice = selected.voice.strip()
    selected_voice_name = selected.name.strip()
    if not voice or not selected_voice_name:
        raise ValueError("Runtime ad-promo selection has no voice name or provider voice ID")
    settings = selected.voice_settings
    if not isinstance(settings, Mapping):
        raise ValueError("Runtime ad-promo voice settings must be an object")
    effective_settings = tts_module._resolve_elevenlabs_v2_voice_settings(dict(settings))
    route = {
        "provider": provider,
        "voice": voice,
        "model": ELEVENLABS_V2_MODEL,
        # Record the exact provider payload, including house defaults that the
        # runtime helper resolves when radio.toml deliberately leaves its
        # per-voice override table empty.
        "settings": _json_copy(effective_settings),
    }
    return RuntimePromo(
        config_path=config_path,
        config_sha256=_sha256(config_path),
        selector=RUNTIME_PROMO_SELECTOR,
        selected_voice_name=selected_voice_name,
        route=route,
    )


def _runtime_promo_record(promo: RuntimePromo) -> dict[str, object]:
    config_path, config_path_kind = _path_record(promo.config_path)
    return {
        "config_path": config_path,
        "config_path_kind": config_path_kind,
        "config_sha256": promo.config_sha256,
        "selector": promo.selector,
        "selected_voice_name": promo.selected_voice_name,
        "route": _json_copy(promo.route),
    }


def _route_for_spec(
    spec: SpeechSpec,
    approved: ApprovedIdentity,
    promo: RuntimePromo,
) -> Mapping[str, object]:
    if spec.id == "speech.promo":
        return promo.route
    if spec.source_voice_id is None:  # pragma: no cover - static inventory invariant
        raise ValueError(f"Speech clip {spec.id} has no approved identity source")
    return approved.routes[spec.source_voice_id]


def _character_for_spec(spec: SpeechSpec, promo: RuntimePromo) -> str:
    return promo.selected_voice_name if spec.id == "speech.promo" else spec.character


def _source_provenance(
    spec: SpeechSpec,
    approved: ApprovedIdentity,
) -> tuple[str, str | None, str | None]:
    if spec.id == "speech.promo":
        return "runtime-promo-selection", None, None
    if spec.source_voice_id is None:  # pragma: no cover - static inventory invariant
        raise ValueError(f"Speech clip {spec.id} has no approved identity source")
    return (
        "approved-identity",
        spec.source_voice_id,
        approved.audio_sha256[spec.source_voice_id],
    )


def _required_credentials(routes: Mapping[str, Mapping[str, object]]) -> tuple[str, ...]:
    required: set[str] = set()
    for route in routes.values():
        if route["provider"] == "azure":
            required.update(("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"))
        elif route["provider"] == "elevenlabs":
            required.add("ELEVENLABS_API_KEY")
    return tuple(sorted(required))


def _preflight_credentials(routes: Mapping[str, Mapping[str, object]]) -> None:
    missing = [name for name in _required_credentials(routes) if not os.environ.get(name)]
    if missing:
        raise ValueError(f"Speech freeze is missing provider credentials: {', '.join(missing)}")


def _supports_explicit_rate_pitch(helper: Callable[..., object]) -> bool:
    try:
        parameters = inspect.signature(helper).parameters
    except (TypeError, ValueError):
        return False
    return "rate" in parameters and "pitch" in parameters


def _prosody(spec: SpeechSpec, route: Mapping[str, object]) -> dict[str, object]:
    provider = route["provider"]
    settings = _mapping(route["settings"], f"{spec.id}.settings")
    if provider == "azure":
        return {
            "requested_rate": settings["rate"],
            "requested_pitch": settings["pitch"],
            "applied": True,
            "reason": "direct Azure helper supports explicit rate and pitch",
        }
    if spec.runtime_rate is None and spec.runtime_pitch is None:
        return {
            "requested_rate": None,
            "requested_pitch": None,
            "applied": False,
            "reason": "no additional runtime prosody requested",
        }
    supported = _supports_explicit_rate_pitch(tts_module.synthesize_elevenlabs)
    return {
        "requested_rate": spec.runtime_rate,
        "requested_pitch": spec.runtime_pitch,
        "applied": supported,
        "reason": (
            "direct ElevenLabs helper supports explicit rate and pitch"
            if supported
            else "direct ElevenLabs helper exposes no rate or pitch transform; no transform applied"
        ),
    }


def _provider_contract() -> dict[str, object]:
    return {
        "direct_helpers": {
            "azure": "mammamiradio.audio.tts.synthesize_azure",
            "elevenlabs": "mammamiradio.audio.tts.synthesize_elevenlabs",
        },
        "fallback_permitted": False,
        "generic_synthesize_permitted": False,
        "credentials_recorded": False,
    }


async def _render_provider_clip(
    spec: SpeechSpec,
    route: Mapping[str, object],
    output_path: Path,
    *,
    character: str,
) -> Path:
    provider = route["provider"]
    voice = str(route["voice"])
    model = str(route["model"])
    settings = _mapping(route["settings"], f"{spec.id}.settings")
    if provider == "azure":
        rendered = await tts_module.synthesize_azure(
            spec.text,
            voice,
            output_path,
            rate=str(settings["rate"]),
            pitch=str(settings["pitch"]),
            loudnorm=True,
        )
    elif provider == "elevenlabs":
        kwargs: dict[str, object] = {
            "loudnorm": True,
            "voice_settings": _json_copy(settings),
            "elevenlabs_model": model,
            "delivery_cue": "neutral",
            "delivery_profile": "none",
            "host_name": character,
        }
        prosody = _prosody(spec, route)
        if prosody["applied"]:
            kwargs["rate"] = spec.runtime_rate
            kwargs["pitch"] = spec.runtime_pitch
        helper = cast(Callable[..., Awaitable[Path]], tts_module.synthesize_elevenlabs)
        rendered = await helper(
            spec.text,
            voice,
            output_path,
            **kwargs,
        )
    else:  # pragma: no cover - route validation owns this invariant
        raise ValueError(f"Unsupported direct speech-freeze provider: {provider}")
    if rendered != output_path or output_path.is_symlink() or not output_path.is_file():
        raise RuntimeError(f"Direct provider helper returned an unexpected path for {spec.id}")
    return output_path


def _probe_audio(path: Path) -> tuple[dict[str, object], float]:
    """Require exact station-format evidence from ffprobe."""
    try:
        completed = subprocess.run(
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
                str(path.resolve()),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffprobe could not inspect {path.name}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown ffprobe error"
        raise RuntimeError(f"ffprobe rejected {path.name}: {detail}")
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
        format_payload = payload["format"]
        bitrate_bps = int(stream.get("bit_rate") or format_payload["bit_rate"])
        audio_format = {
            "codec": str(stream["codec_name"]),
            "sample_rate_hz": int(stream["sample_rate"]),
            "channels": int(stream["channels"]),
            "bitrate_kbps": bitrate_bps // 1000,
        }
        duration_sec = float(format_payload["duration"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe returned incomplete audio evidence for {path.name}") from exc
    if audio_format != FORMAT or bitrate_bps != 192_000:
        raise RuntimeError(f"Speech clip {path.name} has format {audio_format}, expected {FORMAT}")
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise RuntimeError(f"Speech clip {path.name} has no positive finite duration")
    return audio_format, round(duration_sec, 6)


def _pack_digest(manifest: Mapping[str, object]) -> str:
    material = {key: value for key, value in manifest.items() if key != "pack_digest"}
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clip_record(
    spec: SpeechSpec,
    route: Mapping[str, object],
    approved: ApprovedIdentity,
    character: str,
    output_path: Path,
) -> dict[str, object]:
    audio_format, duration_sec = _probe_audio(output_path)
    source_kind, source_voice_id, source_voice_audio_sha256 = _source_provenance(spec, approved)
    return {
        "id": spec.id,
        "character": character,
        "purpose": spec.purpose,
        "text": spec.text,
        "text_sha256": _text_sha256(spec.text),
        "source_kind": source_kind,
        "source_voice_id": source_voice_id,
        "source_voice_audio_sha256": source_voice_audio_sha256,
        "provider": route["provider"],
        "voice": route["voice"],
        "model": route["model"],
        "settings": _json_copy(route["settings"]),
        "prosody": _prosody(spec, route),
        "path": spec.filename,
        "audio_sha256": _sha256(output_path),
        "duration_sec": duration_sec,
        "format": audio_format,
        "local_postprocess": LOCAL_POSTPROCESS,
    }


def _build_manifest(
    approved: ApprovedIdentity,
    promo: RuntimePromo,
    clips: Sequence[Mapping[str, object]],
    *,
    generated_at: str,
) -> dict[str, object]:
    generated_at = _stamp(generated_at)
    identity_path, identity_path_kind = _path_record(approved.manifest_path)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "generated_at": generated_at,
        "release_ready": False,
        "upstream_identity": {
            "manifest_path": identity_path,
            "manifest_path_kind": identity_path_kind,
            "pack_digest": approved.pack_digest,
            "voice_audio_sha256": dict(approved.audio_sha256),
        },
        "runtime_promo": _runtime_promo_record(promo),
        "provider_contract": _provider_contract(),
        "clips": [_json_copy(clip) for clip in clips],
    }
    manifest["pack_digest"] = _pack_digest(manifest)
    return manifest


def validate_core_cadence_speech_manifest(
    manifest_path: Path,
    *,
    require_ready: bool = True,
) -> tuple[dict[str, object], dict[str, Path]]:
    """Revalidate a frozen source directory and its approved voice lineage."""
    if manifest_path.is_symlink():
        raise ValueError("Speech-freeze manifest must not be a symlink")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Speech-freeze manifest is not readable JSON: {manifest_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Speech-freeze manifest must be an object")
    manifest: dict[str, object] = value
    _exact_fields(manifest, MANIFEST_FIELDS, "speech-freeze manifest")
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("stage") != STAGE:
        raise ValueError("Speech-freeze manifest has an unsupported schema or stage")
    if manifest.get("release_ready") is not False:
        raise ValueError("Speech-freeze sources must not be marked release-ready")
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("Speech-freeze manifest generated_at must be a timestamp")
    _stamp(generated_at)
    if manifest.get("provider_contract") != _provider_contract():
        raise ValueError("Speech-freeze provider contract was altered")

    upstream = _mapping(manifest.get("upstream_identity"), "upstream_identity")
    _exact_fields(upstream, UPSTREAM_FIELDS, "upstream_identity")
    identity_manifest = _path_from_record(
        upstream.get("manifest_path"),
        upstream.get("manifest_path_kind"),
        label="upstream identity manifest",
    )
    approved = _resolve_approved_identity(identity_manifest)
    if identity_manifest != approved.manifest_path:
        raise ValueError("Speech-freeze identity path no longer resolves to the approved manifest")
    if upstream.get("pack_digest") != approved.pack_digest:
        raise ValueError("Speech-freeze upstream identity digest is stale")
    if upstream.get("voice_audio_sha256") != dict(approved.audio_sha256):
        raise ValueError("Speech-freeze upstream voice hashes are stale")

    runtime_promo = _mapping(manifest.get("runtime_promo"), "runtime_promo")
    _exact_fields(runtime_promo, RUNTIME_PROMO_FIELDS, "runtime_promo")
    config_path = _path_from_record(
        runtime_promo.get("config_path"),
        runtime_promo.get("config_path_kind"),
        label="runtime promo config",
    )
    current_promo = _resolve_runtime_promo()
    if config_path != current_promo.config_path:
        raise ValueError("Speech-freeze runtime promo config path is stale")
    recorded_config_sha256 = runtime_promo.get("config_sha256")
    if not isinstance(recorded_config_sha256, str) or not SHA256_RE.fullmatch(recorded_config_sha256):
        raise ValueError("Speech-freeze runtime promo config hash is invalid")
    # The recorded full-file hash is immutable render provenance and remains
    # inside the frozen pack digest. Currentness is semantic: re-resolve the
    # exact runtime selector, selected voice, provider route, model, and
    # settings below so unrelated config sections may evolve independently.
    if runtime_promo.get("selector") != current_promo.selector:
        raise ValueError("Speech-freeze runtime promo selector is stale")
    if runtime_promo.get("selected_voice_name") != current_promo.selected_voice_name:
        raise ValueError("Speech-freeze runtime promo selection is stale")
    if runtime_promo.get("route") != _json_copy(current_promo.route):
        raise ValueError("Speech-freeze runtime promo route is stale")

    raw_clips = manifest.get("clips")
    if not isinstance(raw_clips, list) or len(raw_clips) != len(SPEECH_SPECS):
        raise ValueError("Speech-freeze manifest must contain exactly four clips")
    paths_by_id: dict[str, Path] = {}
    seen_paths: set[str] = set()
    seen_audio_hashes: set[str] = set()
    for index, (raw_clip, spec) in enumerate(zip(raw_clips, SPEECH_SPECS, strict=True)):
        clip = _mapping(raw_clip, f"clips[{index}]")
        _exact_fields(clip, CLIP_FIELDS, f"clips[{index}]")
        route = _route_for_spec(spec, approved, current_promo)
        source_kind, source_voice_id, source_voice_audio_sha256 = _source_provenance(spec, approved)
        expected_static = {
            "id": spec.id,
            "character": _character_for_spec(spec, current_promo),
            "purpose": spec.purpose,
            "text": spec.text,
            "text_sha256": _text_sha256(spec.text),
            "source_kind": source_kind,
            "source_voice_id": source_voice_id,
            "source_voice_audio_sha256": source_voice_audio_sha256,
            "provider": route["provider"],
            "voice": route["voice"],
            "model": route["model"],
            "settings": _json_copy(route["settings"]),
            "prosody": _prosody(spec, route),
            "local_postprocess": LOCAL_POSTPROCESS,
        }
        for field, expected in expected_static.items():
            if clip.get(field) != expected:
                raise ValueError(f"Speech clip {spec.id} has stale {field} evidence")
        prosody = _mapping(clip.get("prosody"), f"{spec.id}.prosody")
        _exact_fields(prosody, PROSODY_FIELDS, f"{spec.id}.prosody")

        path_value = clip.get("path")
        if (
            not isinstance(path_value, str)
            or path_value != spec.filename
            or Path(path_value).name != path_value
            or not path_value.endswith(".mp3")
            or path_value in seen_paths
        ):
            raise ValueError(f"Speech clip {spec.id} has an unsafe or duplicate path")
        seen_paths.add(path_value)
        clip_path = manifest_path.parent / path_value
        if clip_path.is_symlink() or not clip_path.is_file():
            raise ValueError(f"Speech clip is missing: {path_value}")
        audio_sha256 = _sha256(clip_path)
        if clip.get("audio_sha256") != audio_sha256 or not SHA256_RE.fullmatch(audio_sha256):
            raise ValueError(f"Speech clip {spec.id} audio hash differs from the manifest")
        if audio_sha256 in seen_audio_hashes:
            raise ValueError("Speech-freeze clips must not contain exact duplicate audio")
        if audio_sha256 in approved.audio_sha256.values():
            raise ValueError("Speech-freeze output reused an old identity audition recording")
        seen_audio_hashes.add(audio_sha256)

        audio_format, duration_sec = _probe_audio(clip_path)
        if clip.get("format") != audio_format or clip.get("duration_sec") != duration_sec:
            raise ValueError(f"Speech clip {spec.id} format or duration evidence is stale")
        paths_by_id[spec.id] = clip_path

    pack_digest = manifest.get("pack_digest")
    if not isinstance(pack_digest, str) or not SHA256_RE.fullmatch(pack_digest):
        raise ValueError("Speech-freeze manifest has no valid pack digest")
    if pack_digest != _pack_digest(manifest):
        raise ValueError("Speech-freeze pack digest is stale or invalid")

    ready_path = manifest_path.parent / ".ready"
    expected_files = {"manifest.json", ".ready", *(spec.filename for spec in SPEECH_SPECS)}
    actual_files = {
        path.relative_to(manifest_path.parent).as_posix()
        for path in manifest_path.parent.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != expected_files:
        raise ValueError("Speech-freeze directory inventory differs from the manifest")
    if require_ready and (
        ready_path.is_symlink()
        or not ready_path.is_file()
        or ready_path.read_text(encoding="ascii").strip() != pack_digest
    ):
        raise ValueError("Speech-freeze ready marker does not match pack_digest")
    return manifest, paths_by_id


async def freeze_core_cadence_speech(
    identity_manifest: Path,
    output_dir: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Render and atomically publish four no-fallback core speech sources."""
    _require_safe_output_dir(output_dir)
    stamp = _stamp(generated_at)
    approved = _resolve_approved_identity(identity_manifest)
    promo = _resolve_runtime_promo()
    credential_routes = {**approved.routes, "runtime-promo": promo.route}
    _preflight_credentials(credential_routes)
    if os.path.lexists(output_dir):
        raise FileExistsError(f"Refusing to overwrite speech freeze: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.parent.is_symlink():
        raise ValueError("Speech-freeze output parent must not be a symlink")
    claim_path = output_dir.parent / f".{output_dir.name}.claim"
    try:
        claim_fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise FileExistsError(f"Speech freeze is already being rendered: {output_dir}") from None
    os.close(claim_fd)

    staging_dir: Path | None = None
    try:
        staging_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        clips: list[dict[str, object]] = []
        for spec in SPEECH_SPECS:
            route = _route_for_spec(spec, approved, promo)
            character = _character_for_spec(spec, promo)
            output_path = staging_dir / spec.filename
            await _render_provider_clip(spec, route, output_path, character=character)
            clips.append(
                _clip_record(
                    spec,
                    route,
                    approved,
                    character,
                    output_path,
                )
            )
        manifest = _build_manifest(approved, promo, clips, generated_at=stamp)
        manifest_path = staging_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging_dir / ".ready").write_text(f"{manifest['pack_digest']}\n", encoding="ascii")
        validate_core_cadence_speech_manifest(manifest_path)
        if os.path.lexists(output_dir):
            raise FileExistsError(f"Speech-freeze output appeared during render: {output_dir}")
        os.replace(staging_dir, output_dir)
        return manifest
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        claim_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest = asyncio.run(
            freeze_core_cadence_speech(
                args.identity_manifest,
                args.output_dir,
                generated_at=args.generated_at,
            )
        )
    except (FileExistsError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(args.output_dir / "manifest.json")
    print(f"pack_digest={manifest['pack_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
