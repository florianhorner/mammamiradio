#!/usr/bin/env python3
"""Generate neutral, local-review clips into assets/demo/welcome/.

The runtime deliberately does not discover this directory. This historical
utility remains useful for voice review, but generated files are not eligible
for playback unless a future explicit manifest policy admits them.

Defaults to the free Edge engine, so no API key is required to regenerate the
clips. The fixed lines contain no listener arrival, return, or recognition
claim.

Usage:
    python scripts/generate_welcome_clips.py                 # write missing clips
    python scripts/generate_welcome_clips.py --overwrite      # rebuild all clips
    python scripts/generate_welcome_clips.py --dry-run        # list, write nothing
    python scripts/generate_welcome_clips.py --output-dir DIR # write elsewhere
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mammamiradio.audio import tts as tts_module  # noqa: E402
from mammamiradio.audio.audio_quality import (  # noqa: E402
    AudioQualityError,
    AudioToolError,
    _probe_duration_sec,
    _probe_volume,
)
from mammamiradio.core.config import StationConfig, load_config  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "mammamiradio" / "assets" / "demo" / "welcome"
DEFAULT_CONFIG_PATH = REPO_ROOT / "radio.toml"

STATUS_GENERATED = "generated"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_PLANNED = "planned"

# Pure digital silence measures near the floor (~-91 dBFS peak); real speech
# peaks far above this, so -80 cleanly splits them. Runtime TTS now raises when
# every route fails, but this remains a defense against stale or malformed output.
SILENCE_PEAK_DBFS = -80.0
MIN_CLIP_BYTES = 1024
MIN_CLIP_DURATION_SEC = 0.5

# Render intermediates here, never directly in the clip dir. Synthesis also
# drops a sibling ``.raw.mp3``; staging preserves a clean review directory and
# keeps the final publish atomic even though runtime discovery is disabled.
STAGING_DIRNAME = ".staging"


@dataclass(frozen=True)
class WelcomeClip:
    """One welcome clip: output filename, configured host, and line to speak."""

    filename: str
    host_name: str
    text: str
    # Filled from the configured host immediately before synthesis. Keeping it
    # out of the contract prevents stale hard-coded Edge voices from drifting
    # away from the actual on-air host identity.
    voice: str = ""


# The historical contract. Keep filenames stable for reproducible local review.
# Voice IDs are deliberately resolved from radio.toml below, never frozen here.
WELCOME_CLIPS: tuple[WelcomeClip, ...] = (
    WelcomeClip(
        "marco_welcome_1.mp3",
        "Marco",
        "Siamo sempre in onda. La musica continua, piano piano.",
    ),
    WelcomeClip("marco_welcome_2.mp3", "Marco", "Studio B resiste. Un attimo e torna il prossimo disco."),
    WelcomeClip("giulia_welcome_1.mp3", "Giulia", "La frequenza resta accesa. Nessun dramma, quasi."),
    WelcomeClip("giulia_welcome_2.mp3", "Giulia", "Mamma Mi Radio continua. La musica sa dove andare."),
)


@dataclass(frozen=True)
class ClipResult:
    """Outcome for one clip: where it would/did land and what happened."""

    clip: WelcomeClip
    output_path: Path
    status: str
    error: str = ""


def resolve_welcome_clips(
    config: StationConfig,
    clips: tuple[WelcomeClip, ...] = WELCOME_CLIPS,
) -> tuple[WelcomeClip, ...]:
    """Resolve each welcome line through its configured host's usable Edge voice.

    The asset generator remains deliberately free and deterministic, so it
    renders with Edge even when the live host uses a paid engine. In that case,
    use the host's explicit Edge rescue voice rather than a stale, unrelated
    hard-coded default. A missing/ambiguous host is a configuration error rather
    than an opportunity to create a greeting in the wrong character.
    """
    resolved: list[WelcomeClip] = []
    for clip in clips:
        matches = [host for host in config.hosts if host.name.casefold() == clip.host_name.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Welcome clip host '{clip.host_name}' must resolve to exactly one configured host")
        host = matches[0]
        voice = host.voice if (host.engine or "edge").lower() == "edge" else host.edge_fallback_voice
        if not voice:
            raise ValueError(f"Welcome clip host '{host.name}' has no usable Edge voice")
        resolved.append(replace(clip, voice=voice))
    return tuple(resolved)


def _discard(path: Path) -> str:
    """Best-effort delete of a rejected render; returns a note if it couldn't be removed."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # a cleanup failure must not abort the batch
        return f"; could not delete the file ({exc})"
    return ""


def _looks_like_silence(path: Path) -> bool:
    """True if a rendered clip is effectively silent.

    ``synthesize()`` raises when every configured route fails. Measuring peak
    level remains a defense-in-depth check for stale, malformed, or unexpectedly
    silent output before a greeting is committed. Best-effort: if the level
    cannot be measured, do not claim silence and risk a false failure.
    """
    try:
        _mean_db, peak_db = _probe_volume(path)
    except (AudioToolError, OSError):
        return False
    return peak_db is not None and peak_db <= SILENCE_PEAK_DBFS


def _staging_path(dest: Path) -> Path:
    """Path for a render inside the hidden staging subdir of dest's directory.

    Keeps the in-progress render (and synthesize's sibling ``.raw.mp3``) out of
    the runtime-globbed clip directory, so an interrupted generation can never
    leave a servable partial clip behind. The publish back into the clip dir is a
    same-filesystem atomic ``replace`` (see STAGING_DIRNAME).
    """
    return dest.parent / STAGING_DIRNAME / f"{dest.stem}.{uuid4().hex}.tmp{dest.suffix}"


