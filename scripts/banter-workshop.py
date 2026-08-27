#!/usr/bin/env python3
"""Developer-only banter authoring kit: render candidates and build a listening board.

This is not a content factory. It reuses the live scriptwriting, Marco/Giulia
voice, talk-bed, normalization, and audio-validation chain, then emits a
self-contained offline review board. Paid providers are only contacted when
``render`` is run with keys present — tests mock them; do not spend money
merely to validate the tool.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import html
import json
import os
import random
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mammamiradio.audio.audio_quality import _probe_silence, _probe_volume, validate_segment_audio
from mammamiradio.audio.normalizer import probe_duration_sec
from mammamiradio.audio.tts import synthesize_dialogue
from mammamiradio.core.config import load_config
from mammamiradio.core.models import SegmentType, StationState
from mammamiradio.hosts.scriptwriter import write_banter
from mammamiradio.media.starter import load_starter_tracks
from mammamiradio.scheduling.producer import (
    _apply_and_adopt_talk_bed,
    _expected_banter_duration_sec,
    _listener_truth_guard,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = ROOT / "scripts" / "banter-pack-v1.json"
DEFAULT_TEMPLATE = ROOT / "scripts" / "banter-review-board.template.html"
DEFAULT_FEEDBACK = ROOT / "scripts" / "banter-pack-v1-feedback.json"
SHIPPED_BANTER_DIR = ROOT / "mammamiradio" / "assets" / "demo" / "banter"
SPOKEN_ASSETS = ROOT / "mammamiradio" / "assets" / "demo" / "spoken_assets.json"
VALID_MODES = {"normal", "super_italian"}
VALID_CONTEXTS = {"evergreen", "exact_track"}
VALID_VARIANTS = {"original", "english_alternate", "fourth_wall_special"}
HOSTS = {"marco", "giulia"}
MAX_ATTEMPTS = 6
VARIANT_SUBDIRS = {
    "original": None,
    "english_alternate": "normal_alternates",
    "fourth_wall_special": "specials",
}
_CURRENT_TIME_RE = re.compile(
    r"\b(?:today|tonight|this morning|this afternoon|this evening|yesterday|tomorrow|right now|just now|"
    r"currently|lately|recently|this week|last week|next week|this month|last month|next month|this year|"
    r"last year|next year|the other day|the next day|next day|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|oggi|stasera|stanotte|stamattina|questo pomeriggio|ieri|domani|proprio adesso|"
    r"in questo momento|ultimamente|recentemente|questa settimana|la settimana scorsa|questo mese|"
    r"il mese scorso|il mese prossimo|l'altro giorno|poco fa|da (?:giorni|settimane|mesi))\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_EXACT_SONIC_CLAIM_RE = re.compile(
    r"\b(?:sounds?|genre|tempo|beat|edm|electronic|guitars?|drums?|synths?|festival|dancefloor|"
    r"vibes?|mood|banger|suona|genere|ritmo|elettronic[ao]|chitarr[ae]|batteria|sintetizzatore|"
    r"festival|pista da ballo|energia del brano|atmosfera del brano|kevin would understand|kevin capirebbe)\b",
    re.IGNORECASE,
)


def _json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _clip_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("clips"), list):
        return data["clips"]
    raise ValueError("plan must be a list of clips or an object with a clips array")


def _output_subdir(row: Mapping[str, Any]) -> str:
    variant = str(row.get("variant") or "original")
    override = VARIANT_SUBDIRS.get(variant)
    if override is not None:
        return override
    return str(row["mode"])


def _load_plan(path: Path, starter_ids: set[str]) -> list[dict[str, Any]]:
    rows = _clip_rows(_load_json(path))
    if not rows:
        raise ValueError("banter plan must contain at least one clip")

    required = {"id", "mode", "context", "track_id", "direction"}
    ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError(f"row {index} must contain at least {sorted(required)}")
        row_id = row["id"]
        if not isinstance(row_id, str) or not re.fullmatch(r"[a-z0-9-]+", row_id) or row_id in ids:
            raise ValueError(f"row {index} has an invalid or duplicate id")
        ids.add(row_id)
        if row["mode"] not in VALID_MODES or row["context"] not in VALID_CONTEXTS:
            raise ValueError(f"row {row_id} has an invalid mode or context")
        variant = row.get("variant", "original")
        if variant not in VALID_VARIANTS:
            raise ValueError(f"row {row_id} has an invalid variant")
        if not isinstance(row["direction"], str) or not row["direction"].strip():
            raise ValueError(f"row {row_id} needs a creative direction")
        if row["context"] == "evergreen" and row["track_id"] is not None:
            raise ValueError(f"evergreen row {row_id} cannot name a track")
        if row["context"] == "exact_track" and row["track_id"] not in starter_ids:
            raise ValueError(f"exact row {row_id} must name an approved starter track")
    return rows


def _prepare_output(output: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    subdirs = {_output_subdir(row) for row in rows}
    for name in sorted(subdirs):
        (output / name).mkdir()
    (output / "_work").mkdir()


def _row_config(base_config: Any, row: Mapping[str, Any], work_dir: Path) -> Any:
    config = copy.deepcopy(base_config)
    config.super_italian_mode = row["mode"] == "super_italian"
    config.hosts = [host for host in config.hosts if host.name.casefold() in HOSTS]
    if {host.name.casefold() for host in config.hosts} != HOSTS:
        raise ValueError("radio.toml must provide the Marco and Giulia host voices")
    config.homeassistant.enabled = False
    config.homeassistant.context_enabled = False
    config.party_mode = None
    config.ledger_enabled = False
    config.tmp_dir = work_dir / "tmp"
    config.cache_dir = work_dir / "cache"
    config.tmp_dir.mkdir(parents=True)
    config.cache_dir.mkdir(parents=True)
    return config


def _assert_packaged_script_safe(row: Mapping[str, Any], texts: list[str]) -> None:
    script = " ".join(texts)
    match = _CURRENT_TIME_RE.search(script) or _YEAR_RE.search(script)
    if match is not None:
        raise ValueError(f"script is not timeless: {match.group(0)!r}")
    if row["context"] == "exact_track":
        sonic_match = _EXACT_SONIC_CLAIM_RE.search(script)
        if sonic_match is not None:
            raise ValueError(f"exact-track script inferred unprovided audio context: {sonic_match.group(0)!r}")


async def _render_attempt(
    base_config: Any,
    row: Mapping[str, Any],
    starter_by_id: Mapping[str, Any],
    work_dir: Path,
) -> tuple[Path, list[dict[str, str]], float, dict[str, float | None]]:
    config = _row_config(base_config, row, work_dir)
    state = StationState()
    if row["context"] == "exact_track":
        state.played_tracks.append(starter_by_id[row["track_id"]])

    lines, _commit = await write_banter(
        state,
        config,
        packaged_context=row["context"],
        creative_direction=row["direction"],
        include_listener_request=False,
        require_generated=True,
    )
    lines, _transition, truth_changed, _transition_replaced = await _listener_truth_guard(state, config, lines)
    if truth_changed:
        raise ValueError("listener-truth guard replaced the generated script")
    if len(lines) < 2:
        raise ValueError("generated script is not a host exchange")
    if any(line.host.name.casefold() not in HOSTS for line in lines):
        raise ValueError("generated script used a host outside Marco and Giulia")

    texts = [line.text for line in lines]
    _assert_packaged_script_safe(row, texts)
    expected_duration = _expected_banter_duration_sec(texts)
    dry_path = await synthesize_dialogue(lines, config.tmp_dir, state=state)
    await asyncio.to_thread(
        validate_segment_audio,
        dry_path,
        SegmentType.BANTER,
        expected_min_duration_sec=expected_duration,
        expected_line_count=len(lines),
    )
    final_path = await _apply_and_adopt_talk_bed(dry_path, config, state, prefix="banter_workshop")
    await asyncio.to_thread(
        validate_segment_audio,
        final_path,
        SegmentType.BANTER,
        expected_min_duration_sec=expected_duration,
        expected_line_count=len(lines),
    )

    duration = probe_duration_sec(final_path)
    if duration is None:
        raise ValueError("could not measure rendered MP3 duration")
    silence_total, longest_silence = await asyncio.to_thread(_probe_silence, final_path)
    mean_db, peak_db = await asyncio.to_thread(_probe_volume, final_path)
    measurements = {
        "silence_ratio": silence_total / duration,
        "longest_silence_seconds": longest_silence,
        "mean_volume_db": mean_db,
        "peak_volume_db": peak_db,
    }
    transcript = [{"host": line.host.name, "text": line.text} for line in lines]
    return final_path, transcript, duration, measurements


async def render_plan(plan: Path, output: Path, *, build_board: bool = True) -> Path:
    starter_tracks = load_starter_tracks(require_complete=True)
    starter_by_id = {track.provider_track_id: track for track in starter_tracks}
    rows = _load_plan(plan, set(starter_by_id))
    _prepare_output(output, rows)
    base_config = load_config(str(ROOT / "radio.toml"))
    receipts: list[dict[str, Any]] = []

    for number, row in enumerate(rows, start=1):
        print(f"[{number:02d}/{len(rows):02d}] {row['id']}", flush=True)
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempt_dir = output / "_work" / f"{row['id']}-{attempt}"
            random.seed(f"{row['id']}:{attempt}")
            try:
                final_path, transcript, duration, measurements = await _render_attempt(
                    base_config,
                    row,
                    starter_by_id,
                    attempt_dir,
                )
                destination = output / _output_subdir(row) / f"{row['id']}.mp3"
                shutil.copy2(final_path, destination)
                payload = destination.read_bytes()
                track = starter_by_id.get(row["track_id"])
                receipt: dict[str, Any] = {
                    "id": row["id"],
                    "mode": row["mode"],
                    "context": row["context"],
                    "required_previous_starter_id": row["track_id"],
                    "required_previous_track": track.display if track is not None else None,
                    "creative_direction": row["direction"],
                    "file": str(destination.relative_to(output)),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "duration_seconds": round(duration, 3),
                    "audio_measurements": {
                        key: round(value, 3) if value is not None else None for key, value in measurements.items()
                    },
                    "generation_attempt": attempt,
                    "transcript": transcript,
                }
                if row.get("variant"):
                    receipt["variant"] = row["variant"]
                if row.get("title"):
                    receipt["title"] = row["title"]
                receipts.append(receipt)
                shutil.rmtree(attempt_dir, ignore_errors=True)
                print(f"         ready: {destination.name} ({duration:.1f}s, {len(payload):,} bytes)", flush=True)
                break
            except Exception as exc:
                last_error = exc
                print(f"         attempt {attempt} rejected: {type(exc).__name__}: {exc}", flush=True)
        else:
            raise RuntimeError(f"{row['id']} failed after {MAX_ATTEMPTS} attempts") from last_error

    shutil.rmtree(output / "_work", ignore_errors=True)
    total_bytes = sum(item["bytes"] for item in receipts)
    report = {
        "schema_version": "1",
        "complete": len(receipts) == len(rows),
        "candidate_count": len(receipts),
        "total_bytes": total_bytes,
        "total_duration_seconds": round(sum(item["duration_seconds"] for item in receipts), 3),
        "clips": receipts,
    }
    report_path = output / "review-report.json"
    report_path.write_text(_json_dump(report), encoding="utf-8")
    print(f"Complete: {len(receipts)} MP3s, {total_bytes:,} bytes", flush=True)
    if build_board:
        board_path = output / "listening-board.html"
        build_listening_board(report_path, board_path)
        print(f"Board: {board_path}", flush=True)
    return report_path


def _approved_feedback_map(feedback: Mapping[str, Any] | None, clip_ids: set[str]) -> dict[str, Any]:
    if not feedback:
        return {}
    clips = feedback.get("clips")
    if not isinstance(clips, list):
        raise ValueError("feedback must contain a clips array")
    approved: dict[str, Any] = {}
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        clip_id = clip.get("id")
        if clip_id not in clip_ids:
            continue
        approved[str(clip_id)] = {
            "decision": clip.get("decision") or "",
            "writing_score": clip.get("writing_score"),
            "audio_score": clip.get("audio_score"),
            "issues": clip.get("issues") or [],
            "notes": clip.get("notes") or "",
            "updated_at": clip.get("updated_at"),
        }
    return approved


def _titles_map(report: Mapping[str, Any], feedback: Mapping[str, Any] | None) -> dict[str, str]:
    titles: dict[str, str] = {}
    if feedback and isinstance(feedback.get("clips"), list):
        for clip in feedback["clips"]:
            if isinstance(clip, dict) and clip.get("id") and clip.get("title"):
                titles[str(clip["id"])] = str(clip["title"])
    for clip in report["clips"]:
        clip_id = str(clip["id"])
        if clip_id not in titles:
            titles[clip_id] = str(clip.get("title") or clip_id)
    return titles


def _context_audio_map(
    report: Mapping[str, Any],
    *,
    board_path: Path,
    shipped_audio: bool,
) -> dict[str, str]:
    starter_by_id = {track.provider_track_id: track for track in load_starter_tracks(require_complete=True)}
    mapping: dict[str, str] = {}
    for clip in report["clips"]:
        if clip.get("context") != "exact_track":
            continue
        starter_id = clip.get("required_previous_starter_id")
        track = starter_by_id.get(starter_id)
        if track is None or track.local_path is None:
            raise ValueError(f"exact-track clip {clip['id']} is missing starter audio for {starter_id}")
        if shipped_audio:
            mapping[str(clip["id"])] = _relpath_for_board(board_path, Path(track.local_path))
        else:
            context_dir = board_path.parent / "context"
            context_dir.mkdir(parents=True, exist_ok=True)
            destination = context_dir / f"{starter_id}.mp3"
            if not destination.exists():
                shutil.copy2(track.local_path, destination)
            mapping[str(clip["id"])] = f"context/{destination.name}"
    return mapping


def _relpath_for_board(board_path: Path, target: Path) -> str:
    return Path(os.path.relpath(target.resolve(), start=board_path.parent.resolve())).as_posix()


def _rewrite_shipped_audio_paths(report: dict[str, Any], board_path: Path) -> dict[str, Any]:
    rewritten = copy.deepcopy(report)
    spoken = _load_json(SPOKEN_ASSETS)
    expected_by_id = {
        Path(entry["path"]).stem: entry.get("sha256")
        for entry in spoken.get("assets", [])
        if str(entry.get("path", "")).startswith("banter/")
    }
    for clip in rewritten["clips"]:
        clip_id = clip["id"]
        asset = SHIPPED_BANTER_DIR / f"{clip_id}.mp3"
        if not asset.is_file():
            raise FileNotFoundError(f"shipped banter asset missing: {asset}")
        actual = hashlib.sha256(asset.read_bytes()).hexdigest()
        expected = expected_by_id.get(clip_id)
        if expected and actual != expected:
            raise ValueError(f"shipped hash mismatch for {clip_id}: {actual} != {expected}")
        if clip.get("sha256") and clip["sha256"] != actual:
            raise ValueError(f"report hash for {clip_id} does not match shipped audio")
        clip["file"] = _relpath_for_board(board_path, asset)
        clip["bytes"] = asset.stat().st_size
        clip["sha256"] = actual
    return rewritten


def _json_for_html(data: Any) -> str:
    """Serialize JSON for a classic script tag without allowing HTML breakouts."""
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_listening_board(
    report_path: Path,
    output_path: Path,
    *,
    feedback_path: Path | None = None,
    template_path: Path = DEFAULT_TEMPLATE,
    shipped_audio: bool = False,
    base_clip_count: int | None = None,
    hero_eyebrow: str | None = None,
) -> Path:
    report = _load_json(report_path)
    if not isinstance(report, dict) or not isinstance(report.get("clips"), list) or not report["clips"]:
        raise ValueError("review report must contain a non-empty clips array")
    feedback = _load_json(feedback_path) if feedback_path is not None else None
    output_path.parent.mkdir(parents=True, exist_ok=True)

    board_report = _rewrite_shipped_audio_paths(report, output_path) if shipped_audio else copy.deepcopy(report)
    clip_ids = {str(clip["id"]) for clip in board_report["clips"]}
    titles = _titles_map(board_report, feedback if isinstance(feedback, dict) else None)
    approved = _approved_feedback_map(feedback if isinstance(feedback, dict) else None, clip_ids)
    context_audio = _context_audio_map(board_report, board_path=output_path, shipped_audio=shipped_audio)

    keep_count = sum(1 for item in approved.values() if item.get("decision") == "keep")
    candidate_count = len(board_report["clips"])
    if hero_eyebrow is None:
        if keep_count and keep_count == candidate_count:
            hero_eyebrow = f"Accepted pack · {keep_count}/{candidate_count} Keep"
        else:
            hero_eyebrow = f"Listening room · {candidate_count} candidates"
    if base_clip_count is None:
        base_clip_count = min(12, candidate_count)

    template = template_path.read_text(encoding="utf-8")
    replacements = {
        "__REVIEW_REPORT__": _json_for_html(board_report),
        "__TITLES__": _json_for_html(titles),
        "__CONTEXT_AUDIO__": _json_for_html(context_audio),
        "__APPROVED_FEEDBACK__": _json_for_html(approved),
        "__CANDIDATE_COUNT__": str(candidate_count),
        "__HERO_EYEBROW__": html.escape(hero_eyebrow),
        "__BASE_CLIP_COUNT__": str(base_clip_count),
    }
    rendered = template
    for key, value in replacements.items():
        if key not in rendered:
            raise ValueError(f"board template is missing placeholder {key}")
        rendered = rendered.replace(key, value)
    leftover = [token for token in replacements if token in rendered]
    if leftover:
        raise ValueError(f"board template placeholders were not fully replaced: {leftover}")

    output_path.write_text(rendered, encoding="utf-8")
    return output_path


def assert_pack_synchronized_with_spoken_assets(
    feedback_path: Path = DEFAULT_FEEDBACK,
    spoken_path: Path = SPOKEN_ASSETS,
) -> None:
    """Fail closed when baseline IDs/hashes drift from the shipped spoken manifest."""
    feedback = _load_json(feedback_path)
    spoken = _load_json(spoken_path)
    by_id = {
        Path(entry["path"]).stem: entry
        for entry in spoken.get("assets", [])
        if str(entry.get("path", "")).startswith("banter/")
    }
    clips = feedback.get("clips") if isinstance(feedback, dict) else None
    if not isinstance(clips, list) or not clips:
        raise ValueError("feedback baseline has no clips")
    for clip in clips:
        clip_id = clip["id"]
        entry = by_id.get(clip_id)
        if entry is None:
            raise ValueError(f"baseline clip {clip_id} is missing from spoken_assets.json")
        if clip.get("sha256") != entry.get("sha256"):
            raise ValueError(f"baseline hash for {clip_id} does not match spoken_assets.json")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="Generate candidate MP3s and review-report.json")
    render.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--no-board", action="store_true", help="Skip automatic listening-board generation")

    board = sub.add_parser("board", help="Build a self-contained listening board (no provider cost)")
    board.add_argument("--report", type=Path, required=True)
    board.add_argument("--feedback", type=Path, default=None)
    board.add_argument("--output", type=Path, required=True)
    board.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    board.add_argument(
        "--shipped-audio",
        action="store_true",
        help="Point board audio at mammamiradio/assets/demo/banter instead of copied MP3s",
    )
    board.add_argument("--base-clip-count", type=int, default=None)
    board.add_argument("--hero-eyebrow", default=None)

    sync = sub.add_parser("sync-check", help="Verify accepted baseline IDs/hashes against spoken_assets.json")
    sync.add_argument("--feedback", type=Path, default=DEFAULT_FEEDBACK)
    sync.add_argument("--spoken-assets", type=Path, default=SPOKEN_ASSETS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "render":
            asyncio.run(render_plan(args.plan.resolve(), args.output.resolve(), build_board=not args.no_board))
        elif args.command == "board":
            build_listening_board(
                args.report.resolve(),
                args.output.resolve(),
                feedback_path=args.feedback.resolve() if args.feedback else None,
                template_path=args.template.resolve(),
                shipped_audio=args.shipped_audio,
                base_clip_count=args.base_clip_count,
                hero_eyebrow=args.hero_eyebrow,
            )
            print(f"Wrote {args.output}", flush=True)
        elif args.command == "sync-check":
            assert_pack_synchronized_with_spoken_assets(args.feedback.resolve(), args.spoken_assets.resolve())
            print("Baseline synchronized with spoken_assets.json", flush=True)
        else:  # pragma: no cover
            raise ValueError(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
