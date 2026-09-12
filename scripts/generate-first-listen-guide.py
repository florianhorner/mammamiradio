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
import contextlib
import hashlib
import json
import os
import runpy
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
STATION_OUTPUT_ROOT = REPO_ROOT / "mammamiradio" / "assets" / "demo"
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
    sustained_bed: bool = False
    pause_ms: int = 280

    @property
    def transcript(self) -> str:
        return " ".join(f"{line.host}: {line.text}" for line in self.lines)


STATION_OPENING_CLIP = GuideClip(
    "first_listen_admin_show",
    (
        GuideLine(
            "Marco", "Mamma Mi Radio, live from Studio B. The coffee is back where it belongs. Everyone’s happy."
        ),
        GuideLine("Giulia", "The coffee’s happy. I’m reserving judgment."),
        GuideLine("Marco", "Our newest regular deserves a proper welcome."),
        GuideLine("Giulia", "Then put a record on, Marco."),
    ),
    station_sting=True,
    sustained_bed=True,
    pause_ms=600,
)


GUIDE_CLIPS = (
    GuideClip(
        "welcome",
        (
            GuideLine(
                "Marco",
                "Benvenuti! I’m Marco. I cleared you a place in Studio B. Had to move Giulia’s coffee.",
            ),
            GuideLine(
                "Giulia",
                "I’m Giulia. I run the desk. Touch my coffee again and you’re in the corridor, Marco. "
                "Come in. Three small steps. Then we’re on air.",
            ),
        ),
        station_sting=True,
    ),
    GuideClip(
        "sound-check",
        (
            GuideLine(
                "Marco",
                "We are on! Right now, out of that little speaker in your hand. "
                "If you can hear me, and you should, say yes.",
            ),
            GuideLine(
                "Giulia",
                "The machine knows it pressed play. It cannot hear your room. That part is yours, not his.",
            ),
        ),
    ),
    GuideClip(
        "not-yet",
        (
            GuideLine("Marco", "Niente? Va bene. Nobody is blaming anybody. Certainly not me."),
            GuideLine(
                "Giulia",
                "Check the mute switch and the volume, then play it again here. We saved your place. Take your time.",
            ),
        ),
    ),
    GuideClip(
        "receipt-recovery",
        (
            GuideLine("Giulia", "You already heard us. The station just forgot to write it down. Embarrassing."),
            GuideLine(
                "Marco",
                "This only saves the sound check. It does not put me back on air. Tragico.",
            ),
        ),
    ),
    GuideClip(
        "privacy",
        (
            GuideLine("Marco", "Rain all afternoon. I’ve cancelled the rooftop disco."),
            GuideLine(
                "Giulia",
                "It was a speaker on a bin, Marco. Bring it inside. We’re not cancelling Saturday.",
            ),
        ),
    ),
    GuideClip(
        "ai",
        (
            GuideLine("Marco", "Connect a writing service and I’ll have something new to say between records."),
            GuideLine(
                "Giulia",
                "Something new, Marco. That rules out the story about your famous cousin.",
            ),
        ),
    ),
    GuideClip(
        "success",
        (
            GuideLine("Marco", "Siamo in onda! Your first broadcast is live and I was magnificent."),
            GuideLine(
                "Giulia",
                "Sound checked, privacy saved. Mamma Mi Radio is yours now. He was adequate.",
            ),
        ),
        station_sting=True,
    ),
)

FREE_VOICE_CLIP = GuideClip(
    "free-voices",
    (
        GuideLine("Marco", "Mah… Giulia, somebody’s changed my microphone. My cousin says it’s the future."),
        GuideLine("Giulia", "Same us. Different voices. And your cousin still owes us the old microphone."),
    ),
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--clip",
        choices=tuple(clip.clip_id for clip in GUIDE_CLIPS),
        help="replace one clip in an intact canonical pack",
    )
    selection.add_argument(
        "--station-opening", action="store_true", help="render only the English Admin station opening"
    )
    selection.add_argument("--free-voice-example", action="store_true", help="render the configured free voices")
    args = parser.parse_args()
    args.output_root = args.output_root or (STATION_OUTPUT_ROOT if args.station_opening else DEFAULT_OUTPUT_ROOT)
    return args


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
    """Replace a complete file atomically, including across filesystems."""

    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        local_copy = Path(handle.name)
    try:
        shutil.copyfile(staged, local_copy)
        # Keep installed permissions; new public assets must be server-readable.
        local_copy.chmod(destination.stat().st_mode & 0o777 if destination.exists() else 0o644)
        os.replace(local_copy, destination)
        staged.unlink()
    finally:
        local_copy.unlink(missing_ok=True)


