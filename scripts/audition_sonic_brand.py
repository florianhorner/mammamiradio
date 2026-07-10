#!/usr/bin/env python3
"""Build a local A/B and scene-recipe listening pack for recorded Mamma Mi Radio imaging.

The script never calls TTS providers, starts the station, or touches its queue. It
renders the current procedural baseline into a timestamped directory, copies the
packaged Night Drive assets beside it, and writes a standalone ``index.html`` plus
``manifest.json`` for a human listening review.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from mammamiradio.audio.imaging import ImagingLibrary
from mammamiradio.audio.normalizer import (
    AVAILABLE_SFX_TYPES,
    generate_bumper_jingle,
    generate_station_id_bed,
    generate_tone,
    generate_transition_sting,
    loop_audio_bed,
    mix_oneshot_layers,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGED_ASSETS_DIR = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "sonic-brand-auditions"
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
MOTIF_NOTES = [523, 659, 784, 1047]
TALK_BED_DURATION_SEC = 8.0
RECIPE_PREVIEW_DURATION_SEC = 8.0


@dataclass(frozen=True)
class SonicSample:
    """One local listening comparison and its required packaged counterpart."""

    key: str
    label: str
    packaged_asset: Path
    output_name: str


@dataclass(frozen=True)
class SonicAuditionResult:
    """Manifest-safe record of one baseline versus packaged comparison."""

    key: str
    label: str
    source_asset: str
    baseline: str
    night_drive: str


@dataclass(frozen=True)
class RecipeAuditionResult:
    """One local preview of a reviewed ad scene recipe, without a TTS call."""

    id: str
    label: str
    audio: str
    bed: str
    cues: tuple[str, ...]


SAMPLES: tuple[SonicSample, ...] = (
    SonicSample("station_id", "Station ID bed", Path("station_id.mp3"), "station_id.mp3"),
    SonicSample("sweeper", "Sweeper bed", Path("sweeper.mp3"), "sweeper.mp3"),
    SonicSample("time_check", "Time-check chime", Path("time_check.mp3"), "time_check.mp3"),
    SonicSample(
        "music_to_speech",
        "Music to speech stinger",
        Path("stingers") / "music_to_speech.mp3",
        "music_to_speech.mp3",
    ),
    SonicSample(
        "speech_to_music",
        "Speech to music stinger",
        Path("stingers") / "speech_to_music.mp3",
        "speech_to_music.mp3",
    ),
    SonicSample("ad_bumper", "Ad-break bumper", Path("bumpers") / "ad_break.mp3", "ad_bumper.mp3"),
    SonicSample("talk_bed", "Casa Notte talk bed", Path("beds") / "casa_notte.mp3", "talk_bed.mp3"),
)


def _timestamp(value: str | None = None) -> str:
    stamp = value or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDDTHHMMSSZ format")
    return stamp


def required_pack_paths() -> tuple[Path, ...]:
    """Return the complete Night Drive contract, including the shared SFX bank."""
    return (
        Path("manifest.json"),
        *(sample.packaged_asset for sample in SAMPLES),
        *(Path("sfx") / f"{sfx_name}.mp3" for sfx_name in AVAILABLE_SFX_TYPES),
    )


def missing_pack_paths(assets_dir: Path) -> list[Path]:
    """Return missing or empty Night Drive assets relative to ``assets_dir``."""
    missing: list[Path] = []
    for relative_path in required_pack_paths():
        candidate = assets_dir / relative_path
        try:
            usable = candidate.is_file() and candidate.stat().st_size > 0
        except OSError:
            usable = False
        if not usable:
            missing.append(relative_path)
    return missing


def require_pack_assets(assets_dir: Path) -> None:
    """Fail before rendering when the planned packaged asset contract is incomplete."""
    missing = missing_pack_paths(assets_dir)
    if not missing:
        return
    names = ", ".join(path.as_posix() for path in missing)
    raise FileNotFoundError(f"Night Drive pack is incomplete under {assets_dir}: missing or empty {names}")


def render_baseline_sample(sample: SonicSample, output_path: Path) -> Path:
    """Render the live procedural equivalent of one audition surface locally."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if sample.key == "station_id":
        return generate_station_id_bed(output_path, 3.0, MOTIF_NOTES)
    if sample.key == "sweeper":
        return generate_station_id_bed(output_path, 2.0, MOTIF_NOTES)
    if sample.key == "time_check":
        return generate_tone(output_path, 1047, 0.3)
    if sample.key == "music_to_speech":
        return generate_transition_sting("music", "banter", output_path, MOTIF_NOTES)
    if sample.key == "speech_to_music":
        return generate_transition_sting("banter", "music", output_path, MOTIF_NOTES)
    if sample.key == "ad_bumper":
        return generate_bumper_jingle(output_path)
    if sample.key == "talk_bed":
        # A deliberately-empty assets root forces the same synthetic-drone branch
        # used by a cold station when no packaged bed or adjacent track is available.
        baseline_assets_dir = output_path.parent / ".no-packaged-beds"
        return ImagingLibrary(MOTIF_NOTES, output_path.parent, assets_dir=baseline_assets_dir).pick_talk_bed(
            TALK_BED_DURATION_SEC,
            output_path,
        )
    raise ValueError(f"Unsupported sonic audition sample: {sample.key}")


