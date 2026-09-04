"""Single-use, non-persistent Jamendo music preparation.

The provider deliberately does not create ``Track`` objects or use the normal
download/cache pipeline.  It owns one operation directory and one normalized
audio file at a time, publishes an immutable in-memory lease, and deletes that
file through the queued segment's release hook.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import math
import os
import re
import select
import shutil
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Protocol, TypedDict
from urllib.parse import urlparse

import httpx

from mammamiradio.audio.admission import ffmpeg_slot
from mammamiradio.core.config import JAMENDO_CLIENT_ID_RE
from mammamiradio.core.models import MediaAttribution, Segment, SegmentType, safe_media_attribution_dict

logger = logging.getLogger(__name__)

_API_URL = "https://api.jamendo.com/v3.0/tracks/"
_API_INCLUDE = "licenses"

# Single source of truth for every failure code this provider can surface.
# Three lists used to drift independently: the raise sites here, the sanitizer
# allowlist in web/media_sources.py, and the operator copy table in admin.html.
# The middle one silently laundered any code it did not know into "api_failed",
# so the three codes the edge install reports most often never reached a human.
# tests/playlist/test_jamendo_failure_code_contract.py asserts all three agree.
JAMENDO_FAILURE_CODES = frozenset(
    {
        "api_auth_failed",
        "api_failed",
        "api_malformed",
        "audio_empty",
        "audio_fetch_failed",
        "audio_oversize",
        "audio_size_invalid",
        "duration_rejected",
        "empty_results",
        "ffmpeg_failed",
        "ffmpeg_timeout",
        "ffprobe_failed",
        "identity_mismatch",
        "internal_error",
        "invalid_url",
        "lease_invalid",
        "license_rejected",
        "metadata_changed",
        "metadata_invalid",
        "metadata_oversize",
        "network_timeout",
        "rate_limited",
        "source_revision_invalid",
        "temp_root_invalid",
    }
)
# Reasons an attempt can report that are not a rejected candidate. They belong in
# the breakdown the operator card reads, but must never inflate a count of
# candidates thrown away: "no usable songs came back" examined nothing.
_NON_CANDIDATE_REASONS = frozenset({"empty_results"})
_BLOCKED_PROVIDER_CONTRACT_CODES = frozenset({2, 3, 4, 7, 8, 9, 10, 12, 13})
_BLOCKED_PROVIDER_AUTH_CODES = frozenset({5, 11})
# Jamendo provider code 6 means rate limit exceeded. Keep it separate from
# generic API failures so the UI can report its automatic retry truthfully.
_RATE_LIMIT_PROVIDER_CODE = 6
# The same condition arrives two ways: an in-body provider code on a 200, or
# an ordinary HTTP 429. Only the first was mapped, so the copy written for a
# throttle never reached an operator who was actually throttled.
_RATE_LIMIT_STATUS_CODE = 429
_TRACK_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
_OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
_ALLOWED_LICENSES = {
    "https://creativecommons.org/licenses/by/3.0/": "CC-BY-3.0",
    "https://creativecommons.org/licenses/by/4.0/": "CC-BY-4.0",
}
_CONNECT_READ_TIMEOUT_SEC = 10.0
_DISCOVERY_DEADLINE_SEC = 30.0
_ACTIVE_TRANSFER_HEADROOM_SEC = 60.0
_MIN_ACTIVE_TRANSFER_TIMEOUT_SEC = 120.0
_MAX_ACTIVE_TRANSFER_TIMEOUT_SEC = 780.0
_MAX_FFMPEG_PROCESSING_TIMEOUT_SEC = 300.0
_PIPE_PROGRESS_TIMEOUT_SEC = 10.0
_PIPE_POLL_INTERVAL_SEC = 0.25
_FFMPEG_FINALIZE_TIMEOUT_SEC = 30.0
_PROCESS_CLEANUP_TIMEOUT_SEC = 5.0
_MAX_METADATA_RESPONSE_BYTES = 1024 * 1024
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MIN_DURATION_SEC = 60.0
_MAX_DURATION_SEC = 720.0
_MAX_LEASE_AGE_SEC = 30.0 * 60.0
_RETRY_DELAYS_SEC = (60.0, 120.0, 300.0, 900.0)
_TRANSFORM_DESCRIPTION = "Normalized and transcoded by Mamma Mi Radio; no musical edits."
_LEASE_RELEASE_LOG_CODES = frozenset(
    {
        "config_apply_failed",
        "config_changed",
        "consumed",
        "expired",
        "lease_invalid",
        "shutdown",
        "source_changed",
    }
)


class JamendoProviderState(StrEnum):
    """Authoritative internal provider lifecycle."""

    # DISABLED / NEEDS_CONFIG -> IDLE -> DISCOVERING -> FETCHING
    # -> NORMALIZING -> READY -> QUEUED -> PLAYING -> CONSUMED -> IDLE
    # awaited transient failure -> DEGRADED; contract/config failure -> BLOCKED
    DISABLED = "disabled"
    NEEDS_CONFIG = "needs_config"
    IDLE = "idle"
    DISCOVERING = "discovering"
    FETCHING = "fetching"
    NORMALIZING = "normalizing"
    READY = "ready"
    QUEUED = "queued"
    PLAYING = "playing"
    CONSUMED = "consumed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class JamendoLease:
    """One current-process authorization for one queued playback attempt."""

    lease_id: str
    boot_id: str
    operation_id: str
    provider_epoch: int
    client_id_fingerprint: str
    provider_track_id: str
    title: str
    artist: str
    source_url: str
    license_id: str
    license_url: str
    credit: str
    fetched_at: float
    source_revision: int
    artifact_path: Path
    artifact_sha256: str
    duration_sec: float
    transform_description: str

    @property
    def attribution(self) -> MediaAttribution:
        """Return only listener-safe provider-reported attribution."""
        return MediaAttribution(
            provider="jamendo",
            license_id=self.license_id,
            license_url=self.license_url,
            source_url=self.source_url,
            credit=self.credit,
            modified=True,
            basis="provider_reported",
        )


class _CandidateData(TypedDict):
    provider_track_id: str
    title: str
    artist: str
    source_url: str
    license_id: str
    license_url: str
    credit: str
    audio_url: str
    duration_sec: float


@dataclass(frozen=True, slots=True)
class _PreparedArtifact:
    path: Path
    sha256: str
    duration_sec: float
    byte_count: int


class _StreamWorker(Protocol):
    def __call__(
        self,
        candidate: _CandidateData,
        operation_dir: Path,
        on_normalizing: Callable[[], None],
    ) -> _PreparedArtifact: ...


class _ProviderError(RuntimeError):
    def __init__(self, code: str, *, provider_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.provider_code = provider_code


class _TransientError(_ProviderError):
    pass


class _BlockedError(_ProviderError):
    pass


class _CandidateRejectedError(_ProviderError):
    pass


class _StaleOperationError(RuntimeError):
    pass


def _client_id_fingerprint(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def _bounded_text(value: object, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise _CandidateRejectedError("metadata_invalid")
    text = " ".join(value.split()).strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise _CandidateRejectedError("metadata_invalid")
    return text


def _safe_https_url(value: object, *, host_suffix: str, maximum: int = 2048) -> str:
    if not isinstance(value, str):
        raise _CandidateRejectedError("invalid_url")
    url = value.strip()
    if not url or len(url) > maximum or "\\" in url or any(ord(char) < 32 for char in url):
        raise _CandidateRejectedError("invalid_url")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise _CandidateRejectedError("invalid_url") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not host
        or (host != host_suffix and not host.endswith(f".{host_suffix}"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/")
        or bool(parsed.fragment)
        or "%2e" in parsed.path.lower()
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise _CandidateRejectedError("invalid_url")
    return url


def _canonical_license(value: object) -> tuple[str, str]:
    if isinstance(value, str) and value.startswith("http://creativecommons.org/licenses/by/"):
        value = "https://" + value.removeprefix("http://")
    url = _safe_https_url(value, host_suffix="creativecommons.org")
    parsed = urlparse(url)
    if parsed.query or parsed.fragment or url not in _ALLOWED_LICENSES:
        raise _CandidateRejectedError("license_rejected")
    return _ALLOWED_LICENSES[url], url


def _parse_duration(value: object) -> float:
    if isinstance(value, bool):
        raise _CandidateRejectedError("duration_rejected")
    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _CandidateRejectedError("duration_rejected") from exc
    if not math.isfinite(duration) or not _MIN_DURATION_SEC <= duration <= _MAX_DURATION_SEC:
        raise _CandidateRejectedError("duration_rejected")
    return duration


def _candidate_from_exact_result(item: object, expected_id: str) -> _CandidateData:
    if not isinstance(item, Mapping):
        raise _CandidateRejectedError("metadata_invalid")
    track_id = str(item.get("id", "")).strip()
    if track_id != expected_id or _TRACK_ID_RE.fullmatch(track_id) is None:
        raise _CandidateRejectedError("identity_mismatch")
    title = _bounded_text(item.get("name"))
    artist = _bounded_text(item.get("artist_name"))
    source_url = _safe_https_url(item.get("shareurl"), host_suffix="jamendo.com", maximum=512)
    audio_url = _safe_https_url(item.get("audio"), host_suffix="jamendo.com")
    license_id, license_url = _canonical_license(item.get("license_ccurl"))
    duration_sec = _parse_duration(item.get("duration"))
    credit = f"{artist} - {title}, provided by Jamendo, {license_id}"
    attribution = MediaAttribution(
        provider="jamendo",
        license_id=license_id,
        license_url=license_url,
        source_url=source_url,
        credit=credit,
        modified=True,
        basis="provider_reported",
    )
    safe_attribution = safe_media_attribution_dict(attribution)
    if safe_attribution is None:
        # Candidate admission and the public serializer must share one URL
        # contract. Otherwise a track could be prepared and aired only to have
        # its required attribution removed at the listener boundary.
        raise _CandidateRejectedError("invalid_url")
    return {
        "provider_track_id": track_id,
        "title": title,
        "artist": artist,
        "source_url": str(safe_attribution["source_url"]),
        "license_id": license_id,
        "license_url": license_url,
        "credit": str(safe_attribution["credit"]),
        "audio_url": audio_url,
        "duration_sec": duration_sec,
    }


def _stable_admission_identity(candidate: _CandidateData) -> tuple[object, ...]:
    """Fields compared across discovery and exact-ID revalidation.

    Stream URLs and share-page paths can differ between Jamendo list and id
    lookups without changing the track, license, or attribution. Comparing the
    full candidate dict rejected every live candidate after discovery recovered.
    """
    return (
        candidate["provider_track_id"],
        candidate["title"],
        candidate["artist"],
        candidate["license_id"],
        candidate["license_url"],
        candidate["duration_sec"],
    )


def _dominant_code(rejections: Mapping[str, int]) -> str | None:
    """Return the failure code that accounts for most of one attempt's rejections.

    Ties break on the code name so the operator card does not flip between two
    equally-common reasons on consecutive polls. Returns ``None`` for an attempt
    with no rejections, which is how the card knows to stay quiet.
    """
    if not rejections:
        return None
    return max(sorted(rejections), key=lambda code: rejections[code])


def _coarse_provider_code(value: object) -> int | None:
    """Normalize a provider code to an integer from 0 through 9999, or return ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 9999 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 4:
        return int(value)
    return None


