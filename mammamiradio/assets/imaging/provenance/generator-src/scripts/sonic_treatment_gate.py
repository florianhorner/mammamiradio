#!/usr/bin/env python3
"""Build the revised, audition-only sonic treatment selection gate.

The renderer is deliberately local and deterministic. It performs no provider or
network calls, consumes the exact approved Isabella identity clip by ID, and
never writes the packaged imaging directory or station runtime state.
"""

from __future__ import annotations

import hashlib
import html
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mammamiradio.audio.normalizer import mix_voice_with_sting

try:
    _identity_audition = importlib.import_module("scripts.audition_tts_voices")
except ModuleNotFoundError as exc:  # Direct ``python scripts/audition_sonic_brand.py`` invocation.
    if exc.name != "scripts":
        raise
    _identity_audition = importlib.import_module("audition_tts_voices")
approved_identity_clips_by_id = _identity_audition.approved_identity_clips_by_id

FORMAT = {"codec": "mp3", "sample_rate_hz": 48_000, "channels": 2, "bitrate_kbps": 192}
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
STAGE = "sonic-treatment-selection"
TARGET = "Mac + Wohnzimmer Sonos Arc"
SCOPE = "Audition-only treatment selection using one exact approved production identity clip."
LIMITATIONS = (
    "Audition only; no candidate is selected or release-ready.",
    "The treatments are deterministic local prototypes, not packaged imaging assets.",
    "Core cadence, compatibility sounds, advertising recipes, and station runtime remain unchanged.",
)
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DECISION_FIELDS = frozenset({"pack_digest", "status", "candidate_id", "mac", "sonos_arc", "rationale"})
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "stage",
        "generated_at",
        "release_ready",
        "scope",
        "upstream_identity",
        "production_contract",
        "candidates",
        "pack_digest",
        "listening_receipt",
        "limitations",
    }
)
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "pack_digest",
        "candidate_id",
        "target",
        "prompt",
        "mac",
        "sonos_arc",
        "rationale",
        "reviewed_at",
    }
)
ASSET_FIELDS = frozenset({"path", "sha256", "duration_sec", "format"})
CONTEXT_FIELDS = frozenset({*ASSET_FIELDS, "voice_id", "voice_sha256", "mix"})
LISTENING_PROMPT = (
    "Treatment: neon_relay|velvet_horizon|headlight_cut|none; "
    "Mac: pass|fail; Sonos Arc: pass|fail|not_tested; Notes: <short reason>"
)


@dataclass(frozen=True)
class TreatmentSpec:
    id: str
    label: str
    family: str
    brief: str
    duration_sec: float
    expression_left: str
    expression_right: str
    finishing_filters: tuple[str, ...]