def render_audition(run_dir: Path, *, assets_dir: Path | None = None) -> list[SonicAuditionResult]:
    """Render procedural baselines and copy the packaged recorded counterparts."""
    assets_dir = assets_dir or PACKAGED_ASSETS_DIR
    require_pack_assets(assets_dir)
    if run_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing audition directory: {run_dir}")

    baseline_dir = run_dir / "baseline"
    night_drive_dir = run_dir / "night-drive"
    results: list[SonicAuditionResult] = []
    for sample in SAMPLES:
        baseline_path = baseline_dir / sample.output_name
        night_drive_path = night_drive_dir / sample.output_name
        render_baseline_sample(sample, baseline_path)
        night_drive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets_dir / sample.packaged_asset, night_drive_path)
        results.append(
            SonicAuditionResult(
                key=sample.key,
                label=sample.label,
                source_asset=sample.packaged_asset.as_posix(),
                baseline=baseline_path.relative_to(run_dir).as_posix(),
                night_drive=night_drive_path.relative_to(run_dir).as_posix(),
            )
        )
    return results


def _recipe_preview_offset(anchor: str, cue_duration_sec: float) -> float:
    """Place a dry cue on a neutral review timeline without inventing speech."""
    latest = max(0.0, RECIPE_PREVIEW_DURATION_SEC - cue_duration_sec)
    if anchor == "intro":
        return 0.0
    if anchor == "after_first_voice":
        return min(2.2, latest)
    if anchor == "outro":
        return latest
    return min(4.1, latest)  # documented ``mid`` and safe unknown-label fallback


def render_recipe_previews(run_dir: Path, *, assets_dir: Path | None = None) -> list[RecipeAuditionResult]:
    """Render every declared recipe over its own bed for a zero-provider audition.

    This intentionally contains no fake voice: the result isolates the thing
    a listener needs to approve here — level, texture, and the placement of the
    real crowd/brass/foley details — without asking a network TTS engine to
    manufacture review material.
    """
    assets_dir = assets_dir or PACKAGED_ASSETS_DIR
    manifest = json.loads((assets_dir / "manifest.json").read_text(encoding="utf-8"))
    raw_recipes = manifest.get("recipes")
    if not isinstance(raw_recipes, list):
        raise ValueError("Night Drive pack manifest has no recipe inventory")

    library = ImagingLibrary(MOTIF_NOTES, run_dir / ".recipe-tmp", assets_dir=assets_dir)
    previews_dir = run_dir / "scene-recipes"
    previews_dir.mkdir(parents=True, exist_ok=True)
    results: list[RecipeAuditionResult] = []
    for raw_recipe in raw_recipes:
        if not isinstance(raw_recipe, dict) or not isinstance(raw_recipe.get("id"), str):
            raise ValueError("Night Drive pack contains an invalid recipe id")
        recipe_id = raw_recipe["id"]
        recipe = library.resolve_ad_recipe(recipe_id, variant_key="audition")
        if recipe is None or recipe.bed_path is None:
            raise ValueError(f"Night Drive pack recipe is not auditionable: {recipe_id}")

        bed_render = previews_dir / f".{recipe_id}.bed.mp3"
        output_path = previews_dir / f"{recipe_id}.mp3"
        loop_audio_bed(recipe.bed_path, bed_render, RECIPE_PREVIEW_DURATION_SEC)
        layers = [
            (
                cue.asset_path,
                _recipe_preview_offset(cue.anchor, cue.max_duration_sec),
                cue.gain_db,
                cue.max_duration_sec,
            )
            for cue in recipe.cues
        ]
        if layers:
            mix_oneshot_layers(bed_render, layers, output_path)
            bed_render.unlink(missing_ok=True)
        else:
            shutil.move(str(bed_render), str(output_path))
        results.append(
            RecipeAuditionResult(
                id=recipe.id,
                label=recipe.id.replace("_", " ").title(),
                audio=output_path.relative_to(run_dir).as_posix(),
                bed=recipe.bed_path.relative_to(assets_dir).as_posix(),
                cues=tuple(
                    f"{cue.anchor} → {cue.asset_path.relative_to(assets_dir).as_posix()}" for cue in recipe.cues
                ),
            )
        )
    return results


