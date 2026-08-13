#!/usr/bin/env python3
"""Generate local audition clips for configured and catalog TTS voices.

The station runtime deliberately falls cloud TTS back to Edge when credentials
are missing or a provider fails. This script is stricter: it skips providers
without credentials and records provider failures in a manifest so voice tests
show what actually worked.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mammamiradio.audio import tts as tts_module
from mammamiradio.audio.normalizer import probe_duration_sec
from mammamiradio.audio.tts import (
    _openai_instructions_for_ad_voice,
    _openai_instructions_for_host,
    _prosody_for_host,
    configure_openai_tts_model,
)
from mammamiradio.audio.voice_catalog import AZURE_ITALIAN_VOICES, EDGE_ITALIAN_VOICES, OPENAI_VOICES
from mammamiradio.core.config import StationConfig, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "radio.toml"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "voice-auditions"
SELECTION_RECEIPT_PATH = REPO_ROOT / "proof" / "2026-07-13-voice-diversity-selection.json"
SELECTION_RECEIPT_SCHEMA_VERSION = 1
HOST_PERFORMANCE_RECEIPT_PATH = REPO_ROOT / "proof" / "2026-07-16-v3-host-performance.json"
HOST_PERFORMANCE_RECEIPT_SCHEMA_VERSION = 1
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANDIDATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

PROVIDERS = ("edge", "openai", "azure", "elevenlabs")
PROVIDER_ALIASES = {
    "all": "all",
    "edge": "edge",
    "edge-tts": "edge",
    "edge_tts": "edge",
    "openai": "openai",
    "openai-tts": "openai",
    "openai_tts": "openai",
    "azure": "azure",
    "azure-tts": "azure",
    "azure_tts": "azure",
    "elevenlabs": "elevenlabs",
    "eleven-labs": "elevenlabs",
    "eleven_labs": "elevenlabs",
}

STATUS_PLANNED = "planned"
STATUS_GENERATED = "generated"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

DEFAULT_SAMPLE_TEXT = (
    "Mamma Mi Radio, prova microfono. Questa e una voce italiana per annunci, "
    "sweepers e personaggi in onda. Dimmi se ha carattere, calore e presenza."
)
DEFAULT_V3_HOST_PERFORMANCE_TEXT = "La prossima canzone arriva proprio quando serve: non fate domande, fate spazio."

ELEVENLABS_V2_MODEL = "eleven_multilingual_v2"
ELEVENLABS_V3_MODEL = "eleven_v3"
NEUTRAL_DELIVERY_CUE = "neutral"
V3_DELIVERY_CUES_BY_PROFILE: dict[str, tuple[str, ...]] = {
    "marco": ("energetic", "curious", "playful"),
    "giulia": ("dry", "curious", "playful"),
}


@dataclass
class VoiceAuditionTarget:
    provider: str
    voice: str
    label: str
    source: str
    used_by: tuple[str, ...] = field(default_factory=tuple)
    text: str = DEFAULT_SAMPLE_TEXT
    edge_fallback_voice: str = ""
    rate: str | None = None
    pitch: str | None = None
    openai_instructions: str = ""
    voice_settings: dict | None = None
    elevenlabs_model: str = ELEVENLABS_V2_MODEL
    delivery_profile: str = "none"
    delivery_cue: str = NEUTRAL_DELIVERY_CUE


@dataclass
class VoiceAuditionResult:
    provider: str
    voice: str
    label: str
    source: str
    used_by: tuple[str, ...]
    status: str
    output_path: str = ""
    missing_env: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""
    voice_settings: dict | None = None
    # Safe evidence retained in the ignored local manifest.  It is sufficient
    # for the later receipt writer without preserving raw copy or audio there.
    text_sha256: str = ""
    profile: dict | None = None
    audio_sha256: str | None = None
    audio_duration_seconds: float | None = None
    # V3 performance receipts distinguish canonical spoken text from the
    # provider-only rendered payload. Keep the historic text_sha256 field for
    # the existing V2 selection receipt contract.
    clean_text_sha256: str = ""
    rendered_text_sha256: str = ""
    elevenlabs_model: str = ELEVENLABS_V2_MODEL
    delivery_profile: str = "none"
    delivery_cue: str = NEUTRAL_DELIVERY_CUE


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rendered_text_for_target(target: VoiceAuditionTarget) -> str:
    """Return the provider payload text without contaminating canonical copy.

    Production owns the actual V3 rendering at the TTS request boundary. The
    audition reuses that boundary's resolver only to hash the rendered payload
    without retaining either string. Invalid cue/model combinations are
    rejected instead of being silently rendered as speech.
    """
    if target.provider != "elevenlabs" or target.elevenlabs_model != ELEVENLABS_V3_MODEL:
        return target.text
    if target.delivery_cue == NEUTRAL_DELIVERY_CUE:
        return target.text
    tag, resolved_cue = tts_module._resolve_elevenlabs_v3_delivery_tag(
        target.delivery_cue,
        target.delivery_profile,
    )
    if resolved_cue != target.delivery_cue or not tag:
        raise ValueError(f"delivery cue {target.delivery_cue!r} is not allowed for profile {target.delivery_profile!r}")
    return f"{tag} {target.text}"


def _audition_text_hashes(target: VoiceAuditionTarget) -> tuple[str, str]:
    clean_text_sha256 = _text_sha256(target.text)
    return clean_text_sha256, _text_sha256(_rendered_text_for_target(target))


def _selection_profile_for_target(target: VoiceAuditionTarget) -> dict[str, object]:
    """Record the exact safe profile used for a candidate render.

    The normal ElevenLabs route is V2 and merges its documented house defaults
    with a configured override. Reusing the V2 resolver prevents a receipt from
    claiming a profile different from the audition payload.
    """
    if target.provider == "elevenlabs":
        if target.elevenlabs_model == ELEVENLABS_V2_MODEL:
            voice_settings = tts_module._resolve_elevenlabs_v2_voice_settings(target.voice_settings)
        elif target.elevenlabs_model == ELEVENLABS_V3_MODEL:
            unsupported = set(target.voice_settings or {}) - {"stability"}
            if unsupported:
                raise ValueError(
                    "ElevenLabs V3 auditions only support stability; unsupported settings: "
                    + ", ".join(sorted(unsupported))
                )
            voice_settings = dict(target.voice_settings or {})
        else:
            raise ValueError(f"Unsupported ElevenLabs audition model: {target.elevenlabs_model}")
        return {"engine": target.provider, "model": target.elevenlabs_model, "voice_settings": voice_settings}
    models = {
        "edge": "edge_read_aloud",
        "openai": "openai_tts",
        "azure": "azure_speech",
    }
    return {"engine": target.provider, "model": models[target.provider], "voice_settings": {}}


def _generated_audio_evidence(path: Path) -> tuple[str | None, float | None]:
    """Return receipt-safe audio evidence without retaining a local path."""
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None, None
    duration = probe_duration_sec(path)
    return digest, duration if duration is not None and duration > 0 else None


def _timestamp(value: str | None = None) -> str:
    stamp = value or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDDTHHMMSSZ format")
    return stamp


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug[:96] or "voice"


def _canonical_provider(name: str) -> str:
    key = name.strip().lower()
    return PROVIDER_ALIASES.get(key, key.replace("-", "_"))


def expand_providers(provider_names: list[str] | None) -> list[str]:
    requested = provider_names or ["all"]
    seen: set[str] = set()
    providers: list[str] = []
    for name in requested:
        provider = _canonical_provider(name)
        if provider == "all":
            for known in PROVIDERS:
                if known not in seen:
                    seen.add(known)
                    providers.append(known)
        elif provider in PROVIDERS and provider not in seen:
            seen.add(provider)
            providers.append(provider)
        elif provider not in PROVIDERS:
            allowed = ", ".join(PROVIDERS)
            raise ValueError(f"Unsupported provider '{name}'. Allowed: {allowed}, all")
    return providers


def parse_manual_voice_specs(specs: list[str] | None) -> list[tuple[str, str]]:
    voices: list[tuple[str, str]] = []
    for spec in specs or []:
        if ":" not in spec:
            raise ValueError(f"Manual voice '{spec}' must use provider:voice_id")
        provider_raw, voice = spec.split(":", 1)
        provider = _canonical_provider(provider_raw)
        if provider not in PROVIDERS:
            allowed = ", ".join(PROVIDERS)
            raise ValueError(f"Unsupported manual voice provider '{provider_raw}'. Allowed: {allowed}")
        voice = voice.strip()
        if not voice:
            raise ValueError(f"Manual voice '{spec}' is missing a voice ID")
        voices.append((provider, voice))
    return voices


def required_env_for_provider(provider: str) -> tuple[str, ...]:
    if provider == "openai":
        return ("OPENAI_API_KEY",)
    if provider == "azure":
        return ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")
    if provider == "elevenlabs":
        return ("ELEVENLABS_API_KEY",)
    return ()


def missing_env_for_provider(provider: str, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    # Honor an explicitly-passed env (including an empty mapping) — only fall back
    # to the process environment when no env was supplied. Using `env or os.environ`
    # treated an empty `{}` as "unset" and leaked real credentials into callers that
    # asked for a clean environment (e.g. the strict-mode missing-credentials test).
    env_map = env if env is not None else os.environ
    return tuple(name for name in required_env_for_provider(provider) if not env_map.get(name))


def _target_text(target_name: str, sample_text: str) -> str:
    return f"{target_name}. {sample_text}"


def _merge_sources(existing: str, new: str) -> str:
    parts = []
    for value in (*existing.split("+"), *new.split("+")):
        if value and value not in parts:
            parts.append(value)
    return "+".join(parts)


def _voice_settings_key(voice_settings: Mapping[str, object] | None) -> str:
    """Return a stable render-profile identity for target de-duplication.

    An ElevenLabs voice ID alone is not enough to identify an audition: two
    configured characters may deliberately use the same voice under different
    settings. Keep those renders separate so the local manifest proves the
    profile actually auditioned. Empty settings and ``None`` are equivalent.
    """
    if not voice_settings:
        return "{}"
    return json.dumps(dict(voice_settings), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _target_key(target: VoiceAuditionTarget) -> tuple[str, str, str, str, str, str]:
    """Keep model/cue variants distinct even when they share a voice ID."""
    return (
        target.provider,
        target.voice,
        _voice_settings_key(target.voice_settings),
        target.elevenlabs_model,
        target.delivery_profile,
        target.delivery_cue,
    )


def _add_target(
    targets: dict[tuple[str, str, str, str, str, str], VoiceAuditionTarget], target: VoiceAuditionTarget
) -> None:
    key = _target_key(target)
    existing = targets.get(key)
    if existing is None:
        targets[key] = target
        return

    existing.source = _merge_sources(existing.source, target.source)
    used_by = list(existing.used_by)
    for label in target.used_by:
        if label not in used_by:
            used_by.append(label)
    existing.used_by = tuple(used_by)
    if not existing.edge_fallback_voice:
        existing.edge_fallback_voice = target.edge_fallback_voice
    if not existing.rate:
        existing.rate = target.rate
    if not existing.pitch:
        existing.pitch = target.pitch
    if not existing.openai_instructions:
        existing.openai_instructions = target.openai_instructions


def collect_configured_targets(
    config: StationConfig,
    *,
    sample_text: str = DEFAULT_SAMPLE_TEXT,
) -> list[VoiceAuditionTarget]:
    targets: dict[tuple[str, str, str, str, str, str], VoiceAuditionTarget] = {}

    for host in config.hosts:
        provider = _canonical_provider(host.engine or "edge")
        prosody = _prosody_for_host(host)
        _add_target(
            targets,
            VoiceAuditionTarget(
                provider=provider,
                voice=host.voice,
                label=f"host-{_slug(host.name)}",
                source="configured",
                used_by=(f"host:{host.name}",),
                text=_target_text(f"Host {host.name}", sample_text),
                edge_fallback_voice=host.edge_fallback_voice,
                rate=prosody.get("rate"),
                pitch=prosody.get("pitch"),
                openai_instructions=_openai_instructions_for_host(host),
                elevenlabs_model=getattr(host, "elevenlabs_model", ELEVENLABS_V2_MODEL),
                delivery_profile=getattr(host, "delivery_profile", "none"),
            ),
        )

    sonic = config.sonic_brand
    if sonic.sweeper_voice:
        _add_target(
            targets,
            VoiceAuditionTarget(
                provider=_canonical_provider(sonic.sweeper_engine or "edge"),
                voice=sonic.sweeper_voice,
                label="sonic-brand-sweeper",
                source="configured",
                used_by=("sonic_brand:sweeper",),
                text=_target_text("Sonic brand sweeper", sample_text),
                edge_fallback_voice=sonic.sweeper_edge_fallback_voice,
            ),
        )

    for ad_voice in config.ads.voices:
        provider = _canonical_provider(ad_voice.engine or "edge")
        _add_target(
            targets,
            VoiceAuditionTarget(
                provider=provider,
                voice=ad_voice.voice,
                label=f"ad-{_slug(ad_voice.name)}",
                source="configured",
                used_by=(f"ad:{ad_voice.name}",),
                text=_target_text(f"Commercial voice {ad_voice.name}", sample_text),
                edge_fallback_voice=ad_voice.edge_fallback_voice,
                openai_instructions=_openai_instructions_for_ad_voice(ad_voice),
                # Ad voices now carry the same selected ElevenLabs profile as
                # runtime. ``getattr`` keeps this script compatible while an
                # older config object is being inspected during an upgrade.
                voice_settings=dict(getattr(ad_voice, "voice_settings", {}) or {}) or None,
            ),
        )

    return list(targets.values())


def build_v3_host_performance_targets(
    config: StationConfig,
    *,
    sample_text: str = DEFAULT_V3_HOST_PERFORMANCE_TEXT,
) -> list[VoiceAuditionTarget]:
    """Build the reproducible V2/V3 comparison matrix for Marco and Giulia.

    This is intentionally narrower than normal casting: it renders only
    configured ElevenLabs hosts whose profiles authorize the V3 cue vocabulary.
    Every row for one host uses the same clean text; only model/cue changes.
    """
    targets: list[VoiceAuditionTarget] = []
    for host in config.hosts:
        profile = host.delivery_profile
        if _canonical_provider(host.engine or "edge") != "elevenlabs":
            continue
        if profile not in V3_DELIVERY_CUES_BY_PROFILE:
            continue

        label_prefix = f"host-{_slug(host.name)}"

        def make_target(
            label: str,
            model: str,
            delivery_cue: str,
            *,
            voice: str = host.voice,
            host_name: str = host.name,
            edge_fallback_voice: str = host.edge_fallback_voice,
            voice_settings: Mapping[str, object] | None = None,
            delivery_profile: str = profile,
        ) -> VoiceAuditionTarget:
            return VoiceAuditionTarget(
                provider="elevenlabs",
                voice=voice,
                label=label,
                source="v3-host-performance",
                used_by=(f"host:{host_name}", f"v3_performance:{delivery_profile}"),
                text=sample_text,
                edge_fallback_voice=edge_fallback_voice,
                voice_settings=dict(voice_settings or {}) or None,
                elevenlabs_model=model,
                delivery_profile=delivery_profile,
                delivery_cue=delivery_cue,
            )

        targets.append(
            make_target(
                f"{label_prefix}-v2-clean",
                ELEVENLABS_V2_MODEL,
                NEUTRAL_DELIVERY_CUE,
                voice_settings=host.voice_settings,
            )
        )
        targets.append(
            make_target(
                f"{label_prefix}-v3-clean",
                ELEVENLABS_V3_MODEL,
                NEUTRAL_DELIVERY_CUE,
                voice_settings=host.voice_settings,
            )
        )
        for delivery_cue in V3_DELIVERY_CUES_BY_PROFILE[profile]:
            targets.append(
                make_target(
                    f"{label_prefix}-v3-{delivery_cue}",
                    ELEVENLABS_V3_MODEL,
                    delivery_cue,
                    voice_settings=host.voice_settings,
                )
            )
    return targets


def collect_catalog_targets(*, sample_text: str = DEFAULT_SAMPLE_TEXT) -> list[VoiceAuditionTarget]:
    targets: list[VoiceAuditionTarget] = []
    for voice in sorted(EDGE_ITALIAN_VOICES):
        targets.append(
            VoiceAuditionTarget(
                provider="edge",
                voice=voice,
                label=f"catalog-edge-{_slug(voice)}",
                source="catalog",
                used_by=("catalog:edge",),
                text=_target_text(f"Catalog Edge {voice}", sample_text),
            )
        )
    for voice in sorted(OPENAI_VOICES):
        targets.append(
            VoiceAuditionTarget(
                provider="openai",
                voice=voice,
                label=f"catalog-openai-{_slug(voice)}",
                source="catalog",
                used_by=("catalog:openai",),
                text=_target_text(f"Catalog OpenAI {voice}", sample_text),
                openai_instructions="Speak Italian with a natural radio audition delivery.",
            )
        )
    for voice in sorted(AZURE_ITALIAN_VOICES):
        targets.append(
            VoiceAuditionTarget(
                provider="azure",
                voice=voice,
                label=f"catalog-azure-{_slug(voice)}",
                source="catalog",
                used_by=("catalog:azure",),
                text=_target_text(f"Catalog Azure {voice}", sample_text),
            )
        )
    return targets


def build_audition_targets(
    config: StationConfig,
    *,
    providers: list[str],
    include_configured: bool = True,
    include_catalog: bool = False,
    manual_voices: list[tuple[str, str]] | None = None,
    sample_text: str = DEFAULT_SAMPLE_TEXT,
) -> list[VoiceAuditionTarget]:
    provider_set = set(providers)
    merged: dict[tuple[str, str, str, str, str, str], VoiceAuditionTarget] = {}
    candidates: list[VoiceAuditionTarget] = []
    if include_configured:
        candidates.extend(collect_configured_targets(config, sample_text=sample_text))
    if include_catalog:
        candidates.extend(collect_catalog_targets(sample_text=sample_text))
    for provider, voice in manual_voices or []:
        candidates.append(
            VoiceAuditionTarget(
                provider=provider,
                voice=voice,
                label=f"manual-{provider}-{_slug(voice)}",
                source="manual",
                used_by=(f"manual:{provider}",),
                text=_target_text(f"Manual {provider} voice {voice}", sample_text),
            )
        )

    for target in candidates:
        target.provider = _canonical_provider(target.provider)
        if target.provider in provider_set:
            _add_target(merged, target)
    return sorted(merged.values(), key=lambda t: (PROVIDERS.index(t.provider), t.label, t.voice))


def expand_stability_variants(
    targets: list[VoiceAuditionTarget],
    stabilities: list[float] | None,
) -> list[VoiceAuditionTarget]:
    """Fan out each ElevenLabs target into one variant per stability value.

    Used to A/B a host voice's clarity: low ElevenLabs stability mumbles, higher
    tightens diction. Non-ElevenLabs targets and the empty-sweep case pass through
    unchanged. Each variant carries ``voice_settings={'stability': s}`` and a label
    suffix so the manifest stays distinct.
    """
    if not stabilities:
        return targets
    expanded: list[VoiceAuditionTarget] = []
    for target in targets:
        if target.provider != "elevenlabs":
            expanded.append(target)
            continue
        for stability in stabilities:
            expanded.append(
                replace(
                    target,
                    label=f"{target.label}-stab{round(stability * 100):02d}",
                    voice_settings={**(target.voice_settings or {}), "stability": stability},
                )
            )
    return expanded


async def _synthesize_target(target: VoiceAuditionTarget, output_path: Path) -> Path:
    if target.provider == "openai":
        return await tts_module.synthesize_openai(
            target.text,
            target.voice,
            output_path,
            instructions=target.openai_instructions,
        )
    if target.provider == "azure":
        return await tts_module.synthesize_azure(
            target.text,
            target.voice,
            output_path,
            rate=target.rate,
            pitch=target.pitch,
        )
    if target.provider == "elevenlabs":
        return await tts_module.synthesize_elevenlabs(
            target.text,
            target.voice,
            output_path,
            voice_settings=target.voice_settings,
            elevenlabs_model=target.elevenlabs_model,
            delivery_cue=target.delivery_cue,
            delivery_profile=target.delivery_profile,
        )
    return await tts_module.synthesize(
        target.text,
        target.voice,
        output_path,
        rate=target.rate,
        pitch=target.pitch,
        engine="edge",
        edge_fallback_voice=target.edge_fallback_voice,
    )


def _result_for_target(
    target: VoiceAuditionTarget,
    *,
    status: str,
    output_path: str = "",
    missing_env: tuple[str, ...] = (),
    error: str = "",
    audio_sha256: str | None = None,
    audio_duration_seconds: float | None = None,
) -> VoiceAuditionResult:
    clean_text_sha256, rendered_text_sha256 = _audition_text_hashes(target)
    return VoiceAuditionResult(
        provider=target.provider,
        voice=target.voice,
        label=target.label,
        source=target.source,
        used_by=target.used_by,
        status=status,
        output_path=output_path,
        missing_env=missing_env,
        error=error,
        voice_settings=target.voice_settings,
        # Keep the legacy V2 receipt's field stable: it is the clean text hash.
        text_sha256=clean_text_sha256,
        profile=_selection_profile_for_target(target),
        audio_sha256=audio_sha256,
        audio_duration_seconds=audio_duration_seconds,
        clean_text_sha256=clean_text_sha256,
        rendered_text_sha256=rendered_text_sha256,
        elevenlabs_model=target.elevenlabs_model,
        delivery_profile=target.delivery_profile,
        delivery_cue=target.delivery_cue,
    )


async def run_auditions(
    targets: list[VoiceAuditionTarget],
    run_dir: Path,
    *,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    strict: bool = False,
) -> list[VoiceAuditionResult]:
    results: list[VoiceAuditionResult] = []
    if not dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)

    for index, target in enumerate(targets, start=1):
        missing_env = missing_env_for_provider(target.provider, env)
        if dry_run:
            status = STATUS_PLANNED if not missing_env else STATUS_SKIPPED
            results.append(
                _result_for_target(
                    target,
                    status=status,
                    missing_env=missing_env,
                    error="missing provider credentials" if missing_env else "",
                )
            )
            continue

        stability = target.voice_settings.get("stability") if target.voice_settings else None
        stab_suffix = f"-stab{round(stability * 100):02d}" if stability is not None else ""
        model_suffix = f"-{_slug(target.elevenlabs_model)}" if target.provider == "elevenlabs" else ""
        cue_suffix = (
            f"-{_slug(target.delivery_cue)}"
            if target.provider == "elevenlabs" and target.delivery_cue != NEUTRAL_DELIVERY_CUE
            else ""
        )
        output_path = run_dir / (
            f"{index:02d}-{target.provider}-{_slug(target.voice)}{model_suffix}{cue_suffix}{stab_suffix}.mp3"
        )
        if missing_env:
            results.append(
                _result_for_target(
                    target,
                    status=STATUS_FAILED if strict else STATUS_SKIPPED,
                    output_path=str(output_path),
                    missing_env=missing_env,
                    error="missing provider credentials",
                )
            )
            continue

        try:
            rendered_path = await _synthesize_target(target, output_path)
        except Exception as exc:
            results.append(
                _result_for_target(
                    target,
                    status=STATUS_FAILED,
                    output_path=str(output_path),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            audio_sha256, audio_duration_seconds = _generated_audio_evidence(rendered_path)
            results.append(
                _result_for_target(
                    target,
                    status=STATUS_GENERATED,
                    output_path=str(rendered_path),
                    audio_sha256=audio_sha256,
                    audio_duration_seconds=audio_duration_seconds,
                )
            )
    return results


def write_manifest(
    results: list[VoiceAuditionResult],
    run_dir: Path,
    *,
    config_path: Path,
    timestamp: str,
    dry_run: bool = False,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing manifest: {manifest_path}")

    payload = {
        "generated_at": timestamp,
        "config": str(config_path),
        "dry_run": dry_run,
        "counts": dict(Counter(result.status for result in results)),
        "results": [_manifest_result(result) for result in results],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return manifest_path


_SELECTION_RECEIPT_TOP_LEVEL_FIELDS = frozenset({"schema_version", "candidates"})
_SELECTION_RECEIPT_ENTRY_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_name",
        "profile",
        "profile_fingerprint",
        "text_sha256",
        "provider_result",
        "audio_sha256",
        "audio_duration_seconds",
        "approval_status",
        "rationale",
    }
)
_SELECTION_PROFILE_FIELDS = frozenset({"engine", "model", "voice_settings"})
_SELECTION_VOICE_SETTING_FIELDS = frozenset({"stability", "similarity_boost", "style", "use_speaker_boost"})
_SELECTION_PROVIDER_RESULTS = frozenset({STATUS_GENERATED, STATUS_FAILED, STATUS_SKIPPED})
_SELECTION_APPROVAL_STATUSES = frozenset({"accepted", "rejected"})
_SELECTION_ACCEPTED_RATIONALES = frozenset(
    {
        "accepted_clear_natural_delivery",
        "accepted_distinct_character",
        "accepted_balanced_brand_fit",
    }
)
_SELECTION_REJECTED_RATIONALES = frozenset(
    {
        "rejected_provider_failure",
        "rejected_unintelligible_delivery",
        "rejected_unconvincing_character",
        "rejected_off_brand_delivery",
        "rejected_profile_mismatch",
    }
)
_SELECTION_PROFILE_MODEL_BY_ENGINE = {
    "edge": "edge_read_aloud",
    "openai": "openai_tts",
    "azure": "azure_speech",
    "elevenlabs": "eleven_multilingual_v2",
}


def _receipt_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} keys must be strings")
    return value


def _receipt_exact_fields(value: Mapping[str, object], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{field} contains prohibited fields: {', '.join(unknown)}")


def _selection_candidate_id(provider: object, voice: object, profile: object) -> str:
    """Return the opaque identity for one provider/voice/render-profile audition."""

    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("candidate.provider must name a supported provider")
    if not isinstance(voice, str) or not voice:
        raise ValueError("candidate.voice must be a non-empty string")
    profile_mapping = _receipt_mapping(profile, "candidate.profile")
    canonical = json.dumps(
        {"provider": provider, "voice": voice, "profile": dict(profile_mapping)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"audition-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _manifest_result(result: VoiceAuditionResult) -> dict[str, object]:
    """Expose a profile-aware opaque candidate ID without leaking it into receipts."""

    payload = asdict(result)
    if result.profile is not None:
        payload["candidate_id"] = _selection_candidate_id(result.provider, result.voice, result.profile)
    if result.source == "v3-host-performance":
        payload["performance_id"] = _host_performance_id(
            result.provider,
            result.voice,
            result.elevenlabs_model,
            result.delivery_profile,
            result.delivery_cue,
            result.clean_text_sha256,
            result.rendered_text_sha256,
        )
    return payload


def _safe_receipt_note(value: object, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise ValueError(f"{field} is too long")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError(f"{field} must be a single line")
    # The receipt is deliberately portable evidence, not a copy or filesystem
    # archive. Keep explanatory notes free of URL/file-path-shaped values.
    if "://" in value or "/" in value or "\\" in value:
        raise ValueError(f"{field} must not contain a URL or local path")
    return value


def _validate_selection_rationale(value: object, field: str, *, approval_status: object) -> str:
    """Require a controlled rationale code instead of retaining audition copy."""

    if not isinstance(value, str):
        raise ValueError(f"{field} must be a controlled rationale code")
    allowed = (
        _SELECTION_ACCEPTED_RATIONALES
        if approval_status == "accepted"
        else _SELECTION_REJECTED_RATIONALES
        if approval_status == "rejected"
        else frozenset()
    )
    if value not in allowed:
        raise ValueError(f"{field} must be a controlled rationale code")
    return value


def _sha256(value: object, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_selection_profile(value: object) -> None:
    profile = _receipt_mapping(value, "candidate.profile")
    _receipt_exact_fields(profile, _SELECTION_PROFILE_FIELDS, "candidate.profile")
    missing = sorted(_SELECTION_PROFILE_FIELDS - set(profile))
    if missing:
        raise ValueError(f"candidate.profile is missing fields: {', '.join(missing)}")

    engine = profile["engine"]
    if not isinstance(engine, str) or engine not in PROVIDERS:
        raise ValueError("candidate.profile.engine must name a supported provider")
    model = profile["model"]
    expected_model = _SELECTION_PROFILE_MODEL_BY_ENGINE[engine]
    if model != expected_model:
        raise ValueError(f"candidate.profile.model must be {expected_model!r} for engine {engine!r}")

    settings = _receipt_mapping(profile["voice_settings"], "candidate.profile.voice_settings")
    required_settings = _SELECTION_VOICE_SETTING_FIELDS if engine == "elevenlabs" else frozenset()
    _receipt_exact_fields(settings, required_settings, "candidate.profile.voice_settings")
    missing_settings = sorted(required_settings - set(settings))
    if missing_settings:
        raise ValueError(f"candidate.profile.voice_settings is missing fields: {', '.join(missing_settings)}")
    if engine != "elevenlabs":
        return
    for setting, setting_value in settings.items():
        if setting == "use_speaker_boost":
            if type(setting_value) is not bool:
                raise ValueError("candidate.profile.voice_settings.use_speaker_boost must be a boolean")
            continue
        if isinstance(setting_value, bool) or not isinstance(setting_value, int | float):
            raise ValueError(f"candidate.profile.voice_settings.{setting} must be a finite number")
        numeric_setting = float(setting_value)
        if not math.isfinite(numeric_setting):
            raise ValueError(f"candidate.profile.voice_settings.{setting} must be a finite number")
        if not 0.0 <= numeric_setting <= 1.0:
            raise ValueError(f"candidate.profile.voice_settings.{setting} must be between 0 and 1")


def _validate_selection_entry(value: object, index: int) -> str:
    entry = _receipt_mapping(value, f"candidates[{index}]")
    _receipt_exact_fields(entry, _SELECTION_RECEIPT_ENTRY_FIELDS, f"candidates[{index}]")
    required = {
        "candidate_id",
        "candidate_name",
        "text_sha256",
        "provider_result",
        "audio_sha256",
        "audio_duration_seconds",
        "approval_status",
        "rationale",
    }
    missing = sorted(required - set(entry))
    if missing:
        raise ValueError(f"candidates[{index}] is missing fields: {', '.join(missing)}")

    has_profile = "profile" in entry
    has_fingerprint = "profile_fingerprint" in entry
    if has_profile == has_fingerprint:
        raise ValueError(f"candidates[{index}] must contain exactly one of profile or profile_fingerprint")
    if has_profile:
        _validate_selection_profile(entry["profile"])
    else:
        _sha256(entry["profile_fingerprint"], f"candidates[{index}].profile_fingerprint")

    candidate_id = entry["candidate_id"]
    if not isinstance(candidate_id, str) or not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ValueError(f"candidates[{index}].candidate_id must be a safe voice identifier")
    _safe_receipt_note(entry["candidate_name"], f"candidates[{index}].candidate_name", max_length=200)
    _sha256(entry["text_sha256"], f"candidates[{index}].text_sha256")

    provider_result = entry["provider_result"]
    if provider_result not in _SELECTION_PROVIDER_RESULTS:
        raise ValueError(f"candidates[{index}].provider_result is invalid")
    approval_status = entry["approval_status"]
    if approval_status not in _SELECTION_APPROVAL_STATUSES:
        raise ValueError(f"candidates[{index}].approval_status is invalid")
    if approval_status == "accepted" and provider_result != STATUS_GENERATED:
        raise ValueError(f"candidates[{index}] cannot be accepted without generated provider audio")

    audio_sha256 = _sha256(entry["audio_sha256"], f"candidates[{index}].audio_sha256", allow_none=True)
    duration = entry["audio_duration_seconds"]
    invalid_duration = False
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, int | float):
            invalid_duration = True
        else:
            invalid_duration = not math.isfinite(float(duration)) or duration <= 0
    if duration is not None and invalid_duration:
        raise ValueError(f"candidates[{index}].audio_duration_seconds must be a positive finite number or null")
    if provider_result == STATUS_GENERATED:
        if audio_sha256 is None or duration is None:
            raise ValueError(f"candidates[{index}] needs audio checksum and duration after generated provider audio")
    elif audio_sha256 is not None or duration is not None:
        raise ValueError(f"candidates[{index}] must not include audio evidence without generated provider audio")

    _validate_selection_rationale(
        entry["rationale"],
        f"candidates[{index}].rationale",
        approval_status=approval_status,
    )
    return candidate_id


def validate_selection_receipt(receipt: object) -> None:
    """Fail closed unless a tracked voice-selection receipt is safe and complete.

    The proof intentionally stores only reproducible identifiers and hashes. It
    must never become an archive of raw audition copy, audio, local paths, or
    provider credentials. A human acceptance/rejection remains explicit for
    every candidate, including provider failures.
    """
    payload = _receipt_mapping(receipt, "receipt")
    _receipt_exact_fields(payload, _SELECTION_RECEIPT_TOP_LEVEL_FIELDS, "receipt")
    if set(payload) != _SELECTION_RECEIPT_TOP_LEVEL_FIELDS:
        missing = sorted(_SELECTION_RECEIPT_TOP_LEVEL_FIELDS - set(payload))
        raise ValueError(f"receipt is missing fields: {', '.join(missing)}")
    if payload["schema_version"] != SELECTION_RECEIPT_SCHEMA_VERSION:
        raise ValueError(f"receipt.schema_version must be {SELECTION_RECEIPT_SCHEMA_VERSION}")
    candidates = payload["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("receipt.candidates must be a non-empty array")
    candidate_ids = [_validate_selection_entry(candidate, index) for index, candidate in enumerate(candidates)]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("receipt.candidates must not repeat candidate_id")


def selection_receipt(candidates: list[Mapping[str, object]]) -> dict[str, object]:
    """Build and validate the stable, tracked proof payload for selected voices."""
    receipt: dict[str, object] = {
        "schema_version": SELECTION_RECEIPT_SCHEMA_VERSION,
        "candidates": [dict(candidate) for candidate in candidates],
    }
    validate_selection_receipt(receipt)
    return receipt


def _commit_selection_receipt(receipt: Mapping[str, object], *, path: Path, overwrite: bool) -> Path:
    """Persist reviewed evidence without a time-of-check/time-of-use overwrite race."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if overwrite:
            temporary_path.replace(path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                raise FileExistsError(f"Refusing to overwrite existing selection receipt: {path}") from None
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def write_selection_receipt(
    candidates: list[Mapping[str, object]],
    *,
    path: Path = SELECTION_RECEIPT_PATH,
    overwrite: bool = False,
) -> Path:
    """Atomically write validated, redacted selection evidence once approved.

    This deliberately refuses to replace a previously reviewed receipt by
    default. The audition command's local manifest remains under ignored
    ``tmp/voice-auditions``; this is the small, safe artifact suitable for
    version control after provider and human approval.
    """
    receipt = selection_receipt(candidates)
    return _commit_selection_receipt(receipt, path=path, overwrite=overwrite)


def load_selection_receipt(path: Path = SELECTION_RECEIPT_PATH) -> dict[str, object]:
    """Load and validate a committed receipt without touching provider APIs."""
    value = json.loads(path.read_text())
    validate_selection_receipt(value)
    return value


_SELECTION_DECISION_FIELDS = frozenset({"candidate_id", "candidate_name", "approval_status", "rationale"})


def _selection_decisions(value: object) -> list[Mapping[str, object]]:
    """Validate the small human-only sidecar used to make a receipt.

    The ignored audition manifest already holds hashes and provider outcome.
    Requiring this separate, deliberately tiny file forces the final accept or
    reject decision to stay a human action without inviting raw copy, paths, or
    credentials into the tracked proof.
    """
    if not isinstance(value, list) or not value:
        raise ValueError("selection decisions must be a non-empty array")
    decisions: list[Mapping[str, object]] = []
    candidate_ids: set[str] = set()
    for index, item in enumerate(value):
        decision = _receipt_mapping(item, f"selection decisions[{index}]")
        _receipt_exact_fields(decision, _SELECTION_DECISION_FIELDS, f"selection decisions[{index}]")
        if set(decision) != _SELECTION_DECISION_FIELDS:
            missing = sorted(_SELECTION_DECISION_FIELDS - set(decision))
            raise ValueError(f"selection decisions[{index}] is missing fields: {', '.join(missing)}")
        candidate_id = decision["candidate_id"]
        if not isinstance(candidate_id, str) or not CANDIDATE_ID_RE.fullmatch(candidate_id):
            raise ValueError(f"selection decisions[{index}].candidate_id must be a safe voice identifier")
        if candidate_id in candidate_ids:
            raise ValueError("selection decisions must not repeat candidate_id")
        candidate_ids.add(candidate_id)
        _safe_receipt_note(decision["candidate_name"], f"selection decisions[{index}].candidate_name", max_length=200)
        if decision["approval_status"] not in _SELECTION_APPROVAL_STATUSES:
            raise ValueError(f"selection decisions[{index}].approval_status is invalid")
        _validate_selection_rationale(
            decision["rationale"],
            f"selection decisions[{index}].rationale",
            approval_status=decision["approval_status"],
        )
        decisions.append(decision)
    return decisions


def selection_receipt_from_manifest(
    manifest: object,
    decisions: object,
) -> dict[str, object]:
    """Join audited local render evidence with explicit human decisions.

    The resulting payload is passed through :func:`selection_receipt`, which
    is the last redaction boundary before a tracked proof file is written.
    """
    manifest_data = _receipt_mapping(manifest, "audition manifest")
    raw_results = manifest_data.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("audition manifest.results must be an array")

    candidates: list[Mapping[str, object]] = []
    for decision in _selection_decisions(decisions):
        candidate_id = str(decision["candidate_id"])
        candidate_name = str(decision["candidate_name"])
        matches: list[Mapping[str, object]] = []
        for raw_result in raw_results:
            result = _receipt_mapping(raw_result, "audition manifest.results[]")
            used_by = result.get("used_by")
            result_candidate_id = _selection_candidate_id(
                result.get("provider"),
                result.get("voice"),
                result.get("profile"),
            )
            if result.get("candidate_id") != result_candidate_id:
                raise ValueError("audition manifest candidate_id must match its provider, voice, and profile")
            if result_candidate_id == candidate_id and isinstance(used_by, list) and f"ad:{candidate_name}" in used_by:
                matches.append(result)
        if len(matches) != 1:
            raise ValueError(
                f"selection decision for {candidate_name!r} must match exactly one configured ad result in the manifest"
            )
        result = matches[0]
        provider_result = result.get("status")
        if provider_result not in _SELECTION_PROVIDER_RESULTS:
            raise ValueError(f"selection decision for {candidate_name!r} has no completed provider result")
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_name": candidate_name,
                "profile": result.get("profile"),
                "text_sha256": result.get("text_sha256"),
                "provider_result": provider_result,
                "audio_sha256": result.get("audio_sha256"),
                "audio_duration_seconds": result.get("audio_duration_seconds"),
                "approval_status": decision["approval_status"],
                "rationale": decision["rationale"],
            }
        )
    return selection_receipt(candidates)