TREATMENTS: tuple[TreatmentSpec, ...] = (
    TreatmentSpec(
        id="neon_relay",
        label="Neon Relay",
        family="rhythmic-pulse",
        brief="Two compact low pulses with one narrow glass answer; direct, urban, and unsentimental.",
        duration_sec=0.92,
        expression_left=(
            "0.20*sin(2*PI*98*(t-0.025))*exp(-20*(t-0.025))*between(t,0.025,0.23)"
            "+0.17*sin(2*PI*98*(t-0.285))*exp(-22*(t-0.285))*between(t,0.285,0.47)"
            "+0.075*sin(2*PI*587.33*(t-0.59))*exp(-9*(t-0.59))*between(t,0.59,0.91)"
            "+0.035*sin(2*PI*880*(t-0.60))*exp(-12*(t-0.60))*between(t,0.60,0.88)"
        ),
        expression_right=(
            "0.19*sin(2*PI*98.7*(t-0.025))*exp(-20*(t-0.025))*between(t,0.025,0.23)"
            "+0.16*sin(2*PI*98.7*(t-0.285))*exp(-22*(t-0.285))*between(t,0.285,0.47)"
            "+0.076*sin(2*PI*590.2*(t-0.605))*exp(-9*(t-0.605))*between(t,0.605,0.92)"
            "+0.032*sin(2*PI*884*(t-0.615))*exp(-12*(t-0.615))*between(t,0.615,0.89)"
        ),
        finishing_filters=("highpass=f=55", "lowpass=f=10500", "afade=t=out:st=0.82:d=0.10"),
    ),
    TreatmentSpec(
        id="velvet_horizon",
        label="Velvet Horizon",
        family="atmospheric-bloom",
        brief="One dark suspended bloom with a slow edge and soft fall; cinematic without becoming a tune.",
        duration_sec=1.16,
        expression_left=(
            "(1-exp(-10*t))*exp(-1.9*t)*(0.12*sin(2*PI*110*t)"
            "+0.075*sin(2*PI*146.83*t)+0.055*sin(2*PI*220*t)+0.018*sin(2*PI*293.66*t))"
        ),
        expression_right=(
            "(1-exp(-9*t))*exp(-1.85*t)*(0.118*sin(2*PI*110.45*t)"
            "+0.073*sin(2*PI*146.25*t)+0.052*sin(2*PI*220.8*t)+0.019*sin(2*PI*292.9*t))"
        ),
        finishing_filters=("highpass=f=48", "lowpass=f=6200", "afade=t=out:st=0.86:d=0.30"),
    ),
    TreatmentSpec(
        id="headlight_cut",
        label="Headlight Cut",
        family="editorial-cut",
        brief="A dry relay snap, brief air gate, and downward spectral close; fast and deliberately non-melodic.",
        duration_sec=0.84,
        expression_left=(
            "0.18*sin(2*PI*1800*t)*exp(-95*t)*between(t,0,0.08)"
            "+0.13*sin(2*PI*(1600-1050*t)*t)*exp(-5*(t-0.16))*between(t,0.16,0.73)"
            "+0.045*sin(2*PI*(4200-3200*t)*t)*exp(-7*(t-0.18))*between(t,0.18,0.70)"
        ),
        expression_right=(
            "0.17*sin(2*PI*2050*t)*exp(-100*t)*between(t,0,0.075)"
            "+0.12*sin(2*PI*(1690-1110*t)*t)*exp(-5.2*(t-0.175))*between(t,0.175,0.75)"
            "+0.047*sin(2*PI*(3900-2850*t)*t)*exp(-6.8*(t-0.19))*between(t,0.19,0.72)"
        ),
        finishing_filters=("highpass=f=120", "lowpass=f=9800", "afade=t=out:st=0.68:d=0.16"),
    ),
)

TREATMENT_IDS = frozenset(spec.id for spec in TREATMENTS)


