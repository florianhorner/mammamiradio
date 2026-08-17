#!/usr/bin/env python3
"""Build a local A/B and recipe board for Mamma Mi Radio's Modern Night Drive imaging.

The script never calls TTS providers, starts the station, or touches its queue. It
can render the current procedural-versus-packaged review, the historical
source-backed motif gate, or the revised local-synthesis treatment gate bound to
an approved production-voice manifest. It can also render the provider-free core
cadence board from an approved treatment receipt plus a frozen approved-route
speech manifest. Every mode writes a standalone local listening page.
"""

from __future__ import annotations

import argparse
import html
import importlib
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

try:
    pack_builder = importlib.import_module("scripts.build_public_imaging_pack")
except ModuleNotFoundError:  # Direct ``python scripts/audition_sonic_brand.py`` invocation.
    pack_builder = importlib.import_module("build_public_imaging_pack")

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGED_ASSETS_DIR = REPO_ROOT / "mammamiradio" / "assets" / "imaging"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "tmp" / "sonic-brand-auditions"
TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")
# Historical comparison only. New motif candidates never use this retired
# C-E-G-C sequence.
LEGACY_MOTIF_NOTES = [523, 659, 784, 1047]
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
    modern_night_drive: str


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
    SonicSample("ad_in", "Ad-break entry bumper", Path("bumpers") / "ad_in.mp3", "ad_in.mp3"),
    SonicSample("ad_mid", "Ad-break middle bumper", Path("bumpers") / "ad_mid.mp3", "ad_mid.mp3"),
    SonicSample("ad_out", "Ad-break exit bumper", Path("bumpers") / "ad_out.mp3", "ad_out.mp3"),
    SonicSample("talk_bed", "Casa Notte talk bed", Path("beds") / "casa_notte.mp3", "talk_bed.mp3"),
)


def _timestamp(value: str | None = None) -> str:
    stamp = value or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if not TIMESTAMP_RE.fullmatch(stamp):
        raise ValueError("timestamp must use YYYYMMDDTHHMMSSZ format")
    return stamp


def required_pack_paths() -> tuple[Path, ...]:
    """Return the Modern Night Drive audition contract, including compatibility SFX."""
    return (
        Path("manifest.json"),
        *(sample.packaged_asset for sample in SAMPLES),
        *(Path("sfx") / f"{sfx_name}.mp3" for sfx_name in AVAILABLE_SFX_TYPES),
    )


def missing_pack_paths(assets_dir: Path) -> list[Path]:
    """Return missing or empty Modern Night Drive assets relative to ``assets_dir``."""
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
    raise FileNotFoundError(f"Modern Night Drive pack is incomplete under {assets_dir}: missing or empty {names}")