def write_selection_receipt_from_manifest(
    *,
    manifest_path: Path,
    decisions_path: Path,
    path: Path = SELECTION_RECEIPT_PATH,
    overwrite: bool = False,
) -> Path:
    """Write a redacted receipt from an ignored audition manifest and review sidecar."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    receipt = selection_receipt_from_manifest(manifest, decisions)
    return _commit_selection_receipt(receipt, path=path, overwrite=overwrite)


_HOST_PERFORMANCE_RECEIPT_TOP_LEVEL_FIELDS = frozenset({"schema_version", "performances"})
_HOST_PERFORMANCE_RECEIPT_ENTRY_FIELDS = frozenset(
    {
        "performance_id",
        "host",
        "voice_id",
        "model",
        "delivery_profile",
        "delivery_cue",
        "clean_text_sha256",
        "rendered_text_sha256",
        "provider_result",
        "audio_sha256",
        "audio_duration_seconds",
        "human_disposition",
        "rationale",
    }
)
_HOST_PERFORMANCE_DECISION_FIELDS = frozenset({"performance_id", "host", "human_disposition", "rationale"})
_HOST_PERFORMANCE_ACCEPTED_RATIONALES = frozenset(
    {
        "accepted_clear_natural_delivery",
        "accepted_distinct_character",
        "accepted_v3_tonal_fit",
    }
)
_HOST_PERFORMANCE_REJECTED_RATIONALES = frozenset(
    {
        "rejected_provider_failure",
        "rejected_unintelligible_delivery",
        "rejected_unconvincing_character",
        "rejected_off_brand_delivery",
        "rejected_tag_spoken",
        "rejected_audio_artifacts",
    }
)
_HOST_PERFORMANCE_DISPOSITIONS = frozenset({"accepted", "rejected"})


def _host_performance_id(
    provider: object,
    voice_id: object,
    model: object,
    delivery_profile: object,
    delivery_cue: object,
    clean_text_sha256: object,
    rendered_text_sha256: object,
) -> str:
    """Return an opaque identity for one exact V2/V3 host-performance render."""
    if provider != "elevenlabs":
        raise ValueError("host performance provider must be elevenlabs")
    if not isinstance(voice_id, str) or not CANDIDATE_ID_RE.fullmatch(voice_id):
        raise ValueError("host performance voice_id must be a safe voice identifier")
    if model not in {ELEVENLABS_V2_MODEL, ELEVENLABS_V3_MODEL}:
        raise ValueError("host performance model must be an allowed ElevenLabs V2 or V3 model")
    if not isinstance(delivery_profile, str) or delivery_profile not in V3_DELIVERY_CUES_BY_PROFILE:
        raise ValueError("host performance delivery_profile is invalid")
    if not isinstance(delivery_cue, str):
        raise ValueError("host performance delivery_cue must be a string")
    _sha256(clean_text_sha256, "host performance clean_text_sha256")
    _sha256(rendered_text_sha256, "host performance rendered_text_sha256")
    canonical = json.dumps(
        {
            "provider": provider,
            "voice_id": voice_id,
            "model": model,
            "delivery_profile": delivery_profile,
            "delivery_cue": delivery_cue,
            "clean_text_sha256": clean_text_sha256,
            "rendered_text_sha256": rendered_text_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"performance-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _validate_host_performance_rationale(
    value: object,
    field: str,
    *,
    human_disposition: object,
) -> str:
    allowed = (
        _HOST_PERFORMANCE_ACCEPTED_RATIONALES
        if human_disposition == "accepted"
        else _HOST_PERFORMANCE_REJECTED_RATIONALES
        if human_disposition == "rejected"
        else frozenset()
    )
    if value not in allowed:
        raise ValueError(f"{field} must be a controlled rationale code")
    return str(value)


def _validate_host_performance_entry(value: object, index: int) -> dict[str, object]:
    entry = _receipt_mapping(value, f"performances[{index}]")
    _receipt_exact_fields(entry, _HOST_PERFORMANCE_RECEIPT_ENTRY_FIELDS, f"performances[{index}]")
    missing = sorted(_HOST_PERFORMANCE_RECEIPT_ENTRY_FIELDS - set(entry))
    if missing:
        raise ValueError(f"performances[{index}] is missing fields: {', '.join(missing)}")

    host = _safe_receipt_note(entry["host"], f"performances[{index}].host", max_length=100)
    delivery_profile = entry["delivery_profile"]
    if not isinstance(delivery_profile, str) or delivery_profile not in V3_DELIVERY_CUES_BY_PROFILE:
        raise ValueError(f"performances[{index}].delivery_profile is invalid")
    if host.casefold() != delivery_profile:
        raise ValueError(f"performances[{index}].host must match its delivery_profile")

    voice_id = entry["voice_id"]
    if not isinstance(voice_id, str) or not CANDIDATE_ID_RE.fullmatch(voice_id):
        raise ValueError(f"performances[{index}].voice_id must be a safe voice identifier")
    model = entry["model"]
    if model not in {ELEVENLABS_V2_MODEL, ELEVENLABS_V3_MODEL}:
        raise ValueError(f"performances[{index}].model is invalid")
    delivery_cue = entry["delivery_cue"]
    if not isinstance(delivery_cue, str):
        raise ValueError(f"performances[{index}].delivery_cue must be a string")
    if model == ELEVENLABS_V2_MODEL and delivery_cue != NEUTRAL_DELIVERY_CUE:
        raise ValueError(f"performances[{index}] must keep V2 delivery_cue neutral")
    if model == ELEVENLABS_V3_MODEL and delivery_cue not in {
        NEUTRAL_DELIVERY_CUE,
        *V3_DELIVERY_CUES_BY_PROFILE[delivery_profile],
    }:
        raise ValueError(f"performances[{index}].delivery_cue is invalid for its profile")

    clean_text_sha256 = _sha256(entry["clean_text_sha256"], f"performances[{index}].clean_text_sha256")
    rendered_text_sha256 = _sha256(entry["rendered_text_sha256"], f"performances[{index}].rendered_text_sha256")
    assert isinstance(clean_text_sha256, str)
    assert isinstance(rendered_text_sha256, str)
    if model == ELEVENLABS_V3_MODEL and delivery_cue != NEUTRAL_DELIVERY_CUE:
        if clean_text_sha256 == rendered_text_sha256:
            raise ValueError(f"performances[{index}] must distinguish V3 rendered text from clean text")
    elif clean_text_sha256 != rendered_text_sha256:
        raise ValueError(f"performances[{index}] must keep neutral/V2 rendered text equal to clean text")

    performance_id = entry["performance_id"]
    expected_performance_id = _host_performance_id(
        "elevenlabs",
        voice_id,
        model,
        delivery_profile,
        delivery_cue,
        clean_text_sha256,
        rendered_text_sha256,
    )
    if performance_id != expected_performance_id:
        raise ValueError(f"performances[{index}].performance_id must match the immutable render identity")

    provider_result = entry["provider_result"]
    if provider_result not in _SELECTION_PROVIDER_RESULTS:
        raise ValueError(f"performances[{index}].provider_result is invalid")
    human_disposition = entry["human_disposition"]
    if human_disposition not in _HOST_PERFORMANCE_DISPOSITIONS:
        raise ValueError(f"performances[{index}].human_disposition is invalid")
    if human_disposition == "accepted" and provider_result != STATUS_GENERATED:
        raise ValueError(f"performances[{index}] cannot be accepted without generated provider audio")

    audio_sha256 = _sha256(entry["audio_sha256"], f"performances[{index}].audio_sha256", allow_none=True)
    duration = entry["audio_duration_seconds"]
    invalid_duration = duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, int | float)
        or not math.isfinite(float(duration))
        or duration <= 0
    )
    if invalid_duration:
        raise ValueError(f"performances[{index}].audio_duration_seconds must be a positive finite number or null")
    if provider_result == STATUS_GENERATED:
        if audio_sha256 is None or duration is None:
            raise ValueError(f"performances[{index}] needs audio checksum and duration after generated provider audio")
    elif audio_sha256 is not None or duration is not None:
        raise ValueError(f"performances[{index}] must not include audio evidence without generated provider audio")
    _validate_host_performance_rationale(
        entry["rationale"],
        f"performances[{index}].rationale",
        human_disposition=human_disposition,
    )
    return dict(entry)


def validate_host_performance_receipt(receipt: object) -> None:
    """Validate safe V2/V3 host-performance evidence without requiring approval."""
    payload = _receipt_mapping(receipt, "host performance receipt")
    _receipt_exact_fields(payload, _HOST_PERFORMANCE_RECEIPT_TOP_LEVEL_FIELDS, "host performance receipt")
    if set(payload) != _HOST_PERFORMANCE_RECEIPT_TOP_LEVEL_FIELDS:
        missing = sorted(_HOST_PERFORMANCE_RECEIPT_TOP_LEVEL_FIELDS - set(payload))
        raise ValueError(f"host performance receipt is missing fields: {', '.join(missing)}")
    if payload["schema_version"] != HOST_PERFORMANCE_RECEIPT_SCHEMA_VERSION:
        raise ValueError(f"host performance receipt.schema_version must be {HOST_PERFORMANCE_RECEIPT_SCHEMA_VERSION}")
    performances = payload["performances"]
    if not isinstance(performances, list) or not performances:
        raise ValueError("host performance receipt.performances must be a non-empty array")
    validated = [_validate_host_performance_entry(performance, index) for index, performance in enumerate(performances)]
    performance_ids = [str(performance["performance_id"]) for performance in validated]
    if len(performance_ids) != len(set(performance_ids)):
        raise ValueError("host performance receipt.performances must not repeat performance_id")


def assert_host_performance_gate(receipt: object) -> None:
    """Require every V2/V3 comparison row to be generated and human-accepted.

    A rejected receipt remains valuable evidence, so the general schema accepts
    it. This explicit gate is the release-time blocker for the V3 host rollout.
    """
    validate_host_performance_receipt(receipt)
    payload = _receipt_mapping(receipt, "host performance receipt")
    performances = payload["performances"]
    assert isinstance(performances, list)
    by_profile: dict[str, dict[tuple[str, str], Mapping[str, object]]] = {}
    for raw_performance in performances:
        performance = _receipt_mapping(raw_performance, "host performance receipt.performances[]")
        profile = str(performance["delivery_profile"])
        key = (str(performance["model"]), str(performance["delivery_cue"]))
        profile_rows = by_profile.setdefault(profile, {})
        if key in profile_rows:
            raise ValueError(f"host performance receipt repeats {profile} {key[0]} {key[1]}")
        profile_rows[key] = performance

    for profile, cues in V3_DELIVERY_CUES_BY_PROFILE.items():
        expected = {(ELEVENLABS_V2_MODEL, NEUTRAL_DELIVERY_CUE), (ELEVENLABS_V3_MODEL, NEUTRAL_DELIVERY_CUE)}
        expected.update((ELEVENLABS_V3_MODEL, cue) for cue in cues)
        actual = set(by_profile.get(profile, {}))
        missing = sorted(expected - actual)
        if missing:
            formatted = ", ".join(f"{model}/{cue}" for model, cue in missing)
            raise ValueError(f"host performance receipt is missing {profile} rows: {formatted}")
        clean_hashes = {str(by_profile[profile][key]["clean_text_sha256"]) for key in expected}
        if len(clean_hashes) != 1:
            raise ValueError(f"host performance receipt must use one clean comparison text for {profile}")
        for model, cue in expected:
            row = by_profile[profile][(model, cue)]
            if row["provider_result"] != STATUS_GENERATED or row["human_disposition"] != "accepted":
                raise ValueError(f"host performance receipt is not approved for {profile} {model}/{cue}")


def host_performance_receipt(performances: Sequence[Mapping[str, object]]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": HOST_PERFORMANCE_RECEIPT_SCHEMA_VERSION,
        "performances": [dict(performance) for performance in performances],
    }
    validate_host_performance_receipt(receipt)
    return receipt


def _commit_host_performance_receipt(receipt: Mapping[str, object], *, path: Path, overwrite: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if overwrite:
            temporary_path.replace(path)
        else:
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                raise FileExistsError(f"Refusing to overwrite existing host-performance receipt: {path}") from None
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def write_host_performance_receipt(
    performances: Sequence[Mapping[str, object]],
    *,
    path: Path = HOST_PERFORMANCE_RECEIPT_PATH,
    overwrite: bool = False,
) -> Path:
    """Atomically write reviewed, redacted V3 host-performance evidence."""
    receipt = host_performance_receipt(performances)
    return _commit_host_performance_receipt(receipt, path=path, overwrite=overwrite)


def load_host_performance_receipt(
    path: Path = HOST_PERFORMANCE_RECEIPT_PATH,
    *,
    require_approved_matrix: bool = False,
) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_host_performance_receipt(value)
    if require_approved_matrix:
        assert_host_performance_gate(value)
    return value


def _host_performance_decisions(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise ValueError("host performance decisions must be a non-empty array")
    decisions: list[Mapping[str, object]] = []
    performance_ids: set[str] = set()
    for index, item in enumerate(value):
        decision = _receipt_mapping(item, f"host performance decisions[{index}]")
        _receipt_exact_fields(decision, _HOST_PERFORMANCE_DECISION_FIELDS, f"host performance decisions[{index}]")
        if set(decision) != _HOST_PERFORMANCE_DECISION_FIELDS:
            missing = sorted(_HOST_PERFORMANCE_DECISION_FIELDS - set(decision))
            raise ValueError(f"host performance decisions[{index}] is missing fields: {', '.join(missing)}")
        performance_id = decision["performance_id"]
        if not isinstance(performance_id, str) or not CANDIDATE_ID_RE.fullmatch(performance_id):
            raise ValueError(f"host performance decisions[{index}].performance_id is invalid")
        if performance_id in performance_ids:
            raise ValueError("host performance decisions must not repeat performance_id")
        performance_ids.add(performance_id)
        _safe_receipt_note(decision["host"], f"host performance decisions[{index}].host", max_length=100)
        if decision["human_disposition"] not in _HOST_PERFORMANCE_DISPOSITIONS:
            raise ValueError(f"host performance decisions[{index}].human_disposition is invalid")
        _validate_host_performance_rationale(
            decision["rationale"],
            f"host performance decisions[{index}].rationale",
            human_disposition=decision["human_disposition"],
        )
        decisions.append(decision)
    return decisions


def host_performance_receipt_from_manifest(manifest: object, decisions: object) -> dict[str, object]:
    """Join a local V2/V3 audition manifest with explicit human disposition."""
    manifest_data = _receipt_mapping(manifest, "audition manifest")
    raw_results = manifest_data.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("audition manifest.results must be an array")

    performances: list[Mapping[str, object]] = []
    for decision in _host_performance_decisions(decisions):
        performance_id = str(decision["performance_id"])
        host = str(decision["host"])
        matches: list[Mapping[str, object]] = []
        for raw_result in raw_results:
            result = _receipt_mapping(raw_result, "audition manifest.results[]")
            if result.get("source") != "v3-host-performance":
                continue
            computed_id = _host_performance_id(
                result.get("provider"),
                result.get("voice"),
                result.get("elevenlabs_model"),
                result.get("delivery_profile"),
                result.get("delivery_cue"),
                result.get("clean_text_sha256"),
                result.get("rendered_text_sha256"),
            )
            if result.get("performance_id") != computed_id:
                raise ValueError("audition manifest performance_id must match its immutable render identity")
            used_by = result.get("used_by")
            if computed_id == performance_id and isinstance(used_by, list) and f"host:{host}" in used_by:
                matches.append(result)
        if len(matches) != 1:
            raise ValueError(
                f"host performance decision for {host!r} must match exactly one host performance result in the manifest"
            )
        result = matches[0]
        performances.append(
            {
                "performance_id": performance_id,
                "host": host,
                "voice_id": result.get("voice"),
                "model": result.get("elevenlabs_model"),
                "delivery_profile": result.get("delivery_profile"),
                "delivery_cue": result.get("delivery_cue"),
                "clean_text_sha256": result.get("clean_text_sha256"),
                "rendered_text_sha256": result.get("rendered_text_sha256"),
                "provider_result": result.get("status"),
                "audio_sha256": result.get("audio_sha256"),
                "audio_duration_seconds": result.get("audio_duration_seconds"),
                "human_disposition": decision["human_disposition"],
                "rationale": decision["rationale"],
            }
        )
    return host_performance_receipt(performances)


def write_host_performance_receipt_from_manifest(
    *,
    manifest_path: Path,
    decisions_path: Path,
    path: Path = HOST_PERFORMANCE_RECEIPT_PATH,
    overwrite: bool = False,
) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    receipt = host_performance_receipt_from_manifest(manifest, decisions)
    return _commit_host_performance_receipt(receipt, path=path, overwrite=overwrite)


def _print_summary(results: list[VoiceAuditionResult], *, dry_run: bool, run_dir: Path | None = None) -> None:
    counts = Counter(result.status for result in results)
    prefix = "Dry-run targets" if dry_run else "Audition results"
    print(
        f"{prefix}: {len(results)} total, "
        f"{counts.get(STATUS_GENERATED, 0)} generated, "
        f"{counts.get(STATUS_PLANNED, 0)} planned, "
        f"{counts.get(STATUS_SKIPPED, 0)} skipped, "
        f"{counts.get(STATUS_FAILED, 0)} failed"
    )
    skipped_missing = sorted(
        {
            f"{result.provider} missing {', '.join(result.missing_env)}"
            for result in results
            if result.status == STATUS_SKIPPED and result.missing_env
        }
    )
    for line in skipped_missing:
        print(f"Skipped: {line}")
    if run_dir is not None:
        print(f"Output: {run_dir}")


def _stability_arg(value: str) -> float:
    """argparse type for --elevenlabs-stability: a finite float in [0.0, 1.0].

    ElevenLabs stability is bounded 0-1; rejecting out-of-range/NaN/inf at parse
    time gives an immediate CLI error instead of a late API/format failure once
    the targets have already been expanded.
    """
    stability = float(value)
    if not math.isfinite(stability) or not (0.0 <= stability <= 1.0):
        raise argparse.ArgumentTypeError("stability must be a finite float in [0.0, 1.0]")
    return stability


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="radio.toml path to audition")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["all"],
        help="Providers to include: edge, openai, azure, elevenlabs, or all",
    )
    parser.add_argument(
        "--include-catalog",
        action="store_true",
        help="Also audition built-in Edge/OpenAI/Azure catalogs",
    )
    parser.add_argument(
        "--no-configured",
        action="store_true",
        help="Do not include voices currently configured in radio.toml",
    )
    parser.add_argument("--voice", action="append", help="Add one explicit provider:voice_id target; repeatable")
    parser.add_argument(
        "--sample-text",
        help="Italian sample sentence for all auditions (V3 host-performance uses its paired banter sample by default)",
    )
    parser.add_argument(
        "--v3-host-performance",
        action="store_true",
        help="Build only the Marco/Giulia paired V2-clean, V3-clean, and allowed V3-cue comparison matrix",
    )
    parser.add_argument(
        "--elevenlabs-stability",
        nargs="*",
        type=_stability_arg,
        default=None,
        help="Sweep ElevenLabs stability values (e.g. 0.42 0.6 0.75); fans out each "
        "ElevenLabs voice into one clip per value to A/B clarity (low = mumbly).",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Base output directory")
    parser.add_argument(
        "--timestamp",
        help="Override run timestamp in YYYYMMDDTHHMMSSZ format, useful for deterministic tests",
    )
    parser.add_argument("--dry-run", action="store_true", help="List planned/skipped voices without writing files")
    parser.add_argument("--strict", action="store_true", help="Treat missing provider credentials as failed auditions")
    parser.add_argument(
        "--selection-manifest",
        type=Path,
        help="Ignored manifest.json from a completed audition; pair with --selection-decisions to write reviewed proof",
    )
    parser.add_argument(
        "--selection-decisions",
        type=Path,
        help="Local JSON array of human candidate_id/name/approval_status/controlled-rationale decisions",
    )
    parser.add_argument(
        "--selection-receipt-path",
        type=Path,
        default=SELECTION_RECEIPT_PATH,
        help="Tracked redacted receipt path (default: proof/2026-07-13-voice-diversity-selection.json)",
    )
    parser.add_argument(
        "--overwrite-selection-receipt",
        action="store_true",
        help="Allow replacing an existing reviewed selection receipt",
    )
    parser.add_argument(
        "--host-performance-manifest",
        type=Path,
        help="Ignored manifest.json from a V3 host-performance audition; pair with --host-performance-decisions",
    )
    parser.add_argument(
        "--host-performance-decisions",
        type=Path,
        help="Local JSON array of human performance_id/host/disposition/controlled-rationale decisions",
    )
    parser.add_argument(
        "--host-performance-receipt-path",
        type=Path,
        default=HOST_PERFORMANCE_RECEIPT_PATH,
        help="Tracked redacted V3 performance receipt path (default: proof/2026-07-16-v3-host-performance.json)",
    )
    parser.add_argument(
        "--overwrite-host-performance-receipt",
        action="store_true",
        help="Allow replacing an existing reviewed host-performance receipt",
    )
    parser.add_argument(
        "--verify-host-performance-gate",
        action="store_true",
        help="Validate the tracked V3 receipt and require the complete approved Marco/Giulia comparison matrix",
    )
    args = parser.parse_args(argv)

    if bool(args.host_performance_manifest) != bool(args.host_performance_decisions):
        print(
            "ERROR: --host-performance-manifest and --host-performance-decisions must be used together",
            file=sys.stderr,
        )
        return 2
    if (args.selection_manifest or args.selection_decisions) and (
        args.host_performance_manifest or args.host_performance_decisions
    ):
        print("ERROR: selection and host-performance receipt modes cannot be combined", file=sys.stderr)
        return 2
    if bool(args.selection_manifest) != bool(args.selection_decisions):
        print("ERROR: --selection-manifest and --selection-decisions must be used together", file=sys.stderr)
        return 2
    if args.verify_host_performance_gate:
        if args.host_performance_manifest or args.host_performance_decisions:
            print("ERROR: receipt verification cannot be combined with receipt writing", file=sys.stderr)
            return 2
        try:
            load_host_performance_receipt(
                args.host_performance_receipt_path,
                require_approved_matrix=True,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Host-performance gate: approved ({args.host_performance_receipt_path})")
        return 0
    if args.host_performance_manifest and args.host_performance_decisions:
        try:
            receipt_path = write_host_performance_receipt_from_manifest(
                manifest_path=args.host_performance_manifest,
                decisions_path=args.host_performance_decisions,
                path=args.host_performance_receipt_path,
                overwrite=args.overwrite_host_performance_receipt,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Host-performance receipt: {receipt_path}")
        return 0
    if args.selection_manifest and args.selection_decisions:
        try:
            receipt_path = write_selection_receipt_from_manifest(
                manifest_path=args.selection_manifest,
                decisions_path=args.selection_decisions,
                path=args.selection_receipt_path,
                overwrite=args.overwrite_selection_receipt,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Selection receipt: {receipt_path}")
        return 0

    try:
        providers = expand_providers(args.providers)
        manual_voices = parse_manual_voice_specs(args.voice)
        stamp = _timestamp(args.timestamp)
        config = load_config(str(args.config))
        # Use the registry that load_config resolved from --config (sibling of
        # radio.toml), not a cwd-relative one — otherwise OpenAI auditions run
        # from another directory would fall back to the wrong/absent registry.
        # Mirrors mammamiradio.main.startup.
        configure_openai_tts_model(config.models.tts_model("openai"))
        if args.v3_host_performance:
            if args.include_catalog or args.no_configured or manual_voices or args.elevenlabs_stability:
                raise ValueError(
                    "--v3-host-performance cannot be combined with catalog, manual voices, "
                    "--no-configured, or stability sweeps"
                )
            if "elevenlabs" not in providers:
                raise ValueError("--v3-host-performance requires the elevenlabs provider")
            targets = build_v3_host_performance_targets(
                config,
                sample_text=args.sample_text or DEFAULT_V3_HOST_PERFORMANCE_TEXT,
            )
        else:
            targets = build_audition_targets(
                config,
                providers=providers,
                include_configured=not args.no_configured,
                include_catalog=args.include_catalog,
                manual_voices=manual_voices,
                sample_text=args.sample_text or DEFAULT_SAMPLE_TEXT,
            )
            targets = expand_stability_variants(targets, args.elevenlabs_stability)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not targets:
        print("ERROR: no voices matched the requested providers/scope", file=sys.stderr)
        return 2

    run_dir = args.output_dir / f"audition-{stamp}"
    results = asyncio.run(run_auditions(targets, run_dir, dry_run=args.dry_run, strict=args.strict))
    _print_summary(results, dry_run=args.dry_run, run_dir=None if args.dry_run else run_dir)

    if args.dry_run:
        for result in results:
            missing = f" missing={','.join(result.missing_env)}" if result.missing_env else ""
            performance = ""
            if result.provider == "elevenlabs":
                performance = (
                    f" model={result.elevenlabs_model} profile={result.delivery_profile} cue={result.delivery_cue}"
                )
            print(
                f"{result.status}\t{result.provider}\t{result.voice}\t{';'.join(result.used_by)}{performance}{missing}"
            )
        return 1 if any(result.status == STATUS_FAILED for result in results) else 0

    try:
        manifest_path = write_manifest(results, run_dir, config_path=args.config, timestamp=stamp)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Manifest: {manifest_path}")
    return 1 if any(result.status == STATUS_FAILED for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