def write_manifest(
    results: list[SonicAuditionResult],
    run_dir: Path,
    *,
    timestamp: str,
    recipe_results: list[RecipeAuditionResult] | None = None,
) -> Path:
    """Write the local, relative-path manifest consumed by a listening review."""
    manifest_path = run_dir / "manifest.json"
    payload = {
        "generated_at": timestamp,
        "mode": "local-only",
        "pack": "Recorded Night Drive",
        "samples": [asdict(result) for result in results],
        "scene_recipes": [asdict(result) for result in recipe_results or []],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return manifest_path


def write_index_html(
    results: list[SonicAuditionResult],
    run_dir: Path,
    *,
    timestamp: str,
    recipe_results: list[RecipeAuditionResult] | None = None,
) -> Path:
    """Write a dependency-free A/B listening page beside the generated audio."""
    rows = "\n".join(
        f"""        <section class=\"sample\">
          <h2>{html.escape(result.label)}</h2>
          <p><code>{html.escape(result.source_asset)}</code></p>
          <div class=\"pair\">
            <figure>
              <figcaption>Procedural baseline</figcaption>
              <audio controls preload=\"metadata\">
                <source src=\"{html.escape(result.baseline, quote=True)}\" type=\"audio/mpeg\">
                Your browser cannot play this file.
              </audio>
            </figure>
            <figure>
              <figcaption>Night Drive packaged asset</figcaption>
              <audio controls preload=\"metadata\">
                <source src=\"{html.escape(result.night_drive, quote=True)}\" type=\"audio/mpeg\">
                Your browser cannot play this file.
              </audio>
            </figure>
          </div>
        </section>"""
        for result in results
    )
    recipe_rows = "\n".join(
        f"""        <section class=\"sample recipe\">
          <h2>{html.escape(result.label)}</h2>
          <p><code>{html.escape(result.bed)}</code></p>
          <p class=\"lede\">{" · ".join(html.escape(cue) for cue in result.cues) or "Bed only"}</p>
          <audio controls preload=\"metadata\">
            <source src=\"{html.escape(result.audio, quote=True)}\" type=\"audio/mpeg\">
            Your browser cannot play this file.
          </audio>
        </section>"""
        for result in recipe_results or []
    )
    document = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Mamma Mi Radio — Night Drive A/B audition</title>
    <style>
      :root {{ color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif;
        background: #14110f; color: #f5edd8; }}
      body {{ max-width: 64rem; margin: 0 auto; padding: 2rem 1rem 4rem; }}
      h1 {{ margin-bottom: .25rem; }}
      .lede, code {{ color: #d8c9af; }}
      .sample {{ border-top: 1px solid #59463a; padding: 1.25rem 0; }}
      .recipe {{ background: #1b211d; padding-inline: 1rem; border-radius: .75rem; }}
      .sample h2 {{ margin: 0; }}
      .sample p {{ margin: .35rem 0 1rem; }}
      .pair {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 1rem; }}
      figure {{ background: #251e19; border-radius: .75rem; margin: 0; padding: 1rem; }}
      figcaption {{ font-weight: 700; margin-bottom: .75rem; }}
      audio {{ width: 100%; }}
    </style>
  </head>
  <body>
    <h1>Recorded Night Drive audition</h1>
    <p class=\"lede\">Generated locally at {html.escape(timestamp)}. Left is the current procedural render;
      right is the packaged recorded asset. Scene recipes below isolate their real bed and cues.
      No station queue or network provider was used.</p>
{rows}
    <h1>Ad scene recipes</h1>
{recipe_rows}
  </body>
</html>
"""
    index_path = run_dir / "index.html"
    index_path.write_text(document, encoding="utf-8")
    return index_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Base directory for audition runs")
    parser.add_argument(
        "--timestamp",
        help="Override run timestamp in YYYYMMDDTHHMMSSZ format, useful for deterministic reviews",
    )
    parser.add_argument(
        "--no-recipe-previews",
        action="store_true",
        help="Skip the local scene-recipe board (useful for a fast core-identity comparison)",
    )
    args = parser.parse_args(argv)

    try:
        timestamp = _timestamp(args.timestamp)
        # Validate first so an incomplete package cannot leave a partial audition
        # directory that looks reviewable.
        require_pack_assets(PACKAGED_ASSETS_DIR)
        run_dir = args.output_dir / f"audition-{timestamp}"
        results = render_audition(run_dir)
        recipe_results = [] if args.no_recipe_previews else render_recipe_previews(run_dir)
        manifest_path = write_manifest(results, run_dir, timestamp=timestamp, recipe_results=recipe_results)
        index_path = write_index_html(results, run_dir, timestamp=timestamp, recipe_results=recipe_results)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Audition: {run_dir}")
    print(f"Manifest: {manifest_path}")
    print(f"Listening page: {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
