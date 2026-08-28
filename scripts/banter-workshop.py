#!/usr/bin/env python3
"""Developer-only banter authoring kit: render candidates and build a listening board."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
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
from mammamiradio.hosts.scriptwriter import PACKAGED_BANTER_DIRECTION_MAX_CHARS, write_banter
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
DECISIONS = {"keep", "maybe", "redo"}
MAX_ATTEMPTS = 6
VARIANT_SUBDIRS = {"original": None, "english_alternate": "normal_alternates", "fourth_wall_special": "specials"}
_TIME_RE = re.compile(
    r"\b(?:today|tonight|this morning|this afternoon|this evening|yesterday|tomorrow|right now|just now|"
    r"currently|lately|recently|this week|last week|next week|this month|last month|next month|this year|"
    r"last year|next year|the other day|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre|"
    r"spring|summer|autumn|fall|winter|primavera|estate|autunno|inverno|oggi|stasera|stanotte|"
    r"stamattina|ieri|domani|proprio adesso|in questo momento|ultimamente|recentemente|"
    r"questa settimana|la settimana scorsa|l'altro giorno|noon|midnight|midday|mezzogiorno|"
    r"mezzanotte|o'clock)\b|\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|\b\d{1,2}:\d{2}\b",
    re.IGNORECASE,
)
_WEATHER_RE = re.compile(
    r"\b(?:rain|raining|rainy|snow|snowing|sunny|storm|stormy|cloudy|fog|hail|pioggia|piove|piovendo|"
    r"neve|nevica|soleggiato|temporale|nuvoloso)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_EXACT_SONIC_RE = re.compile(
    r"\b(?:sounds?|genre|tempo|beat|edm|electronic|guitars?|drums?|synths?|festival|dancefloor|vibes?|"
    r"mood|banger|slow|fast|piano|bass|vocals?|melody|riff|hook|groove|acoustic|saxophone|sax|chorus|"
    r"instrumentation|suona|genere|ritmo|elettronic[ao]|chitarr[ae]|batteria|sintetizzatore|lento|"
    r"veloce|basso|voc[ei]|melodia|sassofono|ritornello|strumenti)\b",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_REL_AUDIO_RE = re.compile(r"^(?:\.\./|[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+$")
_ID_RE = re.compile(r"[a-z0-9-]+")
_SPOKEN_LINE_RE = re.compile(r"(?=(?:Marco|Giulia):\s)")
_SPOKEN_LINE_PARSE_RE = re.compile(r"^(Marco|Giulia):\s*(.*)$", re.DOTALL)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _predecessor(value: Any) -> str:
    return "" if value in (None, "") else str(value)


def _special(row: Mapping[str, Any]) -> bool:
    return str(row.get("variant") or "original") == "fourth_wall_special"


def _clip_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("clips"), list):
        return data["clips"]
    raise ValueError("plan must be a list of clips or an object with a clips array")


def _output_subdir(row: Mapping[str, Any]) -> str:
    override = VARIANT_SUBDIRS.get(str(row.get("variant") or "original"))
    return override if override is not None else str(row["mode"])


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
        if not isinstance(row_id, str) or not _ID_RE.fullmatch(row_id) or row_id in ids:
            raise ValueError(f"row {index} has an invalid or duplicate id")
        ids.add(row_id)
        if row["mode"] not in VALID_MODES or row["context"] not in VALID_CONTEXTS:
            raise ValueError(f"row {row_id} has an invalid mode or context")
        if row.get("variant", "original") not in VALID_VARIANTS:
            raise ValueError(f"row {row_id} has an invalid variant")
        if not isinstance(row["direction"], str) or not row["direction"].strip():
            raise ValueError(f"row {row_id} needs a creative direction")
        if len(row["direction"]) > PACKAGED_BANTER_DIRECTION_MAX_CHARS:
            raise ValueError(
                f"row {row_id} creative direction exceeds {PACKAGED_BANTER_DIRECTION_MAX_CHARS} characters"
            )
        if row["context"] == "evergreen" and row["track_id"] is not None:
            raise ValueError(f"evergreen row {row_id} cannot name a track")
        if row["context"] == "exact_track" and row["track_id"] not in starter_ids:
            raise ValueError(f"exact row {row_id} must name an approved starter track")
    return rows


def _prepare_output(output: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for name in sorted({_output_subdir(row) for row in rows}):
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
    match = _TIME_RE.search(script) or _YEAR_RE.search(script) or _WEATHER_RE.search(script)
    if match is not None:
        raise ValueError(f"script is not timeless: {match.group(0)!r}")
    if row["context"] == "exact_track":
        sonic_match = _EXACT_SONIC_RE.search(script)
        if sonic_match is not None:
            raise ValueError(f"exact-track script inferred unprovided audio context: {sonic_match.group(0)!r}")


async def _render_attempt(
    base_config: Any,
    row: Mapping[str, Any],
    starter_by_id: Mapping[str, Any],
    work_dir: Path,
) -> tuple[Path, list[dict[str, str]], float]:
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
    lines, _transition, truth_changed, _replaced = await _listener_truth_guard(state, config, lines)
    if truth_changed:
        raise ValueError("listener-truth guard replaced the generated script")
    if len(lines) < 2 or any(line.host.name.casefold() not in HOSTS for line in lines):
        raise ValueError("generated script is not a Marco/Giulia exchange")
    texts = [line.text for line in lines]
    _assert_packaged_script_safe(row, texts)
    expected = _expected_banter_duration_sec(texts)
    dry_path = await synthesize_dialogue(lines, config.tmp_dir, state=state)
    await asyncio.to_thread(
        validate_segment_audio,
        dry_path,
        SegmentType.BANTER,
        expected_min_duration_sec=expected,
        expected_line_count=len(lines),
    )
    final_path = await _apply_and_adopt_talk_bed(dry_path, config, state, prefix="banter_workshop")
    await asyncio.to_thread(
        validate_segment_audio,
        final_path,
        SegmentType.BANTER,
        expected_min_duration_sec=expected,
        expected_line_count=len(lines),
    )
    duration = probe_duration_sec(final_path)
    if duration is None:
        raise ValueError("could not measure rendered MP3 duration")
    await asyncio.to_thread(_probe_silence, final_path)
    await asyncio.to_thread(_probe_volume, final_path)
    return final_path, [{"host": line.host.name, "text": line.text} for line in lines], duration


async def render_plan(plan: Path, output: Path, *, build_board: bool = True) -> Path:
    starter_by_id = {track.provider_track_id: track for track in load_starter_tracks(require_complete=True)}
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
                final_path, transcript, duration = await _render_attempt(base_config, row, starter_by_id, attempt_dir)
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
                    "generation_attempt": attempt,
                    "transcript": transcript,
                }
                if row.get("variant"):
                    receipt["variant"] = row["variant"]
                if row.get("title"):
                    receipt["title"] = row["title"]
                receipts.append(receipt)
                shutil.rmtree(attempt_dir, ignore_errors=True)
                print(f"         ready: {destination.name} ({duration:.1f}s)", flush=True)
                break
            except Exception as exc:
                last_error = exc
                print(f"         attempt {attempt} rejected: {type(exc).__name__}: {exc}", flush=True)
        else:
            raise RuntimeError(f"{row['id']} failed after {MAX_ATTEMPTS} attempts") from last_error
    shutil.rmtree(output / "_work", ignore_errors=True)
    report = {
        "schema_version": "1",
        "complete": len(receipts) == len(rows),
        "candidate_count": len(receipts),
        "total_bytes": sum(item["bytes"] for item in receipts),
        "total_duration_seconds": round(sum(item["duration_seconds"] for item in receipts), 3),
        "clips": receipts,
    }
    report_path = output / "review-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if build_board:
        build_listening_board(report_path, output / "listening-board.html")
    return report_path


def _approved_feedback_map(
    feedback: Mapping[str, Any] | None,
    report_clips: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not feedback:
        return {}
    clips = feedback.get("clips")
    if not isinstance(clips, list):
        raise ValueError("feedback must contain a clips array")
    by_id = {str(clip["id"]): clip for clip in report_clips}
    approved: dict[str, Any] = {}
    for clip in clips:
        if not isinstance(clip, dict) or clip.get("id") is None:
            continue
        report_clip = by_id.get(str(clip["id"]))
        actual_sha = report_clip.get("sha256") if report_clip else None
        if not isinstance(actual_sha, str) or clip.get("sha256") != actual_sha:
            continue
        notes = clip.get("notes") if isinstance(clip.get("notes"), str) else ""
        decision = clip.get("decision") if clip.get("decision") in DECISIONS else ""
        approved[str(clip["id"])] = {"sha256": actual_sha, "decision": decision, "notes": notes}
    return approved


def _relpath_for_board(board_path: Path, target: Path) -> str:
    return Path(os.path.relpath(target.resolve(), start=board_path.parent.resolve())).as_posix()


def _context_audio_map(report: Mapping[str, Any], *, board_path: Path, shipped_audio: bool) -> dict[str, str]:
    starter_by_id = {track.provider_track_id: track for track in load_starter_tracks(require_complete=True)}
    mapping: dict[str, str] = {}
    for clip in report["clips"]:
        if clip.get("context") != "exact_track":
            continue
        starter_id = clip.get("required_previous_starter_id")
        track = starter_by_id.get(starter_id)
        if track is None or track.local_path is None:
            raise ValueError(f"exact-track clip {clip['id']} is missing starter audio for {starter_id}")
        source = Path(track.local_path)
        if shipped_audio:
            mapping[str(clip["id"])] = _relpath_for_board(board_path, source)
            continue
        context_dir = board_path.parent / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        destination = context_dir / f"{starter_id}.mp3"
        shutil.copy2(source, destination)
        mapping[str(clip["id"])] = f"context/{destination.name}"
    return mapping


def _transcript_from_spoken(text: str) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for part in _SPOKEN_LINE_RE.split(text.strip()):
        part = part.strip()
        if not part:
            continue
        match = _SPOKEN_LINE_PARSE_RE.match(part)
        if match is None:
            raise ValueError(f"spoken transcript line is not host-prefixed: {part[:80]!r}")
        lines.append({"host": match.group(1), "text": match.group(2).strip()})
    if len(lines) < 2:
        raise ValueError("spoken transcript must contain at least two host lines")
    return lines


def _banter_spoken_by_id(spoken: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for entry in spoken.get("assets", []):
        if not isinstance(entry, dict) or not str(entry.get("path", "")).startswith("banter/"):
            continue
        clip_id = Path(str(entry["path"])).stem
        if clip_id in by_id:
            raise ValueError(f"spoken_assets.json has duplicate banter id {clip_id}")
        by_id[clip_id] = entry
    return by_id


def _feedback_by_id(feedback_path: Path) -> dict[str, dict[str, Any]]:
    feedback = _load_json(feedback_path)
    clips = feedback.get("clips") if isinstance(feedback, dict) else None
    if not isinstance(clips, list) or not clips:
        raise ValueError("feedback baseline has no clips")
    by_id: dict[str, dict[str, Any]] = {}
    for clip in clips:
        if not isinstance(clip, dict) or not isinstance(clip.get("id"), str):
            raise ValueError("feedback clip is missing an id")
        if clip["id"] in by_id:
            raise ValueError(f"feedback baseline has duplicate id {clip['id']}")
        by_id[clip["id"]] = clip
    return by_id


def _joined_baseline(
    plan_path: Path, feedback_path: Path, spoken_path: Path
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    starter_by_id = {track.provider_track_id: track for track in load_starter_tracks(require_complete=True)}
    plan_rows = _load_plan(plan_path, set(starter_by_id))
    spoken_by_id = _banter_spoken_by_id(_load_json(spoken_path))
    feedback_by_id = _feedback_by_id(feedback_path)
    plan_ids = [row["id"] for row in plan_rows]
    if set(plan_ids) != set(feedback_by_id):
        raise ValueError(
            "plan/feedback id sets diverge: "
            f"only_plan={sorted(set(plan_ids) - set(feedback_by_id))} "
            f"only_feedback={sorted(set(feedback_by_id) - set(plan_ids))}"
        )
    if set(plan_ids) != set(spoken_by_id):
        raise ValueError(
            "plan/spoken id sets diverge: "
            f"only_plan={sorted(set(plan_ids) - set(spoken_by_id))} "
            f"only_spoken={sorted(set(spoken_by_id) - set(plan_ids))}"
        )
    for row in plan_rows:
        clip_id = row["id"]
        spoken_entry, feedback_clip = spoken_by_id[clip_id], feedback_by_id[clip_id]
        asset = SHIPPED_BANTER_DIR / f"{clip_id}.mp3"
        if not asset.is_file():
            raise FileNotFoundError(f"shipped banter asset missing: {asset}")
        actual = _sha256(asset)
        if spoken_entry.get("sha256") != actual or feedback_clip.get("sha256") != actual:
            raise ValueError(f"baseline hash for {clip_id} does not match spoken audio")
        if spoken_entry.get("mode") != row["mode"]:
            raise ValueError(f"spoken mode for {clip_id} diverges from plan")
        if _predecessor(spoken_entry.get("required_previous_starter_id")) != _predecessor(row["track_id"]):
            raise ValueError(f"spoken predecessor for {clip_id} diverges from plan")
        if bool(spoken_entry.get("special")) != _special(row):
            raise ValueError(f"spoken special for {clip_id} diverges from plan")
    return plan_rows, spoken_by_id, feedback_by_id


def build_accepted_baseline_report(
    plan_path: Path = DEFAULT_PLAN,
    feedback_path: Path = DEFAULT_FEEDBACK,
    spoken_path: Path = SPOKEN_ASSETS,
) -> dict[str, Any]:
    starter_by_id = {track.provider_track_id: track for track in load_starter_tracks(require_complete=True)}
    plan_rows, spoken_by_id, feedback_by_id = _joined_baseline(plan_path, feedback_path, spoken_path)
    receipts: list[dict[str, Any]] = []
    for row in plan_rows:
        clip_id = row["id"]
        asset = SHIPPED_BANTER_DIR / f"{clip_id}.mp3"
        duration = probe_duration_sec(asset)
        if duration is None:
            raise ValueError(f"could not measure shipped duration for {clip_id}")
        track = starter_by_id.get(row["track_id"]) if row["track_id"] else None
        title = feedback_by_id[clip_id].get("title") or row.get("title")
        receipt: dict[str, Any] = {
            "id": clip_id,
            "mode": row["mode"],
            "variant": row.get("variant", "original"),
            "context": row["context"],
            "required_previous_starter_id": row["track_id"],
            "required_previous_track": track.display if track is not None else None,
            "creative_direction": row["direction"],
            "file": f"banter/{clip_id}.mp3",
            "bytes": asset.stat().st_size,
            "sha256": _sha256(asset),
            "duration_seconds": round(duration, 3),
            "transcript": _transcript_from_spoken(str(spoken_by_id[clip_id].get("transcript") or "")),
        }
        if title:
            receipt["title"] = str(title)
        receipts.append(receipt)
    return {
        "schema_version": "1",
        "complete": True,
        "candidate_count": len(receipts),
        "total_bytes": sum(item["bytes"] for item in receipts),
        "total_duration_seconds": round(sum(item["duration_seconds"] for item in receipts), 3),
        "clips": receipts,
    }


def _bind_report_audio(
    report: dict[str, Any],
    *,
    report_dir: Path,
    board_path: Path,
    shipped_audio: bool,
) -> dict[str, Any]:
    rewritten = copy.deepcopy(report)
    for clip in rewritten["clips"]:
        clip_id = clip.get("id")
        if shipped_audio:
            source = SHIPPED_BANTER_DIR / f"{clip_id}.mp3"
        else:
            raw = clip.get("file")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"clip {clip_id} is missing an audio file path")
            source = Path(raw)
            source = (report_dir / source).resolve() if not source.is_absolute() else source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"board audio missing for {clip_id}: {source}")
        actual = _sha256(source)
        if isinstance(clip.get("sha256"), str) and clip["sha256"] != actual:
            raise ValueError(f"report hash for {clip_id} does not match audio bytes")
        clip["file"] = _relpath_for_board(board_path, source)
        clip["bytes"] = source.stat().st_size
        clip["sha256"] = actual
    return rewritten


def _validate_report_for_board(report: Mapping[str, Any]) -> None:
    clips = report.get("clips") if isinstance(report, dict) else None
    if not isinstance(clips, list) or not clips:
        raise ValueError("review report must contain a non-empty clips array")
    ids: set[str] = set()
    for index, clip in enumerate(clips, start=1):
        if not isinstance(clip, dict):
            raise ValueError(f"report clip {index} must be an object")
        clip_id = clip.get("id")
        if not isinstance(clip_id, str) or not _ID_RE.fullmatch(clip_id) or clip_id in ids:
            raise ValueError(f"report clip {index} has an invalid or duplicate id")
        ids.add(clip_id)
        sha, audio, transcript = clip.get("sha256"), clip.get("file"), clip.get("transcript")
        if clip.get("mode") not in VALID_MODES or clip.get("context") not in VALID_CONTEXTS:
            raise ValueError(f"report clip {clip_id} has an invalid mode or context")
        if clip.get("variant", "original") not in VALID_VARIANTS:
            raise ValueError(f"report clip {clip_id} has an invalid variant")
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            raise ValueError(f"report clip {clip_id} needs a sha256 digest")
        if not isinstance(audio, str) or not _SAFE_REL_AUDIO_RE.fullmatch(audio):
            raise ValueError(f"report clip {clip_id} has an unsafe audio path")
        if not isinstance(transcript, list) or not transcript:
            raise ValueError(f"report clip {clip_id} needs a transcript")
        if any(
            not isinstance(line, dict) or not isinstance(line.get("host"), str) or not isinstance(line.get("text"), str)
            for line in transcript
        ):
            raise ValueError(f"report clip {clip_id} has a malformed transcript line")
        if not isinstance(clip.get("creative_direction"), str) or not isinstance(
            clip.get("duration_seconds"), int | float
        ):
            raise ValueError(f"report clip {clip_id} needs a creative direction and duration")
        if not isinstance(clip.get("bytes"), int) or clip["bytes"] < 0:
            raise ValueError(f"report clip {clip_id} needs a non-negative byte size")


def _json_for_html(data: Any) -> str:
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _render_template(template: str, values: Mapping[str, str]) -> str:
    for key in values:
        if template.count(key) != 1:
            raise ValueError(f"board template must contain exactly one {key}")
    pattern = re.compile("|".join(re.escape(key) for key in values))
    parts: list[str] = []
    last = 0
    for match in pattern.finditer(template):
        parts.append(template[last : match.start()])
        parts.append(values[match.group(0)])
        last = match.end()
    parts.append(template[last:])
    return "".join(parts)


def build_listening_board(
    report_path: Path | None,
    output_path: Path,
    *,
    feedback_path: Path | None = None,
    template_path: Path = DEFAULT_TEMPLATE,
    shipped_audio: bool = False,
    from_accepted_baseline: bool = False,
) -> Path:
    if from_accepted_baseline:
        if report_path is not None:
            raise ValueError("pass either --report or --from-accepted-baseline, not both")
        feedback_path = feedback_path or DEFAULT_FEEDBACK
        report = build_accepted_baseline_report(feedback_path=feedback_path)
        report_path = feedback_path.resolve()
        shipped_audio = True
    elif report_path is None:
        raise ValueError("board requires --report or --from-accepted-baseline")
    else:
        report = _load_json(report_path)
    if not isinstance(report, dict) or not isinstance(report.get("clips"), list) or not report["clips"]:
        raise ValueError("review report must contain a non-empty clips array")
    if shipped_audio:
        try:
            output_path.resolve().relative_to(ROOT)
        except ValueError as exc:
            raise ValueError("shipped-audio boards must be written under the repository root") from exc
    elif output_path.resolve().parent != report_path.parent.resolve():
        raise ValueError("ordinary boards must be written in the report directory")
    feedback = _load_json(feedback_path) if feedback_path is not None else None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    board_report = _bind_report_audio(
        report, report_dir=report_path.parent, board_path=output_path, shipped_audio=shipped_audio
    )
    _validate_report_for_board(board_report)
    rendered = _render_template(
        template_path.read_text(encoding="utf-8"),
        {
            "__REVIEW_REPORT__": _json_for_html(board_report),
            "__CONTEXT_AUDIO__": _json_for_html(
                _context_audio_map(board_report, board_path=output_path, shipped_audio=shipped_audio)
            ),
            "__APPROVED_FEEDBACK__": _json_for_html(
                _approved_feedback_map(feedback if isinstance(feedback, dict) else None, board_report["clips"])
            ),
        },
    )
    output_path.write_text(rendered, encoding="utf-8")
    return output_path


def assert_pack_synchronized_with_spoken_assets(
    feedback_path: Path = DEFAULT_FEEDBACK,
    spoken_path: Path = SPOKEN_ASSETS,
    plan_path: Path = DEFAULT_PLAN,
) -> None:
    _joined_baseline(plan_path, feedback_path, spoken_path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--no-board", action="store_true")
    board = sub.add_parser("board")
    board.add_argument("--report", type=Path, default=None)
    board.add_argument("--from-accepted-baseline", action="store_true")
    board.add_argument("--feedback", type=Path, default=None)
    board.add_argument("--output", type=Path, required=True)
    board.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    board.add_argument("--shipped-audio", action="store_true")
    sync = sub.add_parser("sync-check")
    sync.add_argument("--feedback", type=Path, default=DEFAULT_FEEDBACK)
    sync.add_argument("--spoken-assets", type=Path, default=SPOKEN_ASSETS)
    sync.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "render":
            asyncio.run(render_plan(args.plan.resolve(), args.output.resolve(), build_board=not args.no_board))
        elif args.command == "board":
            build_listening_board(
                args.report.resolve() if args.report else None,
                args.output.resolve(),
                feedback_path=args.feedback.resolve() if args.feedback else None,
                template_path=args.template.resolve(),
                shipped_audio=args.shipped_audio,
                from_accepted_baseline=args.from_accepted_baseline,
            )
            print(f"Wrote {args.output}", flush=True)
        elif args.command == "sync-check":
            assert_pack_synchronized_with_spoken_assets(
                args.feedback.resolve(), args.spoken_assets.resolve(), args.plan.resolve()
            )
            print("Baseline synchronized with spoken_assets.json", flush=True)
        else:  # pragma: no cover
            raise ValueError(f"unknown command: {args.command}")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