def _publish_staged_pack(files: list[tuple[Path, Path]]) -> None:
    """Publish media then manifest; restore the old pack on a catchable failure."""

    root = Path(os.path.commonpath([destination.parent for _, destination in files]))
    backup_dir = Path(tempfile.mkdtemp(prefix=".first-listen-backup-", dir=root))
    originals = []
    cleanup = True
    try:
        for index, (_, destination) in enumerate(files):
            backup = backup_dir / str(index) if destination.exists() else None
            if backup is not None:
                shutil.copy2(destination, backup)
            originals.append((destination, backup))
        try:
            for staged, destination in files:
                _publish_staged_file(staged, destination)
        except Exception:
            try:
                for destination, backup in reversed(originals):
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        os.replace(backup, destination)
            except OSError as exc:
                cleanup = False
                raise RuntimeError(f"publication rollback failed; retained backups at {backup_dir}") from exc
            raise
    finally:
        if cleanup:
            shutil.rmtree(backup_dir)


def _load_pack_validator():
    """Share release media limits without importing runtime before env selection."""

    return runpy.run_path(str(REPO_ROOT / "scripts" / "validate-spoken-assets.py"))["validate_browser_narration_pack"]


def _validated_pack(root: Path, canonical_receipt: dict[str, object]) -> dict[str, object]:
    """Refuse incomplete, modified, or incompatible packs before reuse/publication."""

    errors = _load_pack_validator()(assets_root=root, radio_config_path=REPO_ROOT / "radio.toml", staged_render=True)
    if errors:
        raise RuntimeError("invalid guide pack: " + "; ".join(errors))
    manifest = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    if manifest.get("canonical_render_receipt") != canonical_receipt:
        raise RuntimeError("guide pack canonical voice receipt does not match radio.toml")
    clips = {f"first_listen/{clip.clip_id}.mp3": clip for clip in GUIDE_CLIPS}
    for entry in manifest["assets"]:
        if (
            entry["kind"] != "speech"
            or entry["language"] != "en"
            or entry.get("speakers") != [line.host for line in clips[entry["path"]].lines]
        ):
            raise RuntimeError(f"{entry['path']} must retain its English canonical host dialogue")
    return manifest


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
    free_voices: bool = False,
) -> dict[str, object]:
    from mammamiradio.audio.normalizer import (
        concat_files,
        generate_station_id_bed,
        mix_voice_with_sting,
        probe_duration_sec,
    )

    line_paths = [work_dir / f"{clip.clip_id}-{index}.mp3" for index in range(len(clip.lines))]
    render_line = _render_free_line if free_voices else _render_line
    await asyncio.gather(
        *(render_line(hosts[line.host], line.text, path) for line, path in zip(clip.lines, line_paths, strict=True))
    )

    dialogue_path = work_dir / f"{clip.clip_id}-dialogue.mp3"
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: concat_files(
            line_paths,
            dialogue_path,
            silence_ms=clip.pause_ms,
            loudnorm=not clip.station_sting,
            strict_duration=True,
        ),
    )
    if clip.station_sting:
        sting_path = work_dir / f"{clip.clip_id}-sting.mp3"
        bed_duration = (probe_duration_sec(dialogue_path) or 0) + 0.4 if clip.sustained_bed else 3.0
        await loop.run_in_executor(None, lambda: generate_station_id_bed(sting_path, bed_duration, motif_notes))
        await loop.run_in_executor(None, lambda: mix_voice_with_sting(dialogue_path, sting_path, destination))
    else:
        shutil.move(dialogue_path, destination)

    duration = probe_duration_sec(destination)
    if duration is None or not 4.0 <= duration <= 20.0:
        raise RuntimeError(f"audio duration for {destination.name} must be between 4 and 20 seconds")
    return {
        "path": f"first_listen/{destination.name}",
        "sha256": _sha256(destination),
        "kind": "speech",
        "language": "en",
        "transcript": clip.transcript,
        "duration_seconds": round(duration, 3),
        "speakers": [line.host for line in clip.lines],
    }


