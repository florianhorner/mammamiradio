#!/usr/bin/env python3
"""Launch smoke gate: fresh-process listener-to-first-byte without external network.

The sibling ``ha-green-perf-smoke.py`` assumes a station is ALREADY running, so
it never measures the add-on update / restart reality — the window where a
listener connects to a freshly-started process whose lookahead queue has not
filled yet. That is exactly where the 1-2s INSTANT AUDIO promise is hardest to
keep and where dead air was measured (first byte at ~5.9s under the old 5s
queue-fallback wait).

This script launches a real uvicorn twice on isolated temp state:

* a realistic add-on restart with one warm normalized-cache song;
* a first boot with an empty cache where only packaged recovery audio can win.

Both processes deny non-loopback sockets and clear every network-backed source
or provider credential. Process startup has its own readiness budget: the smoke
waits for the fresh process to accept TCP before running the listener probe.
The STRICT first-byte timer therefore measures the listener's ``/stream``
request to its first accepted audio byte (default 2.0s vs the perf-smoke's
looser 8s already-running budget); it does not claim process-spawn-to-audio
latency. Launch-specific semantic checks then require health, readiness, and
public status to agree after audio is accepted.

Both scenarios boot as fresh installs, so ``/stream`` answers that first byte
from the client-local First Listen prelude before the listener joins the
shared ``LiveStreamHub`` — first byte alone no longer proves the live station
accepted audio. After the first-byte check, the smoke therefore holds one
listener that keeps reading past the prelude (the producer wakes for it) and
waits for ``/readyz`` to flip ready within the held-listener budget before
asserting the post-stream contract. That budget must stay well inside the
packaged prelude's ~27s of runway audio: readiness has to arrive while the
speaker is still covered, or a real listener would hear the gap.

Pass ``--image IMAGE_REF`` to run the same two scenarios against an already
built add-on image. Image mode creates isolated Docker volumes, starts the
image's real ``/run.sh`` entrypoint with Docker networking disabled, and runs
the perf smoke from inside the container against loopback. It never overlays
repository source or configuration onto the image.

Env overrides:
  MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S   strict first-byte bound (default 2.0)
  MAMMAMIRADIO_LAUNCH_STARTUP_S      boot budget before health ok (default 60)
  MAMMAMIRADIO_LAUNCH_READY_S        held-listener readiness budget (default 15,
                                     capped at 22 so readiness lands while the
                                     ~27s packaged prelude still covers the
                                     listener)

Pass ``--record-release-receipt DIRECTORY`` to run the dedicated cold starter
receipt scenario instead: an empty-cache offline boot whose stream must be
identified as a manifest-attributed starter track and decode to non-silent
PCM. Release receipts are opt-in and can be recorded only when the machine and
device-tree model identify physical Home Assistant Green hardware. Each run is
written as a new immutable JSON file; existing receipts are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PERF_SMOKE = _REPO_ROOT / "scripts" / "ha-green-perf-smoke.py"
_RECEIPT_SCHEMA = _REPO_ROOT / "proof" / "media" / "ha-green-release-receipt.schema.json"
_RECEIPT_SCHEMA_VERSION = 1
_RECEIPT_OBSERVATION_S = 10.0
_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_METRIC = "listener_connection_to_first_accepted_non_silent_manifest_starter_byte"
_STREAM_SAMPLE_BYTES = 32 * 1024
_DEVICE_MODEL_PATHS = (
    Path("/proc/device-tree/model"),
    Path("/sys/firmware/devicetree/base/model"),
)
# Every scenario boots with the attributed starter rotation available, so a
# manifest starter track ("starter") is an approved offline outcome alongside
# each scenario's own rescue rung.
_LAUNCH_SCENARIOS = (
    ("warm norm-cache", True, frozenset({"norm_cache", "starter"})),
    ("cold packaged-only", False, frozenset({"canned", "packaged_recovery", "starter"})),
)
_IMAGE_VOLUME_SEED_SOURCE = """\
import json
import os
import subprocess
from pathlib import Path

data_dir = Path("/data")
cache_dir = data_dir / "cache"
cache_dir.mkdir(parents=True, exist_ok=True)
(data_dir / "music").mkdir(parents=True, exist_ok=True)
(data_dir / "tmp").mkdir(parents=True, exist_ok=True)
(data_dir / "options.json").write_text(
    json.dumps(
        {
            "enable_home_assistant": False,
            "ha_context_enabled": False,
            "ha_media_player_push": False,
            "broadcast_chain": False,
            "guest_host": False,
            "quality_profile": "economy",
        }
    ),
    encoding="utf-8",
)
(data_dir / ".provider_recovery_checked_v2").write_text("checked\\n", encoding="utf-8")

