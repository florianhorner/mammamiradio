#!/usr/bin/env python3
"""Cold-launch smoke gate: process-start to first audio byte.

The sibling ``ha-green-perf-smoke.py`` assumes a station is ALREADY running, so
it never measures the add-on update / restart reality — the window where a
listener connects to a freshly-started process whose lookahead queue has not
filled yet. That is exactly where the 1-2s INSTANT AUDIO promise is hardest to
keep and where dead air was measured (first byte at ~5.9s under the old 5s
queue-fallback wait).

This script launches a real cold uvicorn on a temp cache/tmp (so no warm norm
cache or persisted flags leak in), waits for health, then runs the existing
perf-smoke HTTP checks against it with a STRICT first-byte bound (default 2.0s
vs the perf-smoke's looser 8s already-running budget). It reuses
``ha-green-perf-smoke.py`` as the single source of HTTP-check truth instead of
duplicating the health/readiness/stream assertions.

Env overrides:
  MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S   strict first-byte bound (default 2.0)
  MAMMAMIRADIO_LAUNCH_STARTUP_S      boot budget before health ok (default 60)
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PERF_SMOKE = _REPO_ROOT / "scripts" / "ha-green-perf-smoke.py"


def _env_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float in seconds, got {raw!r}") from exc


FIRST_BYTE_S = _env_float("MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S", "2.0")
STARTUP_S = _env_float("MAMMAMIRADIO_LAUNCH_STARTUP_S", "60")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_accepting(port: int, deadline: float, proc: subprocess.Popen) -> bool:
    """Block until the server accepts TCP, the boot budget runs out, or it dies.

    The perf-smoke fails fast on a refused connection (it assumes an
    already-running station), so the launch smoke owns the start-up wait: it
    polls the port until uvicorn is listening, then hands off to the perf-smoke
    health/first-byte checks.
    """
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # process exited during boot
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _seed_warm_norm_cache(cache_dir: str) -> None:
    """Plant one pre-normalized rescue file so first byte has a rung to land on.

    This models the REALISTIC add-on restart path: ``/data/cache`` survives a
    restart, so a restarted station has a warm norm cache and the rescue ladder
    can serve audio instantly. (The first-ever-boot bare container — no music
    source, no committed welcome asset — is a separate product gap, not what
    this restart smoke measures.) The seed is a copyright-safe synthetic tone,
    not real music, so it never ships and never airs in production.

    select_norm_cache_rescue() globs ``norm_*.mp3``; load_track_metadata() reads
    the companion ``<name>.mp3.json`` sidecar (see normalizer._norm_sidecar_path).
    """
    norm_path = Path(cache_dir) / "norm_launch_smoke_192k.mp3"
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:duration=8",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(norm_path),
            ],
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is required for scripts/ha-green-launch-smoke.py; install ffmpeg and rerun make launch-smoke"
        ) from exc
    (Path(cache_dir) / f"{norm_path.name}.json").write_text(
        json.dumps({"title": "Launch Smoke Bed", "artist": "Test Bench"})
    )


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    with (
        tempfile.TemporaryDirectory(prefix="mmr-launch-cache-") as cache_dir,
        tempfile.TemporaryDirectory(prefix="mmr-launch-tmp-") as tmp_dir,
    ):
        env = os.environ.copy()
        env.update(
            {
                "MAMMAMIRADIO_BIND_HOST": "127.0.0.1",
                "MAMMAMIRADIO_PORT": str(port),
                "MAMMAMIRADIO_CACHE_DIR": cache_dir,
                "MAMMAMIRADIO_TMP_DIR": tmp_dir,
                # No chart fetch: first byte must come from the rescue ladder, not
                # a fresh produced segment (the first produce is the slow Pi render
                # and would blow the 2s budget). The seeded warm cache below is the
                # rung it lands on.
                "MAMMAMIRADIO_ALLOW_YTDLP": "false",
                # Local bind is admin-exempt; keep auth out of the smoke path.
                "ADMIN_PASSWORD": "",
                "ADMIN_TOKEN": "",
            }
        )

        _seed_warm_norm_cache(cache_dir)
        print(f"Launching cold station on {base_url} (cache={cache_dir})")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mammamiradio.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            start_new_session=True,  # own process group so teardown kills children
        )
        try:
            if not _wait_until_accepting(port, time.monotonic() + STARTUP_S, proc):
                print(
                    f"[FAIL] station did not accept connections within {STARTUP_S}s (exit={proc.poll()})",
                    file=sys.stderr,
                )
                return 1
            smoke_env = env.copy()
            smoke_env.update(
                {
                    "MAMMAMIRADIO_PERF_BASE_URL": base_url,
                    # Strict: a freshly-launched process must serve first byte
                    # inside the INSTANT AUDIO promise, not the 8s running budget.
                    "MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S": str(FIRST_BYTE_S),
                    "MAMMAMIRADIO_PERF_STARTUP_TIMEOUT_S": str(STARTUP_S),
                }
            )
            result = subprocess.run(
                [sys.executable, str(_PERF_SMOKE)],
                env=smoke_env,
                cwd=str(_REPO_ROOT),
            )
            if result.returncode != 0:
                print("[FAIL] cold-launch first-byte smoke failed", file=sys.stderr)
                return result.returncode
            print(f"Cold-launch smoke passed (first byte under {FIRST_BYTE_S}s).")
            return 0
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
