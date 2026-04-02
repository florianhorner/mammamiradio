"""Shared runtime contract for external go-librespot ownership."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoLibrespotRuntime:
    go_librespot_bin: str
    config_dir: Path
    fifo_path: Path
    port: int
    tmp_dir: Path
    fingerprint: str
    state_file: Path


def _normalize_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def build_go_librespot_runtime(
    go_librespot_bin: str,
    config_dir: Path | str,
    fifo_path: Path | str,
    port: int,
    tmp_dir: Path | str,
) -> GoLibrespotRuntime:
    config_dir = _normalize_path(config_dir)
    fifo_path = _normalize_path(fifo_path)
    tmp_dir = _normalize_path(tmp_dir)
    payload = {
        "go_librespot_bin": go_librespot_bin,
        "config_dir": str(config_dir),
        "fifo_path": str(fifo_path),
        "port": port,
    }
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return GoLibrespotRuntime(
        go_librespot_bin=go_librespot_bin,
        config_dir=config_dir,
        fifo_path=fifo_path,
        port=port,
        tmp_dir=tmp_dir,
        fingerprint=fingerprint,
        state_file=tmp_dir / "go-librespot.state.json",
    )


def _read_state_file(state_file: Path | str) -> dict | None:
    try:
        return json.loads(Path(state_file).read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def claim_process(
    state_file: Path | str,
    *,
    pid: int,
    fingerprint: str,
    go_librespot_bin: str,
    config_dir: Path | str,
) -> None:
    state_path = Path(state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "fingerprint": fingerprint,
        "go_librespot_bin": go_librespot_bin,
        "config_dir": str(_normalize_path(config_dir)),
    }
    state_path.write_text(json.dumps(payload, sort_keys=True))


def _pid_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            # `ps -o command=` may truncate long command lines on Linux CI, which
            # makes our ownership check drop a valid go-librespot process. `ww`
            # asks for the full, untruncated command line.
            ["ps", "ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def _command_matches_expected_state(command: str, go_librespot_bin: str, config_dir: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    expected_bin_name = Path(go_librespot_bin).name
    has_bin = any(token == go_librespot_bin or Path(token).name == expected_bin_name for token in tokens)
    has_config_dir = any(
        token == "--config_dir" and idx + 1 < len(tokens) and tokens[idx + 1] == config_dir
        for idx, token in enumerate(tokens[:-1])
    )
    return has_bin and has_config_dir


def read_owned_pid(state_file: Path | str, fingerprint: str) -> int | None:
    state_path = Path(state_file)
    state = _read_state_file(state_path)
    if not state:
        return None

    try:
        pid = int(state["pid"])
        expected_fingerprint = str(state["fingerprint"])
        go_librespot_bin = str(state["go_librespot_bin"])
        config_dir = str(state["config_dir"])
    except (KeyError, TypeError, ValueError):
        state_path.unlink(missing_ok=True)
        return None

    if expected_fingerprint != fingerprint:
        state_path.unlink(missing_ok=True)
        return None

    command = _pid_command(pid)
    if not command or not _command_matches_expected_state(command, go_librespot_bin, config_dir):
        state_path.unlink(missing_ok=True)
        return None

    return pid


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) == 6 and args[0] == "claim":
        try:
            pid = int(args[2])
        except ValueError:
            print(f"Invalid PID: {args[2]}", file=sys.stderr)
            return 1
        claim_process(
            args[1],
            pid=pid,
            fingerprint=args[3],
            go_librespot_bin=args[4],
            config_dir=args[5],
        )
        return 0

    if len(args) == 3 and args[0] == "owned-pid":
        owned_pid = read_owned_pid(args[1], args[2])
        if owned_pid is not None:
            print(owned_pid)
        return 0

    print(
        "Usage: python -m mammamiradio.go_librespot_runtime "
        "{claim <state-file> <pid> <fingerprint> <bin> <config-dir>"
        "|owned-pid <state-file> <fingerprint>}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