def _validated_api_results(status_code: int, body: bytes) -> list[object]:
    if status_code in (401, 403):
        raise _BlockedError("api_auth_failed")
    if status_code == _RATE_LIMIT_STATUS_CODE:
        raise _TransientError("rate_limited")
    if status_code != 200:
        raise _TransientError("api_failed")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise _TransientError("api_malformed") from exc
    if not isinstance(payload, Mapping):
        raise _TransientError("api_malformed")
    headers = payload.get("headers")
    if not isinstance(headers, Mapping):
        raise _TransientError("api_failed")
    if headers.get("status") != "success" or headers.get("code") not in (0, "0"):
        provider_code = _coarse_provider_code(headers.get("code"))
        if provider_code == _RATE_LIMIT_PROVIDER_CODE:
            # Keep the existing retry behavior and expose a distinct UI message.
            raise _TransientError("rate_limited", provider_code=provider_code)
        if provider_code in _BLOCKED_PROVIDER_AUTH_CODES:
            raise _BlockedError("api_auth_failed", provider_code=provider_code)
        if provider_code in _BLOCKED_PROVIDER_CONTRACT_CODES:
            raise _BlockedError("api_failed", provider_code=provider_code)
        raise _TransientError("api_failed", provider_code=provider_code)
    results = payload.get("results")
    if not isinstance(results, list):
        raise _TransientError("api_malformed")
    return results[:50]


async def _bounded_api_results(client: httpx.AsyncClient, params: Mapping[str, str]) -> list[object]:
    """Stream one metadata response into a bounded buffer before JSON parsing."""
    async with client.stream(
        "GET",
        _API_URL,
        params=params,
        follow_redirects=False,
        timeout=httpx.Timeout(_CONNECT_READ_TIMEOUT_SEC),
    ) as response:
        if response.status_code in (401, 403):
            raise _BlockedError("api_auth_failed")
        if response.status_code == _RATE_LIMIT_STATUS_CODE:
            raise _TransientError("rate_limited")
        if response.status_code != 200:
            raise _TransientError("api_failed")
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                parsed_length = int(declared_length)
            except ValueError as exc:
                raise _TransientError("api_malformed") from exc
            if parsed_length < 0:
                raise _TransientError("api_malformed")
            if parsed_length > _MAX_METADATA_RESPONSE_BYTES:
                raise _TransientError("metadata_oversize")
        body = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            if len(body) + len(chunk) > _MAX_METADATA_RESPONSE_BYTES:
                raise _TransientError("metadata_oversize")
            body.extend(chunk)
    return _validated_api_results(response.status_code, bytes(body))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _active_transfer_timeout_sec(duration_sec: float) -> float:
    """Set the transfer timeout from the validated track duration."""
    return min(
        max(duration_sec + _ACTIVE_TRANSFER_HEADROOM_SEC, _MIN_ACTIVE_TRANSFER_TIMEOUT_SEC),
        _MAX_ACTIVE_TRANSFER_TIMEOUT_SEC,
    )


def _ffmpeg_processing_timeout_sec(duration_sec: float) -> float:
    """Bound how long FFmpeg may hold a shared slot while it consumes input.

    The transfer budget is a network allowance; this is a local-CPU allowance,
    and they are capped differently on purpose. A background render holds one of
    only two encode slots on a Pi, on a thread that cannot be cancelled, so a
    long track must not be able to hold half the station's encode capacity for
    the whole of its own duration.
    """
    return min(_active_transfer_timeout_sec(duration_sec), _MAX_FFMPEG_PROCESSING_TIMEOUT_SEC)


def _monotonic() -> float:
    """Provide a clock seam for deterministic deadline tests."""
    return time.monotonic()


