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

Pass ``--image IMAGE_REF`` to run the same two scenarios against an already
built add-on image. Image mode creates isolated Docker volumes, starts the
image's real ``/run.sh`` entrypoint with Docker networking disabled, and runs
the perf smoke from inside the container against loopback. It never overlays
repository source or configuration onto the image.

Env overrides:
  MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S   strict first-byte bound (default 2.0)
  MAMMAMIRADIO_LAUNCH_STARTUP_S      boot budget before health ok (default 60)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PERF_SMOKE = _REPO_ROOT / "scripts" / "ha-green-perf-smoke.py"
_LAUNCH_SCENARIOS = (
    ("warm norm-cache", True, frozenset({"norm_cache"})),
    ("cold packaged-only", False, frozenset({"canned", "packaged_recovery"})),
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
        json.dumps({"title": "Launch Smoke Bed", "artist": "Test Bench"}),
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
        json.dumps({"title": "Launch Smoke Bed", "artist": "Test Bench"}),
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


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        metavar="IMAGE_REF",
        help="run the two launch scenarios against this exact built add-on image",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(() if argv is None else argv)
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
