#!/usr/bin/env python3
"""Cold-launch smoke gate for the attributed offline starter catalog.

The sibling ``ha-green-perf-smoke.py`` assumes a station is ALREADY running, so
it never measures the add-on update / restart reality — the window where a
listener connects to a freshly-started process whose lookahead queue has not
filled yet. That is exactly where the 1-2s INSTANT AUDIO promise is hardest to
keep and where dead air was measured (first byte at ~5.9s under the old 5s
queue-fallback wait).

This script launches a real cold uvicorn on empty temp cache/tmp directories,
blocks outbound socket connections in the child process, and connects one real
listener.  The first byte must arrive inside the strict connection budget while
``/public-status`` identifies the emitting segment as a manifest-attributed
starter track.  A bounded sample of those same bytes must also decode to
non-silent PCM.  Packaged continuity, local files, and synthetic cache seeds
cannot satisfy this release gate.

Env overrides:
  MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S   strict first-byte bound (default 2.0)
  MAMMAMIRADIO_LAUNCH_STARTUP_S      boot budget before health ok (default 60)

Release receipts are opt-in and can be recorded only when the machine and
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
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid
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
_DEVICE_MODEL_PATHS = (
    Path("/proc/device-tree/model"),
    Path("/sys/firmware/devicetree/base/model"),
)


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
_STREAM_SAMPLE_BYTES = 32 * 1024

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--record-release-receipt",
        type=Path,
        metavar="DIRECTORY",
        help="after a valid run on physical Home Assistant Green, atomically add one immutable receipt",
    )
    return parser.parse_args(argv)


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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    receipt_hardware: dict[str, str] | None = None
    release_version: str | None = None
    source_commit: str | None = None
    if args.record_release_receipt is not None:
        try:
            receipt_hardware = _detect_ha_green()
            release_version = _release_version()
            source_commit = _source_commit(args.record_release_receipt)
        except RuntimeError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            return 2

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
                    response_timeout_s=(
                        _RECEIPT_OBSERVATION_S if args.record_release_receipt is not None else FIRST_BYTE_S
                    ),
                    enforce_first_byte_budget=args.record_release_receipt is None,
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
            if args.record_release_receipt is not None:
                assert receipt_hardware is not None
                assert release_version is not None
                assert source_commit is not None
                try:
                    receipt = _write_release_receipt(
                        directory=args.record_release_receipt,
                        hardware=receipt_hardware,
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


if __name__ == "__main__":
    raise SystemExit(main())
