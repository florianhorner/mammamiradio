#!/usr/bin/env python3
"""Runtime smoke gate for HA Green-class playback performance.

This script expects a mammamiradio process to already be running and checks the
listener-visible promises that have regressed on constrained hardware: health
does not enter silence failure, readiness does not report extended queue
starvation, and the stream returns audio bytes inside a bounded window.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("MAMMAMIRADIO_PERF_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
STARTUP_TIMEOUT_S = float(os.environ.get("MAMMAMIRADIO_PERF_STARTUP_TIMEOUT_S", "60"))
FIRST_BYTE_TIMEOUT_S = float(os.environ.get("MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S", "8"))
MAX_QUEUE_EMPTY_S = float(os.environ.get("MAMMAMIRADIO_PERF_MAX_QUEUE_EMPTY_S", "30"))
POLL_INTERVAL_S = float(os.environ.get("MAMMAMIRADIO_PERF_POLL_INTERVAL_S", "0.5"))


def _fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def _pass(message: str) -> None:
    print(f"[PASS] {message}")


def _fetch_json(path: str, *, timeout: float = 3.0) -> tuple[int, dict]:
    request = Request(f"{BASE_URL}{path}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body or "{}")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            payload = {"raw": body}
        return exc.code, payload
    except (TimeoutError, URLError) as exc:
        _fail(f"{path} unavailable: {exc}")


def _assert_not_silence_failure(path: str, status: int, payload: dict, *, allow_starting: bool = False) -> None:
    if payload.get("status") == "failing":
        _fail(f"{path} reported status=failing: {payload}")
    if payload.get("silence_with_listeners") is True:
        _fail(f"{path} reported silence_with_listeners=true: {payload}")
    queue_empty = float(payload.get("queue_empty_elapsed_s") or 0)
    if queue_empty > MAX_QUEUE_EMPTY_S:
        _fail(f"{path} queue_empty_elapsed_s={queue_empty:.1f} exceeds {MAX_QUEUE_EMPTY_S:.1f}")
    if allow_starting and status == 503 and payload.get("status") == "starting":
        return
    if status >= 500 and payload.get("session_stopped") is not True:
        _fail(f"{path} returned HTTP {status}: {payload}")


def _wait_for_health() -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    last_payload: dict | None = None
    while time.monotonic() < deadline:
        status, payload = _fetch_json("/healthz")
        last_payload = payload
        _assert_not_silence_failure("/healthz", status, payload)
        if status == 200:
            _pass(f"/healthz ok within {STARTUP_TIMEOUT_S:.0f}s")
            return
        time.sleep(POLL_INTERVAL_S)
    _fail(f"/healthz did not become ok within {STARTUP_TIMEOUT_S:.0f}s; last={last_payload}")


def _check_readiness() -> None:
    status, payload = _fetch_json("/readyz")
    _assert_not_silence_failure("/readyz", status, payload, allow_starting=True)
    if status == 200:
        _pass("/readyz ready")
        return
    if payload.get("status") == "starting":
        _pass("/readyz still starting without silence failure")
        return
    _fail(f"/readyz returned unexpected state HTTP {status}: {payload}")


def _check_public_status() -> None:
    status, payload = _fetch_json("/public-status")
    if status != 200:
        _fail(f"/public-status returned HTTP {status}: {payload}")
    runtime_health = payload.get("runtime_health") or {}
    queue_empty = float(runtime_health.get("queue_empty_elapsed_s") or 0)
    if queue_empty > MAX_QUEUE_EMPTY_S:
        _fail(f"/public-status runtime queue_empty_elapsed_s={queue_empty:.1f} exceeds {MAX_QUEUE_EMPTY_S:.1f}")
    if runtime_health.get("silence_with_listeners") is True:
        _fail(f"/public-status runtime reports silence_with_listeners=true: {runtime_health}")
    _pass("/public-status runtime health within queue-empty budget")


def _check_first_stream_byte() -> None:
    start = time.monotonic()
    request = Request(f"{BASE_URL}/stream", headers={"Accept": "audio/mpeg"})
    try:
        with urlopen(request, timeout=FIRST_BYTE_TIMEOUT_S) as response:
            chunk = response.read(1)
    except (TimeoutError, URLError) as exc:
        _fail(f"/stream did not return audio within {FIRST_BYTE_TIMEOUT_S:.1f}s: {exc}")
    elapsed = time.monotonic() - start
    if not chunk:
        _fail("/stream opened but returned no audio byte")
    if elapsed > FIRST_BYTE_TIMEOUT_S:
        _fail(f"/stream first byte took {elapsed:.2f}s, over {FIRST_BYTE_TIMEOUT_S:.2f}s")
    _pass(f"/stream first byte in {elapsed:.2f}s")


def main() -> int:
    print(f"HA Green perf smoke against {BASE_URL}")
    _wait_for_health()
    _check_readiness()
    _check_public_status()
    _check_first_stream_byte()
    print("HA Green perf smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