def _wait_for_pipe_writable(file_descriptor: int, timeout_sec: float) -> bool:
    """Wait for FFmpeg's nonblocking stdin pipe to accept bytes."""
    _, writable, _ = select.select([], [file_descriptor], [], timeout_sec)
    return bool(writable)


def _raise_if_active_transfer_expired(deadline: float) -> None:
    if _monotonic() >= deadline:
        raise _TransientError("network_timeout")


def _raise_if_write_expired(
    now: float,
    active_deadline: float,
    pipe_deadline: float,
    *,
    deadline_code: str,
) -> None:
    """Raise for the first expired deadline; transfer wins ties."""
    if now >= active_deadline and active_deadline <= pipe_deadline:
        raise _TransientError(deadline_code)
    if now >= pipe_deadline:
        raise _TransientError("ffmpeg_timeout")
    if now >= active_deadline:
        raise _TransientError(deadline_code)


def _write_all_nonblocking(
    process: subprocess.Popen[bytes],
    file_descriptor: int,
    chunk: bytes,
    active_deadline: float,
    *,
    deadline_code: str,
) -> None:
    """Write a chunk while enforcing transfer and pipe-progress deadlines."""
    view = memoryview(chunk)
    offset = 0
    pipe_deadline = _monotonic() + _PIPE_PROGRESS_TIMEOUT_SEC
    while offset < len(view):
        now = _monotonic()
        _raise_if_write_expired(now, active_deadline, pipe_deadline, deadline_code=deadline_code)
        if process.poll() is not None:
            raise _TransientError("ffmpeg_failed")
        wait_sec = max(
            0.0,
            min(_PIPE_POLL_INTERVAL_SEC, active_deadline - now, pipe_deadline - now),
        )
        try:
            if not _wait_for_pipe_writable(file_descriptor, wait_sec):
                continue
        except InterruptedError:
            continue
        now = _monotonic()
        _raise_if_write_expired(now, active_deadline, pipe_deadline, deadline_code=deadline_code)
        if process.poll() is not None:
            raise _TransientError("ffmpeg_failed")
        try:
            written = os.write(file_descriptor, view[offset:])
        except (BlockingIOError, InterruptedError):
            continue
        except BrokenPipeError as exc:
            raise _TransientError("ffmpeg_failed") from exc
        except OSError as exc:
            if exc.errno == errno.EPIPE:
                raise _TransientError("ffmpeg_failed") from exc
            raise
        if written <= 0:
            raise _TransientError("ffmpeg_failed")
        offset += written
        pipe_deadline = _monotonic() + _PIPE_PROGRESS_TIMEOUT_SEC


def _debug_cleanup_failure(phase: str, error: BaseException) -> None:
    logger.debug("Jamendo worker cleanup failed: phase=%s error=%s", phase, type(error).__name__)


def _classify_worker_error(error: BaseException) -> BaseException:
    """Map expected worker failures and preserve unexpected errors."""
    if isinstance(error, _ProviderError):
        return error
    if isinstance(error, subprocess.TimeoutExpired):
        failure = _TransientError("ffmpeg_timeout")
    elif isinstance(error, httpx.TimeoutException):
        failure = _TransientError("network_timeout")
    elif isinstance(error, httpx.HTTPError):
        failure = _TransientError("audio_fetch_failed")
    elif isinstance(error, OSError):
        failure = _TransientError("ffmpeg_failed")
    else:
        return error
    failure.__cause__ = error
    return failure


def _cleanup_operation(operation_dir: Path, jamendo_root: Path) -> None:
    """Remove only a recognized provider-owned operation directory."""
    try:
        relative = operation_dir.relative_to(jamendo_root)
    except ValueError:
        return
    if len(relative.parts) != 2 or any(_OPERATION_ID_RE.fullmatch(part) is None for part in relative.parts):
        return
    try:
        if operation_dir.is_symlink():
            operation_dir.unlink(missing_ok=True)
        else:
            shutil.rmtree(operation_dir, ignore_errors=True)
        boot_dir = operation_dir.parent
        if boot_dir.is_dir() and not boot_dir.is_symlink() and not any(boot_dir.iterdir()):
            boot_dir.rmdir()
    except OSError:
        logger.debug("Could not fully remove Jamendo operation %s", relative.as_posix())


def _prune_startup_operations(temp_dir: Path) -> int:
    """Delete recognized prior-boot operations without traversing unknown paths."""
    if temp_dir.is_symlink():
        raise _BlockedError("temp_root_invalid")
    temp_dir.mkdir(parents=True, exist_ok=True)
    root = temp_dir / "jamendo"
    if root.is_symlink():
        raise _BlockedError("temp_root_invalid")
    root.mkdir(exist_ok=True)
    purged = 0
    for boot_dir in list(root.iterdir()):
        if _OPERATION_ID_RE.fullmatch(boot_dir.name) is None or (not boot_dir.is_dir() and not boot_dir.is_symlink()):
            continue
        if boot_dir.is_symlink():
            boot_dir.unlink(missing_ok=True)
            purged += 1
            continue
        for operation_dir in list(boot_dir.iterdir()):
            if _OPERATION_ID_RE.fullmatch(operation_dir.name) is None:
                continue
            _cleanup_operation(operation_dir, root)
            purged += 1
        try:
            if boot_dir.exists() and not any(boot_dir.iterdir()):
                boot_dir.rmdir()
        except OSError:
            pass
    return purged