def render_baseline_sample(sample: SonicSample, output_path: Path) -> Path:
    """Render the live procedural equivalent of one audition surface locally."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if sample.key == "station_id":
        return generate_station_id_bed(output_path, 3.0, LEGACY_MOTIF_NOTES)
    if sample.key == "sweeper":
        return generate_station_id_bed(output_path, 2.0, LEGACY_MOTIF_NOTES)
    if sample.key == "time_check":
        return generate_tone(output_path, 1047, 0.3)
    if sample.key == "music_to_speech":
        return generate_transition_sting("music", "banter", output_path, LEGACY_MOTIF_NOTES)
    if sample.key == "speech_to_music":
        return generate_transition_sting("banter", "music", output_path, LEGACY_MOTIF_NOTES)
    if sample.key in {"ad_in", "ad_mid", "ad_out"}:
        return generate_bumper_jingle(output_path)
    if sample.key == "talk_bed":
        # A deliberately-empty assets root forces the same synthetic-drone branch
        # used by a cold station when no packaged bed or adjacent track is available.
        baseline_assets_dir = output_path.parent / ".no-packaged-beds"
        return ImagingLibrary(
            LEGACY_MOTIF_NOTES,
            output_path.parent,
            assets_dir=baseline_assets_dir,
        ).pick_talk_bed(
            TALK_BED_DURATION_SEC,
            output_path,
        )
    raise ValueError(f"Unsupported sonic audition sample: {sample.key}")


def render_audition(run_dir: Path, *, assets_dir: Path | None = None) -> list[SonicAuditionResult]:
    """Render procedural baselines and copy the packaged Modern Night Drive counterparts."""
    assets_dir = assets_dir or PACKAGED_ASSETS_DIR
    require_pack_assets(assets_dir)
    if run_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing audition directory: {run_dir}")

    baseline_dir = run_dir / "baseline"
    modern_night_drive_dir = run_dir / "modern-night-drive"
    results: list[SonicAuditionResult] = []
    for sample in SAMPLES:
        baseline_path = baseline_dir / sample.output_name
        modern_night_drive_path = modern_night_drive_dir / sample.output_name
        render_baseline_sample(sample, baseline_path)
        modern_night_drive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets_dir / sample.packaged_asset, modern_night_drive_path)
        results.append(
            SonicAuditionResult(
                key=sample.key,
                label=sample.label,
                source_asset=sample.packaged_asset.as_posix(),
                baseline=baseline_path.relative_to(run_dir).as_posix(),
                modern_night_drive=modern_night_drive_path.relative_to(run_dir).as_posix(),
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
    project-authored electronic cues — without asking a network TTS engine to
    manufacture review material.
    """
    assets_dir = assets_dir or PACKAGED_ASSETS_DIR
    manifest = json.loads((assets_dir / "manifest.json").read_text(encoding="utf-8"))
    raw_recipes = manifest.get("recipes")
    if not isinstance(raw_recipes, list):
        raise ValueError("Modern Night Drive pack manifest has no recipe inventory")

    library = ImagingLibrary(LEGACY_MOTIF_NOTES, run_dir / ".recipe-tmp", assets_dir=assets_dir)
    previews_dir = run_dir / "scene-recipes"
    previews_dir.mkdir(parents=True, exist_ok=True)
    results: list[RecipeAuditionResult] = []
    for raw_recipe in raw_recipes:
        if not isinstance(raw_recipe, dict) or not isinstance(raw_recipe.get("id"), str):
            raise ValueError("Modern Night Drive pack contains an invalid recipe id")
        recipe_id = raw_recipe["id"]
        recipe = library.resolve_ad_recipe(recipe_id, variant_key="audition")
        if recipe is None or recipe.bed_path is None:
            raise ValueError(f"Modern Night Drive pack recipe is not auditionable: {recipe_id}")

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
        "pack": "Modern Night Drive — Neon Relay × Velvet Horizon",
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
              <figcaption>Modern Night Drive packaged asset</figcaption>
              <audio controls preload=\"metadata\">
                <source src=\"{html.escape(result.modern_night_drive, quote=True)}\" type=\"audio/mpeg\">
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
    <title>Mamma Mi Radio — Modern Night Drive A/B audition</title>
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
    <h1>Neon Relay × Velvet Horizon</h1>
    <p class=\"lede\">Generated locally at {html.escape(timestamp)}. Left is the current procedural render;
      right is the Modern Night Drive packaged asset. Neon Relay is the station signature; Velvet Horizon
      is the atmospheric character. Scene recipes below isolate each project-authored bed and cue pair.
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


MOTIF_LISTENING_PROMPT = (
    "Keep volume fixed. On the Mac, play A, B, and C once, then use Repeat x3 on each. "
    "Choose the signature that still feels restrained, contemporary, and recognisable on the third play. "
    "Replay only that candidate on the Wohnzimmer Sonos Arc. Reply exactly: "
    "Motif: <candidate_id>; Mac: pass|fail; Sonos Arc: pass|fail; Notes: <short reason>."
)