if os.environ.get("MAMMAMIRADIO_SMOKE_WARM") == "1":
    norm_path = cache_dir / "norm_launch_smoke_192k.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
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
    (cache_dir / f"{norm_path.name}.json").write_text(
        json.dumps({"title": "Launch Smoke Bed", "artist": "Test Bench", "source_kind": "local"}),
        encoding="utf-8",
    )
"""
_IMAGE_PORT_CHECK_SOURCE = """\
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.5)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", 8000)) == 0 else 1)
"""
_IMAGE_JSON_STATUS_SOURCE = """\
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

path = sys.argv[1]
request = Request(f"http://127.0.0.1:8000{path}", headers={"Accept": "application/json"})
try:
    with urlopen(request, timeout=3.0) as response:
        status = response.status
        payload = json.load(response)
except HTTPError as exc:
    status = exc.code
    payload = json.loads(exc.read().decode("utf-8") or "{}")
print(json.dumps({"status": status, "payload": payload}))
"""
_NETWORK_GUARD_SOURCE = """\
import ipaddress
import socket

_original_create_connection = socket.create_connection
_original_getaddrinfo = socket.getaddrinfo
_original_socket_connect = socket.socket.connect
_original_socket_connect_ex = socket.socket.connect_ex


def _is_loopback_host(host):
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    text = str(host).split("%", 1)[0]
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _guard_address(address):
    if isinstance(address, tuple) and address and not _is_loopback_host(address[0]):
        raise OSError(f"external network disabled by launch smoke: {address[0]}")


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback_host(host):
        raise socket.gaierror(f"external network disabled by launch smoke: {host}")
    return _original_getaddrinfo(host, *args, **kwargs)


def _guarded_socket_connect(sock, address):
    _guard_address(address)
    return _original_socket_connect(sock, address)


def _guarded_socket_connect_ex(sock, address):
    _guard_address(address)
    return _original_socket_connect_ex(sock, address)


def _guarded_create_connection(address, *args, **kwargs):
    _guard_address(address)
    return _original_create_connection(address, *args, **kwargs)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_socket_connect
socket.socket.connect_ex = _guarded_socket_connect_ex
socket.create_connection = _guarded_create_connection
"""
# A fresh install serves the client-local First Listen prelude before the
# listener joins the shared hub, so a single first byte does not prove the
# live station accepted audio. This held listener keeps reading past the
# prelude — the producer wakes for it — while the main thread waits for
# /readyz to flip ready inside the budget. argv: base_url, budget seconds.
# Runs unchanged in local-process mode (loopback subprocess) and inside the
# built add-on image (docker exec), so the two modes cannot drift.
_HELD_LISTENER_READY_SOURCE = """\
import json
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

base_url = sys.argv[1].rstrip("/")
budget_s = float(sys.argv[2])
deadline = time.monotonic() + budget_s
stop_draining = threading.Event()


def _drain_stream() -> None:
    request = Request(base_url + "/stream", headers={"Accept": "audio/mpeg"})
    try:
        with urlopen(request, timeout=budget_s + 5.0) as response:
            while not stop_draining.is_set():
                if not response.read(65536):
                    return
    except Exception:
        # The /readyz poll below owns the verdict; a dropped listener simply
        # stops feeding it and the deadline reports the honest failure.
        return


listener = threading.Thread(target=_drain_stream, daemon=True)
listener.start()

last_observation = "no /readyz response before the deadline"
ready = False
while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        break
    request = Request(base_url + "/readyz", headers={"Accept": "application/json"})
    try:
        # Never let a single poll outlive the budget: a request started just
        # before the deadline must not return ready seconds after listener
        # coverage ended and still count.
        with urlopen(request, timeout=min(3.0, remaining)) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except HTTPError as exc:
        status = exc.code
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
    except (TimeoutError, URLError) as exc:
        last_observation = "/readyz unavailable: " + str(exc)
        time.sleep(0.25)
        continue
    last_observation = "HTTP " + str(status) + " " + json.dumps(payload)
    if time.monotonic() >= deadline:
        # The response arrived after the deadline. Readiness observed outside
        # the held-listener window proves nothing about a covered speaker.
        last_observation += " (received after the readiness deadline)"
        break
    if status == 200 and payload.get("ready") is True:
        ready = True
        break
    time.sleep(0.25)

