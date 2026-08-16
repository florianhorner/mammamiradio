#!/usr/bin/env python3
"""Constrained acquisition and proof workflow for bundled starter music.

This tool deliberately cannot accept an arbitrary URL.  ``acquire`` starts
from one exact row in the canonical manifest, ``stage`` creates a deterministic
broadcast derivative, and ``approve`` requires an explicit full-track human
decision before publishing any bytes into the package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mammamiradio.media.starter import (  # noqa: E402
    CATALOG_PATH,
    STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS,
    STARTER_MINIMUM_DURATION_SECONDS,
    STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS,
    StarterCatalogError,
    load_starter_catalog,
)

OFFICIAL_HOSTS = frozenset({"incompetech.com", "www.incompetech.com"})
PIECES_URL = "https://incompetech.com/music/royalty-free/pieces.json"
LICENSE_TEXT = "Licensed under Creative Commons: By Attribution 4.0 License"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_AUDIO_BYTES = 32 * 1024 * 1024
NETWORK_TIMEOUT_SECONDS = 10
FFMPEG_TIMEOUT_SECONDS = 180


class ToolError(RuntimeError):
    """An actionable catalog workflow failure."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolError(f"JSON root in {path} must be an object")
    return value


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _validate_official_url(url: str, *, audio: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise ToolError(f"URL is not on an approved Incompetech HTTPS host: {url}")
    if parsed.username or parsed.password or parsed.port not in {None, 443} or parsed.fragment:
        raise ToolError(f"URL contains disallowed authority or fragment data: {url}")
    if audio and not parsed.path.startswith("/music/royalty-free/mp3-royaltyfree/"):
        raise ToolError(f"audio URL is outside the official download directory: {url}")


def _validate_piece_url(url: str, *, isrc: str) -> None:
    _validate_official_url(url)
    parsed = urlparse(url)
    if parsed.path != "/music/royalty-free/index.html" or parse_qs(parsed.query) != {"isrc": [isrc]}:
        raise ToolError(f"official piece URL does not name exact ISRC {isrc}: {url}")


def _fetch(url: str, *, maximum_bytes: int, audio: bool = False) -> tuple[bytes, str, str]:
    opener = build_opener(_NoRedirect)
    current = url
    for _ in range(4):
        _validate_official_url(current, audio=audio)
        request = Request(current, headers={"User-Agent": "mammamiradio-starter-proof/1"})
        try:
            response = opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS)
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    raise ToolError(f"redirect from {current} omitted Location") from exc
                current = urljoin(current, location)
                continue
            raise ToolError(f"official source returned HTTP {exc.code}: {current}") from exc
        except OSError as exc:
            raise ToolError(f"could not reach official source {current}: {exc}") from exc
        with response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > maximum_bytes:
                raise ToolError(f"official source exceeds {maximum_bytes} bytes: {current}")
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes:
                raise ToolError(f"official source exceeds {maximum_bytes} bytes: {current}")
            content_type = response.headers.get_content_type()
            final_url = response.geturl()
        _validate_official_url(final_url, audio=audio)
        return body, final_url, content_type
    raise ToolError(f"too many redirects while fetching {url}")


def _catalog_data() -> dict[str, Any]:
    return _read_json(CATALOG_PATH)


def _catalog_row(isrc: str) -> dict[str, Any]:
    data = _catalog_data()
    rows = [row for row in data.get("tracks", []) if isinstance(row, dict) and row.get("isrc") == isrc]
    if len(rows) != 1:
        raise ToolError(f"ISRC {isrc!r} is not one exact canonical manifest row")
    return rows[0]