def write_motif_gate_index(manifest: dict[str, object], run_dir: Path, *, timestamp: str) -> Path:
    """Write the stage-one listening gate without presenting previews as shippable masters."""
    raw_candidates = manifest.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) != 3:
        raise ValueError("Motif gate requires exactly three candidates")

    cards: list[str] = []
    for index, raw_candidate in enumerate(raw_candidates):
        if not isinstance(raw_candidate, dict):
            raise ValueError("Motif gate contains an invalid candidate")
        candidate_id = str(raw_candidate["id"])
        label = str(raw_candidate["label"])
        brief = str(raw_candidate["brief"])
        path = str(raw_candidate["path"])
        letter = chr(ord("A") + index)
        cards.append(
            f"""        <section class=\"candidate\">
          <p class=\"letter\">{letter}</p>
          <h2>{html.escape(label)}</h2>
          <p>{html.escape(brief)}</p>
          <p><code>{html.escape(candidate_id)}</code></p>
          <audio id=\"audio-{html.escape(candidate_id, quote=True)}\" controls preload=\"metadata\">
            <source src=\"{html.escape(path, quote=True)}\" type=\"audio/mpeg\">
            Your browser cannot play this file.
          </audio>
          <button type=\"button\" onclick=\"repeatThree('audio-{html.escape(candidate_id, quote=True)}')\">
            Repeat x3
          </button>
        </section>"""
        )

    document = f"""<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>Mamma Mi Radio — Modern Night Drive motif gate</title>
    <style>
      :root {{ color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif;
        background: #14110f; color: #f5edd8; }}
      body {{ max-width: 64rem; margin: 0 auto; padding: 2rem 1rem 4rem; }}
      h1 {{ margin-bottom: .4rem; }}
      .warning {{ border: 1px solid #b82c20; background: #251e19; border-radius: .75rem; padding: 1rem; }}
      .prompt {{ border-left: .3rem solid #f4d048; padding: .3rem 1rem; color: #f5edd8; }}
      .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr)); gap: 1rem; }}
      .candidate {{ background: #251e19; border: 1px solid #59463a; border-radius: .8rem; padding: 1rem; }}
      .candidate h2 {{ margin: 0; }}
      .letter {{ color: #f4d048; font-size: 1.5rem; font-weight: 800; margin: 0; }}
      code {{ color: #d8c9af; }}
      audio {{ width: 100%; margin: .5rem 0; }}
      button {{ border: 0; border-radius: 999px; padding: .65rem 1rem; font-weight: 750;
        background: #f4d048; color: #14110f; cursor: pointer; }}
    </style>
  </head>
  <body>
    <h1>Modern Night Drive motif gate</h1>
    <p>Generated locally at {html.escape(timestamp)}. Pick one signature before any final station assets
      are rebuilt.</p>
    <p class=\"warning\"><strong>Prototype only.</strong> These files use hash-pinned Freesound HQ preview
      derivatives. They are not original masters and must not ship in the public imaging pack.</p>
    <p class=\"prompt\">{html.escape(MOTIF_LISTENING_PROMPT)}</p>
    <div class=\"grid\">
{chr(10).join(cards)}
    </div>
    <script>
      function repeatThree(id) {{
        const audio = document.getElementById(id);
        let remaining = 3;
        const playAgain = () => {{
          if (remaining <= 0) {{ audio.removeEventListener('ended', playAgain); return; }}
          remaining -= 1;
          audio.currentTime = 0;
          audio.play();
        }};
        audio.pause();
        audio.addEventListener('ended', playAgain);
        playAgain();
      }}
    </script>
  </body>
</html>
"""
    index_path = run_dir / "index.html"
    index_path.write_text(document, encoding="utf-8")
    return index_path


