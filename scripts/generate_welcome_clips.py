#!/usr/bin/env python3
"""Generate the bundled welcome clips into mammamiradio/assets/demo/welcome/.

Welcome clips are the DJ "interrupting" the broadcast to greet a listener.
The playback loop reaches for them via _pick_canned_clip("welcome") as one of
its instant-audio fallbacks (after canned banter, before forced TTS), so an
empty welcome/ directory quietly removes a rescue rung. This script populates
that directory from a fixed, Italian-only contract using the station's own TTS
pipeline, replacing the fragile copy-paste `python -c` snippet that used to
live in welcome/README.md.

Defaults to the free Edge engine, so no API key is required to regenerate the
clips. The clips are committed-asset candidates: run this locally, listen, then
commit the MP3s if they sound right.

Usage:
    python scripts/generate_welcome_clips.py                 # write missing clips
    python scripts/generate_welcome_clips.py --overwrite      # rebuild all clips
    python scripts/generate_welcome_clips.py --dry-run        # list, write nothing
    python scripts/generate_welcome_clips.py --output-dir DIR # write elsewhere
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.audio import tts as tts_module

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "mammamiradio" / "assets" / "demo" / "welcome"

STATUS_GENERATED = "generated"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_PLANNED = "planned"


@dataclass(frozen=True)
class WelcomeClip:
    """One welcome clip: output filename, TTS voice, and the line to speak."""

    filename: str
    voice: str
    text: str


# The contract. Italian-only by design — these match the station identity and
# its two house hosts (Marco / Giulia). Keep filenames stable: the runtime globs
# welcome/*.mp3, but committing predictable names keeps regeneration idempotent.
WELCOME_CLIPS: tuple[WelcomeClip, ...] = (
    WelcomeClip(
        "marco_welcome_1.mp3",
        "it-IT-GiuseppeMultilingualNeural",
        "Eyyy, qualcuno si e collegato! Benvenuto, benvenuto!",
    ),
    WelcomeClip(
        "marco_welcome_2.mp3", "it-IT-GiuseppeMultilingualNeural", "Eccolo! Un nuovo ascoltatore! Che bello, che bello!"
    ),
    WelcomeClip("giulia_welcome_1.mp3", "it-IT-ElsaNeural", "Benvenuto... vediamo cosa ci hai portato oggi."),
    WelcomeClip("giulia_welcome_2.mp3", "it-IT-ElsaNeural", "Oh, qualcuno si e sintonizzato. Finalmente."),
)


@dataclass(frozen=True)
class ClipResult:
    """Outcome for one clip: where it would/did land and what happened."""

    clip: WelcomeClip
    output_path: Path
    status: str
    error: str = ""


async def generate_clips(
    clips: tuple[WelcomeClip, ...],
    output_dir: Path,
    *,
    engine: str = "edge",
    overwrite: bool = False,
    dry_run: bool = False,
) -> list[ClipResult]:
    """Synthesize each welcome clip into output_dir, skipping ones that exist.

    Returns one ClipResult per clip. Best-effort per clip: a single TTS failure
    is recorded as STATUS_FAILED and does not abort the remaining clips, so one
    flaky voice never blocks regenerating the rest.
    """
    results: list[ClipResult] = []
    for clip in clips:
        dest = output_dir / clip.filename
        if dry_run:
            results.append(ClipResult(clip, dest, STATUS_PLANNED))
            continue
        if dest.exists() and not overwrite:
            results.append(ClipResult(clip, dest, STATUS_SKIPPED))
            continue
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            await tts_module.synthesize(clip.text, clip.voice, dest, engine=engine)
            results.append(ClipResult(clip, dest, STATUS_GENERATED))
        except Exception as exc:  # one bad voice must not abort the batch
            results.append(ClipResult(clip, dest, STATUS_FAILED, error=str(exc)))
    return results


def _print_summary(results: list[ClipResult], output_dir: Path) -> None:
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
    parser = argparse.ArgumentParser(
        prog="generate_welcome_clips.py",
        description="Generate the bundled Italian welcome clips for the demo asset tree.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write clips into (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--engine",
        default="edge",
        help="TTS engine to use (default: edge — free, no API key).",
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

    results = asyncio.run(
        generate_clips(
            WELCOME_CLIPS,
            args.output_dir,
            engine=args.engine,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
    )
    _print_summary(results, args.output_dir)
    return 1 if any(r.status == STATUS_FAILED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
