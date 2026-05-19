#!/usr/bin/env python3
"""Static HA Green viability gate for TTS provider candidates.

This script does not add runtime TTS backends and does not call live provider
APIs. It records whether a provider is deployable from the Home Assistant add-on
on HA Green-class hardware: aarch64 CPU, no GPU, constrained RAM/storage, and a
stream that must keep playing even when synthesis fails.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tmp" / "evals" / "tts"
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")

STATUS_PASS = "pass"
STATUS_CONDITIONAL = "conditional"
STATUS_FAIL = "fail"

KIND_CLOUD = "cloud_client"
KIND_LOCAL_CPU = "local_cpu_candidate"
KIND_REJECTED_LOCAL = "local_inference_rejected"

DEFAULT_PROVIDERS = [
    "edge",
    "openai",
    "azure",
    "elevenlabs",
    "kokoro",
    "piper",
    "f5tts",
    "bark_suno",
]

_ALIASES = {
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
    "kokoro": "kokoro",
    "piper": "piper",
    "f5tts": "f5tts",
    "f5-tts": "f5tts",
    "f5_tts": "f5tts",
    "bark": "bark_suno",
    "suno": "bark_suno",
    "bark-suno": "bark_suno",
    "bark_suno": "bark_suno",
    "bark/suno": "bark_suno",
}


@dataclass(frozen=True)
class ProviderViability:
    provider: str
    kind: str
    ha_green_status: str
    reason: str
    required_env: list[str]
    required_packages: list[str]
    aarch64_image_risk: str
    runtime_risk: str
    expected_latency: str
    operator_cost: str


_PROVIDER_MATRIX: dict[str, ProviderViability] = {
    "edge": ProviderViability(
        provider="edge",
        kind=KIND_CLOUD,
        ha_green_status=STATUS_PASS,
        reason=(
            "Already the default add-on TTS path. HA Green runs a lightweight "
            "Edge client plus FFmpeg normalization; no local model or GPU is required."
        ),
        required_env=[],
        required_packages=["edge-tts", "ffmpeg"],
        aarch64_image_risk="low: dependency is already installed by the add-on image",
        runtime_risk="medium: depends on outbound Microsoft websocket access and has no SLA",
        expected_latency="current runtime baseline; covered by existing queue and canned-clip fallbacks",
        operator_cost="free, no account",
    ),
    "openai": ProviderViability(
        provider="openai",
        kind=KIND_CLOUD,
        ha_green_status=STATUS_CONDITIONAL,
        reason=(
            "Existing optional runtime backend. HA Green only performs an HTTPS "
            "API call and FFmpeg normalization, but the operator must provide a key."
        ),
        required_env=["OPENAI_API_KEY"],
        required_packages=["openai", "ffmpeg"],
        aarch64_image_risk="low: OpenAI client is already in pyproject and pure client-side work",
        runtime_risk="low-medium: paid API/network failures fall back to Edge and bundled clips",
        expected_latency="cloud latency; acceptable only with async queue lookahead and fallback",
        operator_cost="paid API usage",
    ),
    "azure": ProviderViability(
        provider="azure",
        kind=KIND_CLOUD,
        ha_green_status=STATUS_CONDITIONAL,
        reason=(
            "Cloud client can be HA Green-safe in principle, but there is no "
            "runtime backend yet and it requires Azure credentials."
        ),
        required_env=["AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"],
        required_packages=["httpx or azure speech client", "ffmpeg"],
        aarch64_image_risk="low-medium: prefer REST/httpx to avoid native SDK packaging risk",
        runtime_risk="medium: account friction, network dependency, and unproven Italian expressiveness",
        expected_latency="cloud latency; must not block first audio or stream recovery",
        operator_cost="paid or free-tier limited Azure usage",
    ),
    "elevenlabs": ProviderViability(
        provider="elevenlabs",
        kind=KIND_CLOUD,
        ha_green_status=STATUS_CONDITIONAL,
        reason=(
            "Expressive cloud TTS keeps inference off HA Green, but it requires "
            "a paid/keyed service and no runtime backend exists yet."
        ),
        required_env=["ELEVENLABS_API_KEY"],
        required_packages=["elevenlabs client or httpx", "ffmpeg"],
        aarch64_image_risk="low-medium: keep as REST/httpx or optional client to avoid base image bloat",
        runtime_risk="medium: quota/cost limits are a poor fit for always-on radio without strict fallback",
        expected_latency="cloud latency; must be measured against queue lookahead",
        operator_cost="paid service; free tier is not enough for continuous radio",
    ),
    "kokoro": ProviderViability(
        provider="kokoro",
        kind=KIND_LOCAL_CPU,
        ha_green_status=STATUS_CONDITIONAL,
        reason=(
            "Promising local/offline CPU candidate only if an aarch64-compatible "
            "runtime and pre-provisioned model are available without startup downloads."
        ),
        required_env=["KOKORO_MODEL_PATH"],
        required_packages=["kokoro CPU runtime", "onnxruntime aarch64 or equivalent", "ffmpeg"],
        aarch64_image_risk="medium-high: aarch64 wheels/model packaging must be proven before image changes",
        runtime_risk="medium-high: CPU synthesis latency, model load, and RAM pressure are unverified on HA Green",
        expected_latency="must prove short host lines stay within queue lookahead on RK3566 CPU",
        operator_cost="free/local, but model storage and packaging cost",
    ),
    "piper": ProviderViability(
        provider="piper",
        kind=KIND_LOCAL_CPU,
        ha_green_status=STATUS_CONDITIONAL,
        reason=(
            "Small offline CPU candidate only if the piper binary and an Italian "
            "voice model are bundled or explicitly operator-provided."
        ),
        required_env=["PIPER_MODEL_PATH"],
        required_packages=["piper binary", "Italian piper voice model", "ffmpeg"],
        aarch64_image_risk="medium: binary and voice model packaging must be validated for add-on aarch64",
        runtime_risk="medium: CPU budget and voice quality must be proven; must not download models at startup",
        expected_latency="should be fast on CPU, but must be measured in the HA add-on container",
        operator_cost="free/local, with model storage overhead",
    ),
    "f5tts": ProviderViability(
        provider="f5tts",
        kind=KIND_REJECTED_LOCAL,
        ha_green_status=STATUS_FAIL,
        reason=(
            "Prior prototype depends on GPU/Apple Silicon MPS-class inference and "
            "reference audio. No practical CPU-only aarch64 add-on path is established."
        ),
        required_env=["F5TTS_REF_AUDIO_MARCO", "F5TTS_REF_AUDIO_GIULIA"],
        required_packages=["f5-tts", "torch", "ffmpeg"],
        aarch64_image_risk="high: PyTorch/model stack would bloat the add-on and may lack suitable aarch64 wheels",
        runtime_risk="high: cold start, memory, and CPU inference would compete with Home Assistant playback",
        expected_latency="out: GPU/MPS requirement fails the HA Green gate",
        operator_cost="free/self-hosted but requires non-HA-Green hardware",
    ),
    "bark_suno": ProviderViability(
        provider="bark_suno",
        kind=KIND_REJECTED_LOCAL,
        ha_green_status=STATUS_FAIL,
        reason=(
            "Previously rejected as too slow/stale for live radio. Heavy local "
            "model inference is not suitable for HA Green as the add-on target."
        ),
        required_env=[],
        required_packages=["bark/suno model stack", "torch", "ffmpeg"],
        aarch64_image_risk="high: heavy model dependencies and packaging risk for aarch64 add-on images",
        runtime_risk="high: slow synthesis and memory pressure would threaten continuous playback",
        expected_latency="out: too slow for live host segments on HA Green-class CPU",
        operator_cost="free/self-hosted but impractical on target hardware",
    ),
}


def _canonical_provider(name: str) -> str:
    key = name.strip().lower()
    return _ALIASES.get(key, key.replace("-", "_"))


def _expand_provider_names(provider_names: list[str] | None) -> list[str]:
    if provider_names is None:
        return list(DEFAULT_PROVIDERS)

    seen: set[str] = set()
    unique: list[str] = []
    for name in provider_names:
        provider = _canonical_provider(name)
        if provider == "all":
            for p in DEFAULT_PROVIDERS:
                if p not in seen:
                    seen.add(p)
                    unique.append(p)
        elif provider not in seen:
            seen.add(provider)
            unique.append(provider)
    return unique


def build_provider_matrix(provider_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Return the static HA Green viability records for selected providers."""
    providers = _expand_provider_names(provider_names)
    unknown = [name for name in providers if name not in _PROVIDER_MATRIX]
    if unknown:
        allowed = ", ".join(DEFAULT_PROVIDERS)
        raise ValueError(f"Unsupported provider(s): {', '.join(unknown)}. Allowed: {allowed}")
    return [asdict(_PROVIDER_MATRIX[name]) for name in providers]