def _cleanup_staging_dir(output_dir: Path) -> None:
    """Best-effort removal of the staging subdir once a batch is done.

    Leftover staging files are already invisible to the runtime glob (they live
    in a subdirectory), so this is hygiene, not safety: a failure to remove the
    dir is ignored rather than allowed to abort or mask the batch result.
    """
    shutil.rmtree(output_dir / STAGING_DIRNAME, ignore_errors=True)


def _validate_render(path: Path) -> str:
    """Return a failure reason if the rendered clip is clearly unusable."""
    if not path.exists():
        return "rendered clip missing"
    size = path.stat().st_size
    if size < MIN_CLIP_BYTES:
        return f"rendered clip too small ({size} bytes < {MIN_CLIP_BYTES} bytes)"
    try:
        duration = _probe_duration_sec(path)
    except (AudioQualityError, AudioToolError, OSError) as exc:
        return f"could not measure rendered clip duration ({exc})"
    if duration is not None and duration < MIN_CLIP_DURATION_SEC:
        return f"rendered clip too short ({duration:.2f}s < {MIN_CLIP_DURATION_SEC:.2f}s)"
    return ""


async def generate_clips(
    clips: tuple[WelcomeClip, ...],
    output_dir: Path,
    *,
    config: StationConfig | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[ClipResult]:
    """Synthesize each welcome clip into output_dir, skipping ones that exist.

    Always renders through Edge. If a configured host uses a cloud engine, its
    explicit Edge fallback is selected before rendering. Returns one ClipResult
    per clip. Best-effort per clip:
    a single failure (a flaky voice, an unwritable output dir, an unexpectedly
    silent render, or a substituted voice) is recorded as STATUS_FAILED and
    does not abort the remaining clips.
    """
    if config is not None:
        clips = resolve_welcome_clips(config, clips)
    elif any(not clip.voice for clip in clips):
        clips = resolve_welcome_clips(load_config(str(DEFAULT_CONFIG_PATH)), clips)

    results: list[ClipResult] = []
    staging_dir_ready = False
    try:
        for clip in clips:
            dest = output_dir / clip.filename
            staging = _staging_path(dest)
            # Check existence before the dry-run short-circuit so a preview reports
            # already-present clips as "skipped" (what a real run would do), not "planned".
            if dest.exists() and not overwrite:
                results.append(ClipResult(clip, dest, STATUS_SKIPPED))
                continue
            if dry_run:
                results.append(ClipResult(clip, dest, STATUS_PLANNED))
                continue
            try:
                if not staging_dir_ready:
                    # Create the staging subdir once, on the first real render — this
                    # also creates output_dir (its parent). Skipped entirely for a dry
                    # run or an all-skipped batch, which therefore write nothing.
                    staging.parent.mkdir(parents=True, exist_ok=True)
                    staging_dir_ready = True
                await tts_module.synthesize(clip.text, clip.voice, staging, engine="edge")
            except Exception as exc:  # one bad voice / FS error must not abort the batch
                # Drop any partial render so a rerun doesn't mistake it for a good clip.
                note = _discard(staging)
                results.append(ClipResult(clip, dest, STATUS_FAILED, error=f"{exc}{note}"))
                continue
            render_error = _validate_render(staging)
            if render_error:
                note = _discard(staging)
                results.append(ClipResult(clip, dest, STATUS_FAILED, error=f"{render_error}; clip discarded{note}"))
                continue
            if _looks_like_silence(staging):
                # Defense in depth: active TTS failures raise, but never let an
                # unexpectedly silent artifact become a packaged greeting.
                note = _discard(staging)
                results.append(
                    ClipResult(
                        clip,
                        dest,
                        STATUS_FAILED,
                        error=f"rendered audio was effectively silent; clip discarded{note}",
                    )
                )
                continue
            if clip.voice in tts_module._failed_edge_voices:
                # synthesize() silently substitutes the default Edge fallback voice when
                # the requested voice fails, so this render would be the right words in the
                # wrong speaker. Reject it rather than ship a mismatched greeting.
                note = _discard(staging)
                results.append(
                    ClipResult(
                        clip,
                        dest,
                        STATUS_FAILED,
                        error=f"requested voice unavailable — rendered in a fallback voice; clip discarded{note}",
                    )
                )
                continue
            try:
                staging.replace(dest)
            except OSError as exc:
                note = _discard(staging)
                results.append(ClipResult(clip, dest, STATUS_FAILED, error=f"could not publish clip ({exc}){note}"))
                continue
            results.append(ClipResult(clip, dest, STATUS_GENERATED))
    finally:
        # Always clear the staging subdir — even on Ctrl-C / task cancellation
        # (BaseException), so no partial render lingers in the packaged asset tree.
        _cleanup_staging_dir(output_dir)
    return results


def _print_summary(results: list[ClipResult], output_dir: Path) -> None:
    """Print one status line per clip plus an aggregate count breakdown."""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        line = f"{result.status}\t{result.clip.filename}\t({result.clip.voice})"
        if result.error:
            line += f"\t{result.error}"
        print(line)
    print(f"\nOutput: {output_dir}")
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"Counts: {breakdown or 'none'}")


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args, run generation, print the summary, and return an exit code.

    Returns 1 if any clip failed (so an operator or CI notices), otherwise 0.
    """
    parser = argparse.ArgumentParser(
        prog="generate_welcome_clips.py",
        description="Generate neutral Italian station-continuity clips for local review.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write clips into (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Station config used to resolve Marco and Giulia (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Rebuild clips that already exist (default: skip existing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the clips that would be generated without writing anything.",
    )
    args = parser.parse_args(argv)

    try:
        clips = resolve_welcome_clips(load_config(str(args.config)))
        results = asyncio.run(
            generate_clips(
                clips,
                args.output_dir,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
            )
        )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    _print_summary(results, args.output_dir)
    return 1 if any(r.status == STATUS_FAILED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