stop_draining.set()
print(last_observation)
raise SystemExit(0 if ready else 1)
"""


def _env_float(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float in seconds, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a finite positive float in seconds, got {raw!r}")
    return value


FIRST_BYTE_S = _env_float("MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S", "2.0")
STARTUP_S = _env_float("MAMMAMIRADIO_LAUNCH_STARTUP_S", "60")
# Held-listener readiness budget. Must stay well under the packaged First
# Listen prelude's ~27s of runway audio (see the module docstring): the live
# station has to be ready while the prelude still covers the speaker. The
# ceiling keeps a 5s safety margin under that runway, so an env override
# cannot quietly accept readiness that lands after the speaker went silent.
_PRELUDE_RUNWAY_S = 27.0
_READY_BUDGET_CEILING_S = _PRELUDE_RUNWAY_S - 5.0
READY_S = _env_float("MAMMAMIRADIO_LAUNCH_READY_S", "15")
if READY_S > _READY_BUDGET_CEILING_S:
    raise RuntimeError(
        f"MAMMAMIRADIO_LAUNCH_READY_S must stay at or under {_READY_BUDGET_CEILING_S:g}s "
        f"so readiness lands while the packaged prelude (~{_PRELUDE_RUNWAY_S:g}s) still "
        f"covers the listener, got {READY_S:g}"
    )


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


def _seed_warm_norm_cache(cache_dir: str | Path) -> None:
    """Plant one pre-normalized rescue file so first byte has a rung to land on.

    This models the realistic add-on restart path: ``/data/cache`` survives a
    restart, so a restarted station has a warm norm cache and the rescue ladder
    can serve audio instantly. The companion cold scenario leaves this cache
    empty and proves packaged-only recovery separately. The seed is a
    copyright-safe synthetic tone, not real music, so it never ships and never
    airs in production.

    select_norm_cache_rescue() globs ``norm_*.mp3``; load_track_metadata() reads
    the companion ``<name>.mp3.json`` sidecar (see normalizer._norm_sidecar_path).
    """
    cache_path = Path(cache_dir)
    norm_path = cache_path / "norm_launch_smoke_192k.mp3"
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
    (cache_path / f"{norm_path.name}.json").write_text(
        json.dumps({"title": "Launch Smoke Bed", "artist": "Test Bench", "source_kind": "local"}),
        encoding="utf-8",
    )


def _write_network_guard(guard_dir: str | Path) -> None:
    """Install a sitecustomize module that rejects every non-loopback socket."""
    Path(guard_dir, "sitecustomize.py").write_text(_NETWORK_GUARD_SOURCE, encoding="utf-8")


def _prepare_run_dir(run_dir: str | Path) -> None:
    """Copy only the canonical runtime config into an otherwise empty cwd."""
    target = Path(run_dir)
    shutil.copy2(_REPO_ROOT / "radio.toml", target / "radio.toml")
    shutil.copy2(_REPO_ROOT / "model_registry.toml", target / "model_registry.toml")


def _launch_env(*, port: int, cache_dir: str, tmp_dir: str, guard_dir: str) -> dict[str, str]:
    """Return an explicit no-external-network environment for one station."""
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    python_paths = [guard_dir, str(_REPO_ROOT)]
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env.update(
        {
            "MAMMAMIRADIO_BIND_HOST": "127.0.0.1",
            "MAMMAMIRADIO_PORT": str(port),
            "MAMMAMIRADIO_CACHE_DIR": cache_dir,
            "MAMMAMIRADIO_TMP_DIR": tmp_dir,
            "MAMMAMIRADIO_ALLOW_YTDLP": "false",
            "JAMENDO_CLIENT_ID": "",
            "ANTHROPIC_API_KEY": "",
            "OPENAI_API_KEY": "",
            "AZURE_SPEECH_KEY": "",
            "AZURE_SPEECH_REGION": "",
            "ELEVENLABS_API_KEY": "",
            "HA_ENABLED": "false",
            "HA_TOKEN": "",
            "HA_URL": "",
            "MAMMAMIRADIO_HA_CONTEXT_ENABLED": "false",
            "MAMMAMIRADIO_HA_MEDIA_PLAYER_PUSH": "false",
            "MAMMAMIRADIO_LEDGER_ENABLED": "false",
            # Local smoke traffic remains direct; every external socket is also
            # rejected inside the child Python process by sitecustomize.
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "no_proxy": "127.0.0.1,localhost,::1",
            "PYTHONPATH": os.pathsep.join(python_paths),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Local bind is admin-exempt; keep auth out of the smoke path.
            "ADMIN_PASSWORD": "",
            "ADMIN_TOKEN": "",
        }
    )
    return env


def _fetch_json_status(base_url: str, path: str) -> tuple[int, dict]:
    request = Request(f"{base_url}{path}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=3.0) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, payload
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        return exc.code, payload
    except (TimeoutError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read post-stream {path}: {exc}") from exc


def _post_stream_statuses(base_url: str) -> dict[str, tuple[int, dict]]:
    return {path: _fetch_json_status(base_url, path) for path in ("/healthz", "/readyz", "/public-status")}


def _hold_listener_until_ready(base_url: str, *, env: dict[str, str], cwd: str | Path) -> None:
    """Hold one draining listener on ``/stream`` until ``/readyz`` flips ready.

    The perf smoke's first-byte probe disconnects after one byte, which on a
    fresh install is First Listen prelude audio served before the listener
    joins the live hub — the producer never sees that listener. This runs the
    shared held-listener source in a child process (under the same loopback
    network guard) so the live station demonstrably wakes and accepts audio
    inside READY_S before the post-stream contract is asserted.
    """
    result = subprocess.run(
        [sys.executable, "-c", _HELD_LISTENER_READY_SOURCE, base_url, str(READY_S)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "unknown error").strip()
        raise RuntimeError(f"station did not accept hub audio within {READY_S:.0f}s of a held listener: {detail}")


def _assert_post_stream_status(statuses: dict[str, tuple[int, dict]]) -> None:
    health_status, health = statuses["/healthz"]
    if health_status != 200 or health.get("status") != "ok" or health.get("silence_with_listeners") is True:
        raise RuntimeError(f"/healthz is not healthy after first audio: HTTP {health_status} {health}")

    ready_status, ready = statuses["/readyz"]
    if ready_status != 200 or ready.get("status") != "ready" or ready.get("ready") is not True:
        raise RuntimeError(f"/readyz is not ready after first audio: HTTP {ready_status} {ready}")

    public_status, public = statuses["/public-status"]
    now_streaming = public.get("now_streaming")
    if public_status != 200 or public.get("session_stopped") is not False or not isinstance(now_streaming, dict):
        raise RuntimeError(f"/public-status is not live after first audio: HTTP {public_status} {public}")


def _current_audio_source(base_url: str) -> tuple[str, dict]:
    status, payload = _fetch_json_status(base_url, "/public-status")
    if status != 200:
        raise RuntimeError(f"could not read post-stream public status: HTTP {status} {payload}")

    return _audio_source_from_payload(payload)


def _audio_source_from_payload(payload: dict) -> tuple[str, dict]:
    now_streaming = payload.get("now_streaming") or {}
    metadata = now_streaming.get("metadata") or {}
    runtime_health = payload.get("runtime_health") or {}
    source = str(runtime_health.get("audio_source") or metadata.get("audio_source") or "")
    return source, metadata


def _docker_command(args: Sequence[str]) -> list[str]:
    return ["docker", *args]


def _run_docker(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            _docker_command(args),
            input=input_text,
            capture_output=capture_output,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "docker is required for built-image launch smoke; install or start Docker and retry"
        ) from exc


def _image_seed_command(image: str, volume_name: str, *, seed_warm_cache: bool) -> list[str]:
    return [
        "run",
        "--rm",
        "--network",
        "none",
        "--volume",
        f"{volume_name}:/data",
        "--env",
        f"MAMMAMIRADIO_SMOKE_WARM={'1' if seed_warm_cache else '0'}",
        # Home Assistant base images inherit ENTRYPOINT ["/init"]. Override it
        # for the one-shot volume seed; passing "python3" after the image only
        # supplies arguments to s6 and leaves the cache unseeded.
        "--entrypoint",
        "python3",
        image,
        "-c",
        _IMAGE_VOLUME_SEED_SOURCE,
    ]


def _image_launch_command(image: str, volume_name: str, container_name: str) -> list[str]:
    return [
        "run",
        "--detach",
        "--name",
        container_name,
        "--network",
        "none",
        "--volume",
        f"{volume_name}:/data",
        "--env",
        "LOG_LEVEL=INFO",
        "--env",
        "SUPERVISOR_TOKEN=smoke-ci",
        "--env",
        "SUPERVISOR_API=http://127.0.0.1:9",
        "--env",
        "ANTHROPIC_API_KEY=",
        "--env",
        "OPENAI_API_KEY=",
        "--env",
        "AZURE_SPEECH_KEY=",
        "--env",
        "AZURE_SPEECH_REGION=",
        "--env",
        "ELEVENLABS_API_KEY=",
        "--env",
        "JAMENDO_CLIENT_ID=",
        image,
    ]


def _image_perf_command(container_name: str) -> list[str]:
    return [
        "exec",
        "--interactive",
        "--env",
        "MAMMAMIRADIO_PERF_BASE_URL=http://127.0.0.1:8000",
        "--env",
        f"MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S={FIRST_BYTE_S}",
        "--env",
        f"MAMMAMIRADIO_PERF_STARTUP_TIMEOUT_S={STARTUP_S}",
        container_name,
        "python3",
        "-",
    ]


def _seed_image_volume(image: str, volume_name: str, *, seed_warm_cache: bool) -> None:
    result = _run_docker(_image_seed_command(image, volume_name, seed_warm_cache=seed_warm_cache))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"could not prepare isolated image state: {detail}")


def _image_container_running(container_name: str) -> bool:
    result = _run_docker(["inspect", "--format", "{{.State.Running}}", container_name])
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _wait_for_image_server(container_name: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not _image_container_running(container_name):
            return False
        result = _run_docker(["exec", container_name, "python3", "-c", _IMAGE_PORT_CHECK_SOURCE])
        if result.returncode == 0:
            return True
        time.sleep(0.25)
    return False


def _run_image_perf_smoke(container_name: str) -> bool:
    result = _run_docker(
        _image_perf_command(container_name),
        input_text=_PERF_SMOKE.read_text(encoding="utf-8"),
    )
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
    return result.returncode == 0


def _image_fetch_json_status(container_name: str, path: str) -> tuple[int, dict]:
    result = _run_docker(["exec", container_name, "python3", "-c", _IMAGE_JSON_STATUS_SOURCE, path])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"could not read built-image {path}: {detail}")
    try:
        result_payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"built-image {path} was not JSON: {result.stdout!r}") from exc
    return int(result_payload.get("status") or 0), result_payload.get("payload") or {}


def _image_post_stream_statuses(container_name: str) -> dict[str, tuple[int, dict]]:
    return {path: _image_fetch_json_status(container_name, path) for path in ("/healthz", "/readyz", "/public-status")}


def _image_current_audio_source(container_name: str) -> tuple[str, dict]:
    status, payload = _image_fetch_json_status(container_name, "/public-status")
    if status != 200:
        raise RuntimeError(f"could not read built-image public status: HTTP {status} {payload}")
    return _audio_source_from_payload(payload)


def _print_image_logs(container_name: str) -> None:
    result = _run_docker(["logs", "--tail", "100", container_name])
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if output:
        print(f"--- {container_name} logs ---\n{output}", file=sys.stderr)


def _run_image_launch_scenario(
    image: str,
    label: str,
    *,
    seed_warm_cache: bool,
    expected_sources: frozenset[str],
) -> bool:
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
    slug = "warm" if seed_warm_cache else "cold"
    volume_name = f"mmr-launch-{slug}-{suffix}"
    container_name = f"mmr-launch-{slug}-{suffix}"
    volume_created = False
    container_created = False

    try:
        volume_result = _run_docker(["volume", "create", volume_name])
        if volume_result.returncode != 0:
            detail = (volume_result.stderr or volume_result.stdout or "unknown error").strip()
            raise RuntimeError(f"could not create isolated Docker volume: {detail}")
        volume_created = True
        _seed_image_volume(image, volume_name, seed_warm_cache=seed_warm_cache)

        print(f"Launching {label} from built image {image} (Docker network disabled)", flush=True)
        launch_result = _run_docker(_image_launch_command(image, volume_name, container_name))
        if launch_result.returncode != 0:
            detail = (launch_result.stderr or launch_result.stdout or "unknown error").strip()
            raise RuntimeError(f"could not start built image: {detail}")
        container_created = True

        if not _wait_for_image_server(container_name, time.monotonic() + STARTUP_S):
            _print_image_logs(container_name)
            print(
                f"[FAIL] {label}: built image did not accept loopback connections within {STARTUP_S}s",
                file=sys.stderr,
            )
            return False
        if not _run_image_perf_smoke(container_name):
            _print_image_logs(container_name)
            print(f"[FAIL] {label}: built-image first-byte smoke failed", file=sys.stderr)
            return False

        # Same held-listener step as local mode, run inside the container so
        # the network-disabled image proves hub-accepted audio over loopback.
        held = _run_docker(
            [
                "exec",
                container_name,
                "python3",
                "-c",
                _HELD_LISTENER_READY_SOURCE,
                "http://127.0.0.1:8000",
                str(READY_S),
            ]
        )
        if held.returncode != 0:
            _print_image_logs(container_name)
            detail = (held.stdout or held.stderr or "unknown error").strip()
            print(
                f"[FAIL] {label}: built image did not accept hub audio within {READY_S:.0f}s "
                f"of a held listener: {detail}",
                file=sys.stderr,
            )
            return False

        try:
            _assert_post_stream_status(_image_post_stream_statuses(container_name))
        except RuntimeError as exc:
            _print_image_logs(container_name)
            print(f"[FAIL] {label}: {exc}", file=sys.stderr)
            return False

        source, metadata = _image_current_audio_source(container_name)
        if source not in expected_sources:
            _print_image_logs(container_name)
            print(
                f"[FAIL] {label}: expected first audio source in {sorted(expected_sources)}, "
                f"got {source or 'unknown'} (metadata={metadata})",
                file=sys.stderr,
            )
            return False
        print(
            f"[PASS] {label}: built-image first audio source={source}, first byte under {FIRST_BYTE_S}s",
            flush=True,
        )
        return True
    finally:
        if container_created:
            _run_docker(["rm", "--force", container_name])
        if volume_created:
            _run_docker(["volume", "rm", "--force", volume_name])


def _run_launch_scenario(label: str, *, seed_warm_cache: bool, expected_sources: frozenset[str]) -> bool:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    with (
        tempfile.TemporaryDirectory(prefix="mmr-launch-cache-") as cache_dir,
        tempfile.TemporaryDirectory(prefix="mmr-launch-tmp-") as tmp_dir,
        tempfile.TemporaryDirectory(prefix="mmr-launch-run-") as run_dir,
        tempfile.TemporaryDirectory(prefix="mmr-launch-network-guard-") as guard_dir,
    ):
        _write_network_guard(guard_dir)
        _prepare_run_dir(run_dir)
        env = _launch_env(port=port, cache_dir=cache_dir, tmp_dir=tmp_dir, guard_dir=guard_dir)

        if seed_warm_cache:
            _seed_warm_norm_cache(cache_dir)
        print(f"Launching {label} station on {base_url} (external network denied)", flush=True)
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
            cwd=run_dir,
            env=env,
            start_new_session=True,  # own process group so teardown kills children
        )
        try:
            if not _wait_until_accepting(port, time.monotonic() + STARTUP_S, proc):
                print(
                    f"[FAIL] {label}: station did not accept connections within {STARTUP_S}s (exit={proc.poll()})",
                    file=sys.stderr,
                )
                return False
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
                cwd=run_dir,
            )
            if result.returncode != 0:
                print(f"[FAIL] {label}: launch first-byte smoke failed", file=sys.stderr)
                return False

            try:
                _hold_listener_until_ready(base_url, env=smoke_env, cwd=run_dir)
                _assert_post_stream_status(_post_stream_statuses(base_url))
            except RuntimeError as exc:
                print(f"[FAIL] {label}: {exc}", file=sys.stderr)
                return False

            source, metadata = _current_audio_source(base_url)
            if source not in expected_sources:
                print(
                    f"[FAIL] {label}: expected first audio source in {sorted(expected_sources)}, "
                    f"got {source or 'unknown'} (metadata={metadata})",
                    file=sys.stderr,
                )
                return False
            print(f"[PASS] {label}: first audio source={source}, first byte under {FIRST_BYTE_S}s", flush=True)
            return True
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


_OFFLINE_UVICORN = r"""
import ipaddress
import socket
import sys

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _allow(address):
    if not isinstance(address, tuple) or not address:
        return True
    host = str(address[0]).strip("[]")
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _offline_connect(sock, address):
    if not _allow(address):
        raise OSError("cold-launch smoke blocks outbound network connections")
    return _connect(sock, address)