def write_motif_gate_handoff(manifest: dict[str, object], run_dir: Path) -> Path:
    """Create the durable Conductor handoff with relative audio and evidence links."""
    raw_candidates = manifest.get("candidates")
    raw_sources = manifest.get("sources")
    if not isinstance(raw_candidates, list) or not isinstance(raw_sources, list):
        raise ValueError("Motif prototype manifest is incomplete")

    candidate_lines = []
    for index, raw_candidate in enumerate(raw_candidates):
        if not isinstance(raw_candidate, dict):
            raise ValueError("Motif prototype manifest has an invalid candidate")
        candidate_lines.append(
            f"- {chr(ord('A') + index)} — [{raw_candidate['label']}](./{raw_candidate['path']}) "
            f"(`{raw_candidate['id']}`, SHA-256 `{raw_candidate['sha256']}`)"
        )
    source_lines = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            raise ValueError("Motif prototype manifest has an invalid source")
        source_lines.append(
            f"- [{raw_source['title']}]({raw_source['source_url']}) by {raw_source['creator']} — "
            f"CC0 1.0; HQ derivative SHA-256 `{raw_source['source_sha256']}`"
        )

    document = f"""# Modern Night Drive motif selection

Status: **listening approval pending**. These are audition-only Freesound HQ preview derivatives,
not original masters. Do not copy them into the public imaging pack.

- [Open the listening board](./index.html)
- [Inspect the complete provenance manifest](./manifest.json)

## Candidates

{chr(10).join(candidate_lines)}

## Exact listening prompt

> {MOTIF_LISTENING_PROMPT}

## Source and hash evidence

{chr(10).join(source_lines)}

The source-page metadata identifies each recording as CC0 1.0. The generator verifies the
downloaded HQ derivative hashes before rendering. Final production remains blocked until one
candidate is selected and original source masters pass a separate provenance review.
"""
    handoff_path = run_dir / "README.md"
    handoff_path.write_text(document, encoding="utf-8")
    return handoff_path


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
    gate_mode = parser.add_mutually_exclusive_group()
    gate_mode.add_argument(
        "--motif-source-dir",
        type=Path,
        help="Run only the three-candidate motif gate from hash-pinned HQ preview derivatives",
    )
    gate_mode.add_argument(
        "--treatment-identity-manifest",
        type=Path,
        help="Run the revised three-direction treatment gate from an approved production-voice manifest",
    )
    gate_mode.add_argument(
        "--core-treatment-manifest",
        type=Path,
        help="Run the five-group core cadence gate from an approved treatment manifest",
    )
    parser.add_argument(
        "--core-speech-manifest",
        type=Path,
        help="Frozen approved-route speech manifest required by --core-treatment-manifest",
    )
    args = parser.parse_args(argv)

    try:
        timestamp = _timestamp(args.timestamp)
        if args.core_treatment_manifest is not None:
            if args.core_speech_manifest is None:
                raise ValueError("--core-speech-manifest is required with --core-treatment-manifest")
            try:
                core_gate = importlib.import_module("scripts.core_cadence_gate")
            except ModuleNotFoundError as exc:
                if exc.name != "scripts":
                    raise
                core_gate = importlib.import_module("core_cadence_gate")
            run_dir = args.output_dir / f"core-cadence-gate-{timestamp}"
            manifest = core_gate.render_core_cadence_gate(
                args.core_treatment_manifest,
                args.core_speech_manifest,
                run_dir,
                generated_at=timestamp,
            )
            print(f"Core cadence gate: {run_dir}")
            print(f"Manifest: {run_dir / 'manifest.json'}")
            print(f"Listening page: {run_dir / 'index.html'}")
            print(f"Handoff: {run_dir / 'README.md'}")
            print(f"Pack digest: {manifest['pack_digest']}")
            return 0
        if args.core_speech_manifest is not None:
            raise ValueError("--core-speech-manifest requires --core-treatment-manifest")
        if args.treatment_identity_manifest is not None:
            try:
                treatment_gate = importlib.import_module("scripts.sonic_treatment_gate")
            except ModuleNotFoundError as exc:
                if exc.name != "scripts":
                    raise
                treatment_gate = importlib.import_module("sonic_treatment_gate")
            run_dir = args.output_dir / f"treatment-gate-{timestamp}"
            manifest = treatment_gate.render_treatment_gate(
                args.treatment_identity_manifest,
                run_dir,
                generated_at=timestamp,
            )
            print(f"Treatment gate: {run_dir}")
            print(f"Manifest: {run_dir / 'manifest.json'}")
            print(f"Listening page: {run_dir / 'index.html'}")
            print(f"Handoff: {run_dir / 'README.md'}")
            print(f"Pack digest: {manifest['pack_digest']}")
            return 0
        if args.motif_source_dir is not None:
            run_dir = args.output_dir / f"motif-gate-{timestamp}"
            manifest = pack_builder.render_motif_prototypes(
                args.motif_source_dir,
                run_dir,
                generated_at=timestamp,
            )
            manifest_path = run_dir / "manifest.json"
            index_path = write_motif_gate_index(manifest, run_dir, timestamp=timestamp)
            handoff_path = write_motif_gate_handoff(manifest, run_dir)
            print(f"Motif gate: {run_dir}")
            print(f"Manifest: {manifest_path}")
            print(f"Listening page: {index_path}")
            print(f"Handoff: {handoff_path}")
            return 0
        # Validate first so an incomplete package cannot leave a partial audition
        # directory that looks reviewable.
        require_pack_assets(PACKAGED_ASSETS_DIR)
        run_dir = args.output_dir / f"audition-{timestamp}"
        results = render_audition(run_dir)
        recipe_results = [] if args.no_recipe_previews else render_recipe_previews(run_dir)
        manifest_path = write_manifest(results, run_dir, timestamp=timestamp, recipe_results=recipe_results)
        index_path = write_index_html(results, run_dir, timestamp=timestamp, recipe_results=recipe_results)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Audition: {run_dir}")
    print(f"Manifest: {manifest_path}")
    print(f"Listening page: {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
