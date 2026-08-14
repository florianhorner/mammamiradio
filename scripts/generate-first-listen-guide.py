#!/usr/bin/env python3
"""Render the immutable Marco and Giulia guide bundled with First Listen.

The browser never synthesizes these clips at runtime.  This script renders the
reviewed dialogue with the configured host voices, normalizes every file to the
station format, and writes the hash-bound manifest consumed by the repository
validation gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from mammamiradio.core.models import HostPersonality

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT_ROOT = REPO_ROOT / "mammamiradio" / "web" / "static" / "audio"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
MANIFEST_FILENAME = "spoken_assets.json"
CANONICAL_HOST_NAMES = ("Marco", "Giulia")


@dataclass(frozen=True, slots=True)
class GuideLine:
    host: str
    text: str


@dataclass(frozen=True, slots=True)
class GuideClip:
    clip_id: str
    lines: tuple[GuideLine, ...]
    station_sting: bool = False

    @property
    def transcript(self) -> str:
        return " ".join(f"{line.host}: {line.text}" for line in self.lines)


GUIDE_CLIPS = (
    GuideClip(
        "welcome",
        (
            GuideLine("Marco", "Benvenuti. Five small steps, and Mamma Mi Radio is in your room."),
            GuideLine(
                "Giulia",
                "He means your speaker. Nothing personal and nothing automatic. You stay in charge.",
            ),
        ),
        station_sting=True,
    ),
    GuideClip(
        "speaker",
        (
            GuideLine("Marco", "Pick the room. Kitchen, living room, wherever radio belongs."),
            GuideLine(
                "Giulia",
                "We only show speakers Home Assistant already knows. We never choose one or touch the volume.",
            ),
        ),
    ),
    GuideClip(
        "sound-check",
        (
            GuideLine("Marco", "We sent the station. If you can hear us, say yes."),
            GuideLine(
                "Giulia",
                "Home Assistant can confirm the request, not the sound in your room. That part is yours.",
            ),
        ),
    ),
    GuideClip(
        "not-yet",
        (
            GuideLine("Marco", "No sound? Va bene. We are not blaming the speaker."),
            GuideLine(
                "Giulia",
                "Check mute and volume, then try the same room again. We saved your place.",
            ),
        ),
    ),
    GuideClip(
        "receipt-recovery",
        (
            GuideLine("Giulia", "Home Assistant already sent the station."),
            GuideLine(
                "Marco",
                "This only restores the listening check. It will not play us again.",
            ),
        ),
    ),
    GuideClip(
        "privacy",
        (
            GuideLine("Marco", "Give me the weather and I will make it radio."),
            GuideLine(
                "Giulia",
                "Only after you preview the exact details. Keep Home private and we read nothing.",
            ),
        ),
    ),
    GuideClip(
        "ai",
        (
            GuideLine("Marco", "Connect an AI provider and the conversations keep changing."),
            GuideLine(
                "Giulia",
                "Optional. The station already works. Saved keys stay hidden, and an empty field changes nothing.",
            ),
        ),
    ),
    GuideClip(
        "success",
        (
            GuideLine("Marco", "Siamo in onda! Your first broadcast is live."),
            GuideLine(
                "Giulia",
                "Speaker checked, privacy saved. Mamma Mi Radio is yours now.",
            ),
        ),
        station_sting=True,
    ),
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_environment(env_file: Path) -> None:
    """Load the selected dotenv before any runtime module can load repo .env."""

    if env_file.is_file():
        # Real process environment always wins. Loading this before importing
        # core.config prevents its implicit repo .env load from selecting a
        # different paid-provider account than the operator requested.
        load_dotenv(dotenv_path=env_file, override=False)


def _load_station_config(path: Path):
    """Import config lazily so --env-file establishes provider precedence."""

    from mammamiradio.core.config import load_config

    return load_config(str(path))


def _temporary_work_directory():
    """Create render scratch in the OS temp area; the repo tmp/ may not exist."""

    return tempfile.TemporaryDirectory(prefix="first-listen-guide-")


def _display_path(path: Path) -> str:
    """Prefer a repo-relative receipt while supporting external output roots."""

    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _publish_staged_file(staged: Path, destination: Path) -> None:
    """Publish a render even when OS temp and --output-root use different filesystems."""

    shutil.move(str(staged), destination)


def _canonical_voice_settings(host: HostPersonality) -> dict[str, object] | None:
    """Return the exact provider settings used for this canonical host render."""

    from mammamiradio.audio.tts import (
        _ELEVENLABS_V2_MODEL,
        _ELEVENLABS_V3_MODEL,
        _resolve_elevenlabs_v2_voice_settings,
        _resolve_elevenlabs_v3_voice_settings,
    )

    if host.elevenlabs_model == _ELEVENLABS_V2_MODEL:
        return _resolve_elevenlabs_v2_voice_settings(host.voice_settings)
    if host.elevenlabs_model == _ELEVENLABS_V3_MODEL:
        return _resolve_elevenlabs_v3_voice_settings(host.voice_settings)
    raise RuntimeError(f"{host.name} uses unsupported ElevenLabs model {host.elevenlabs_model!r}")


def _canonical_render_receipt(hosts: dict[str, HostPersonality]) -> dict[str, object]:
    """Snapshot the non-secret radio.toml voice inputs that produced the pack."""

    receipt_hosts: list[dict[str, object]] = []
    for name in CANONICAL_HOST_NAMES:
        host = hosts[name]
        if host.engine != "elevenlabs":
            raise RuntimeError(f"{name} is not configured with the canonical ElevenLabs provider")
        receipt_hosts.append(
            {
                "name": name,
                "voice_id": host.voice,
                "model_id": host.elevenlabs_model,
                "voice_settings": _canonical_voice_settings(host),
                "delivery_profile": host.delivery_profile,
            }
        )
    return {
        "schema_version": 1,
        "source": "radio.toml",
        "provider": "elevenlabs",
        "fallback": False,
        "hosts": receipt_hosts,
    }


async def _render_line(
    host: HostPersonality,
    text: str,
    output_path: Path,
) -> Path:
    from mammamiradio.audio.tts import synthesize_elevenlabs

    if host.engine != "elevenlabs":
        raise RuntimeError(f"{host.name} is not configured with a canonical ElevenLabs voice")
    return await synthesize_elevenlabs(
        text,
        host.voice,
        output_path,
        loudnorm=False,
        voice_settings=host.voice_settings,
        elevenlabs_model=host.elevenlabs_model,
        delivery_profile=host.delivery_profile,
        host_name=host.name,
    )


async def _render_clip(
    clip: GuideClip,
    hosts: dict[str, HostPersonality],
    work_dir: Path,
    destination: Path,
    *,
    motif_notes: list[int],
) -> dict[str, object]:
    from mammamiradio.audio.normalizer import (
        concat_files,
        generate_station_id_bed,
        mix_voice_with_sting,
        probe_duration_sec,
    )

    line_paths = [work_dir / f"{clip.clip_id}-{index}.mp3" for index in range(len(clip.lines))]
    await asyncio.gather(
        *(_render_line(hosts[line.host], line.text, path) for line, path in zip(clip.lines, line_paths, strict=True))
    )

    dialogue_path = work_dir / f"{clip.clip_id}-dialogue.mp3"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: concat_files(
            line_paths,
            dialogue_path,
            silence_ms=280,
            loudnorm=not clip.station_sting,
            strict_duration=True,
        ),
    )
    if clip.station_sting:
        sting_path = work_dir / f"{clip.clip_id}-sting.mp3"
        await loop.run_in_executor(None, lambda: generate_station_id_bed(sting_path, 3.0, motif_notes))
        await loop.run_in_executor(None, lambda: mix_voice_with_sting(dialogue_path, sting_path, destination))
    else:
        shutil.move(dialogue_path, destination)

    duration = probe_duration_sec(destination)
    if duration is None or duration <= 0:
        raise RuntimeError(f"could not prove audio duration for {destination.name}")
    return {
        "path": f"first_listen/{destination.name}",
        "sha256": _sha256(destination),
        "kind": "speech",
        "language": "en",
        "transcript": clip.transcript,
        "duration_seconds": round(duration, 3),
        "speakers": [line.host for line in clip.lines],
    }


async def _run(args: argparse.Namespace) -> None:
    _load_environment(args.env_file)
    if not os.getenv("ELEVENLABS_API_KEY"):
        raise RuntimeError("canonical rendering requires ELEVENLABS_API_KEY")

    config = _load_station_config(REPO_ROOT / "radio.toml")
    hosts = {host.name: host for host in config.hosts if host.name in CANONICAL_HOST_NAMES}
    if set(hosts) != set(CANONICAL_HOST_NAMES):
        raise RuntimeError("radio.toml must configure both Marco and Giulia")
    canonical_receipt = _canonical_render_receipt(hosts)

    output_root = args.output_root.resolve()
    output_dir = output_root / "first_listen"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    with _temporary_work_directory() as raw_work_dir:
        work_dir = Path(raw_work_dir)
        staging_dir = work_dir / "staging"
        staging_dir.mkdir()
        entries = []
        for clip in GUIDE_CLIPS:
            destination = staging_dir / f"{clip.clip_id}.mp3"
            entries.append(
                await _render_clip(
                    clip,
                    hosts,
                    work_dir,
                    destination,
                    motif_notes=config.sonic_brand.motif_notes,
                )
            )
            print(f"rendered {clip.clip_id}: {entries[-1]['duration_seconds']}s")

        for staged in sorted(staging_dir.glob("*.mp3")):
            _publish_staged_file(staged, output_dir / staged.name)

    manifest = {
        "schema_version": 1,
        "bundle": "first-listen-guide",
        "render_provider": "canonical",
        "canonical_render_receipt": canonical_receipt,
        "assets": entries,
    }
    manifest_path = output_root / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {_display_path(manifest_path)}")


def main() -> int:
    args = _arguments()
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        print(f"first-listen-guide: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