def _offline_connect_ex(sock, address):
    if not _allow(address):
        return 101
    return _connect_ex(sock, address)


socket.socket.connect = _offline_connect
socket.socket.connect_ex = _offline_connect_ex

import uvicorn

uvicorn.run(
    "mammamiradio.main:app",
    host="127.0.0.1",
    port=int(sys.argv[1]),
    access_log=False,
)
"""


def _release_version() -> str:
    config = _REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml"
    try:
        for line in config.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                value = line.split(":", 1)[1].strip().strip('"')
                if _SEMVER.fullmatch(value):
                    return value
    except OSError as exc:
        raise RuntimeError(f"cannot read the release version from {config}: {exc}") from exc
    raise RuntimeError(f"cannot read an exact X.Y.Z release version from {config}")


def _source_commit(receipt_directory: Path, *, repo_root: Path = _REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD^{commit}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip().casefold()
    if result.returncode != 0 or not _COMMIT.fullmatch(value):
        raise RuntimeError("cannot resolve the tested git commit; record receipts from a committed checkout")
    try:
        receipt_prefix = (
            receipt_directory.expanduser().absolute().resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
            + "/"
        )
    except ValueError as exc:
        raise RuntimeError("release receipts must be recorded into this checkout's proof directory") from exc
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--no-renames"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        raise RuntimeError(f"cannot inspect the tested checkout: {status.stderr.strip() or 'git status failed'}")
    disallowed: list[str] = []
    for line in status.stdout.splitlines():
        if not line:
            continue
        path = line[3:]
        allowed_untracked_receipt = (
            line.startswith("?? ")
            and path.startswith(receipt_prefix)
            and re.fullmatch(r"run-[0-9a-f-]{36}\.json", path.removeprefix(receipt_prefix)) is not None
        )
        if not allowed_untracked_receipt:
            disallowed.append(line)
    if disallowed:
        preview = ", ".join(disallowed[:5])
        if len(disallowed) > 5:
            preview += f", and {len(disallowed) - 5} more"
        raise RuntimeError(
            "release receipts require a clean tested checkout; only earlier untracked receipt JSON files "
            f"in {receipt_prefix} are allowed (found {preview})"
        )
    return value


def _detect_ha_green() -> dict[str, str]:
    machine = platform.machine().strip().casefold()
    if machine not in {"aarch64", "arm64"}:
        raise RuntimeError(
            "release receipts require physical Home Assistant Green aarch64 hardware; "
            f"this machine reports {machine or 'unknown'}"
        )
    for model_path in _DEVICE_MODEL_PATHS:
        try:
            model = model_path.read_bytes()[:512].decode("utf-8", errors="strict").rstrip("\x00\n ")
        except (OSError, UnicodeError):
            continue
        if "home assistant green" in model.casefold():
            return {
                "model": model,
                "machine": machine,
                "detected_from": model_path.as_posix(),
            }
    raise RuntimeError(
        "release receipts require a device-tree model identifying Home Assistant Green; "
        "run the ordinary smoke without --record-release-receipt on other machines"
    )


def _reject_symlink_components(path: Path) -> None:
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"release receipt path must not traverse a symlink: {current}")


def _write_release_receipt(
    *,
    directory: Path,
    hardware: dict[str, str],
    release_version: str,
    source_commit: str,
    boot_to_tcp_s: float,
    connection_to_first_byte_s: float,
    run_id: uuid.UUID | None = None,
    recorded_at: datetime | None = None,
) -> Path:
    if ".context" in directory.expanduser().absolute().parts:
        raise RuntimeError("release evidence must not be written under .context")
    _reject_symlink_components(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(directory)
    if not directory.is_dir():
        raise RuntimeError(f"release receipt destination is not a directory: {directory}")

    receipt_id = run_id or uuid.uuid4()
    timestamp = recorded_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise RuntimeError("release receipt timestamp must be timezone-aware")
    payload: dict[str, Any] = {
        "$schema": f"../{_RECEIPT_SCHEMA.name}",
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "evidence_kind": "ha_green_cold_launch",
        "release_version": release_version,
        "source_commit": source_commit,
        "run_id": str(receipt_id),
        "recorded_at": timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "hardware": hardware,
        "timing": {
            "metric": _METRIC,
            "boot_to_tcp_ms": round(boot_to_tcp_s * 1_000, 3),
            "connection_to_first_byte_ms": round(connection_to_first_byte_s * 1_000, 3),
        },
        "assertions": {
            "cache_empty": True,
            "outbound_network_blocked": True,
            "manifest_attributed_starter": True,
            "non_silent": True,
            "provider": "incompetech",
            "basis": "bundled_manifest",
        },
    }
    destination = directory / f"run-{receipt_id}.json"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".receipt-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise RuntimeError(f"refusing to overwrite existing release receipt: {destination}") from exc
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def _public_status(base_url: str) -> dict:
    try:
        with urlopen(
            Request(f"{base_url}/public-status", headers={"Accept": "application/json"}),
            timeout=3,
        ) as response:
            import json

            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"public status unavailable during cold-launch smoke: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("public status returned a non-object")
    return payload


def _assert_manifest_starter_status(payload: dict) -> None:
    now = payload.get("now_streaming")
    metadata = now.get("metadata") if isinstance(now, dict) else None
    attribution = metadata.get("music_attribution") if isinstance(metadata, dict) else None
    if (
        not isinstance(now, dict)
        or now.get("type") != "music"
        or not isinstance(metadata, dict)
        or metadata.get("source_kind") != "starter"
        or not isinstance(attribution, dict)
        or attribution.get("provider") != "incompetech"
        or attribution.get("basis") != "bundled_manifest"
    ):
        raise RuntimeError("first streamed byte was not from a manifest-attributed starter track")


def _assert_non_silent_mp3(sample: bytes) -> None:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "mp3",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "pipe:1",
            ],
            input=sample,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ffmpeg is required for scripts/ha-green-launch-smoke.py; install ffmpeg and rerun make launch-smoke"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg timed out while validating the cold-launch stream sample") from exc
    pcm = result.stdout[: len(result.stdout) - (len(result.stdout) % 2)]
    peak = max((abs(value[0]) for value in struct.iter_unpack("<h", pcm)), default=0)
    if result.returncode != 0 or peak < 32:
        raise RuntimeError("manifest-attributed starter sample was undecodable or silent")


def _check_first_starter_audio(
    base_url: str,
    *,
    response_timeout_s: float = FIRST_BYTE_S,
    enforce_first_byte_budget: bool = True,
) -> float:
    start = time.monotonic()
    try:
        with urlopen(
            Request(f"{base_url}/stream", headers={"Accept": "audio/mpeg"}),
            timeout=response_timeout_s,
        ) as response:
            first = response.read(1)
            elapsed = time.monotonic() - start
            if not first:
                raise RuntimeError("stream opened but returned no audio byte")
            if enforce_first_byte_budget and elapsed > FIRST_BYTE_S:
                raise RuntimeError(f"first starter byte took {elapsed:.3f}s, over {FIRST_BYTE_S:.3f}s")
            _assert_manifest_starter_status(_public_status(base_url))
            sample = first + response.read(_STREAM_SAMPLE_BYTES - 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"starter stream did not open inside {response_timeout_s:.3f}s: {exc}") from exc
    _assert_non_silent_mp3(sample)
    return elapsed


def _run_receipt_mode(
    receipt_directory: Path,
    *,
    hardware: dict[str, str],
    release_version: str,
    source_commit: str,
) -> int:
    """Dedicated cold starter receipt run (physical Home Assistant Green only).

    Launches one offline cold uvicorn on empty temp cache/tmp directories,
    requires the connected listener's stream to identify as a non-silent,
    manifest-attributed starter track, runs the perf smoke against the same
    process, and records one immutable release receipt.
    """
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
                "MAMMAMIRADIO_ALLOW_YTDLP": "false",
                "MAMMAMIRADIO_JAMENDO_ENABLED": "false",
                "MAMMAMIRADIO_JAMENDO_NONCOMMERCIAL_ACKNOWLEDGED": "false",
                "JAMENDO_CLIENT_ID": "",
                "ANTHROPIC_API_KEY": "",
                "OPENAI_API_KEY": "",
                "HA_TOKEN": "",
                "HA_URL": "",
                # Local bind is admin-exempt; keep auth out of the smoke path.
                "ADMIN_PASSWORD": "",
                "ADMIN_TOKEN": "",
            }
        )
        if any(Path(cache_dir).iterdir()):
            print("[FAIL] cold-launch cache was not empty before startup", file=sys.stderr)
            return 1
        print(f"Launching offline cold station on {base_url} (empty cache={cache_dir})")
        process_started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-c", _OFFLINE_UVICORN, str(port)],
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
            boot_to_tcp_s = time.monotonic() - process_started
            try:
                first_byte_elapsed = _check_first_starter_audio(
                    base_url,
                    response_timeout_s=_RECEIPT_OBSERVATION_S,
                    enforce_first_byte_budget=False,
                )
            except RuntimeError as exc:
                print(f"[FAIL] {exc}", file=sys.stderr)
                return 1
            budget_passed = first_byte_elapsed <= FIRST_BYTE_S
            result_label = "PASS" if budget_passed else "FAIL"
            print(
                f"[{result_label}] connected listener received a non-silent, manifest-attributed "
                f"starter byte in {first_byte_elapsed:.3f}s; boot-to-TCP was {boot_to_tcp_s:.3f}s"
            )
            smoke_env = env.copy()
            smoke_env.update(
                {
                    "MAMMAMIRADIO_PERF_BASE_URL": base_url,
                    # The dedicated check above owns the strict starter-byte
                    # assertion. Keep the generic running-station check bounded.
                    "MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S": "8",
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
            try:
                receipt = _write_release_receipt(
                    directory=receipt_directory,
                    hardware=hardware,
                    release_version=release_version,
                    source_commit=source_commit,
                    boot_to_tcp_s=boot_to_tcp_s,
                    connection_to_first_byte_s=first_byte_elapsed,
                )
            except RuntimeError as exc:
                print(f"[FAIL] release receipt was not written: {exc}", file=sys.stderr)
                return 2
            print(f"Recorded immutable Home Assistant Green release receipt: {receipt}")
            if not budget_passed:
                print(
                    f"[FAIL] first starter byte took {first_byte_elapsed:.3f}s, over {FIRST_BYTE_S:.3f}s",
                    file=sys.stderr,
                )
                return 1
            print(f"Cold-launch starter smoke passed (first byte under {FIRST_BYTE_S}s).")
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


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        metavar="IMAGE_REF",
        help="run the two launch scenarios against this exact built add-on image",
    )
    parser.add_argument(
        "--record-release-receipt",
        type=Path,
        metavar="DIRECTORY",
        help="after a valid run on physical Home Assistant Green, atomically add one immutable receipt",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(() if argv is None else argv)
    if args.record_release_receipt is not None:
        if args.image:
            print(
                "[FAIL] --record-release-receipt runs the dedicated local cold scenario; drop --image",
                file=sys.stderr,
            )
            return 2
        try:
            hardware = _detect_ha_green()
            release_version = _release_version()
            source_commit = _source_commit(args.record_release_receipt)
        except RuntimeError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2
        return _run_receipt_mode(
            args.record_release_receipt,
            hardware=hardware,
            release_version=release_version,
            source_commit=source_commit,
        )
    failures: list[str] = []
    for label, seed_warm_cache, expected_sources in _LAUNCH_SCENARIOS:
        try:
            if args.image:
                passed = _run_image_launch_scenario(
                    args.image,
                    label,
                    seed_warm_cache=seed_warm_cache,
                    expected_sources=expected_sources,
                )
            else:
                passed = _run_launch_scenario(
                    label,
                    seed_warm_cache=seed_warm_cache,
                    expected_sources=expected_sources,
                )
        except RuntimeError as exc:
            print(f"[FAIL] {label}: {exc}", file=sys.stderr)
            passed = False
        if not passed:
            failures.append(label)

    if failures:
        print(f"Launch smoke failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    target = f"built image {args.image}" if args.image else "local process"
    print(f"Launch smoke passed for {len(_LAUNCH_SCENARIOS)} offline {target} scenarios.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