async def _render_free_line(host, text: str, output_path: Path) -> Path:
    import aiohttp
    import edge_tts

    from mammamiradio.audio.normalizer import normalize

    class SingleAttemptConnector(aiohttp.TCPConnector):
        attempted = False

        async def connect(self, *args, **kwargs):
            # Edge's public stream retries a 403. Fence that retry before another
            # request leaves the machine; this audition must never change voices.
            if self.attempted:
                raise RuntimeError("free voice request failed; automatic retry is disabled")
            self.attempted = True
            return await super().connect(*args, **kwargs)

    raw = output_path.with_suffix(".edge-raw.mp3")
    async with SingleAttemptConnector() as connector:
        voice = edge_tts.Communicate(text, host.edge_fallback_voice, rate="+0%", pitch="+0Hz", connector=connector)
        await asyncio.wait_for(voice.save(str(raw)), timeout=30)
    await asyncio.to_thread(normalize, raw, output_path, loudnorm=False)
    return output_path


async def _render_free_example(output_root, hosts, canonical_receipt, motif_notes) -> None:
    validator = runpy.run_path(str(REPO_ROOT / "scripts" / "validate-spoken-assets.py"))
    _validated_pack(output_root, canonical_receipt)
    receipt = validator["free_voice_render_receipt"](REPO_ROOT / "radio.toml")
    assert [host["voice_id"] for host in receipt["hosts"]] == [
        hosts[name].edge_fallback_voice for name in CANONICAL_HOST_NAMES
    ]
    with _temporary_work_directory() as raw_work_dir:
        work_dir = Path(raw_work_dir)
        staging = work_dir / "staging"
        shutil.copytree(output_root, staging)
        side = staging / "voice_examples"
        side.mkdir(exist_ok=True)
        destination = side / "free-voices.mp3"
        entry = await _render_clip(
            FREE_VOICE_CLIP, hosts, work_dir, destination, motif_notes=motif_notes, free_voices=True
        )
        entry["path"] = destination.name
        manifest_path = side / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bundle": "first-listen-free-voices",
                    "render_provider": "edge",
                    "render_receipt": receipt,
                    "assets": [entry],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        _validated_pack(staging, canonical_receipt)
        target = output_root / "voice_examples"
        created = not target.exists()
        target.mkdir(exist_ok=True)
        try:
            _publish_staged_pack(
                [(destination, target / destination.name), (manifest_path, target / MANIFEST_FILENAME)]
            )
        except Exception:
            if created:
                with contextlib.suppress(OSError):
                    target.rmdir()  # Only remove the empty directory created by this attempt.
            raise
        print(f"rendered free voices: {entry['duration_seconds']}s")