def _repo_receipt_path(relative: str) -> Path:
    declared = Path(relative)
    if declared.is_absolute() or ".." in declared.parts:
        raise ToolError(f"unsafe media receipt path: {relative!r}")
    proof_root = (ROOT / "proof" / "media").resolve()
    candidate = ROOT / declared
    if candidate.is_symlink():
        raise ToolError(f"media receipt may not be a symlink: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"media receipt is missing: {relative}") from exc
    if resolved.parent != proof_root or not resolved.is_file():
        raise ToolError(f"media receipt is outside proof/media: {relative}")
    return resolved


def _work_root() -> Path:
    configured = os.environ.get("MAMMAMIRADIO_STARTER_WORK_DIR")
    return Path(configured).expanduser() if configured else ROOT / "tmp" / "starter-media"


def _candidate_dir(candidate: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,100}", candidate):
        raise ToolError("candidate must be the identifier printed by acquire")
    path = (_work_root() / "candidates" / candidate).resolve()
    expected_parent = (_work_root() / "candidates").resolve()
    if path.parent != expected_parent:
        raise ToolError("candidate path escaped the work directory")
    if not path.is_dir():
        raise ToolError(f"candidate does not exist: {candidate}")
    return path


def _duration_from_clock(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 3:
        raise ToolError(f"unexpected official duration: {value!r}")
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError as exc:
        raise ToolError(f"unexpected official duration: {value!r}") from exc
    return hours * 3600 + minutes * 60 + seconds


def acquire(isrc: str) -> Path:
    row = _catalog_row(isrc)
    source = row.get("source")
    if not isinstance(source, dict):
        raise ToolError(f"canonical row {isrc} has no source object")
    piece_url = row.get("official_piece_url")
    source_url = source.get("url")
    filename = source.get("filename")
    if (
        not isinstance(piece_url, str)
        or not piece_url
        or not isinstance(source_url, str)
        or not source_url
        or not isinstance(filename, str)
        or not filename
    ):
        raise ToolError(f"canonical row {isrc} has incomplete official URLs")
    _validate_piece_url(piece_url, isrc=isrc)
    _validate_official_url(source_url, audio=True)

    page, final_page_url, page_content_type = _fetch(piece_url, maximum_bytes=MAX_PAGE_BYTES)
    _validate_piece_url(final_page_url, isrc=isrc)
    page_text = page.decode("utf-8", errors="strict")
    if LICENSE_TEXT not in page_text or "http://creativecommons.org/licenses/by/4.0/" not in page_text:
        raise ToolError(f"official page for {isrc} does not expose the expected CC BY 4.0 block")

    pieces_raw, final_index_url, _ = _fetch(PIECES_URL, maximum_bytes=MAX_INDEX_BYTES)
    try:
        pieces = json.loads(pieces_raw)
    except json.JSONDecodeError as exc:
        raise ToolError("official pieces index is invalid JSON") from exc
    if not isinstance(pieces, list):
        raise ToolError("official pieces index is not a list")
    matches = [piece for piece in pieces if isinstance(piece, dict) and piece.get("isrc") == isrc]
    if len(matches) != 1:
        raise ToolError(f"official pieces index has {len(matches)} rows for {isrc}")
    official = matches[0]
    if official.get("title") != row.get("title") or official.get("filename") != filename:
        raise ToolError(f"official title or filename changed for {isrc}")
    if _duration_from_clock(str(official.get("length"))) != row.get("expected_duration_seconds"):
        raise ToolError(f"official duration changed for {isrc}")
    if unquote(Path(urlparse(source_url).path).name) != filename:
        raise ToolError(f"source URL filename does not match official index for {isrc}")

    audio_bytes, final_audio_url, audio_content_type = _fetch(source_url, maximum_bytes=MAX_AUDIO_BYTES, audio=True)
    if unquote(Path(urlparse(final_audio_url).path).name) != filename:
        raise ToolError(f"official download redirected to a different filename for {isrc}")
    if audio_content_type not in {"application/octet-stream", "audio/mpeg", "audio/mp3"}:
        raise ToolError(f"official download returned unexpected content type {audio_content_type!r}")
    if len(audio_bytes) < 1024:
        raise ToolError("official download is implausibly small")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate_id = f"{isrc.lower()}-{stamp}"
    candidate_dir = _work_root() / "candidates" / candidate_id
    if candidate_dir.exists():
        raise ToolError(f"candidate already exists: {candidate_id}")
    candidate_dir.mkdir(parents=True)
    original_path = candidate_dir / "original.mp3"
    original_path.write_bytes(audio_bytes)
    receipt = {
        "schema_version": "1",
        "candidate_id": candidate_id,
        "isrc": isrc,
        "title": row["title"],
        "artist": row["artist"],
        "acquired_at": _utc_now(),
        "source": {
            "requested_url": source_url,
            "final_url": final_audio_url,
            "filename": filename,
            "sha256": _sha256_bytes(audio_bytes),
            "bytes": len(audio_bytes),
            "content_type": audio_content_type,
        },
        "evidence": {
            "piece_url": piece_url,
            "final_piece_url": final_page_url,
            "piece_content_type": page_content_type,
            "piece_sha256": _sha256_bytes(page),
            "pieces_url": final_index_url,
            "pieces_sha256": _sha256_bytes(pieces_raw),
            "license_id": "CC-BY-4.0",
            "license_url": LICENSE_URL,
            "official_title": official["title"],
            "official_duration_seconds": row["expected_duration_seconds"],
        },
        "original_file": original_path.name,
        "stage": None,
    }
    _write_json_atomic(candidate_dir / "candidate.json", receipt)
    return candidate_dir


def _probe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels:format=duration,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        media_format = payload["format"]
        return {
            "codec": stream["codec_name"],
            "sample_rate_hz": int(stream["sample_rate"]),
            "channels": int(stream["channels"]),
            "duration_seconds": round(float(media_format["duration"]), 3),
            "bitrate_kbps": round(int(media_format["bit_rate"]) / 1000),
        }
    except (OSError, subprocess.SubprocessError, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"ffprobe could not validate {path.name}: {exc}") from exc


def _loudnorm_filter(path: Path) -> str:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(f"FFmpeg could not analyze {path.name}: {exc}") from exc
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.DOTALL)
    if not matches:
        raise ToolError(f"FFmpeg did not report loudnorm analysis for {path.name}")
    try:
        measured = json.loads(matches[-1])
        values = {
            "measured_I": float(measured["input_i"]),
            "measured_TP": float(measured["input_tp"]),
            "measured_LRA": float(measured["input_lra"]),
            "measured_thresh": float(measured["input_thresh"]),
            "offset": float(measured["target_offset"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"FFmpeg returned invalid loudnorm analysis for {path.name}") from exc
    measured_args = ":".join(f"{name}={value}" for name, value in values.items())
    return f"loudnorm=I=-16:TP=-1.5:LRA=11:{measured_args}:linear=true:print_format=summary"


def _measure_lufs(path: Path) -> float:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-filter_complex",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(f"FFmpeg could not measure {path.name}: {exc}") from exc
    values = re.findall(r"I:\s*(-?\d+(?:\.\d+)?) LUFS", result.stderr)
    if not values:
        raise ToolError(f"FFmpeg did not report integrated loudness for {path.name}")
    return float(values[-1])


def _measure_approved_entries(
    entries: tuple[Any, ...],
    *,
    expected_loudness_by_isrc: Mapping[str, object],
) -> tuple[float, list[str]]:
    """Remeasure release audio without trusting duration or loudness declarations."""

    measured_duration_seconds = 0.0
    failures: list[str] = []
    for entry in entries:
        try:
            facts = _probe(entry.path)
        except ToolError as exc:
            failures.append(f"{entry.isrc}: {exc}")
        else:
            measured_duration_seconds += float(facts["duration_seconds"])
            if (
                facts["codec"] != "mp3"
                or facts["sample_rate_hz"] != 48000
                or facts["channels"] != 2
                or abs(facts["bitrate_kbps"] - 192) > 2
                or abs(facts["duration_seconds"] - entry.duration_seconds) > 0.1
            ):
                failures.append(f"{entry.isrc}: measured audio facts do not match the manifest")

        try:
            measured_lufs = _measure_lufs(entry.path)
        except ToolError as exc:
            failures.append(f"{entry.isrc}: {exc}")
            continue
        if not (STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS <= measured_lufs <= STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS):
            failures.append(
                f"{entry.isrc}: measured loudness {measured_lufs:.1f} LUFS is outside "
                f"{STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS:.1f}.."
                f"{STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS:.1f} LUFS"
            )
        declared_lufs = expected_loudness_by_isrc.get(entry.isrc)
        if not isinstance(declared_lufs, int | float) or abs(measured_lufs - float(declared_lufs)) > 0.1:
            failures.append(f"{entry.isrc}: measured loudness does not match the manifest")
    return measured_duration_seconds, failures


def stage(candidate: str) -> Path:
    candidate_dir = _candidate_dir(candidate)
    receipt_path = candidate_dir / "candidate.json"
    receipt = _read_json(receipt_path)
    source = receipt.get("source")
    if not isinstance(source, dict):
        raise ToolError("candidate source receipt is missing")
    original = candidate_dir / str(receipt.get("original_file"))
    if not original.is_file() or _sha256_file(original) != source.get("sha256"):
        raise ToolError("candidate original no longer matches its acquisition hash")

    staged = candidate_dir / "staged.mp3"
    partial = candidate_dir / "staged.partial.mp3"
    partial.unlink(missing_ok=True)
    input_lufs = _measure_lufs(original)
    loudnorm_filter = (
        "anull"
        if STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS <= input_lufs <= STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS
        else _loudnorm_filter(original)
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(original),
        "-map",
        "0:a:0",
        "-vn",
        "-map_metadata",
        "-1",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-b:a",
        "192k",
        "-af",
        loudnorm_filter,
        str(partial),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        partial.unlink(missing_ok=True)
        raise ToolError(f"FFmpeg could not stage {candidate}: {exc}") from exc
    facts = _probe(partial)
    if facts["codec"] != "mp3" or facts["sample_rate_hz"] != 48000 or facts["channels"] != 2:
        partial.unlink(missing_ok=True)
        raise ToolError("staged derivative is not 48 kHz stereo MP3")
    if abs(facts["bitrate_kbps"] - 192) > 2:
        partial.unlink(missing_ok=True)
        raise ToolError("staged derivative is not 192 kbps")
    expected = int(receipt["evidence"]["official_duration_seconds"])
    if abs(float(facts["duration_seconds"]) - expected) > 10.0:
        partial.unlink(missing_ok=True)
        raise ToolError("staged derivative duration differs from the official duration")
    lufs = _measure_lufs(partial)
    if not (STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS <= lufs <= STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS):
        reconciled = candidate_dir / "staged.reconciled.partial.mp3"
        reconciled.unlink(missing_ok=True)
        gain_db = max(-6.0, min(6.0, -16.0 - lufs))
        reconcile_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(partial),
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-b:a",
            "192k",
            "-af",
            f"volume={gain_db:.3f}dB,alimiter=limit=0.75:level=false",
            str(reconciled),
        ]
        try:
            subprocess.run(
                reconcile_command,
                check=True,
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reconciled.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            raise ToolError(f"FFmpeg could not reconcile {candidate}: {exc}") from exc
        os.replace(reconciled, partial)
        facts = _probe(partial)
        lufs = _measure_lufs(partial)
    if not (STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS <= lufs <= STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS):
        partial.unlink(missing_ok=True)
        raise ToolError(f"staged derivative measured {lufs:.1f} LUFS; expected about -16 LUFS")
    os.replace(partial, staged)
    receipt["stage"] = {
        **facts,
        "integrated_loudness_lufs": lufs,
        "sha256": _sha256_file(staged),
        "bytes": staged.stat().st_size,
        "staged_at": _utc_now(),
        "file": staged.name,
        "modification_notice": (
            "Converted from the official MP3, stripped of metadata/artwork, resampled to 48 kHz stereo, "
            "encoded at 192 kbps, and level-checked/normalized to approximately -16 LUFS."
        ),
    }
    _write_json_atomic(receipt_path, receipt)
    return staged


REQUIRED_DECISIONS = (
    "listened_from_start_to_finish",
    "title_and_artist_match",
    "no_unexpected_speech_or_restricted_content",
    "editorially_approved",
    "license_evidence_reviewed",
)


def _validate_decisions(path: Path, *, isrc: str) -> dict[str, Any]:
    decision = _read_json(path)
    if decision.get("schema_version") != "1" or decision.get("isrc") != isrc:
        raise ToolError("audition decision schema or ISRC does not match the candidate")
    reviewed_at = decision.get("reviewed_at")
    if not isinstance(reviewed_at, str) or not reviewed_at.endswith(("Z", "+00:00")):
        raise ToolError("audition decision needs a timezone-qualified reviewed_at")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError("audition decision reviewed_at is not a valid ISO timestamp") from exc
    if parsed_reviewed_at.tzinfo is None:
        raise ToolError("audition decision needs a timezone-qualified reviewed_at")
    if decision.get("reviewer_role") not in {"project-maintainer", "designated-media-reviewer"}:
        raise ToolError("audition decision needs an approved non-personal reviewer_role")
    missing = [field for field in REQUIRED_DECISIONS if decision.get(field) is not True]
    if missing:
        raise ToolError("full-track human approval is incomplete: " + ", ".join(missing))
    if "reviewer" in decision:
        raise ToolError("use reviewer_role, not a personal reviewer name, in the tracked decision")
    return decision


def _slug(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return compact or "track"


def approve(candidate: str, *, replace_isrc: str, decisions_path: Path) -> tuple[Path, Path]:
    candidate_dir = _candidate_dir(candidate)
    candidate_receipt = _read_json(candidate_dir / "candidate.json")
    isrc = candidate_receipt.get("isrc")
    if not isinstance(isrc, str) or replace_isrc != isrc:
        raise ToolError("--replace must name the candidate's exact canonical ISRC")
    stage_data = candidate_receipt.get("stage")
    if not isinstance(stage_data, dict):
        raise ToolError("candidate must be staged before approval")
    decision = _validate_decisions(decisions_path, isrc=isrc)
    staged = candidate_dir / str(stage_data.get("file"))
    if not staged.is_file() or _sha256_file(staged) != stage_data.get("sha256"):
        raise ToolError("staged derivative no longer matches its recorded hash")
    measured = _probe(staged)
    measured_lufs = _measure_lufs(staged)
    for field in ("codec", "sample_rate_hz", "channels", "bitrate_kbps"):
        if measured[field] != stage_data.get(field):
            raise ToolError(f"staged derivative {field} no longer matches its recorded fact")
    if (
        measured["codec"] != "mp3"
        or measured["sample_rate_hz"] != 48000
        or measured["channels"] != 2
        or abs(measured["bitrate_kbps"] - 192) > 2
    ):
        raise ToolError("staged derivative is not the required 48 kHz stereo 192 kbps MP3")
    if abs(float(measured["duration_seconds"]) - float(stage_data.get("duration_seconds", 0))) > 0.1:
        raise ToolError("staged derivative duration no longer matches its recorded fact")
    if abs(measured_lufs - float(stage_data.get("integrated_loudness_lufs", 0))) > 0.1:
        raise ToolError("staged derivative loudness no longer matches its recorded fact")
    if not (STARTER_MINIMUM_INTEGRATED_LOUDNESS_LUFS <= measured_lufs <= STARTER_MAXIMUM_INTEGRATED_LOUDNESS_LUFS):
        raise ToolError("staged derivative is outside the approved -16 LUFS tolerance")
    manifest = _catalog_data()
    rows = manifest.get("tracks")
    if not isinstance(rows, list):
        raise ToolError("canonical manifest has no track list")
    indexes = [index for index, row in enumerate(rows) if isinstance(row, dict) and row.get("isrc") == isrc]
    if len(indexes) != 1:
        raise ToolError(f"canonical manifest does not contain exactly one row for {isrc}")
    row = dict(rows[indexes[0]])
    expected_duration = row.get("expected_duration_seconds")
    if not isinstance(expected_duration, int) or abs(measured["duration_seconds"] - expected_duration) > 10.0:
        raise ToolError("staged derivative duration differs from the canonical official duration")
    previous_derivative_value = row.get("derivative")
    previous_evidence_value = row.get("evidence")
    previous_derivative: dict[str, Any] = (
        dict(previous_derivative_value) if isinstance(previous_derivative_value, dict) else {}
    )
    previous_evidence: dict[str, Any] = (
        dict(previous_evidence_value) if isinstance(previous_evidence_value, dict) else {}
    )
    content_id = str(stage_data["sha256"])[:12]
    destination_relative = f"tracks/{isrc.lower()}-{_slug(str(row['title']))}-{content_id}.mp3"
    destination = CATALOG_PATH.parent / destination_relative
    approval_relative = f"proof/media/{isrc.lower()}-{content_id}-approval.json"
    approval_path = ROOT / approval_relative
    source_data = candidate_receipt["source"]
    reviewed_at = decision["reviewed_at"]
    receipt = {
        "schema_version": "1",
        "receipt_type": "starter-track-approval",
        "isrc": isrc,
        "title": row["title"],
        "artist": row["artist"],
        "reviewed_at": reviewed_at,
        "reviewer_role": decision["reviewer_role"],
        "decisions": {field: True for field in REQUIRED_DECISIONS},
        "notes_present": bool(decision.get("notes")),
        "notes_sha256": (
            hashlib.sha256(str(decision["notes"]).encode()).hexdigest() if decision.get("notes") else None
        ),
        "source": {
            "requested_url": source_data["requested_url"],
            "final_url": source_data["final_url"],
            "sha256": source_data["sha256"],
            "bytes": source_data["bytes"],
            "acquired_at": candidate_receipt["acquired_at"],
            "piece_sha256": candidate_receipt["evidence"]["piece_sha256"],
            "pieces_sha256": candidate_receipt["evidence"]["pieces_sha256"],
        },
        "derivative": {
            key: stage_data[key]
            for key in (
                "sha256",
                "bytes",
                "codec",
                "sample_rate_hz",
                "channels",
                "bitrate_kbps",
                "duration_seconds",
                "integrated_loudness_lufs",
                "staged_at",
                "modification_notice",
            )
        },
    }

    source = dict(row["source"])
    source.update(
        {
            "url": source_data["requested_url"],
            "sha256": source_data["sha256"],
            "bytes": source_data["bytes"],
            "acquired_at": candidate_receipt["acquired_at"],
        }
    )
    row["source"] = source
    row["derivative"] = {
        "path": destination_relative,
        "sha256": stage_data["sha256"],
        "bytes": stage_data["bytes"],
        "codec": stage_data["codec"],
        "sample_rate_hz": stage_data["sample_rate_hz"],
        "channels": stage_data["channels"],
        "bitrate_kbps": stage_data["bitrate_kbps"],
        "duration_seconds": stage_data["duration_seconds"],
        "integrated_loudness_lufs": stage_data["integrated_loudness_lufs"],
    }
    row["modification_notice"] = stage_data["modification_notice"]
    evidence = dict(row["evidence"])
    evidence["source_receipt"] = approval_relative
    evidence["audition_receipt"] = approval_relative
    row["evidence"] = evidence
    row["approval"] = {"status": "approved", "reviewed_at": reviewed_at}
    rows[indexes[0]] = row

    destination.parent.mkdir(parents=True, exist_ok=True)
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    destination_tmp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    receipt_tmp = approval_path.with_name(f".{approval_path.name}.{os.getpid()}.tmp")
    manifest_tmp = CATALOG_PATH.with_name(f".{CATALOG_PATH.name}.{os.getpid()}.tmp")
    committed = False
    try:
        shutil.copyfile(staged, destination_tmp)
        receipt_tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(destination_tmp, destination)
        os.replace(receipt_tmp, approval_path)
        # The manifest is the commit marker: runtime cannot admit the new bytes
        # until both content-addressed artifacts already exist.
        os.replace(manifest_tmp, CATALOG_PATH)
        committed = True
    finally:
        destination_tmp.unlink(missing_ok=True)
        receipt_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)
    if committed:
        old_derivative = previous_derivative.get("path")
        if isinstance(old_derivative, str) and old_derivative != destination_relative:
            old_path = CATALOG_PATH.parent / old_derivative
            if old_path.parent == CATALOG_PATH.parent / "tracks":
                old_path.unlink(missing_ok=True)
        old_receipt = previous_evidence.get("audition_receipt")
        if isinstance(old_receipt, str) and old_receipt != approval_relative:
            old_path = ROOT / old_receipt
            if old_path.parent == ROOT / "proof" / "media" and old_path.name.endswith("-approval.json"):
                old_path.unlink(missing_ok=True)
    return destination, approval_path


def check(*, allow_incomplete: bool = False) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {
        name: {"status": "PASS", "messages": []}
        for name in ("MEDIA-EVIDENCE", "MEDIA-BYTES", "MEDIA-AUDIO", "MEDIA-PACKAGE")
    }

    def record(group: str, message: str, *, incomplete: bool = False) -> None:
        status = "WARN" if incomplete and allow_incomplete else "FAIL"
        if groups[group]["status"] != "FAIL":
            groups[group]["status"] = status
        groups[group]["messages"].append(message)

    try:
        catalog = _catalog_data()
        tracks = catalog.get("tracks")
        requirements = catalog.get("release_requirements")
        if catalog.get("schema_version") != "1" or catalog.get("catalog_id") != "starter-v1":
            record("MEDIA-EVIDENCE", "manifest schema/catalog id is invalid")
            tracks = []
        if not isinstance(tracks, list) or not isinstance(requirements, dict):
            record("MEDIA-EVIDENCE", "manifest tracks/release requirements are invalid")
            tracks = []
        exact_count = requirements.get("exact_track_count", 0) if isinstance(requirements, dict) else 0
        if len(tracks) != exact_count:
            record("MEDIA-EVIDENCE", f"declared tracks {len(tracks)}/{exact_count}")
        minimum_seconds = requirements.get("minimum_duration_seconds", 0) if isinstance(requirements, dict) else 0
        if not isinstance(minimum_seconds, int) or minimum_seconds < STARTER_MINIMUM_DURATION_SECONDS:
            record(
                "MEDIA-EVIDENCE",
                f"release duration floor must be at least {STARTER_MINIMUM_DURATION_SECONDS}s",
            )
        effective_minimum_seconds = max(
            minimum_seconds if isinstance(minimum_seconds, int) else 0,
            STARTER_MINIMUM_DURATION_SECONDS,
        )
        expected_seconds = sum(row.get("expected_duration_seconds", 0) for row in tracks if isinstance(row, dict))
        if expected_seconds < effective_minimum_seconds:
            record(
                "MEDIA-EVIDENCE",
                f"declared duration {expected_seconds}s is below {effective_minimum_seconds}s",
            )

        approved_paths: set[str] = set()
        approved_loudness_by_isrc: dict[str, object] = {}
        approved_count = 0
        receipt_cache: dict[Path, dict[str, Any]] = {}
        for row in tracks:
            if not isinstance(row, dict):
                record("MEDIA-EVIDENCE", "manifest contains a non-object track")
                continue
            isrc = row.get("isrc", "unknown")
            license_data = row.get("license")
            source = row.get("source")
            evidence = row.get("evidence")
            if not isinstance(license_data, dict) or license_data.get("id") != "CC-BY-4.0":
                record("MEDIA-EVIDENCE", f"{isrc}: license is not CC-BY-4.0")
            if not isinstance(source, dict):
                record("MEDIA-EVIDENCE", f"{isrc}: source facts are missing")
            else:
                try:
                    _validate_official_url(str(source.get("url")), audio=True)
                    _validate_piece_url(str(source.get("evidence_url")), isrc=str(isrc))
                except ToolError as exc:
                    record("MEDIA-EVIDENCE", f"{isrc}: {exc}")
                source_sha = source.get("sha256")
                if not isinstance(source_sha, str) or len(source_sha) != 64:
                    record("MEDIA-EVIDENCE", f"{isrc}: acquisition hash is pending", incomplete=True)
            if not isinstance(evidence, dict):
                record("MEDIA-EVIDENCE", f"{isrc}: evidence links are missing")
            else:
                source_receipt = evidence.get("source_receipt")
                try:
                    if not isinstance(source_receipt, str):
                        raise ToolError("source receipt path is missing")
                    receipt_path = _repo_receipt_path(source_receipt)
                    receipt_data = receipt_cache.setdefault(receipt_path, _read_json(receipt_path))
                    if receipt_data.get("receipt_type") == "starter-source-acquisition":
                        receipt_tracks = receipt_data.get("tracks")
                        if not isinstance(receipt_tracks, list):
                            raise ToolError("source acquisition receipt has no track facts")
                        matches = [
                            item for item in receipt_tracks if isinstance(item, dict) and item.get("isrc") == isrc
                        ]
                        if len(matches) != 1:
                            raise ToolError("source acquisition receipt does not uniquely name the ISRC")
                        receipt_source_sha = matches[0].get("source_sha256")
                        receipt_source_bytes = matches[0].get("source_bytes")
                    elif receipt_data.get("receipt_type") == "starter-track-approval":
                        receipt_source = receipt_data.get("source")
                        if receipt_data.get("isrc") != isrc or not isinstance(receipt_source, dict):
                            raise ToolError("approval source receipt does not name the ISRC")
                        receipt_source_sha = receipt_source.get("sha256")
                        receipt_source_bytes = receipt_source.get("bytes")
                    else:
                        raise ToolError("source receipt type is not recognized")
                    if not isinstance(source, dict) or (
                        receipt_source_sha != source.get("sha256") or receipt_source_bytes != source.get("bytes")
                    ):
                        raise ToolError("source receipt does not match manifest facts")
                except ToolError as exc:
                    approval_value = row.get("approval")
                    pending_row = not isinstance(approval_value, dict) or approval_value.get("status") != "approved"
                    missing_receipt = "missing" in str(exc)
                    record(
                        "MEDIA-EVIDENCE",
                        f"{isrc}: {exc}",
                        incomplete=pending_row and missing_receipt,
                    )
            approval = row.get("approval")
            if not isinstance(approval, dict) or approval.get("status") != "approved":
                record("MEDIA-EVIDENCE", f"{isrc}: full-track human audition is pending", incomplete=True)
                continue
            approved_count += 1
            derivative = row.get("derivative")
            if isinstance(derivative, dict) and isinstance(derivative.get("path"), str):
                approved_paths.add(derivative["path"])
                approved_loudness_by_isrc[str(isrc)] = derivative.get("integrated_loudness_lufs")
            else:
                record("MEDIA-BYTES", f"{isrc}: approved row has no derivative")
                continue
            audition_receipt = evidence.get("audition_receipt") if isinstance(evidence, dict) else None
            try:
                if not isinstance(audition_receipt, str):
                    raise ToolError("audition receipt path is missing")
                approval_path = _repo_receipt_path(audition_receipt)
                approval_receipt = receipt_cache.setdefault(approval_path, _read_json(approval_path))
                decisions = approval_receipt.get("decisions")
                receipt_source = approval_receipt.get("source")
                receipt_derivative = approval_receipt.get("derivative")
                if (
                    approval_receipt.get("schema_version") != "1"
                    or approval_receipt.get("receipt_type") != "starter-track-approval"
                    or approval_receipt.get("isrc") != isrc
                    or not isinstance(decisions, dict)
                    or any(decisions.get(field) is not True for field in REQUIRED_DECISIONS)
                    or not isinstance(receipt_source, dict)
                    or receipt_source.get("sha256") != (source.get("sha256") if isinstance(source, dict) else None)
                    or not isinstance(receipt_derivative, dict)
                    or receipt_derivative.get("sha256") != derivative.get("sha256")
                ):
                    raise ToolError("audition receipt facts do not match the manifest")
            except ToolError as exc:
                record("MEDIA-EVIDENCE", f"{isrc}: {exc}")

        if approved_count != exact_count:
            record("MEDIA-BYTES", f"approved derivatives {approved_count}/{exact_count}", incomplete=True)
            record("MEDIA-AUDIO", f"playable derivatives {approved_count}/{exact_count}", incomplete=True)
        loaded_entries: tuple[Any, ...] = ()
        try:
            loaded = load_starter_catalog(require_complete=not allow_incomplete, verify_all=True)
            loaded_entries = loaded.entries
            if len(loaded.entries) != approved_count:
                record("MEDIA-BYTES", "approved manifest rows and loader output disagree")
        except StarterCatalogError as exc:
            if allow_incomplete and approved_count == 0:
                pass
            else:
                record("MEDIA-BYTES", str(exc), incomplete=approved_count != exact_count)
        if approved_count and len(loaded_entries) != approved_count:
            record(
                "MEDIA-AUDIO",
                f"remeasured approved derivatives {len(loaded_entries)}/{approved_count}",
                incomplete=approved_count != exact_count,
            )
        if loaded_entries:
            measured_duration_seconds, audio_failures = _measure_approved_entries(
                loaded_entries,
                expected_loudness_by_isrc=approved_loudness_by_isrc,
            )
            for failure in audio_failures:
                record("MEDIA-AUDIO", failure)
            if measured_duration_seconds < effective_minimum_seconds:
                record(
                    "MEDIA-AUDIO",
                    f"measured approved duration {measured_duration_seconds:.3f}s is below "
                    f"{effective_minimum_seconds}s",
                    incomplete=approved_count != exact_count,
                )

        audio_suffixes = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
        declared_audio = {
            path.relative_to(CATALOG_PATH.parent).as_posix()
            for path in CATALOG_PATH.parent.rglob("*")
            if path.suffix.casefold() in audio_suffixes
        }
        undeclared = sorted(declared_audio - approved_paths)
        if undeclared:
            record("MEDIA-PACKAGE", "undeclared packaged audio: " + ", ".join(undeclared))
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        if '"assets/**/*"' not in pyproject:
            record("MEDIA-PACKAGE", "pyproject does not package assets/**/*")
    except (OSError, ToolError, TypeError, ValueError) as exc:
        record("MEDIA-EVIDENCE", str(exc))

    return {
        "schema_version": "1",
        "catalog": str(CATALOG_PATH.relative_to(ROOT)),
        "allow_incomplete": allow_incomplete,
        "groups": groups,
        "ok": all(group["status"] in ({"PASS", "WARN"} if allow_incomplete else {"PASS"}) for group in groups.values()),
    }


def _print_report(report: dict[str, Any]) -> None:
    for name, group in report["groups"].items():
        messages = group["messages"] or ["all checks passed"]
        print(f"{name} {group['status']}: {messages[0]}")
        for message in messages[1:]:
            print(f"  - {message}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    acquire_parser = subparsers.add_parser("acquire", help="acquire one predeclared official original")
    acquire_parser.add_argument("--isrc", required=True)
    stage_parser = subparsers.add_parser("stage", help="normalize one acquired candidate")
    stage_parser.add_argument("--candidate", required=True)
    approve_parser = subparsers.add_parser("approve", help="publish a staged, human-approved candidate")
    approve_parser.add_argument("--candidate", required=True)
    approve_parser.add_argument("--replace", required=True, dest="replace_isrc")
    approve_parser.add_argument("--decisions", required=True, type=Path)
    check_parser = subparsers.add_parser("check", help="validate the canonical source catalog")
    check_parser.add_argument("--allow-incomplete", action="store_true")
    check_parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "acquire":
            output = acquire(args.isrc)
            print(f"candidate={output.name}")
            print(f"receipt={output / 'candidate.json'}")
            return 0
        if args.command == "stage":
            output = stage(args.candidate)
            print(f"staged={output}")
            return 0
        if args.command == "approve":
            derivative, receipt = approve(args.candidate, replace_isrc=args.replace_isrc, decisions_path=args.decisions)
            print(f"derivative={derivative}")
            print(f"receipt={receipt}")
            return 0
        report = check(allow_incomplete=args.allow_incomplete)
        if args.json_output:
            print(json.dumps(report, indent=2))
        else:
            _print_report(report)
        return 0 if report["ok"] else 1
    except ToolError as exc:
        print(f"starter-catalog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