def _join_cell(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(value) if value else "none"
    return str(value)


def _md_escape(value: Any) -> str:
    return _join_cell(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(records: list[dict[str, Any]]) -> str:
    """Render a deterministic Markdown report for the viability matrix."""
    lines = [
        "# HA Green TTS Viability Matrix",
        "",
        "Provider viability is gated on Home Assistant Green-class deployment: aarch64 CPU, no GPU, "
        "constrained RAM/storage, and continuous playback with existing Edge/bundled-clip fallback.",
        "",
        "| Provider | Kind | HA Green status | Reason | Required env | Required packages | "
        "aarch64 image risk | Runtime risk | Expected latency | Operator cost |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                _md_escape(record[field])
                for field in (
                    "provider",
                    "kind",
                    "ha_green_status",
                    "reason",
                    "required_env",
                    "required_packages",
                    "aarch64_image_risk",
                    "runtime_risk",
                    "expected_latency",
                    "operator_cost",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Statuses:",
            "- pass: eligible for the HA Green add-on target now.",
            "- conditional: worth evaluating, but blocked on credentials, packaging, "
            "model provisioning, or latency proof.",
            "- fail: out by default because it violates the HA Green hardware/deployability gate.",
        ]
    )
    return "\n".join(lines) + "\n"


def _report_timestamp(timestamp: str | None) -> str:
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDDTHHMMSSZ format")
    return stamp


def write_reports(
    records: list[dict[str, Any]],
    output_dir: Path,
    *,
    timestamp: str | None = None,
    md_content: str | None = None,
) -> dict[str, Path]:
    """Write JSONL and Markdown reports. Returns the created paths."""
    stamp = _report_timestamp(timestamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / f"tts-ha-green-viability-{stamp}.jsonl"
    md_path = output_dir / f"tts-ha-green-viability-{stamp}.md"
    for path in (jsonl_path, md_path):
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing report: {path}")

    with jsonl_path.open("x", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    with md_path.open("x", encoding="utf-8") as f:
        f.write(md_content if md_content is not None else render_markdown(records))
    return {"jsonl": jsonl_path, "markdown": md_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        help="Providers to include, or 'all'. Default: all practical candidates.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Report output directory")
    parser.add_argument(
        "--timestamp",
        help="Override report timestamp in YYYYMMDDTHHMMSSZ format, useful for deterministic test runs",
    )
    parser.add_argument("--no-write", action="store_true", help="Print the report without writing files")
    args = parser.parse_args(argv)

    try:
        records = build_provider_matrix(args.providers)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    report = render_markdown(records)
    print(report, end="")

    if not args.no_write:
        try:
            paths = write_reports(records, args.output_dir, timestamp=args.timestamp, md_content=report)
        except (FileExistsError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(f"Wrote {len(records)} records to {paths['jsonl']}")
        print(f"Wrote Markdown report to {paths['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