def _require_safe_output_root(output_root: Path) -> None:
    resolved = output_root.resolve(strict=False)
    allowed_roots = ((REPO_ROOT / "tmp").resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError("Treatment gates may be rendered only under the workspace tmp directory or system temp")


def _identity_path_record(identity_manifest: Path) -> tuple[str, str]:
    try:
        return identity_manifest.relative_to(REPO_ROOT).as_posix(), "repo-relative"
    except ValueError:
        return str(identity_manifest), "absolute"


def _identity_path_from_record(upstream: Mapping[str, object]) -> Path:
    raw_path = upstream.get("manifest_path")
    path_kind = upstream.get("manifest_path_kind")
    if not isinstance(raw_path, str):
        raise ValueError("Treatment manifest has no identity manifest path")
    if path_kind == "repo-relative":
        resolved = (REPO_ROOT / raw_path).resolve(strict=True)
        repo_root = REPO_ROOT.resolve()
        if resolved != repo_root and repo_root not in resolved.parents:
            raise ValueError("Treatment identity manifest escapes the repository")
        return resolved
    if path_kind == "absolute":
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise ValueError("Treatment identity manifest path is not absolute")
        return candidate.resolve(strict=True)
    raise ValueError("Treatment identity manifest has an invalid path kind")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_ffmpeg(command: Sequence[str], output_path: Path) -> Path:
    """Run one bounded local render and require its declared output."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(list(command), check=True)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg did not create treatment output: {output_path}")
    return output_path


def _render_solo(spec: TreatmentSpec, output_path: Path) -> Path:
    source = f"aevalsrc=exprs='{spec.expression_left}|{spec.expression_right}':s=48000:d={spec.duration_sec}:c=stereo"
    filters = [
        *spec.finishing_filters,
        "loudnorm=I=-16:LRA=5:TP=-1.5",
        "aformat=sample_rates=48000:channel_layouts=stereo",
    ]
    command = [
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
        ",".join(filters),
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-write_xing",
        "0",
        str(output_path),
    ]
    return _run_ffmpeg(command, output_path)


def _probe_audio(path: Path) -> tuple[dict[str, object], float]:
    command = [
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
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise ValueError(f"Treatment output has no single audio stream: {path}")
    stream = streams[0]
    raw_format = payload.get("format")
    container = raw_format if isinstance(raw_format, Mapping) else {}
    codec = stream.get("codec_name")
    sample_rate = stream.get("sample_rate")
    channels = stream.get("channels")
    bit_rate = stream.get("bit_rate") or container.get("bit_rate")
    duration = container.get("duration")
    try:
        evidence = {
            "codec": str(codec),
            "sample_rate_hz": int(str(sample_rate)),
            "channels": int(str(channels)),
            "bitrate_kbps": round(int(str(bit_rate)) / 1000),
        }
        duration_sec = float(str(duration))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Treatment output has incomplete ffprobe evidence: {path}") from exc
    if evidence != FORMAT or not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError(f"Treatment output violates the audio contract: {path} ({evidence}, {duration_sec})")
    return evidence, round(duration_sec, 3)


def _asset_record(path: Path, root: Path) -> dict[str, object]:
    audio_format, duration_sec = _probe_audio(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "duration_sec": duration_sec,
        "format": audio_format,
    }


def _pack_digest(
    upstream_identity: Mapping[str, object],
    candidates: Sequence[Mapping[str, object]],
    *,
    generated_at: str,
) -> str:
    material = {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "generated_at": generated_at,
        "scope": SCOPE,
        "limitations": list(LIMITATIONS),
        "upstream_identity": upstream_identity,
        "production_contract": {
            "voice_id": "identity-isabella",
            "mix_helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
            "sting_gain_linear": 0.15,
            "voice_gain_linear": 1.2,
            "voice_delay_ms": 400,
        },
        "candidates": list(candidates),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _treatment_readme(manifest: Mapping[str, object]) -> str:
    raw_candidates = manifest["candidates"]
    assert isinstance(raw_candidates, list)
    rows: list[str] = []
    for index, candidate in enumerate(raw_candidates):
        assert isinstance(candidate, Mapping)
        solo = candidate["solo"]
        context = candidate["in_context"]
        assert isinstance(solo, Mapping) and isinstance(context, Mapping)
        rows.extend(
            [
                f"### {chr(ord('A') + index)} — {candidate['label']}",
                "",
                str(candidate["brief"]),
                "",
                f"- [With Isabella](./{context['path']})",
                f"- [Solo treatment](./{solo['path']})",
                "",
            ]
        )
    return "\n".join(
        [
            "# Mamma Mi Radio — sonic treatment selection",
            "",
            (
                "This is a local audition board, not a shipped sound pack. "
                "The digest-bound decision state lives in the manifest."
            ),
            "Each direction uses the exact production Isabella clip approved in the preceding identity gate.",
            "",
            "[Open the listening board](./index.html)",
            "",
            *rows,
            "## Procedure",
            "",
            "1. Keep Mac volume fixed and play each With Isabella clip once, in order.",
            "2. Replay the solo treatment for any direction that survives the first pass.",
            "3. Play the leading direction three times, then hear that same clip on the Wohnzimmer Sonos Arc.",
            "4. Choosing `none` is a valid result; no later stage will be built from a rejected direction.",
            "5. Reply exactly:",
            "",
            f"`{LISTENING_PROMPT}`",
            "",
            f"Pack digest: `{manifest['pack_digest']}`",
            "",
            "The renderer is local-only and changes neither station runtime nor packaged imaging assets.",
            "",
        ]
    )


def _treatment_html(manifest: Mapping[str, object]) -> str:
    raw_candidates = manifest["candidates"]
    assert isinstance(raw_candidates, list)
    cards: list[str] = []
    for index, candidate in enumerate(raw_candidates):
        assert isinstance(candidate, Mapping)
        solo = candidate["solo"]
        context = candidate["in_context"]
        assert isinstance(solo, Mapping) and isinstance(context, Mapping)
        context_src = html.escape(str(context["path"]), quote=True)
        solo_src = html.escape(str(solo["path"]), quote=True)
        cards.append(
            "\n".join(
                [
                    '<article class="card">',
                    f'<p class="letter">{chr(ord("A") + index)}</p>',
                    f"<h2>{html.escape(str(candidate['label']))}</h2>",
                    f"<p>{html.escape(str(candidate['brief']))}</p>",
                    "<h3>With Isabella</h3>",
                    f'<audio controls preload="metadata" src="{context_src}"></audio>',
                    "<h3>Solo treatment</h3>",
                    f'<audio controls preload="metadata" src="{solo_src}"></audio>',
                    f"<p><code>{html.escape(str(candidate['id']))}</code></p>",
                    "</article>",
                ]
            )
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mamma Mi Radio — sonic treatment selection</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #14110f;
  --surface: #251e19;
  --cream: #f5edd8;
  --cream-dk: #eaddc4;
  --muted: #9b8574;
  --sun: #f4d048;
  --line: rgba(245, 237, 216, 0.16);
  --font-display: Georgia, "Times New Roman", serif;
  --font-body: system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
html {{
  min-height: 100%;
  background:
    radial-gradient(400px 400px at 88% 14%, rgba(244, 208, 72, 0.10), transparent 70%),
    linear-gradient(180deg, #1e1610 0%, var(--bg) 12%, var(--bg) 100%);
}}
body {{
  max-width: 960px;
  margin: 0 auto;
  padding: 32px 20px 64px;
  color: var(--cream);
  font-family: var(--font-body);
  line-height: 1.5;
}}
h1, h2 {{ font-family: var(--font-display); font-style: italic; }}
h1 {{ letter-spacing: -.035em; }}
h3 {{
  margin: 20px 0 8px;
  color: var(--cream-dk);
  font-size: .78rem;
  letter-spacing: .12em;
  text-transform: uppercase;
}}
.notice {{
  padding: 14px 16px;
  border: 1px solid rgba(244, 208, 72, .35);
  border-radius: 8px;
  background: var(--surface);
}}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 16px; margin-top: 24px; }}
.card {{
  padding: 18px;
  border: 1px solid var(--line);
  border-top-color: rgba(244, 208, 72, .4);
  border-radius: 8px;
  background: var(--surface);
}}
.card h2 {{ margin-top: 0; }}
.letter {{ color: var(--sun); font-family: var(--font-body); font-size: 1.4rem; font-weight: 800; margin: 0; }}
audio {{ width: 100%; }}
code {{ color: var(--sun); font-family: var(--font-mono); }}
</style>
</head>
<body>
<h1>Sonic treatment selection</h1>
<p class="notice"><strong>Audition only.</strong>
Same approved Isabella in every context clip. Only the treatment changes.</p>
<p>Hear A, B, and C once on the Mac. Repeat the leader three times and then hear that same clip
on the Wohnzimmer Sonos Arc. Choosing none is valid.</p>
<main class="grid">
{"".join(cards)}
</main>
<p>Reply: <code>{html.escape(LISTENING_PROMPT)}</code></p>
<p>Pack digest: <code>{manifest["pack_digest"]}</code></p>
</body>
</html>
"""


def _validate_receipt(value: object, *, pack_digest: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != RECEIPT_FIELDS:
        raise ValueError("Treatment listening receipt has an invalid field set")
    if value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError("Treatment listening receipt has an unsupported schema")
    if (
        value.get("pack_digest") != pack_digest
        or value.get("target") != TARGET
        or value.get("prompt") != LISTENING_PROMPT
    ):
        raise ValueError("Treatment listening receipt is stale or targets the wrong surfaces")
    status = value.get("status")
    if status == "pending":
        if any(
            value.get(field) is not None for field in ("candidate_id", "mac", "sonos_arc", "rationale", "reviewed_at")
        ):
            raise ValueError("Pending treatment receipt contains a decision")
        return value
    if status not in {"approved", "rejected"}:
        raise ValueError("Treatment listening receipt has an invalid status")
    candidate_id = value.get("candidate_id")
    mac = value.get("mac")
    sonos_arc = value.get("sonos_arc")
    rationale = value.get("rationale")
    reviewed_at = value.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not reviewed_at.strip():
        raise ValueError("Decided treatment receipt has no review time")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise ValueError("Decided treatment receipt has an invalid review time") from exc
    if parsed_reviewed_at.tzinfo is None or parsed_reviewed_at.utcoffset() is None:
        raise ValueError("Decided treatment receipt review time must include a timezone")
    if status == "approved" and (
        candidate_id not in TREATMENT_IDS
        or mac != "pass"
        or sonos_arc != "pass"
        or rationale != "accepted_sonic_direction"
    ):
        raise ValueError("Approved treatment receipt does not identify an accepted direction")
    if status == "rejected" and (
        candidate_id != "none"
        or mac not in {"pass", "fail"}
        or sonos_arc not in {"pass", "fail", "not_tested"}
        or (mac == "pass" and sonos_arc == "pass")
        or rationale != "rejected_all_directions"
    ):
        raise ValueError("Rejected treatment receipt must reject every direction")
    return value


def _validate_asset(
    root: Path,
    value: object,
    *,
    expected_path: str,
    expected_fields: frozenset[str],
) -> tuple[Mapping[str, object], Path]:
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError(f"Treatment asset {expected_path} has an invalid field set")
    if value.get("path") != expected_path:
        raise ValueError(f"Treatment asset has the wrong path: {expected_path}")
    path = root / expected_path
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Treatment asset is missing or unsafe: {expected_path}")
    actual_hash = _sha256(path)
    if value.get("sha256") != actual_hash:
        raise ValueError(f"Treatment asset hash differs from the manifest: {expected_path}")
    audio_format, duration_sec = _probe_audio(path)
    if value.get("format") != audio_format:
        raise ValueError(f"Treatment asset format differs from probe evidence: {expected_path}")
    recorded_duration = value.get("duration_sec")
    if isinstance(recorded_duration, bool) or not isinstance(recorded_duration, int | float):
        raise ValueError(f"Treatment asset has no numeric duration: {expected_path}")
    if not math.isclose(float(recorded_duration), duration_sec, abs_tol=0.001):
        raise ValueError(f"Treatment asset duration differs from probe evidence: {expected_path}")
    return value, path


def _validate_board_inventory(root: Path, audio_paths: Sequence[Path], *, require_ready: bool) -> None:
    expected = {
        "audio",
        "manifest.json",
        "README.md",
        "index.html",
        *(path.relative_to(root).as_posix() for path in audio_paths),
    }
    ready_path = root / ".ready"
    if require_ready or ready_path.exists() or ready_path.is_symlink():
        expected.add(".ready")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        details = []
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        if missing:
            details.append("missing: " + ", ".join(missing))
        raise ValueError("Treatment board inventory differs from the manifest (" + "; ".join(details) + ")")


def validate_treatment_gate(
    manifest_path: Path,
    *,
    require_ready: bool = True,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    """Validate every upstream, audio, digest, and listening-surface binding."""
    if manifest_path.is_symlink():
        raise ValueError("Treatment manifest must not be a symlink")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Treatment manifest is not readable JSON: {manifest_path}") from exc
    if not isinstance(raw_manifest, dict) or set(raw_manifest) != MANIFEST_FIELDS:
        raise ValueError("Treatment manifest has an invalid field set")
    manifest: dict[str, Any] = raw_manifest
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("stage") != STAGE:
        raise ValueError("Treatment manifest has the wrong schema or stage")
    if manifest.get("release_ready") is not False:
        raise ValueError("Treatment audition must never be release-ready")
    if not isinstance(manifest.get("generated_at"), str) or not TIMESTAMP_RE.fullmatch(manifest["generated_at"]):
        raise ValueError("Treatment manifest has an invalid generation time")
    if manifest.get("scope") != SCOPE or manifest.get("limitations") != list(LIMITATIONS):
        raise ValueError("Treatment manifest has stale scope or limitations")

    upstream = manifest.get("upstream_identity")
    if not isinstance(upstream, Mapping) or set(upstream) != {
        "manifest_path",
        "manifest_path_kind",
        "pack_digest",
        "isabella_id",
        "isabella_audio_sha256",
    }:
        raise ValueError("Treatment manifest has invalid upstream identity evidence")
    identity_manifest = _identity_path_from_record(upstream)
    identity_digest, paths_by_id, metadata_by_id = approved_identity_clips_by_id(identity_manifest)
    isabella_metadata = metadata_by_id.get("identity-isabella")
    if "identity-isabella" not in paths_by_id or not isinstance(isabella_metadata, Mapping):
        raise ValueError("Approved identity board has no Isabella clip")
    if upstream.get("pack_digest") != identity_digest or upstream.get("isabella_id") != "identity-isabella":
        raise ValueError("Treatment manifest points to stale identity evidence")
    if upstream.get("isabella_audio_sha256") != isabella_metadata.get("audio_sha256"):
        raise ValueError("Treatment manifest has a stale Isabella hash")

    production_contract = manifest.get("production_contract")
    expected_contract = {
        "voice_id": "identity-isabella",
        "mix_helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
        "sting_gain_linear": 0.15,
        "voice_gain_linear": 1.2,
        "voice_delay_ms": 400,
    }
    if production_contract != expected_contract:
        raise ValueError("Treatment manifest differs from the production mix contract")

    raw_candidates = manifest.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) != len(TREATMENTS):
        raise ValueError("Treatment gate requires exactly three candidates")
    candidates_by_id: dict[str, Mapping[str, object]] = {}
    audio_paths: list[Path] = []
    output_hashes: set[str] = set()
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping):
            raise ValueError("Treatment gate contains invalid candidate metadata")
        candidate_id = raw_candidate.get("id")
        if not isinstance(candidate_id, str) or candidate_id in candidates_by_id:
            raise ValueError("Treatment gate repeats or omits a candidate ID")
        candidates_by_id[candidate_id] = raw_candidate
    if set(candidates_by_id) != TREATMENT_IDS:
        raise ValueError("Treatment gate contains an unexpected candidate set")

    for spec in TREATMENTS:
        candidate = candidates_by_id[spec.id]
        expected_candidate_fields = {
            "id",
            "label",
            "family",
            "brief",
            "duration_target_sec",
            "generator",
            "solo",
            "in_context",
        }
        if set(candidate) != expected_candidate_fields:
            raise ValueError(f"Treatment candidate {spec.id} has an invalid field set")
        if (
            candidate.get("label") != spec.label
            or candidate.get("family") != spec.family
            or candidate.get("brief") != spec.brief
            or candidate.get("duration_target_sec") != spec.duration_sec
            or candidate.get("generator") != "deterministic-local-synthesis-v1"
        ):
            raise ValueError(f"Treatment candidate {spec.id} metadata is stale")
        solo, solo_path = _validate_asset(
            manifest_path.parent,
            candidate.get("solo"),
            expected_path=f"audio/{spec.id}.mp3",
            expected_fields=ASSET_FIELDS,
        )
        solo_duration = solo.get("duration_sec")
        if isinstance(solo_duration, bool) or not isinstance(solo_duration, int | float):
            raise ValueError(f"Treatment candidate {spec.id} has no numeric solo duration")
        if not math.isclose(float(solo_duration), spec.duration_sec, abs_tol=0.05):
            raise ValueError(f"Treatment candidate {spec.id} exceeds its duration target tolerance")
        context, context_path = _validate_asset(
            manifest_path.parent,
            candidate.get("in_context"),
            expected_path=f"audio/{spec.id}_isabella.mp3",
            expected_fields=CONTEXT_FIELDS,
        )
        if context.get("voice_id") != "identity-isabella" or context.get("voice_sha256") != isabella_metadata.get(
            "audio_sha256"
        ):
            raise ValueError(f"Treatment candidate {spec.id} uses the wrong voice evidence")
        if context.get("mix") != expected_contract:
            raise ValueError(f"Treatment candidate {spec.id} uses the wrong mix contract")
        for asset in (solo, context):
            asset_hash = asset.get("sha256")
            if not isinstance(asset_hash, str) or asset_hash in output_hashes:
                raise ValueError("Treatment outputs must have distinct hashes")
            output_hashes.add(asset_hash)
        audio_paths.extend((solo_path, context_path))

    declared_names = {path.relative_to(manifest_path.parent).as_posix() for path in audio_paths}
    actual_names = {
        path.relative_to(manifest_path.parent).as_posix()
        for path in manifest_path.parent.rglob("*.mp3")
        if path.is_file()
    }
    if actual_names != declared_names:
        raise ValueError("Treatment board contains undeclared or missing audio")
    _validate_board_inventory(manifest_path.parent, audio_paths, require_ready=require_ready)

    pack_digest = manifest.get("pack_digest")
    if not isinstance(pack_digest, str) or not SHA256_RE.fullmatch(pack_digest):
        raise ValueError("Treatment manifest has an invalid pack digest")
    expected_digest = _pack_digest(upstream, raw_candidates, generated_at=manifest["generated_at"])
    if pack_digest != expected_digest:
        raise ValueError("Treatment manifest pack_digest is stale or invalid")
    _validate_receipt(manifest.get("listening_receipt"), pack_digest=pack_digest)

    expected_surfaces = {
        "README.md": _treatment_readme(manifest),
        "index.html": _treatment_html(manifest),
    }
    for filename, expected_text in expected_surfaces.items():
        surface = manifest_path.parent / filename
        if surface.is_symlink():
            raise ValueError(f"Treatment listening surface must not be a symlink: {filename}")
        try:
            actual_text = surface.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Treatment listening surface is missing: {filename}") from exc
        if actual_text != expected_text:
            raise ValueError(f"Treatment listening surface differs from the validated manifest: {filename}")
    if require_ready:
        ready_path = manifest_path.parent / ".ready"
        if ready_path.is_symlink():
            raise ValueError("Treatment ready marker must not be a symlink")
        try:
            ready_digest = ready_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ValueError("Treatment board has no ready publication marker") from exc
        if ready_digest != pack_digest:
            raise ValueError("Treatment ready marker does not match pack_digest")
    return manifest, tuple(audio_paths)


def _manifest(
    *,
    generated_at: str,
    identity_manifest: Path,
    identity_digest: str,
    isabella_sha256: str,
    candidates: list[dict[str, object]],
) -> dict[str, Any]:
    manifest_path_record, manifest_path_kind = _identity_path_record(identity_manifest)
    upstream_identity = {
        "manifest_path": manifest_path_record,
        "manifest_path_kind": manifest_path_kind,
        "pack_digest": identity_digest,
        "isabella_id": "identity-isabella",
        "isabella_audio_sha256": isabella_sha256,
    }
    production_contract = {
        "voice_id": "identity-isabella",
        "mix_helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
        "sting_gain_linear": 0.15,
        "voice_gain_linear": 1.2,
        "voice_delay_ms": 400,
    }
    pack_digest = _pack_digest(upstream_identity, candidates, generated_at=generated_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": STAGE,
        "generated_at": generated_at,
        "release_ready": False,
        "scope": SCOPE,
        "upstream_identity": upstream_identity,
        "production_contract": production_contract,
        "candidates": candidates,
        "pack_digest": pack_digest,
        "listening_receipt": {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": "pending",
            "pack_digest": pack_digest,
            "candidate_id": None,
            "target": TARGET,
            "prompt": LISTENING_PROMPT,
            "mac": None,
            "sonos_arc": None,
            "rationale": None,
            "reviewed_at": None,
        },
        "limitations": list(LIMITATIONS),
    }


def _decision_evidence_snapshot(
    manifest_path: Path,
    manifest: Mapping[str, object],
    audio_paths: Sequence[Path],
) -> dict[str, str]:
    upstream = manifest.get("upstream_identity")
    if not isinstance(upstream, Mapping):  # pragma: no cover - validator invariant
        raise ValueError("Treatment manifest has no upstream identity evidence")
    identity_manifest = _identity_path_from_record(upstream)
    _identity_digest, identity_paths, _identity_metadata = approved_identity_clips_by_id(identity_manifest)
    paths = [
        manifest_path.parent / "README.md",
        manifest_path.parent / "index.html",
        manifest_path.parent / ".ready",
        identity_manifest,
        *audio_paths,
        *identity_paths.values(),
    ]
    return {str(path.resolve(strict=True)): _sha256(path) for path in paths}


def render_treatment_gate(
    identity_manifest: Path,
    output_root: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Render and atomically publish three treatments after exact identity approval."""
    _require_safe_output_root(output_root)
    if identity_manifest.is_symlink():
        raise ValueError("Identity manifest must not be a symlink")
    identity_manifest = identity_manifest.resolve(strict=True)
    identity_digest, paths_by_id, metadata_by_id = approved_identity_clips_by_id(identity_manifest)
    isabella_path = paths_by_id.get("identity-isabella")
    isabella_metadata = metadata_by_id.get("identity-isabella")
    if isabella_path is None or not isinstance(isabella_metadata, Mapping):
        raise ValueError("Approved identity board has no Isabella clip")
    isabella_sha256 = isabella_metadata.get("audio_sha256")
    if not isinstance(isabella_sha256, str) or not SHA256_RE.fullmatch(isabella_sha256):
        raise ValueError("Approved Isabella clip has no valid audio hash")
    if _sha256(isabella_path) != isabella_sha256:
        raise ValueError("Approved Isabella clip hash changed before treatment rendering")
    stamp = generated_at or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("generated_at must use YYYYMMDDTHHMMSSZ format")
    if output_root.exists():
        raise FileExistsError(f"Refusing to overwrite treatment gate: {output_root}")

    output_root.parent.mkdir(parents=True, exist_ok=True)
    claim_path = output_root.parent / f".{output_root.name}.claim"
    try:
        claim_fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise FileExistsError(f"Treatment gate is already being rendered: {output_root}") from None
    os.close(claim_fd)
    staging_root: Path | None = None
    try:
        staging_root = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.", dir=output_root.parent))
        audio_root = staging_root / "audio"
        audio_root.mkdir()
        candidates: list[dict[str, object]] = []
        production_contract = {
            "voice_id": "identity-isabella",
            "mix_helper": "mammamiradio.audio.normalizer.mix_voice_with_sting",
            "sting_gain_linear": 0.15,
            "voice_gain_linear": 1.2,
            "voice_delay_ms": 400,
        }
        for spec in TREATMENTS:
            solo_path = _render_solo(spec, audio_root / f"{spec.id}.mp3")
            context_path = audio_root / f"{spec.id}_isabella.mp3"
            mix_voice_with_sting(isabella_path, solo_path, context_path)
            solo = _asset_record(solo_path, staging_root)
            context = {
                **_asset_record(context_path, staging_root),
                "voice_id": "identity-isabella",
                "voice_sha256": isabella_sha256,
                "mix": production_contract,
            }
            candidates.append(
                {
                    "id": spec.id,
                    "label": spec.label,
                    "family": spec.family,
                    "brief": spec.brief,
                    "duration_target_sec": spec.duration_sec,
                    "generator": "deterministic-local-synthesis-v1",
                    "solo": solo,
                    "in_context": context,
                }
            )
        manifest = _manifest(
            generated_at=stamp,
            identity_manifest=identity_manifest,
            identity_digest=identity_digest,
            isabella_sha256=isabella_sha256,
            candidates=candidates,
        )
        manifest_path = staging_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (staging_root / "README.md").write_text(_treatment_readme(manifest), encoding="utf-8")
        (staging_root / "index.html").write_text(_treatment_html(manifest), encoding="utf-8")
        (staging_root / ".ready").write_text(f"{manifest['pack_digest']}\n", encoding="ascii")
        validate_treatment_gate(manifest_path)
        if output_root.exists():
            raise FileExistsError(f"Treatment gate appeared during render: {output_root}")
        os.replace(staging_root, output_root)
        return manifest
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        claim_path.unlink(missing_ok=True)


def write_treatment_listening_receipt_from_decision(
    *,
    manifest_path: Path,
    decision_path: Path,
    reviewed_at: str | None = None,
) -> Path:
    """Record one digest-bound human decision under an exclusive lock."""
    lock_path = manifest_path.parent.parent / f".{manifest_path.parent.name}.treatment-listening-receipt.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Treatment listening receipt is already being decided") from None
    try:
        original_bytes = manifest_path.read_bytes()
        manifest, audio_paths = validate_treatment_gate(manifest_path)
        original_evidence = _decision_evidence_snapshot(manifest_path, manifest, audio_paths)
        current = manifest.get("listening_receipt")
        if not isinstance(current, Mapping) or current.get("status") != "pending":
            raise ValueError("Treatment listening receipt has already been decided")
        try:
            raw_decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Treatment decision is not readable JSON: {decision_path}") from exc
        if not isinstance(raw_decision, Mapping) or set(raw_decision) != DECISION_FIELDS:
            raise ValueError("Treatment decision has an invalid field set")
        pack_digest = manifest["pack_digest"]
        if raw_decision.get("pack_digest") != pack_digest:
            raise ValueError("Treatment decision pack_digest does not match the current board")
        receipt = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "status": raw_decision.get("status"),
            "pack_digest": pack_digest,
            "candidate_id": raw_decision.get("candidate_id"),
            "target": TARGET,
            "prompt": LISTENING_PROMPT,
            "mac": raw_decision.get("mac"),
            "sonos_arc": raw_decision.get("sonos_arc"),
            "rationale": raw_decision.get("rationale"),
            "reviewed_at": reviewed_at or datetime.now(UTC).isoformat(),
        }
        _validate_receipt(receipt, pack_digest=str(pack_digest))
        current_manifest, current_audio_paths = validate_treatment_gate(manifest_path)
        if manifest_path.read_bytes() != original_bytes:
            raise ValueError("Treatment manifest changed while its decision was being recorded")
        if _decision_evidence_snapshot(manifest_path, current_manifest, current_audio_paths) != original_evidence:
            raise ValueError("Treatment board changed while its decision was being recorded")
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
            decided_manifest, decided_audio_paths = validate_treatment_gate(manifest_path)
            if _decision_evidence_snapshot(manifest_path, decided_manifest, decided_audio_paths) != original_evidence:
                raise ValueError("Treatment board changed while its decision was being committed")
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


def assert_treatment_listening_gate(manifest_path: Path) -> str:
    """Return the selected direction or fail before any core-cadence build."""
    manifest, _paths = validate_treatment_gate(manifest_path)
    receipt = manifest["listening_receipt"]
    if not isinstance(receipt, Mapping) or receipt.get("status") != "approved":
        raise ValueError("Treatment listening gate is not approved for this pack digest")
    candidate_id = receipt.get("candidate_id")
    if not isinstance(candidate_id, str) or candidate_id not in TREATMENT_IDS:
        raise ValueError("Treatment listening gate has no approved direction")
    return candidate_id