async def _render_station_opening(output_root, hosts, canonical_receipt, motif_notes) -> None:
    """Retain both inventories, stage one paid render, and validate before publication."""

    validator = runpy.run_path(str(REPO_ROOT / "scripts" / "validate-spoken-assets.py"))
    _validated_pack(DEFAULT_OUTPUT_ROOT, canonical_receipt)
    errors = validator["validate_demo_spoken_assets"](
        assets_root=output_root, package_assets_root=output_root, include_admin_opening=False
    )
    if errors:
        raise RuntimeError("invalid station pack: " + "; ".join(errors))
    manifest = json.loads((output_root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    relative = f"first_listen/{STATION_OPENING_CLIP.clip_id}.mp3"

    def retained(pack):
        return {entry["path"] for entry in pack["assets"] if entry["path"] != relative}

    if retained(manifest) != set(validator["DEMO_SPOKEN_PATHS"]) - {relative}:
        raise RuntimeError("station pack retained inventory does not match the shipped pack")
    with _temporary_work_directory() as raw_work_dir:
        work_dir = Path(raw_work_dir)
        staging = work_dir / "staging"
        shutil.copytree(output_root, staging)
        destination = staging / relative
        entry = await _render_clip(STATION_OPENING_CLIP, hosts, work_dir, destination, motif_notes=motif_notes)
        entry["canonical_render_receipt"] = canonical_receipt
        manifest["assets"] = [old for old in manifest["assets"] if old["path"] != relative] + [entry]
        staged_manifest = staging / MANIFEST_FILENAME
        staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        errors = validator["validate_demo_spoken_assets"](assets_root=staging, package_assets_root=staging)
        if errors:
            raise RuntimeError("invalid station render: " + "; ".join(errors))
        _publish_staged_pack(
            [(destination, output_root / relative), (staged_manifest, output_root / MANIFEST_FILENAME)]
        )
        print(f"rendered station opening: {entry['duration_seconds']}s; wrote {_display_path(output_root / relative)}")


async def _run(args: argparse.Namespace) -> None:
    _load_environment(args.env_file)
    if not getattr(args, "free_voice_example", False) and not os.getenv("ELEVENLABS_API_KEY"):
        raise RuntimeError("canonical rendering requires ELEVENLABS_API_KEY")

    config = _load_station_config(REPO_ROOT / "radio.toml")
    hosts = {host.name: host for host in config.hosts if host.name in CANONICAL_HOST_NAMES}
    if set(hosts) != set(CANONICAL_HOST_NAMES):
        raise RuntimeError("radio.toml must configure both Marco and Giulia")
    canonical_receipt = _canonical_render_receipt(hosts)

    output_root = args.output_root.resolve()
    if getattr(args, "free_voice_example", False):
        await _render_free_example(output_root, hosts, canonical_receipt, config.sonic_brand.motif_notes)
        return
    if getattr(args, "station_opening", False):
        await _render_station_opening(output_root, hosts, canonical_receipt, config.sonic_brand.motif_notes)
        return
    selected_clip = args.clip
    # Validation happens before rendering: partial generation must never silently
    # turn six missing or incompatible clips into a mixed-provider release pack.
    manifest = (
        _validated_pack(output_root, canonical_receipt)
        if selected_clip
        else {
            "schema_version": 1,
            "bundle": "first-listen-guide",
            "render_provider": "canonical",
            "canonical_render_receipt": canonical_receipt,
            "assets": [],
        }
    )

    with _temporary_work_directory() as raw_work_dir:
        work_dir = Path(raw_work_dir)
        staging_dir = work_dir / "staging"
        (staging_dir / "first_listen").mkdir(parents=True)
        if (output_root / "voice_examples").exists() or (output_root / "voice_examples").is_symlink():
            # Preserve the separately attested audition even for a full guide render.
            if not selected_clip:
                errors = runpy.run_path(str(REPO_ROOT / "scripts/validate-spoken-assets.py"))[
                    "validate_free_voice_example"
                ](assets_root=output_root, staged_render=True)
                if errors:
                    raise RuntimeError("invalid retained free voice example: " + "; ".join(errors))
            shutil.copytree(output_root / "voice_examples", staging_dir / "voice_examples")
        if selected_clip:
            for entry in manifest["assets"]:
                shutil.copyfile(output_root / entry["path"], staging_dir / entry["path"])
        clips = [clip for clip in GUIDE_CLIPS if not selected_clip or clip.clip_id == selected_clip]
        for clip in clips:
            destination = staging_dir / "first_listen" / f"{clip.clip_id}.mp3"
            entry = await _render_clip(clip, hosts, work_dir, destination, motif_notes=config.sonic_brand.motif_notes)
            if selected_clip:
                manifest["assets"] = [entry if old["path"] == entry["path"] else old for old in manifest["assets"]]
            else:
                manifest["assets"].append(entry)
            print(f"rendered {clip.clip_id}: {entry['duration_seconds']}s")

        staged_manifest = staging_dir / MANIFEST_FILENAME
        staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _validated_pack(staging_dir, canonical_receipt)
        # No destination is touched until every selected render and the complete
        # staged manifest pass. Retained clips are never republished.
        output_dir = output_root / "first_listen"
        output_dir.mkdir(parents=True, exist_ok=True)
        _publish_staged_pack(
            [
                (staging_dir / "first_listen" / f"{clip.clip_id}.mp3", output_dir / f"{clip.clip_id}.mp3")
                for clip in clips
            ]
            + [(staged_manifest, output_root / MANIFEST_FILENAME)]
        )
        print(f"wrote {_display_path(output_root / MANIFEST_FILENAME)}")


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
