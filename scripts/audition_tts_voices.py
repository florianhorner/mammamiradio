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
import json
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from mammamiradio.audio import tts as tts_module
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
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")

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


def _add_target(targets: dict[tuple[str, str], VoiceAuditionTarget], target: VoiceAuditionTarget) -> None:
    key = (target.provider, target.voice)
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
    targets: dict[tuple[str, str], VoiceAuditionTarget] = {}

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
            ),
        )

    return list(targets.values())


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
    merged: dict[tuple[str, str], VoiceAuditionTarget] = {}
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
            target.text, target.voice, output_path, voice_settings=target.voice_settings
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
                VoiceAuditionResult(
                    provider=target.provider,
                    voice=target.voice,
                    label=target.label,
                    source=target.source,
                    used_by=target.used_by,
                    status=status,
                    missing_env=missing_env,
                    error="missing provider credentials" if missing_env else "",
                )
            )
            continue

        stability = target.voice_settings.get("stability") if target.voice_settings else None
        stab_suffix = f"-stab{round(stability * 100):02d}" if stability is not None else ""
        output_path = run_dir / f"{index:02d}-{target.provider}-{_slug(target.voice)}{stab_suffix}.mp3"
        if missing_env:
            results.append(
                VoiceAuditionResult(
                    provider=target.provider,
                    voice=target.voice,
                    label=target.label,
                    source=target.source,
                    used_by=target.used_by,
                    status=STATUS_FAILED if strict else STATUS_SKIPPED,
                    output_path=str(output_path),
                    missing_env=missing_env,
                    error="missing provider credentials",
                )
            )
            continue

        try:
            await _synthesize_target(target, output_path)
        except Exception as exc:
            results.append(
                VoiceAuditionResult(
                    provider=target.provider,
                    voice=target.voice,
                    label=target.label,
                    source=target.source,
                    used_by=target.used_by,
                    status=STATUS_FAILED,
                    output_path=str(output_path),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            results.append(
                VoiceAuditionResult(
                    provider=target.provider,
                    voice=target.voice,
                    label=target.label,
                    source=target.source,
                    used_by=target.used_by,
                    status=STATUS_GENERATED,
                    output_path=str(output_path),
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
        "results": [asdict(result) for result in results],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return manifest_path


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
    parser.add_argument("--sample-text", default=DEFAULT_SAMPLE_TEXT, help="Italian sample sentence for all auditions")
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
    args = parser.parse_args(argv)

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
        targets = build_audition_targets(
            config,
            providers=providers,
            include_configured=not args.no_configured,
            include_catalog=args.include_catalog,
            manual_voices=manual_voices,
            sample_text=args.sample_text,
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
            print(f"{result.status}\t{result.provider}\t{result.voice}\t{';'.join(result.used_by)}{missing}")
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