def _stream_and_normalize(
    candidate: _CandidateData,
    operation_dir: Path,
    on_normalizing: Callable[[], None],
) -> _PreparedArtifact:
    """Stream one provider response into FFmpeg and publish one normalized file."""
    partial = operation_dir / "normalized.part"
    final = operation_dir / "normalized.mp3"
    audio_buffer = BytesIO()
    client: httpx.Client | None = None
    response: httpx.Response | None = None
    process: subprocess.Popen[bytes] | None = None
    stdin_open = False
    byte_count = 0
    normalized_duration_sec: float | None = None
    primary_error: BaseException | None = None
    published = False

    def remember_cleanup_failure(phase: str, error: Exception, failure_code: str) -> None:
        nonlocal primary_error
        if primary_error is None:
            failure = _TransientError(failure_code)
            failure.__cause__ = error
            primary_error = failure
        else:
            _debug_cleanup_failure(phase, error)

    try:
        timeout = httpx.Timeout(
            _CONNECT_READ_TIMEOUT_SEC,
            connect=_CONNECT_READ_TIMEOUT_SEC,
            read=_CONNECT_READ_TIMEOUT_SEC,
            write=_CONNECT_READ_TIMEOUT_SEC,
            pool=_CONNECT_READ_TIMEOUT_SEC,
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            "pipe:0",
            "-vn",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-write_xing",
            "0",
            "-f",
            "mp3",
            str(partial),
        ]
        try:
            active_deadline = _monotonic() + _active_transfer_timeout_sec(candidate["duration_sec"])
            client = httpx.Client(timeout=timeout, follow_redirects=False)
            request = client.build_request("GET", candidate["audio_url"])
            response = client.send(request, stream=True)
            _raise_if_active_transfer_expired(active_deadline)
            if response.status_code != 200:
                raise _TransientError("audio_fetch_failed")
            try:
                content_length = int(response.headers.get("content-length", "0") or "0")
            except ValueError as exc:
                raise _CandidateRejectedError("audio_size_invalid") from exc
            if content_length < 0 or content_length > _MAX_AUDIO_BYTES:
                raise _CandidateRejectedError("audio_oversize")

            # Keep the size-capped response in memory. This avoids holding shared
            # FFmpeg admission during paced network waits and avoids staging raw
            # audio outside the normalized artifact.
            for chunk in response.iter_bytes(64 * 1024):
                _raise_if_active_transfer_expired(active_deadline)
                byte_count += len(chunk)
                if byte_count > _MAX_AUDIO_BYTES:
                    raise _CandidateRejectedError("audio_oversize")
                audio_buffer.write(chunk)
            _raise_if_active_transfer_expired(active_deadline)
            if byte_count == 0:
                raise _CandidateRejectedError("audio_empty")
            audio_buffer.seek(0)
        except BaseException as exc:
            primary_error = _classify_worker_error(exc)
        finally:
            if response is not None:
                try:
                    response.close()
                    response = None
                except Exception as exc:
                    remember_cleanup_failure("response_close", exc, "audio_fetch_failed")
            if client is not None:
                try:
                    client.close()
                    client = None
                except Exception as exc:
                    remember_cleanup_failure("client_close", exc, "audio_fetch_failed")
        if primary_error is not None:
            raise primary_error

        try:
            with ffmpeg_slot(background=True):
                try:
                    # No transfer-deadline check here on purpose. The bytes are
                    # already in hand (checked at the end of the download), and
                    # acquiring the encode slot is an unbounded LOCAL wait. Failing
                    # here raised "network_timeout" for a track that downloaded
                    # perfectly, blaming the provider for the station's own queue.
                    # The encode budget below is the correct bound for local work.
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        bufsize=0,
                    )
                    if process.stdin is None:
                        raise _TransientError("ffmpeg_failed")
                    stdin_open = True
                    file_descriptor = process.stdin.fileno()
                    os.set_blocking(file_descriptor, False)
                    ffmpeg_deadline = _monotonic() + _ffmpeg_processing_timeout_sec(candidate["duration_sec"])
                    while chunk := audio_buffer.read(64 * 1024):
                        _write_all_nonblocking(
                            process,
                            file_descriptor,
                            chunk,
                            ffmpeg_deadline,
                            deadline_code="ffmpeg_timeout",
                        )
                    process.stdin.close()
                    stdin_open = False
                    on_normalizing()
                    returncode = process.wait(timeout=_FFMPEG_FINALIZE_TIMEOUT_SEC)
                    process = None
                    if returncode != 0 or not partial.is_file() or partial.is_symlink() or partial.stat().st_size <= 0:
                        raise _TransientError("ffmpeg_failed")
                    try:
                        probe = subprocess.run(
                            [
                                "ffprobe",
                                "-v",
                                "error",
                                "-show_entries",
                                "format=duration",
                                "-of",
                                "default=noprint_wrappers=1:nokey=1",
                                str(partial),
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=10,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise _TransientError("ffprobe_failed") from exc
                    if probe.returncode != 0:
                        raise _TransientError("ffprobe_failed")
                    normalized_duration_sec = _parse_duration(probe.stdout.strip())
                except BaseException as exc:
                    primary_error = _classify_worker_error(exc)
                finally:
                    if stdin_open and process is not None and process.stdin is not None:
                        try:
                            process.stdin.close()
                            stdin_open = False
                        except Exception as exc:
                            remember_cleanup_failure("stdin_close", exc, "ffmpeg_failed")
                    if process is not None:
                        try:
                            running = process.poll() is None
                        except Exception as exc:
                            remember_cleanup_failure("process_poll", exc, "ffmpeg_failed")
                            running = True
                        if running:
                            try:
                                process.kill()
                            except Exception as exc:
                                remember_cleanup_failure("process_kill", exc, "ffmpeg_failed")
                            try:
                                process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SEC)
                            except Exception as exc:
                                remember_cleanup_failure("process_reap", exc, "ffmpeg_failed")
        except Exception as exc:
            if primary_error is None:
                failure = _TransientError("ffmpeg_failed")
                failure.__cause__ = exc
                primary_error = failure
            else:
                _debug_cleanup_failure("slot_release", exc)
        if primary_error is not None:
            raise primary_error
        if normalized_duration_sec is None:
            raise RuntimeError("Jamendo worker completed without a normalized duration")
        artifact_hash = _hash_file(partial)
        prepared = _PreparedArtifact(final, artifact_hash, normalized_duration_sec, byte_count)
        os.replace(partial, final)
        published = True
        return prepared
    except BaseException as exc:
        classified = _classify_worker_error(exc)
        if classified is exc:
            raise
        raise classified from exc
    finally:
        if stdin_open and process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except Exception as exc:
                _debug_cleanup_failure("stdin_close", exc)
        if process is not None:
            try:
                running = process.poll() is None
            except Exception as exc:
                _debug_cleanup_failure("process_poll", exc)
                running = True
            if running:
                try:
                    process.kill()
                except Exception as exc:
                    _debug_cleanup_failure("process_kill", exc)
                try:
                    process.wait(timeout=_PROCESS_CLEANUP_TIMEOUT_SEC)
                except Exception as exc:
                    _debug_cleanup_failure("process_reap", exc)
        if not published:
            try:
                partial.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
            except OSError:
                pass
            _cleanup_operation(operation_dir, operation_dir.parent.parent)


class JamendoStreamProvider:
    """Own the complete lifecycle of one transient Jamendo audio lease."""

    def __init__(
        self,
        temp_dir: Path,
        source_revision_getter: Callable[[], int],
        *,
        http_client: httpx.AsyncClient | None = None,
        stream_worker: _StreamWorker | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays_sec: tuple[float, ...] = _RETRY_DELAYS_SEC,
    ) -> None:
        self._temp_dir = Path(temp_dir)
        self._jamendo_root = self._temp_dir / "jamendo"
        self._boot_id = uuid.uuid4().hex
        self._source_revision_getter = source_revision_getter
        self._clock = clock
        self._sleep = sleep
        self._retry_delays = retry_delays_sec or (_RETRY_DELAYS_SEC[-1],)
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(_CONNECT_READ_TIMEOUT_SEC),
            follow_redirects=False,
        )
        self._owns_http_client = http_client is None
        self._stream_worker = stream_worker or _stream_and_normalize
        self._lock = RLock()
        self._state = JamendoProviderState.DISABLED
        self._enabled = False
        self._client_id = ""
        self._acknowledged = False
        self._tags = "pop"
        self._country = ""
        self._order = "popularity_week"
        self._limit = 20
        self._epoch = 0
        self._task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[_PreparedArtifact] | None = None
        self._worker_operation_dir: Path | None = None
        self._abandoned_operations: set[str] = set()
        self._pending_delay: float | None = None
        self._retry_sleeping = False
        self._retry_index = 0
        self._lease: JamendoLease | None = None
        self._lease_expiry_handle: asyncio.TimerHandle | None = None
        self._last_failure_code: str | None = None
        self._last_success_at: float | None = None
        self._rejected_count = 0
        # Per-attempt rejection breakdown, replaced wholesale at the end of each
        # discovery pass. The lifetime ``_rejected_count`` above only ever grows,
        # so it answers "how long has this been broken" and nothing else; the
        # operator card needs "what is going wrong right now", which is this.
        # Never rendered as a bare number — it selects which sentence to show.
        self._attempt_rejections: dict[str, int] = {}
        # Resolved once per attempt at publish time. The error that ENDED the
        # attempt outranks the most common rejection, so a pass killed by a
        # timeout does not report an earlier candidate mismatch as the reason.
        self._attempt_dominant: str | None = None
        self._configuration_apply_blocked = False
        self._started = False
        self._stopped = False

    async def start(
        self,
        *,
        enabled: bool = False,
        client_id: str = "",
        noncommercial_acknowledged: bool = False,
        tags: str = "pop",
        country: str = "",
        order: str = "popularity_week",
        limit: int = 20,
    ) -> None:
        """Prune prior-boot operations and apply an inert-by-default config."""
        with self._lock:
            if self._started and not self._stopped:
                return
            self._started = True
            self._stopped = False
        try:
            purged = await asyncio.to_thread(_prune_startup_operations, self._temp_dir)
            if purged:
                logger.info("Pruned %d prior Jamendo operation(s)", purged)
        except _BlockedError as exc:
            with self._lock:
                self._state = JamendoProviderState.BLOCKED
                self._last_failure_code = exc.code
            logger.warning(
                "Jamendo provider blocked failure_code=%s provider_code=%s",
                exc.code,
                exc.provider_code if exc.provider_code is not None else "none",
            )
            return
        await self.apply_config(
            enabled=enabled,
            client_id=client_id,
            noncommercial_acknowledged=noncommercial_acknowledged,
            tags=tags,
            country=country,
            order=order,
            limit=limit,
        )

    async def apply_config(
        self,
        *,
        enabled: bool,
        client_id: str,
        noncommercial_acknowledged: bool,
        tags: str = "pop",
        country: str = "",
        order: str = "popularity_week",
        limit: int = 20,
    ) -> None:
        """Apply already-persisted configuration; newer choices invalidate old work."""
        normalized_client_id = client_id.strip()
        normalized_tags = tags.strip()[:100] or "pop"
        normalized_country = country.strip().upper()[:3]
        normalized_order = order.strip()[:50]
        normalized_limit = max(1, min(int(limit), 50))
        with self._lock:
            same = (
                self._enabled == bool(enabled)
                and self._client_id == normalized_client_id
                and self._acknowledged == bool(noncommercial_acknowledged)
                and self._tags == normalized_tags
                and self._country == normalized_country
                and self._order == normalized_order
                and self._limit == normalized_limit
            )
            if same and self._state not in (JamendoProviderState.BLOCKED, JamendoProviderState.DEGRADED):
                self._ensure_scheduled_unlocked()
                return
            self._epoch += 1
            self._enabled = bool(enabled)
            self._client_id = normalized_client_id
            self._acknowledged = bool(noncommercial_acknowledged)
            self._tags = normalized_tags
            self._country = normalized_country
            self._order = normalized_order
            self._limit = normalized_limit
            self._configuration_apply_blocked = False
            self._retry_index = 0
            self._pending_delay = None
            self._last_failure_code = None
            self._clear_attempt_reasons_unlocked()
            if self._lease is not None and self._state != JamendoProviderState.PLAYING:
                self._release_current_unlocked("config_changed")
            playing = self._lease is not None and self._state == JamendoProviderState.PLAYING
            if not self._enabled and not playing:
                self._state = JamendoProviderState.DISABLED
            elif not playing and (
                not self._client_id or JAMENDO_CLIENT_ID_RE.match(self._client_id) is None or not self._acknowledged
            ):
                self._state = JamendoProviderState.NEEDS_CONFIG
            elif not playing and self._lease is None:
                self._state = JamendoProviderState.IDLE
                self._pending_delay = 0.0
            task = self._task
            worker_running = self._worker_task is not None and not self._worker_task.done()
            if task is not None and not task.done() and not worker_running:
                task.cancel()
            self._ensure_scheduled_unlocked()

    def mark_apply_failure(self) -> None:
        """Fail closed when durable intent could not be applied to the live provider.

        The API has already committed the operator's choice at this point, so it
        must not roll that write back.  Invalidating the current epoch prevents
        an older provider task or lease from publishing under stale authority;
        a later API/startup re-apply clears this marker and converges on the
        saved configuration.
        """
        with self._lock:
            self._configuration_apply_blocked = True
            self._epoch += 1
            self._pending_delay = None
            self._last_failure_code = "api_failed"
            playing = self._lease is not None and self._state == JamendoProviderState.PLAYING
            if self._lease is not None and not playing:
                self._release_current_unlocked("config_apply_failed")
            if not playing:
                self._state = JamendoProviderState.BLOCKED
            task = self._task
            worker_running = self._worker_task is not None and not self._worker_task.done()
            if task is not None and not task.done() and not worker_running:
                task.cancel()

    async def invalidate_source(self) -> None:
        """Discard work authorized for an older base source revision."""
        with self._lock:
            self._epoch += 1
            self._pending_delay = None
            if self._lease is not None and self._state != JamendoProviderState.PLAYING:
                self._release_current_unlocked("source_changed")
            if self._enabled and self._configuration_valid_unlocked() and self._lease is None:
                self._state = JamendoProviderState.IDLE
                self._pending_delay = 0.0
            task = self._task
            worker_running = self._worker_task is not None and not self._worker_task.done()
            if task is not None and not task.done() and not worker_running:
                task.cancel()
            self._ensure_scheduled_unlocked()

    def retry(self) -> bool:
        """Coalesce an operator retry; return whether a new immediate try was requested."""
        with self._lock:
            if self._stopped or not self._enabled or not self._configuration_valid_unlocked():
                return False
            if self._worker_task is not None and not self._worker_task.done():
                return False
            if self._task is not None and not self._task.done():
                if not self._retry_sleeping:
                    return False
                self._retry_index = 0
                self._last_failure_code = None
                self._clear_attempt_reasons_unlocked()
                self._state = JamendoProviderState.IDLE
                self._pending_delay = 0.0
                self._retry_sleeping = False
                self._task.cancel()
                return True
            self._retry_index = 0
            self._last_failure_code = None
            self._clear_attempt_reasons_unlocked()
            self._state = JamendoProviderState.IDLE
            self._pending_delay = 0.0
            self._ensure_scheduled_unlocked()
            return True

    def take_ready_segment(self) -> Segment | None:
        """Synchronously take the prepared lease or let the caller use base music."""
        with self._lock:
            lease = self._lease
            if self._state != JamendoProviderState.READY or lease is None or not self._lease_valid_unlocked(lease):
                if lease is not None and self._state == JamendoProviderState.READY:
                    self._release_current_unlocked("lease_invalid")
                return None
            self._state = JamendoProviderState.QUEUED
            if self._lease_expiry_handle is not None:
                self._lease_expiry_handle.cancel()
                self._lease_expiry_handle = None
            attribution = lease.attribution.to_dict()
            lease_id = lease.lease_id

            def release_lease() -> None:
                self.release(lease_id)

            def mark_lease_playing() -> bool:
                return self.mark_playing(lease_id)

            return Segment(
                type=SegmentType.MUSIC,
                path=lease.artifact_path,
                duration_sec=lease.duration_sec,
                metadata={
                    "title": f"{lease.artist} – {lease.title}",
                    "artist": lease.artist,
                    "title_only": lease.title,
                    "duration_ms": round(lease.duration_sec * 1000),
                    "provider_track_id": lease.provider_track_id,
                    "source_kind": "jamendo",
                    "audio_source": "jamendo_transient",
                    "music_attribution": attribution,
                },
                ephemeral=True,
                playback_start_callback=mark_lease_playing,
                release_callback=release_lease,
            )

    def mark_playing(self, lease_id: str) -> bool:
        """Revalidate and admit a queued lease immediately before playback."""
        with self._lock:
            if self._lease is None or self._lease.lease_id != lease_id or self._state != JamendoProviderState.QUEUED:
                return False
            if not self._lease_valid_unlocked(self._lease):
                self._release_current_unlocked("lease_invalid")
                return False
            self._state = JamendoProviderState.PLAYING
            return True

    def release(self, lease: str | JamendoLease, *, reason: str = "consumed") -> bool:
        """Idempotently consume a lease and remove its only audio artifact."""
        lease_id = lease if isinstance(lease, str) else lease.lease_id
        with self._lock:
            if self._lease is None or self._lease.lease_id != lease_id:
                return False
            self._release_current_unlocked(reason)
            return True

    def status(self) -> dict[str, object]:
        """Return the redacted, non-blocking administrative snapshot."""
        with self._lock:
            success_age = None
            if self._last_success_at is not None:
                success_age = max(0.0, self._clock() - self._last_success_at)
            return {
                "enabled": self._enabled,
                "state": self._state.value,
                "client_id_configured": bool(self._client_id),
                "noncommercial_acknowledged": self._acknowledged,
                "terms_scope": "noncommercial_api_use",
                # Deliberately always "pending": this reports whether JAMENDO has
                # cleared the operator's station model, which it never does. It is
                # not the operator's acknowledgement — that is
                # ``noncommercial_acknowledged`` above. See
                # docs/troubleshooting.md "Jamendo stays off or temporarily
                # unavailable" before changing this.
                "provider_confirmation": "pending",
                "ready": self._state == JamendoProviderState.READY,
                "in_flight": self._state
                in {
                    JamendoProviderState.DISCOVERING,
                    JamendoProviderState.FETCHING,
                    JamendoProviderState.NORMALIZING,
                }
                or (self._worker_task is not None and not self._worker_task.done()),
                "last_success_age_sec": round(success_age, 3) if success_age is not None else None,
                "last_failure_code": self._last_failure_code,
                "rejected_count": self._rejected_count,
                "rejected_this_attempt": sum(self._attempt_rejections.values()),
                "dominant_failure_code_this_attempt": self._attempt_dominant,
                "attempt_rejections": dict(self._attempt_rejections),
            }

    async def stop(self) -> None:
        """Stop scheduling and arrange cleanup for any late thread-backed worker."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._epoch += 1
            self._pending_delay = None
            task = self._task
            worker = self._worker_task
            operation_dir = self._worker_operation_dir
            if worker is not None and not worker.done() and operation_dir is not None:
                self._abandoned_operations.add(operation_dir.name)
            if task is not None and not task.done():
                task.cancel()
            if self._lease is not None:
                self._release_current_unlocked("shutdown")
            self._state = JamendoProviderState.DISABLED
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if self._owns_http_client:
            await self._http_client.aclose()

    def _read_source_revision(self) -> int:
        value = self._source_revision_getter()
        if isinstance(value, bool) or not isinstance(value, int):
            raise _BlockedError("source_revision_invalid")
        return value

    def _configuration_valid_unlocked(self) -> bool:
        return bool(
            not self._configuration_apply_blocked
            and self._enabled
            and self._acknowledged
            and self._client_id
            and JAMENDO_CLIENT_ID_RE.match(self._client_id)
        )

    def _snapshot_current_unlocked(self) -> tuple[int, str, int]:
        return self._epoch, _client_id_fingerprint(self._client_id), self._read_source_revision()

    def _still_current_unlocked(self, epoch: int, fingerprint: str, source_revision: int) -> bool:
        try:
            actual_source_revision = self._read_source_revision()
        except _BlockedError:
            return False
        return bool(
            not self._stopped
            and self._configuration_valid_unlocked()
            and self._epoch == epoch
            and _client_id_fingerprint(self._client_id) == fingerprint
            and actual_source_revision == source_revision
        )

    def _ensure_scheduled_unlocked(self) -> None:
        if (
            not self._started
            or self._stopped
            or self._pending_delay is None
            or not self._configuration_valid_unlocked()
            or self._lease is not None
            or (self._task is not None and not self._task.done())
            or (self._worker_task is not None and not self._worker_task.done())
        ):
            return
        delay = self._pending_delay
        self._pending_delay = None
        task = asyncio.get_running_loop().create_task(self._run_after(delay), name="jamendo-transient-provider")
        self._task = task
        task.add_done_callback(self._on_provider_task_done)

    def _on_provider_task_done(self, task: asyncio.Task[None]) -> None:
        retry_delay: float | None = None
        with self._lock:
            current_task = self._task is task
            if current_task:
                self._task = None
            if task.cancelled():
                pass
            else:
                try:
                    error = task.exception()
                    if error is not None:
                        if (
                            current_task
                            and self._started
                            and not self._stopped
                            and self._configuration_valid_unlocked()
                            and self._lease is None
                        ):
                            retry_delay = self._record_transient_failure_unlocked("internal_error")
                        else:
                            logger.debug("Jamendo provider task ended unexpectedly: %s", type(error).__name__)
                except asyncio.CancelledError:
                    logger.debug("Jamendo provider task ended unexpectedly", exc_info=True)
            self._ensure_scheduled_unlocked()
        if retry_delay is not None:
            logger.warning(
                "Jamendo provider attempt failed failure_code=internal_error provider_code=none retry_in_seconds=%d",
                int(retry_delay),
            )

    async def _run_after(self, delay: float) -> None:
        try:
            with self._lock:
                epoch = self._epoch
            if delay > 0:
                with self._lock:
                    self._retry_sleeping = True
                await self._sleep(delay)
                with self._lock:
                    self._retry_sleeping = False
                    if epoch != self._epoch:
                        raise _StaleOperationError
            await self._prepare_once()
        except asyncio.CancelledError:
            raise
        except _StaleOperationError:
            return
        finally:
            with self._lock:
                self._retry_sleeping = False

    async def _prepare_once(self) -> None:
        operation_dir: Path | None = None
        try:
            with self._lock:
                if not self._configuration_valid_unlocked() or self._lease is not None:
                    return
                epoch, fingerprint, source_revision = self._snapshot_current_unlocked()
                self._state = JamendoProviderState.DISCOVERING
            logger.info("Jamendo provider preparation started")
            # The shared wall-clock budget lives inside _discover_candidate so
            # a timeout is already named when the per-attempt breakdown
            # publishes; it surfaces here as _TransientError("network_timeout").
            candidate = await self._discover_candidate(epoch, fingerprint, source_revision)
            with self._lock:
                if not self._still_current_unlocked(epoch, fingerprint, source_revision):
                    raise _StaleOperationError
                if self._temp_dir.is_symlink() or self._jamendo_root.is_symlink():
                    raise _BlockedError("temp_root_invalid")
                operation_id = uuid.uuid4().hex
                operation_dir = self._jamendo_root / self._boot_id / operation_id
                operation_dir.mkdir(parents=True, exist_ok=False)
                if operation_dir.is_symlink():
                    raise _BlockedError("temp_root_invalid")
                self._state = JamendoProviderState.FETCHING
                owned_operation_dir = operation_dir
            loop = asyncio.get_running_loop()

            def on_normalizing() -> None:
                try:
                    loop.call_soon_threadsafe(
                        self._mark_normalizing,
                        operation_id,
                        epoch,
                        fingerprint,
                        source_revision,
                    )
                except RuntimeError:
                    pass

            worker = loop.create_task(
                asyncio.to_thread(self._stream_worker, candidate, operation_dir, on_normalizing),
                name=f"jamendo-worker-{operation_id}",
            )
            with self._lock:
                self._worker_task = worker
                self._worker_operation_dir = operation_dir
            worker.add_done_callback(lambda done: self._on_worker_done(done, owned_operation_dir))
            artifact = await asyncio.shield(worker)
            with self._lock:
                if not self._still_current_unlocked(epoch, fingerprint, source_revision):
                    raise _StaleOperationError
                expected = operation_dir / "normalized.mp3"
                if (
                    artifact.path != expected
                    or not expected.is_file()
                    or expected.is_symlink()
                    or artifact.sha256 != _hash_file(expected)
                    or not _MIN_DURATION_SEC <= artifact.duration_sec <= _MAX_DURATION_SEC
                ):
                    raise _CandidateRejectedError("lease_invalid")
                now = self._clock()
                self._lease = JamendoLease(
                    lease_id=uuid.uuid4().hex,
                    boot_id=self._boot_id,
                    operation_id=operation_id,
                    provider_epoch=epoch,
                    client_id_fingerprint=fingerprint,
                    provider_track_id=candidate["provider_track_id"],
                    title=candidate["title"],
                    artist=candidate["artist"],
                    source_url=candidate["source_url"],
                    license_id=candidate["license_id"],
                    license_url=candidate["license_url"],
                    credit=candidate["credit"],
                    fetched_at=now,
                    source_revision=source_revision,
                    artifact_path=artifact.path,
                    artifact_sha256=artifact.sha256,
                    duration_sec=artifact.duration_sec,
                    transform_description=_TRANSFORM_DESCRIPTION,
                )
                self._lease_expiry_handle = loop.call_later(
                    _MAX_LEASE_AGE_SEC,
                    self._expire_ready_lease,
                    self._lease.lease_id,
                )
                self._last_success_at = now
                self._last_failure_code = None
                self._retry_index = 0
                self._state = JamendoProviderState.READY
                operation_dir = None
            logger.info("Jamendo provider ready")
        except asyncio.CancelledError:
            with self._lock:
                worker_running = (
                    operation_dir is not None
                    and self._worker_operation_dir == operation_dir
                    and self._worker_task is not None
                    and not self._worker_task.done()
                )
            if worker_running:
                # ``asyncio.to_thread`` cannot be cancelled. Its completion
                # callback owns cleanup so the worker keeps a private directory
                # until it has actually stopped touching the file.
                operation_dir = None
            raise
        except _StaleOperationError:
            with self._lock:
                if self._enabled and self._configuration_valid_unlocked() and self._lease is None:
                    self._state = JamendoProviderState.IDLE
                    self._pending_delay = 0.0
        except _BlockedError as exc:
            with self._lock:
                self._last_failure_code = exc.code
                self._state = JamendoProviderState.BLOCKED
            logger.warning(
                "Jamendo provider blocked failure_code=%s provider_code=%s",
                exc.code,
                exc.provider_code if exc.provider_code is not None else "none",
            )
        except _CandidateRejectedError as exc:
            with self._lock:
                self._rejected_count += 1
                retry_delay = self._record_transient_failure_unlocked(exc.code)
            logger.warning(
                "Jamendo provider attempt failed failure_code=%s provider_code=none retry_in_seconds=%d",
                exc.code,
                int(retry_delay),
            )
        except (_TransientError, httpx.HTTPError, OSError) as exc:
            code = exc.code if isinstance(exc, _TransientError) else "api_failed"
            provider_code = exc.provider_code if isinstance(exc, _TransientError) else None
            with self._lock:
                retry_delay = self._record_transient_failure_unlocked(code)
            logger.warning(
                "Jamendo provider attempt failed failure_code=%s provider_code=%s retry_in_seconds=%d",
                code,
                provider_code if provider_code is not None else "none",
                int(retry_delay),
            )
        finally:
            if operation_dir is not None:
                _cleanup_operation(operation_dir, self._jamendo_root)

    def _clear_attempt_reasons_unlocked(self) -> None:
        """Drop the per-attempt reason alongside ``_last_failure_code``.

        The card prefers the dominant code over ``last_failure_code``, so a site
        that clears only the latter leaves the previous attempt's sentence on
        screen. That is what an operator sees right after changing settings or
        pressing Check again, which is exactly when the row must stop explaining
        the run they just replaced.
        """
        self._attempt_rejections = {}
        self._attempt_dominant = None

    def _publish_attempt_rejections(
        self,
        rejections: Mapping[str, int],
        epoch: int,
        fingerprint: str,
        source_revision: int,
        *,
        succeeded: bool,
        terminal_code: str | None = None,
    ) -> None:
        """Replace the per-attempt breakdown, and log it once per attempt.

        A superseded attempt publishes nothing: its counts describe a source or
        configuration the operator has already moved on from, and showing them
        would explain the wrong thing on the card.

        A *successful* attempt publishes an empty breakdown even when it rejected
        candidates on the way. Discovery routinely skips a few rows before one
        works; leaving those counts on the card meant a track that prepared
        correctly sat in the queue under "Jamendo sent back a different song than
        expected", and then aired under it.

        ``terminal_code`` names the error that ended the attempt. When it is not
        itself a candidate rejection — a network timeout on the third exact-ID
        lookup, say — it wins the dominant slot, because otherwise an attempt
        killed by a timeout would report "Jamendo sent back a different song than
        expected" from two earlier rejections while the retry schedule is acting
        on the timeout.

        The log counts only real candidate rejections. ``empty_results`` means
        nothing came back to examine, so counting it would report "rejecting 1
        candidate(s)" for a response that contained none.
        """
        candidate_rejections = {code: count for code, count in rejections.items() if code not in _NON_CANDIDATE_REASONS}
        dominant = _dominant_code(rejections)
        if terminal_code and terminal_code not in rejections:
            dominant = terminal_code
        with self._lock:
            if not self._still_current_unlocked(epoch, fingerprint, source_revision):
                return
            if succeeded:
                self._attempt_rejections = {}
                self._attempt_dominant = None
            else:
                self._attempt_rejections = dict(rejections)
                self._attempt_dominant = dominant
        # or terminal_code: an attempt whose very first call fails rejects no
        # candidate, so gating on the breakdown alone left the diagnosis channel
        # silent exactly when the provider is fully down.
        if rejections or terminal_code:
            event = "Jamendo discovery selected a candidate" if succeeded else "Jamendo attempt failed"
            logger.info(
                "%s after rejecting %d candidate(s) breakdown=%s dominant=%s",
                event,
                sum(candidate_rejections.values()),
                ",".join(f"{code}:{count}" for code, count in sorted(rejections.items())),
                None if succeeded else dominant,
            )

    async def _discover_candidate(self, epoch: int, fingerprint: str, source_revision: int) -> _CandidateData:
        # Built locally rather than reset on the instance: a concurrent attempt
        # cannot clobber a local Counter, so there is no window where one pass
        # zeroes another's in-flight tally. Published once, atomically, at exit.
        attempt_rejections: Counter[str] = Counter()

        def record_rejection(code: str) -> None:
            with self._lock:
                self._rejected_count += 1
            attempt_rejections[code] += 1

        def note_attempt_reason(code: str) -> None:
            """Name a reason without counting a rejected candidate.

            ``empty_results`` means nothing came back to examine, so it belongs
            in the breakdown the card reads but not in the lifetime tally of
            candidates thrown away — those are different facts.
            """
            attempt_rejections[code] += 1

        def publish(succeeded: bool, terminal_code: str | None = None) -> None:
            self._publish_attempt_rejections(
                attempt_rejections,
                epoch,
                fingerprint,
                source_revision,
                succeeded=succeeded,
                terminal_code=terminal_code,
            )

        try:
            # Discovery and every serial exact-ID revalidation share this one
            # wall-clock budget; no candidate can reset the deadline. The budget
            # lives here rather than in the caller so that the code which ended
            # the attempt is already known when the breakdown publishes. Wrapped
            # from outside, a timeout arrived as CancelledError with no terminal
            # code and was only named afterwards, so the card kept reporting an
            # earlier candidate rejection while the retry acted on the timeout.
            async with asyncio.timeout(_DISCOVERY_DEADLINE_SEC):
                candidate = await self._discover_candidate_inner(
                    epoch, fingerprint, source_revision, record_rejection, note_attempt_reason
                )
        except TimeoutError as exc:
            # Must precede OSError: builtin TimeoutError subclasses it.
            publish(False, "network_timeout")
            raise _TransientError("network_timeout") from exc
        except _ProviderError as exc:
            publish(False, exc.code)
            raise
        except (httpx.HTTPError, OSError):
            # _prepare_once reports these as api_failed; say the same thing here
            # so the card and the retry schedule cannot disagree.
            publish(False, "api_failed")
            raise
        except BaseException:
            publish(False)
            raise
        publish(True)
        return candidate

    async def _discover_candidate_inner(
        self,
        epoch: int,
        fingerprint: str,
        source_revision: int,
        record_rejection: Callable[[str], None],
        note_attempt_reason: Callable[[str], None],
    ) -> _CandidateData:
        params = {
            "client_id": self._client_id,
            "format": "json",
            "limit": str(self._limit),
            "tags": self._tags,
            "audioformat": "mp32",
            "include": _API_INCLUDE,
            "ccnc": "false",
            "ccsa": "false",
            "ccnd": "false",
        }
        if self._country:
            params["country"] = self._country
        if self._order:
            params["order"] = self._order
        results = await _bounded_api_results(self._http_client, params)
        with self._lock:
            if not self._still_current_unlocked(epoch, fingerprint, source_revision):
                raise _StaleOperationError
        candidates: list[tuple[str, _CandidateData]] = []
        seen_ids: set[str] = set()
        last_rejection: _CandidateRejectedError | None = None
        for item in results:
            if not isinstance(item, Mapping):
                record_rejection("metadata_invalid")
                last_rejection = _CandidateRejectedError("metadata_invalid")
                continue
            track_id = str(item.get("id", "")).strip()
            if _TRACK_ID_RE.fullmatch(track_id) is None or track_id in seen_ids:
                record_rejection("identity_mismatch")
                last_rejection = _CandidateRejectedError("identity_mismatch")
                continue
            seen_ids.add(track_id)
            try:
                candidates.append((track_id, _candidate_from_exact_result(item, track_id)))
            except _CandidateRejectedError as exc:
                record_rejection(exc.code)
                last_rejection = exc
        if not candidates:
            if last_rejection is not None:
                raise _TransientError(last_rejection.code)
            # Noted, not just raised: an attempt that found nothing at all is
            # the single most useful thing the breakdown can say, and without
            # this the code could never appear in it or be the dominant reason.
            note_attempt_reason("empty_results")
            raise _TransientError("empty_results")
        for track_id, discovered_candidate in candidates:
            # Exact-ID lookup omits discovery license filters. Combining ``id``
            # with ``ccnc``/``ccsa``/``ccnd`` can return no rows for tracks that
            # discovery already found. The exact payload still goes through
            # ``_canonical_license``.
            exact_params = {
                "client_id": self._client_id,
                "format": "json",
                "limit": "1",
                "id": track_id,
                "audioformat": "mp32",
                "include": _API_INCLUDE,
            }
            exact_results = await _bounded_api_results(self._http_client, exact_params)
            with self._lock:
                if not self._still_current_unlocked(epoch, fingerprint, source_revision):
                    raise _StaleOperationError
            if len(exact_results) != 1:
                record_rejection("identity_mismatch")
                last_rejection = _CandidateRejectedError("identity_mismatch")
                continue
            try:
                exact_candidate = _candidate_from_exact_result(exact_results[0], track_id)
                if _stable_admission_identity(exact_candidate) != _stable_admission_identity(discovered_candidate):
                    raise _CandidateRejectedError("metadata_changed")
                # Use the exact lookup's stream and share URLs, which are the
                # latest private audio link and attribution page.
                return exact_candidate
            except _CandidateRejectedError as exc:
                record_rejection(exc.code)
                last_rejection = exc
        if last_rejection is not None:
            raise _TransientError(last_rejection.code)
        note_attempt_reason("empty_results")
        raise _TransientError("empty_results")

    def _mark_normalizing(self, operation_id: str, epoch: int, fingerprint: str, source_revision: int) -> None:
        with self._lock:
            if (
                self._worker_operation_dir is not None
                and self._worker_operation_dir.name == operation_id
                and self._still_current_unlocked(epoch, fingerprint, source_revision)
                and self._state == JamendoProviderState.FETCHING
            ):
                self._state = JamendoProviderState.NORMALIZING

    def _on_worker_done(self, task: asyncio.Task[_PreparedArtifact], operation_dir: Path) -> None:
        with self._lock:
            abandoned = operation_dir.name in self._abandoned_operations
            self._abandoned_operations.discard(operation_dir.name)
            if self._worker_task is task:
                self._worker_task = None
                self._worker_operation_dir = None
            if abandoned:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                _cleanup_operation(operation_dir, self._jamendo_root)
            self._ensure_scheduled_unlocked()

    def _record_transient_failure_unlocked(self, code: str) -> float:
        self._last_failure_code = code
        self._state = JamendoProviderState.DEGRADED
        retry_index = min(self._retry_index, len(self._retry_delays) - 1)
        self._pending_delay = self._retry_delays[retry_index]
        self._retry_index += 1
        return self._pending_delay

    def _expire_ready_lease(self, lease_id: str) -> None:
        with self._lock:
            if self._lease is None or self._lease.lease_id != lease_id:
                return
            self._lease_expiry_handle = None
            if self._state == JamendoProviderState.READY:
                self._release_current_unlocked("expired")

    def _lease_valid_unlocked(self, lease: JamendoLease) -> bool:
        expected = self._jamendo_root / lease.boot_id / lease.operation_id / "normalized.mp3"
        age = self._clock() - lease.fetched_at
        try:
            source_revision = self._read_source_revision()
        except _BlockedError:
            return False
        if (
            lease.boot_id != self._boot_id
            or lease.provider_epoch != self._epoch
            or lease.source_revision != source_revision
            or lease.client_id_fingerprint != _client_id_fingerprint(self._client_id)
            or lease.artifact_path != expected
            or age < 0
            or age > _MAX_LEASE_AGE_SEC
            or not expected.is_file()
            or expected.is_symlink()
        ):
            return False
        try:
            return _hash_file(expected) == lease.artifact_sha256
        except OSError:
            return False

    def _release_current_unlocked(self, reason: str) -> None:
        lease = self._lease
        if lease is None:
            return
        self._lease = None
        if self._lease_expiry_handle is not None:
            self._lease_expiry_handle.cancel()
            self._lease_expiry_handle = None
        _cleanup_operation(lease.artifact_path.parent, self._jamendo_root)
        self._state = JamendoProviderState.CONSUMED
        release_code = reason if reason in _LEASE_RELEASE_LOG_CODES else "released"
        logger.info("jamendo_lease_released code=%s", release_code)
        if not self._stopped and self._configuration_valid_unlocked():
            self._pending_delay = 0.0
            self._ensure_scheduled_unlocked()
        elif not self._enabled:
            self._state = JamendoProviderState.DISABLED
        elif self._configuration_apply_blocked:
            self._state = JamendoProviderState.BLOCKED
        elif not self._configuration_valid_unlocked():
            self._state = JamendoProviderState.NEEDS_CONFIG


__all__ = ["JamendoLease", "JamendoProviderState", "JamendoStreamProvider"]
