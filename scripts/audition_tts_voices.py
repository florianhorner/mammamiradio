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
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
from mammamiradio.core.models import HostPersonality
from mammamiradio.hosts.fallbacks import (
    AD_BREAK_NORMAL_INTROS,
    AD_BREAK_NORMAL_OUTROS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "radio.toml"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "voice-auditions"
SELECTION_RECEIPT_PATH = REPO_ROOT / "proof" / "2026-07-13-voice-diversity-selection.json"
SELECTION_RECEIPT_SCHEMA_VERSION = 1
HOST_PERFORMANCE_RECEIPT_PATH = REPO_ROOT / "proof" / "2026-07-16-v3-host-performance.json"
HOST_PERFORMANCE_RECEIPT_SCHEMA_VERSION = 1
HOST_PERFORMANCE_RECEIPT_SHA256 = "f4da2b626e1d0b8d5af826d97bb3133d900362cfe1944f5c0f06c6277db13c6f"
HOST_CASTING_PROOF_PATH = REPO_ROOT / "proof" / "host-voice-casting-tests.txt"
HOST_CASTING_PROOF_SHA256 = "41b1f5afb710c43342e4bd304a3111946d09208c72cbde1e08294921cb2b74bc"
IDENTITY_RECALIBRATION_SCHEMA_VERSION = 2
IDENTITY_LISTENING_RECEIPT_SCHEMA_VERSION = 1
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

# Fixed, repository-owned copy for the casting recovery gate. These are the
# resolved full ident and active Normal Mode ad-wrapper lines, not invented
# audition prose. Keeping the words fixed isolates voice identity from copy and
# sound-design taste.
# This matches the resolved runtime value. Identity normalization capitalizes
# the sentence after the ellipsis even though the raw TOML uses lowercase.
IDENTITY_ANNOUNCER_TEXT = "Mamma Mi Radio... Da Windor a Vergen, la voce che non si spegne mai!"
IDENTITY_MARCO_TEXT = "And now... a word from our sponsors, amici!"
IDENTITY_GIULIA_TEXT = "Back to the music, finally — grazie for staying with us!"
IDENTITY_LISTENING_PROMPT = (
    "Voices: pass|fail; Wrong: Isabella|Marco|Giulia|none; Notes: <what does not sound like the station>"
)

IDENTITY_EXPECTED_ELEVENLABS_SETTINGS: dict[str, dict[str, object]] = {
    "Marco": {
        "similarity_boost": 0.78,
        "stability": 0.6,
        "style": 0.45,
        "use_speaker_boost": True,
    },
    "Giulia": {
        "similarity_boost": 0.78,
        "stability": 0.42,
        "style": 0.45,
        "use_speaker_boost": True,
    },
}
IDENTITY_EXPECTED_VOICE_IDS = {
    "Isabella": "it-IT-Isabella:DragonHDLatestNeural",
    "Marco": "o4b57JYAECRMJyCEXyIE",
    "Giulia": "fNmw8sukfGuvWVOp33Ge",
}
IDENTITY_ROLE_METADATA = {
    "identity-isabella": (
        "Isabella",
        "Station ID announcer",
        "resolved StationConfig.sonic_brand.full_ident from radio.toml",
    ),
    "identity-marco": (
        "Marco",
        "Deterministic Normal Mode ad-break intro casting check",
        "mammamiradio.hosts.fallbacks.AD_BREAK_NORMAL_INTROS",
    ),
    "identity-giulia": (
        "Giulia",
        "Deterministic Normal Mode ad-break outro casting check",
        "mammamiradio.hosts.fallbacks.AD_BREAK_NORMAL_OUTROS",
    ),
}
IDENTITY_SCOPE = "Dry voice identity only; no motif, imaging treatment, cadence, pack replacement, or runtime change."
IDENTITY_NEXT_STAGE = "Three sonic treatments remain blocked until all three voices pass."
IDENTITY_LOCAL_POSTPROCESS = "mammamiradio.audio.normalizer.normalize(loudnorm=True)"
IDENTITY_HOST_RECEIPT_SCOPE = (
    "Marco/Giulia voice ID, ElevenLabs V2 model, and neutral delivery only; settings are excluded."
)
IDENTITY_PROFILE_PROVENANCE_SCOPE = (
    "Supporting Sonos casting record; exact effective settings also come from the hash-bound config."
)
IDENTITY_PROVIDER_CONTRACT: dict[str, object] = {
    "direct_cloud_helpers": True,
    "fallback_permitted": False,
    "credentials_recorded": False,
}
IDENTITY_LISTENING_DECISION_FIELDS = frozenset({"pack_digest", "status", "wrong", "rationale"})
IDENTITY_LISTENING_STATUSES = frozenset({"approved", "rejected"})
IDENTITY_LISTENING_WRONG = frozenset({"none", "Isabella", "Marco", "Giulia"})
IDENTITY_LISTENING_ACCEPTED_RATIONALES = frozenset({"accepted_station_identity"})
IDENTITY_LISTENING_REJECTED_RATIONALES = frozenset(
    {
        "rejected_wrong_voice_identity",
        "rejected_off_brand_delivery",
        "rejected_unintelligible_delivery",
    }
)

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
                voice_settings=dict(getattr(host, "voice_settings", {}) or {}) or None,
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


def _identity_host(config: StationConfig, name: str) -> HostPersonality:
    matches = [host for host in config.hosts if host.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(f"Identity recalibration requires exactly one configured {name} host")
    return matches[0]


def build_identity_recalibration_targets(config: StationConfig) -> list[VoiceAuditionTarget]:
    """Return the three canonical, direct-cloud casting checks.

    This is deliberately narrower than a normal voice audition. The station
    announcer, Marco, and Giulia are the identities the listener must recognize;
    every route is validated before any provider receives copy.
    """
    sonic = config.sonic_brand
    announcer_provider = _canonical_provider(sonic.sweeper_engine or "")
    if announcer_provider != "azure":
        raise ValueError("Identity announcer must use the configured Azure route")
    if not sonic.sweeper_voice.strip():
        raise ValueError("Identity announcer requires a configured Azure voice")
    if sonic.sweeper_voice != IDENTITY_EXPECTED_VOICE_IDS["Isabella"]:
        raise ValueError("Identity announcer no longer uses the canonical Isabella voice")
    if config.super_italian_mode:
        raise ValueError("Identity recalibration is fixed to the active Normal Mode wrapper inventory")
    configured_ident = sonic.full_ident.strip()
    if configured_ident != IDENTITY_ANNOUNCER_TEXT:
        raise ValueError("Identity announcer copy must match the resolved StationConfig full ident")
    if IDENTITY_MARCO_TEXT not in AD_BREAK_NORMAL_INTROS or IDENTITY_GIULIA_TEXT not in AD_BREAK_NORMAL_OUTROS:
        raise ValueError("Identity host copy must remain in the active Normal Mode wrapper inventory")

    hosts = {name: _identity_host(config, name) for name in ("Marco", "Giulia")}
    for name, host in hosts.items():
        if _canonical_provider(host.engine or "") != "elevenlabs":
            raise ValueError(f"Identity host {name} must use ElevenLabs")
        if host.elevenlabs_model != ELEVENLABS_V2_MODEL:
            raise ValueError(f"Identity host {name} must use the accepted ElevenLabs V2 model")
        if not host.voice.strip():
            raise ValueError(f"Identity host {name} requires a configured ElevenLabs voice")
        if host.voice != IDENTITY_EXPECTED_VOICE_IDS[name]:
            raise ValueError(f"Identity host {name} no longer uses the canonical voice ID")

    marco = hosts["Marco"]
    giulia = hosts["Giulia"]
    return [
        VoiceAuditionTarget(
            provider="azure",
            voice=sonic.sweeper_voice,
            label="identity-isabella",
            source="identity-recalibration",
            used_by=("sonic_brand:full_ident",),
            text=configured_ident,
            edge_fallback_voice=sonic.sweeper_edge_fallback_voice,
            rate="+0%",
            pitch="+0Hz",
        ),
        VoiceAuditionTarget(
            provider="elevenlabs",
            voice=marco.voice,
            label="identity-marco",
            source="identity-recalibration",
            used_by=("host:Marco", "ad_wrapper:intro"),
            text=IDENTITY_MARCO_TEXT,
            edge_fallback_voice=marco.edge_fallback_voice,
            voice_settings=dict(marco.voice_settings or {}) or None,
            elevenlabs_model=marco.elevenlabs_model,
            delivery_profile=marco.delivery_profile,
        ),
        VoiceAuditionTarget(
            provider="elevenlabs",
            voice=giulia.voice,
            label="identity-giulia",
            source="identity-recalibration",
            used_by=("host:Giulia", "ad_wrapper:outro"),
            text=IDENTITY_GIULIA_TEXT,
            edge_fallback_voice=giulia.edge_fallback_voice,
            voice_settings=dict(giulia.voice_settings or {}) or None,
            elevenlabs_model=giulia.elevenlabs_model,
            delivery_profile=giulia.delivery_profile,
        ),
    ]


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


def _identity_output_path(run_dir: Path, index: int, target: VoiceAuditionTarget) -> Path:
    stability = target.voice_settings.get("stability") if target.voice_settings else None
    stab_suffix = f"-stab{round(stability * 100):02d}" if stability is not None else ""
    model_suffix = f"-{_slug(target.elevenlabs_model)}" if target.provider == "elevenlabs" else ""
    return run_dir / f"{index:02d}-{target.provider}-{_slug(target.voice)}{model_suffix}{stab_suffix}.mp3"


def _probe_identity_audio(path: Path, config: StationConfig) -> tuple[dict[str, object], float]:
    """Return verified final-format evidence for an identity clip.

    The identity gate is release input, so best-effort duration probing is not
    sufficient here. Fail closed unless ffprobe confirms the configured MP3
    sample rate, channel count, bitrate, and a positive finite duration.
    """
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
                str(path),
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
        streams = payload["streams"]
        stream = streams[0]
        format_payload = payload["format"]
        codec = str(stream["codec_name"])
        sample_rate_hz = int(stream["sample_rate"])
        channels = int(stream["channels"])
        bitrate_bps = int(stream.get("bit_rate") or format_payload["bit_rate"])
        duration_sec = float(format_payload["duration"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ffprobe returned incomplete audio evidence for {path.name}") from exc

    expected = {
        "codec": "mp3",
        "sample_rate_hz": config.audio.sample_rate,
        "channels": config.audio.channels,
        "bitrate_kbps": config.audio.bitrate,
    }
    actual = {
        "codec": codec,
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
        "bitrate_kbps": bitrate_bps // 1000,
    }
    if actual != expected or bitrate_bps != config.audio.bitrate * 1000:
        raise RuntimeError(f"Identity clip {path.name} has format {actual}, expected {expected}")
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise RuntimeError(f"Identity clip {path.name} has no positive finite duration")
    return actual, duration_sec


def _identity_audio_evidence(
    path: Path,
    config: StationConfig,
) -> tuple[str, float, dict[str, object]]:
    try:
        audio_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"Identity clip {path.name} could not be read") from exc
    audio_format, duration_sec = _probe_identity_audio(path, config)
    return audio_sha256, duration_sec, audio_format


async def _render_identity_targets_fail_fast(
    targets: Sequence[VoiceAuditionTarget],
    run_dir: Path,
    config: StationConfig,
) -> tuple[list[VoiceAuditionResult], dict[str, dict[str, object]]]:
    """Render direct-cloud identity clips, stopping at the first failed call."""
    results: list[VoiceAuditionResult] = []
    formats: dict[str, dict[str, object]] = {}
    for index, target in enumerate(targets, start=1):
        output_path = _identity_output_path(run_dir, index, target)
        try:
            rendered_path = await _synthesize_target(target, output_path)
            if rendered_path != output_path or rendered_path.is_symlink():
                raise RuntimeError("provider helper returned an unexpected output path")
            audio_sha256, duration_sec, audio_format = _identity_audio_evidence(rendered_path, config)
        except Exception as exc:
            raise RuntimeError(
                f"Identity recalibration failed closed at {target.label}: {type(exc).__name__}: {exc}"
            ) from exc
        result = _result_for_target(
            target,
            status=STATUS_GENERATED,
            output_path=str(rendered_path),
            audio_sha256=audio_sha256,
            audio_duration_seconds=duration_sec,
        )
        results.append(result)
        formats[target.label] = audio_format
    return results, formats


def _require_accepted_identity_hosts(
    targets: Sequence[VoiceAuditionTarget],
    receipt_path: Path,
    casting_proof_path: Path,
) -> tuple[str, str]:
    """Bind voice/model acceptance and separately documented V2 settings."""
    receipt = load_host_performance_receipt(receipt_path)
    raw_performances = receipt.get("performances")
    if not isinstance(raw_performances, list):  # pragma: no cover - receipt validator owns this
        raise ValueError("Host-performance receipt has no performances")

    by_label = {target.label: target for target in targets}
    for host_name in ("Marco", "Giulia"):
        target = by_label[f"identity-{host_name.casefold()}"]
        profile = _selection_profile_for_target(target)
        if profile.get("voice_settings") != IDENTITY_EXPECTED_ELEVENLABS_SETTINGS[host_name]:
            raise ValueError(f"Identity host {host_name} no longer uses the documented production voice profile")
        matches = [
            performance
            for performance in raw_performances
            if isinstance(performance, Mapping)
            and str(performance.get("host", "")).casefold() == host_name.casefold()
            and performance.get("voice_id") == target.voice
            and performance.get("model") == target.elevenlabs_model
            and performance.get("delivery_cue") == NEUTRAL_DELIVERY_CUE
            and performance.get("provider_result") == STATUS_GENERATED
            and performance.get("human_disposition") == "accepted"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Identity host {host_name} must match exactly one accepted ElevenLabs V2 performance receipt"
            )
    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    if receipt_sha256 != HOST_PERFORMANCE_RECEIPT_SHA256:
        raise ValueError("Accepted host voice/model receipt changed; re-audit casting provenance before rendering")
    casting_proof_sha256 = hashlib.sha256(casting_proof_path.read_bytes()).hexdigest()
    if casting_proof_sha256 != HOST_CASTING_PROOF_SHA256:
        raise ValueError("Identity host casting proof changed; re-audit profile provenance before rendering")
    return receipt_sha256, casting_proof_sha256


def _identity_route(target: VoiceAuditionTarget, result: VoiceAuditionResult) -> dict[str, object]:
    profile = result.profile or _selection_profile_for_target(target)
    model = str(profile["model"])
    if target.provider == "azure":
        settings: dict[str, object] = {
            "pitch": target.pitch or "+0Hz",
            "provider_output_format": "audio-24khz-160kbitrate-mono-mp3",
            "rate": target.rate or "+0%",
        }
    else:
        raw_settings = profile.get("voice_settings", {})
        settings = dict(raw_settings) if isinstance(raw_settings, Mapping) else {}
    route = {
        "provider": target.provider,
        "voice": target.voice,
        "model": model,
        "settings": settings,
    }
    return {
        "requested": route,
        "effective": dict(route),
        "fallback_used": False,
    }


def _identity_pack_digest(
    clips: Sequence[Mapping[str, object]],
    *,
    config_sha256: str,
    host_receipt_sha256: str,
    casting_proof_sha256: str,
) -> str:
    payload = {
        "clips": [
            {
                "audio_sha256": clip["audio_sha256"],
                "character": clip["character"],
                "copy_source": clip["copy_source"],
                "duration_sec": clip["duration_sec"],
                "format": clip["format"],
                "id": clip["id"],
                "local_postprocess": clip["local_postprocess"],
                "path": clip["path"],
                "role": clip["role"],
                "route": clip["route"],
                "text": clip["text"],
                "text_sha256": clip["text_sha256"],
            }
            for clip in clips
        ],
        "casting_proof_sha256": casting_proof_sha256,
        "config_sha256": config_sha256,
        "host_receipt_sha256": host_receipt_sha256,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identity_manifest(
    targets: Sequence[VoiceAuditionTarget],
    results: Sequence[VoiceAuditionResult],
    audio_formats: Mapping[str, Mapping[str, object]],
    *,
    config_path: Path,
    receipt_path: Path,
    casting_proof_path: Path,
    timestamp: str,
    host_receipt_sha256: str,
    casting_proof_sha256: str,
) -> dict[str, object]:
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    result_by_label = {result.label: result for result in results}
    clips: list[dict[str, object]] = []
    for target in targets:
        result = result_by_label[target.label]
        character, role, copy_source = IDENTITY_ROLE_METADATA[target.label]
        clips.append(
            {
                "id": target.label,
                "character": character,
                "role": role,
                "copy_source": copy_source,
                "text": target.text,
                "text_sha256": result.clean_text_sha256,
                "path": Path(result.output_path).name,
                "audio_sha256": result.audio_sha256,
                "duration_sec": result.audio_duration_seconds,
                "format": dict(audio_formats[target.label]),
                "route": _identity_route(target, result),
                "local_postprocess": IDENTITY_LOCAL_POSTPROCESS,
            }
        )
    pack_digest = _identity_pack_digest(
        clips,
        config_sha256=config_sha256,
        host_receipt_sha256=host_receipt_sha256,
        casting_proof_sha256=casting_proof_sha256,
    )
    return {
        "schema_version": IDENTITY_RECALIBRATION_SCHEMA_VERSION,
        "stage": "voice-identity-recalibration",
        "generated_at": timestamp,
        "release_ready": False,
        "scope": IDENTITY_SCOPE,
        "active_spoken_mode": "normal",
        "config": {"path": str(config_path), "sha256": config_sha256},
        "accepted_host_voice_model_receipt": {
            "path": str(receipt_path),
            "sha256": host_receipt_sha256,
            "scope": IDENTITY_HOST_RECEIPT_SCOPE,
        },
        "host_profile_provenance": {
            "path": str(casting_proof_path),
            "sha256": casting_proof_sha256,
            "scope": IDENTITY_PROFILE_PROVENANCE_SCOPE,
        },
        "provider_contract": dict(IDENTITY_PROVIDER_CONTRACT),
        "clips": clips,
        "pack_digest": pack_digest,
        "listening_receipt": {
            "schema_version": IDENTITY_LISTENING_RECEIPT_SCHEMA_VERSION,
            "status": "pending",
            "pack_digest": pack_digest,
            "target": "Mac",
            "prompt": IDENTITY_LISTENING_PROMPT,
            "wrong": None,
            "rationale": None,
            "reviewed_at": None,
        },
        "next_stage": IDENTITY_NEXT_STAGE,
    }


def _identity_readme(manifest: Mapping[str, object]) -> str:
    clips = manifest["clips"]
    assert isinstance(clips, list)
    rows = "\n".join(
        f"- **{clip['character']} — {clip['role']}**: [`{clip['path']}`](./{clip['path']})"
        for clip in clips
        if isinstance(clip, Mapping)
    )
    return f"""# Mamma Mi Radio voice identity recalibration

Status: **voice approval pending**. This board intentionally contains no music, motif, radio texture,
or ad production. It isolates the three configured production identities before another sound-design pass.

{rows}

- [Open the listening board](./index.html)
- [Inspect the exact provider and hash manifest](./manifest.json)

Listen once on the Mac and answer exactly:

> {IDENTITY_LISTENING_PROMPT}

Pack digest: `{manifest["pack_digest"]}`

The three sonic treatments remain blocked until these voices are recognizable as the station.
"""


def _identity_html(manifest: Mapping[str, object]) -> str:
    raw_clips = manifest["clips"]
    assert isinstance(raw_clips, list)
    cards: list[str] = []
    for raw_clip in raw_clips:
        assert isinstance(raw_clip, Mapping)
        route = raw_clip["route"]
        assert isinstance(route, Mapping)
        effective = route["effective"]
        assert isinstance(effective, Mapping)
        cards.append(
            f"""
<article>
  <p class="role">{html.escape(str(raw_clip["role"]))}</p>
  <h2>{html.escape(str(raw_clip["character"]))}</h2>
  <p class="route">{html.escape(str(effective["provider"]))} · {html.escape(str(effective["model"]))}</p>
  <blockquote>{html.escape(str(raw_clip["text"]))}</blockquote>
  <audio controls preload="none" src="{html.escape(str(raw_clip["path"]))}"></audio>
</article>"""
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Mamma Mi Radio — voice identity check</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui, sans-serif; background:#14110f; color:#f5edd8; }}
    body {{ max-width:760px; margin:0 auto; padding:32px 18px 56px; }}
    h1 {{ font-family:Georgia,serif; font-size:clamp(2rem,7vw,3.5rem); margin-bottom:.4rem; }}
    .warning {{ color:#f4d048; font-weight:700; }}
    article {{ background:#251e19; border:1px solid #514335; border-radius:16px; padding:20px; margin:18px 0; }}
    h2 {{ margin:.15rem 0; font-size:1.65rem; }}
    .role,.route {{ margin:.2rem 0; color:#cdbfa8; }}
    blockquote {{ margin:18px 0; padding-left:14px; border-left:3px solid #b82c20; font-style:italic; }}
    audio {{ width:100%; }}
    code {{ color:#f4d048; }}
  </style>
</head>
<body>
  <p class="warning">Casting only — no sound treatment in this gate.</p>
  <h1>Do these sound like your station?</h1>
  <p>These are direct renders from the configured production routes. No fallback is permitted.</p>
  {"".join(cards)}
  <h2>Decision</h2>
  <p>Reply: <code>{html.escape(IDENTITY_LISTENING_PROMPT)}</code></p>
  <p>Pack digest: <code>{manifest["pack_digest"]}</code></p>
</body>
</html>
"""


async def render_identity_recalibration(
    output_root: Path,
    *,
    config_path: Path,
    timestamp: str,
    receipt_path: Path = HOST_PERFORMANCE_RECEIPT_PATH,
    casting_proof_path: Path = HOST_CASTING_PROOF_PATH,
) -> tuple[Path, dict[str, object]]:
    """Render and publish the direct-cloud three-voice gate without fallback."""
    timestamp = _timestamp(timestamp)
    canonical_sources = (
        (config_path, DEFAULT_CONFIG_PATH, "config"),
        (receipt_path, HOST_PERFORMANCE_RECEIPT_PATH, "accepted host receipt"),
        (casting_proof_path, HOST_CASTING_PROOF_PATH, "host casting proof"),
    )
    for supplied_path, canonical_path, label in canonical_sources:
        if supplied_path.is_symlink() or supplied_path.resolve() != canonical_path.resolve():
            raise ValueError(f"Identity recalibration requires the canonical repository {label}")
    config_path = config_path.resolve()
    receipt_path = receipt_path.resolve()
    casting_proof_path = casting_proof_path.resolve()
    config = load_config(str(config_path))
    targets = build_identity_recalibration_targets(config)
    host_receipt_sha256, casting_proof_sha256 = _require_accepted_identity_hosts(
        targets,
        receipt_path,
        casting_proof_path,
    )
    missing = sorted(
        {variable for target in targets for variable in missing_env_for_provider(target.provider, os.environ)}
    )
    if missing:
        raise ValueError(f"Identity recalibration is missing provider credentials: {', '.join(missing)}")

    run_dir = output_root / f"identity-gate-{timestamp}"
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        run_dir.mkdir()
    except FileExistsError:
        raise FileExistsError(f"Refusing to overwrite existing identity gate: {run_dir}") from None
    staging_dir: Path | None = None
    published = False
    try:
        staging_dir = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.", dir=output_root))
        results, audio_formats = await _render_identity_targets_fail_fast(targets, staging_dir, config)
        manifest = _identity_manifest(
            targets,
            results,
            audio_formats,
            config_path=config_path,
            receipt_path=receipt_path,
            casting_proof_path=casting_proof_path,
            timestamp=timestamp,
            host_receipt_sha256=host_receipt_sha256,
            casting_proof_sha256=casting_proof_sha256,
        )
        (staging_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging_dir / "README.md").write_text(_identity_readme(manifest), encoding="utf-8")
        (staging_dir / "index.html").write_text(_identity_html(manifest), encoding="utf-8")
        # The empty run directory was claimed before the first paid call. Move
        # audio and human-facing files first, then publish the manifest and a
        # digest marker last so consumers never treat a partial board as ready.
        for child in sorted(staging_dir.iterdir(), key=lambda path: (path.name == "manifest.json", path.name)):
            child.replace(run_dir / child.name)
        _validate_identity_board(run_dir / "manifest.json", require_ready=False)
        (run_dir / ".ready").write_text(str(manifest["pack_digest"]) + "\n", encoding="ascii")
        published = True
        return run_dir, manifest
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not published:
            shutil.rmtree(run_dir, ignore_errors=True)


def _identity_manifest_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _identity_exact_fields(value: Mapping[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected:
        detail = sorted(expected.symmetric_difference(value))
        raise ValueError(f"{field} has an invalid field set: {', '.join(detail)}")


def _identity_source_evidence(value: object, field: str) -> tuple[Path, str]:
    evidence = _identity_manifest_mapping(value, field)
    path_value = evidence.get("path")
    digest = evidence.get("sha256")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"{field}.path must be a non-empty string")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise ValueError(f"{field}.sha256 must be a lowercase SHA-256 digest")
    path = Path(path_value)
    if not path.is_absolute():
        raise ValueError(f"{field}.path must be absolute")
    if path.is_symlink():
        raise ValueError(f"{field}.path must not be a symlink")
    try:
        current_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"{field}.path is not readable") from exc
    if current_digest != digest:
        raise ValueError(f"{field} is stale: current file hash differs from the board")
    return path, digest


def _validate_identity_receipt(receipt: object, *, pack_digest: str) -> Mapping[str, object]:
    value = _identity_manifest_mapping(receipt, "listening_receipt")
    required = {
        "schema_version",
        "status",
        "pack_digest",
        "target",
        "prompt",
        "wrong",
        "rationale",
        "reviewed_at",
    }
    if set(value) != required:
        detail = sorted(required.symmetric_difference(value))
        raise ValueError(f"listening_receipt has an invalid field set: {', '.join(detail)}")
    if value["schema_version"] != IDENTITY_LISTENING_RECEIPT_SCHEMA_VERSION:
        raise ValueError("listening_receipt.schema_version is unsupported")
    if value["pack_digest"] != pack_digest:
        raise ValueError("listening_receipt.pack_digest does not match the identity board")
    if value["target"] != "Mac" or value["prompt"] != IDENTITY_LISTENING_PROMPT:
        raise ValueError("listening_receipt does not describe the Mac voice-identity gate")

    status = value["status"]
    if status == "pending":
        if any(value[field] is not None for field in ("wrong", "rationale", "reviewed_at")):
            raise ValueError("pending listening_receipt must not contain a decision")
        return value
    if status not in IDENTITY_LISTENING_STATUSES:
        raise ValueError("listening_receipt.status must be pending, approved, or rejected")
    wrong = value["wrong"]
    rationale = value["rationale"]
    reviewed_at = value["reviewed_at"]
    if wrong not in IDENTITY_LISTENING_WRONG:
        raise ValueError("listening_receipt.wrong must name one auditioned voice or none")
    allowed_rationales = (
        IDENTITY_LISTENING_ACCEPTED_RATIONALES if status == "approved" else IDENTITY_LISTENING_REJECTED_RATIONALES
    )
    if rationale not in allowed_rationales:
        raise ValueError("listening_receipt.rationale is not valid for its status")
    if status == "approved" and wrong != "none":
        raise ValueError("an approved listening_receipt must set wrong to none")
    if status == "rejected" and wrong == "none":
        raise ValueError("a rejected listening_receipt must identify the wrong voice")
    if not isinstance(reviewed_at, str):
        raise ValueError("listening_receipt.reviewed_at must be a timezone-aware timestamp")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("listening_receipt.reviewed_at must be an ISO-8601 timestamp") from exc
    if parsed_reviewed_at.tzinfo is None:
        raise ValueError("listening_receipt.reviewed_at must include a timezone")
    return value


def _validate_identity_board(
    manifest_path: Path,
    *,
    require_ready: bool = True,
) -> tuple[dict[str, object], tuple[Path, ...]]:
    """Verify a published identity board against its audio and source lineage."""
    if manifest_path.is_symlink():
        raise ValueError("Identity manifest must not be a symlink")
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Identity manifest is not readable JSON: {manifest_path}") from exc
    if not isinstance(manifest_value, dict):
        raise ValueError("Identity manifest must be an object")
    manifest: dict[str, object] = manifest_value
    _identity_exact_fields(
        manifest,
        {
            "schema_version",
            "stage",
            "generated_at",
            "release_ready",
            "scope",
            "active_spoken_mode",
            "config",
            "accepted_host_voice_model_receipt",
            "host_profile_provenance",
            "provider_contract",
            "clips",
            "pack_digest",
            "listening_receipt",
            "next_stage",
        },
        "identity manifest",
    )
    if manifest.get("schema_version") != IDENTITY_RECALIBRATION_SCHEMA_VERSION:
        raise ValueError("Identity manifest schema_version is unsupported")
    if manifest.get("stage") != "voice-identity-recalibration":
        raise ValueError("Identity manifest has the wrong stage")
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("Identity manifest generated_at must be a timestamp")
    _timestamp(generated_at)
    if manifest.get("release_ready") is not False or manifest.get("active_spoken_mode") != "normal":
        raise ValueError("Identity manifest must remain a non-release Normal Mode gate")
    if manifest.get("scope") != IDENTITY_SCOPE or manifest.get("next_stage") != IDENTITY_NEXT_STAGE:
        raise ValueError("Identity manifest scope or stage boundary was altered")
    provider_contract = _identity_manifest_mapping(manifest.get("provider_contract"), "provider_contract")
    _identity_exact_fields(provider_contract, set(IDENTITY_PROVIDER_CONTRACT), "provider_contract")
    if provider_contract != IDENTITY_PROVIDER_CONTRACT:
        raise ValueError("Identity manifest provider contract was altered")

    config_evidence = _identity_manifest_mapping(manifest.get("config"), "config")
    _identity_exact_fields(config_evidence, {"path", "sha256"}, "config")
    config_path, config_sha256 = _identity_source_evidence(config_evidence, "config")
    if config_path.resolve() != DEFAULT_CONFIG_PATH.resolve():
        raise ValueError("Identity manifest does not bind the canonical repository config")
    receipt_evidence = _identity_manifest_mapping(
        manifest.get("accepted_host_voice_model_receipt"),
        "accepted_host_voice_model_receipt",
    )
    _identity_exact_fields(
        receipt_evidence,
        {"path", "sha256", "scope"},
        "accepted_host_voice_model_receipt",
    )
    if receipt_evidence.get("scope") != IDENTITY_HOST_RECEIPT_SCOPE:
        raise ValueError("Identity manifest overstates the accepted host receipt scope")
    receipt_path, host_receipt_sha256 = _identity_source_evidence(
        receipt_evidence,
        "accepted_host_voice_model_receipt",
    )
    if receipt_path.resolve() != HOST_PERFORMANCE_RECEIPT_PATH.resolve():
        raise ValueError("Identity manifest does not bind the canonical accepted host receipt")
    profile_evidence = _identity_manifest_mapping(
        manifest.get("host_profile_provenance"),
        "host_profile_provenance",
    )
    _identity_exact_fields(profile_evidence, {"path", "sha256", "scope"}, "host_profile_provenance")
    if profile_evidence.get("scope") != IDENTITY_PROFILE_PROVENANCE_SCOPE:
        raise ValueError("Identity manifest host profile provenance scope was altered")
    casting_proof_path, casting_proof_sha256 = _identity_source_evidence(
        profile_evidence,
        "host_profile_provenance",
    )
    if casting_proof_path.resolve() != HOST_CASTING_PROOF_PATH.resolve():
        raise ValueError("Identity manifest does not bind the canonical host casting proof")
    config = load_config(str(config_path))
    targets = build_identity_recalibration_targets(config)
    accepted_hash, profile_hash = _require_accepted_identity_hosts(targets, receipt_path, casting_proof_path)
    if accepted_hash != host_receipt_sha256 or profile_hash != casting_proof_sha256:
        raise ValueError("Identity manifest provenance no longer matches its validated sources")

    raw_clips = manifest.get("clips")
    if not isinstance(raw_clips, list) or len(raw_clips) != 3:
        raise ValueError("Identity manifest must contain exactly three clips")
    target_by_id = {target.label: target for target in targets}
    expected_ids = set(target_by_id)
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    seen_audio_hashes: set[str] = set()
    clip_paths: list[Path] = []
    clips: list[Mapping[str, object]] = []
    for index, raw_clip in enumerate(raw_clips):
        clip = _identity_manifest_mapping(raw_clip, f"clips[{index}]")
        _identity_exact_fields(
            clip,
            {
                "id",
                "character",
                "role",
                "copy_source",
                "text",
                "text_sha256",
                "path",
                "audio_sha256",
                "duration_sec",
                "format",
                "route",
                "local_postprocess",
            },
            f"clips[{index}]",
        )
        clip_id = clip.get("id")
        if not isinstance(clip_id, str) or clip_id not in expected_ids or clip_id in seen_ids:
            raise ValueError(f"clips[{index}].id is missing, duplicated, or unexpected")
        seen_ids.add(clip_id)
        target = target_by_id[clip_id]
        character, role, copy_source = IDENTITY_ROLE_METADATA[clip_id]
        if (
            clip.get("character") != character
            or clip.get("role") != role
            or clip.get("copy_source") != copy_source
            or clip.get("local_postprocess") != IDENTITY_LOCAL_POSTPROCESS
        ):
            raise ValueError(f"Identity clip {clip_id} display or provenance metadata was altered")
        path_value = clip.get("path")
        if (
            not isinstance(path_value, str)
            or Path(path_value).name != path_value
            or not path_value.endswith(".mp3")
            or path_value in seen_paths
        ):
            raise ValueError(f"clips[{index}].path must be a unique local MP3 filename")
        seen_paths.add(path_value)
        clip_path = manifest_path.parent / path_value
        if clip_path.is_symlink() or not clip_path.is_file():
            raise ValueError(f"Identity clip is missing: {path_value}")
        text_value = clip.get("text")
        if text_value != target.text or clip.get("text_sha256") != _text_sha256(target.text):
            raise ValueError(f"Identity clip {clip_id} no longer has its fixed runtime copy")
        expected_route = _identity_route(target, _result_for_target(target, status=STATUS_GENERATED))
        if clip.get("route") != expected_route:
            raise ValueError(f"Identity clip {clip_id} route differs from the loaded production config")
        audio_sha256, duration_sec, audio_format = _identity_audio_evidence(clip_path, config)
        if clip.get("audio_sha256") != audio_sha256:
            raise ValueError(f"Identity clip {clip_id} audio hash differs from the manifest")
        if audio_sha256 in seen_audio_hashes:
            raise ValueError("Identity clips must not contain exact duplicate audio")
        seen_audio_hashes.add(audio_sha256)
        if clip.get("format") != audio_format:
            raise ValueError(f"Identity clip {clip_id} format differs from verified ffprobe evidence")
        recorded_duration = clip.get("duration_sec")
        if (
            isinstance(recorded_duration, bool)
            or not isinstance(recorded_duration, int | float)
            or not math.isclose(float(recorded_duration), duration_sec, abs_tol=0.001)
        ):
            raise ValueError(f"Identity clip {clip_id} duration differs from verified ffprobe evidence")
        clips.append(clip)
        clip_paths.append(clip_path)
    if seen_ids != expected_ids:
        raise ValueError("Identity manifest is missing a required voice")
    board_mp3_names = {path.name for path in manifest_path.parent.iterdir() if path.suffix.casefold() == ".mp3"}
    if board_mp3_names != seen_paths:
        raise ValueError("Identity board contains undeclared or missing MP3 files")

    expected_pack_digest = _identity_pack_digest(
        clips,
        config_sha256=config_sha256,
        host_receipt_sha256=host_receipt_sha256,
        casting_proof_sha256=casting_proof_sha256,
    )
    pack_digest = manifest.get("pack_digest")
    if pack_digest != expected_pack_digest:
        raise ValueError("Identity manifest pack_digest is stale or invalid")
    _validate_identity_receipt(manifest.get("listening_receipt"), pack_digest=expected_pack_digest)
    expected_surfaces = {
        "README.md": _identity_readme(manifest),
        "index.html": _identity_html(manifest),
    }
    for filename, expected_text in expected_surfaces.items():
        surface_path = manifest_path.parent / filename
        if surface_path.is_symlink():
            raise ValueError(f"Identity listening surface must not be a symlink: {filename}")
        try:
            actual_text = surface_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Identity listening surface is missing: {filename}") from exc
        if actual_text != expected_text:
            raise ValueError(f"Identity listening surface differs from the validated manifest: {filename}")
    if require_ready:
        ready_path = manifest_path.parent / ".ready"
        if ready_path.is_symlink():
            raise ValueError("Identity board ready marker must not be a symlink")
        try:
            ready_digest = ready_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ValueError("Identity board has no ready publication marker") from exc
        if ready_digest != pack_digest:
            raise ValueError("Identity board ready marker does not match pack_digest")
    return manifest, tuple(clip_paths)


def write_identity_listening_receipt_from_decision(
    *,
    manifest_path: Path,
    decision_path: Path,
    reviewed_at: str | None = None,
) -> Path:
    """Record one human decision under an exclusive per-board lock."""
    lock_path = manifest_path.parent / ".listening-receipt.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Identity listening receipt is already being decided") from None
    try:
        original_manifest_bytes = manifest_path.read_bytes()
        manifest, _clip_paths = _validate_identity_board(manifest_path)
        current_receipt = _identity_manifest_mapping(manifest.get("listening_receipt"), "listening_receipt")
        if current_receipt.get("status") != "pending":
            raise ValueError("Identity listening receipt has already been decided")
        try:
            decision_value = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Identity decision is not readable JSON: {decision_path}") from exc
        decision = _identity_manifest_mapping(decision_value, "identity decision")
        if set(decision) != IDENTITY_LISTENING_DECISION_FIELDS:
            detail = sorted(IDENTITY_LISTENING_DECISION_FIELDS.symmetric_difference(decision))
            raise ValueError(f"identity decision has an invalid field set: {', '.join(detail)}")
        pack_digest = manifest["pack_digest"]
        if decision.get("pack_digest") != pack_digest:
            raise ValueError("identity decision pack_digest does not match the current board")
        receipt: dict[str, object] = {
            "schema_version": IDENTITY_LISTENING_RECEIPT_SCHEMA_VERSION,
            "status": decision.get("status"),
            "pack_digest": pack_digest,
            "target": "Mac",
            "prompt": IDENTITY_LISTENING_PROMPT,
            "wrong": decision.get("wrong"),
            "rationale": decision.get("rationale"),
            "reviewed_at": reviewed_at or datetime.now(UTC).isoformat(),
        }
        _validate_identity_receipt(receipt, pack_digest=str(pack_digest))

        # Revalidate immediately before the compare-and-swap. The lock prevents
        # another receipt writer, while the byte comparison catches unrelated
        # edits that occurred during human-decision parsing.
        _validate_identity_board(manifest_path)
        if manifest_path.read_bytes() != original_manifest_bytes:
            raise ValueError("Identity manifest changed while its decision was being recorded")
        manifest["listening_receipt"] = receipt
        temporary_path = manifest_path.with_name(f".{manifest_path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(manifest_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
    return manifest_path


def assert_identity_listening_gate(manifest_path: Path) -> tuple[Path, ...]:
    """Return exact approved clips or fail before a downstream treatment build."""
    manifest, clip_paths = _validate_identity_board(manifest_path)
    receipt = _identity_manifest_mapping(manifest.get("listening_receipt"), "listening_receipt")
    if receipt.get("status") != "approved":
        raise ValueError("Identity listening gate is not approved for this pack digest")
    return clip_paths


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
        "--identity-recalibration",
        action="store_true",
        help="Render the fail-closed Isabella/Marco/Giulia dry-voice recovery gate",
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
    parser.add_argument(
        "--identity-listening-manifest",
        type=Path,
        help="Published identity manifest to decide or verify",
    )
    parser.add_argument(
        "--identity-listening-decision",
        type=Path,
        help="Human pack_digest/status/wrong/rationale JSON used to decide an identity manifest once",
    )
    parser.add_argument(
        "--verify-identity-listening-gate",
        action="store_true",
        help="Require a digest-current approved identity listening receipt without calling a provider",
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
    identity_receipt_mode = bool(
        args.identity_listening_manifest or args.identity_listening_decision or args.verify_identity_listening_gate
    )
    if identity_receipt_mode and (
        args.providers != ["all"]
        or args.include_catalog
        or args.no_configured
        or args.voice is not None
        or args.sample_text is not None
        or args.v3_host_performance
        or args.elevenlabs_stability is not None
        or args.config != DEFAULT_CONFIG_PATH
        or args.output_dir != DEFAULT_OUTPUT_ROOT
        or args.timestamp is not None
        or args.dry_run
        or args.strict
        or args.selection_receipt_path != SELECTION_RECEIPT_PATH
        or args.overwrite_selection_receipt
        or args.host_performance_receipt_path != HOST_PERFORMANCE_RECEIPT_PATH
        or args.overwrite_host_performance_receipt
    ):
        print("ERROR: identity listening receipt mode does not accept audition or output options", file=sys.stderr)
        return 2
    if identity_receipt_mode and (
        args.selection_manifest
        or args.selection_decisions
        or args.host_performance_manifest
        or args.host_performance_decisions
        or args.verify_host_performance_gate
        or args.identity_recalibration
    ):
        print("ERROR: identity listening receipt mode cannot be combined with other audition modes", file=sys.stderr)
        return 2
    if args.identity_recalibration and (
        args.selection_manifest
        or args.selection_decisions
        or args.host_performance_manifest
        or args.host_performance_decisions
        or args.verify_host_performance_gate
    ):
        print("ERROR: identity recalibration cannot be combined with receipt modes", file=sys.stderr)
        return 2
    if bool(args.selection_manifest) != bool(args.selection_decisions):
        print("ERROR: --selection-manifest and --selection-decisions must be used together", file=sys.stderr)
        return 2
    if args.identity_listening_decision and not args.identity_listening_manifest:
        print("ERROR: --identity-listening-decision requires --identity-listening-manifest", file=sys.stderr)
        return 2
    if args.verify_identity_listening_gate and not args.identity_listening_manifest:
        print("ERROR: --verify-identity-listening-gate requires --identity-listening-manifest", file=sys.stderr)
        return 2
    if args.verify_identity_listening_gate and args.identity_listening_decision:
        print("ERROR: identity listening decision and verification cannot be combined", file=sys.stderr)
        return 2
    if args.identity_listening_manifest and not (
        args.identity_listening_decision or args.verify_identity_listening_gate
    ):
        print("ERROR: identity listening manifest requires a decision or verification action", file=sys.stderr)
        return 2
    if args.identity_listening_manifest and args.identity_listening_decision:
        try:
            receipt_manifest_path = write_identity_listening_receipt_from_decision(
                manifest_path=args.identity_listening_manifest,
                decision_path=args.identity_listening_decision,
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Identity listening receipt: {receipt_manifest_path}")
        return 0
    if args.verify_identity_listening_gate:
        assert args.identity_listening_manifest is not None
        try:
            assert_identity_listening_gate(args.identity_listening_manifest)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Identity listening gate: approved ({args.identity_listening_manifest})")
        return 0
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

    if args.identity_recalibration:
        if (
            args.v3_host_performance
            or args.include_catalog
            or args.no_configured
            or args.voice is not None
            or args.elevenlabs_stability is not None
            or args.sample_text is not None
            or args.dry_run
            or args.strict
            or args.overwrite_selection_receipt
            or args.overwrite_host_performance_receipt
            or args.providers != ["all"]
        ):
            print(
                "ERROR: --identity-recalibration uses fixed production routes and copy; only "
                "--config, --output-dir, --timestamp, and --host-performance-receipt-path are allowed",
                file=sys.stderr,
            )
            return 2
        try:
            stamp = _timestamp(args.timestamp)
            run_dir, manifest = asyncio.run(
                render_identity_recalibration(
                    args.output_dir,
                    config_path=args.config,
                    timestamp=stamp,
                    receipt_path=args.host_performance_receipt_path,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Identity gate: {run_dir}")
        print(f"Manifest: {run_dir / 'manifest.json'}")
        print(f"Listening page: {run_dir / 'index.html'}")
        print(f"Pack digest: {manifest['pack_digest']}")
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
    except (OSError, RuntimeError, ValueError) as exc:
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
