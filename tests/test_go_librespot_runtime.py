from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mammamiradio.go_librespot_runtime import (
    build_go_librespot_runtime,
    claim_process,
    read_owned_pid,
)


def test_build_runtime_normalizes_paths(tmp_path):
    runtime = build_go_librespot_runtime(
        go_librespot_bin="go-librespot",
        config_dir=tmp_path / "cfg",
        fifo_path=Path(".") / tmp_path.name / "fifo",
        port=3678,
        tmp_dir=tmp_path / "tmp",
    )

    assert runtime.config_dir.is_absolute()
    assert runtime.fifo_path.is_absolute()
    assert runtime.state_file.name == "go-librespot.state.json"


def test_claim_process_and_read_owned_pid(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    runtime = build_go_librespot_runtime(
        go_librespot_bin=str(tmp_path / "go-librespot"),
        config_dir=config_dir,
        fifo_path=tmp_path / "mammamiradio.pcm",
        port=3678,
        tmp_dir=tmp_path / "tmp",
    )
    launcher = tmp_path / "go-librespot"
    launcher.write_text("#!/bin/sh\ntrap 'exit 0' TERM INT HUP\nwhile true; do sleep 1; done\n")
    launcher.chmod(0o755)

    proc = subprocess.Popen([str(launcher), "--config_dir", str(runtime.config_dir)])
    try:
        claim_process(
            runtime.state_file,
            pid=proc.pid,
            fingerprint=runtime.fingerprint,
            go_librespot_bin=runtime.go_librespot_bin,
            config_dir=runtime.config_dir,
        )

        assert read_owned_pid(runtime.state_file, runtime.fingerprint) == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_read_owned_pid_removes_stale_state(tmp_path):
    runtime = build_go_librespot_runtime(
        go_librespot_bin="go-librespot",
        config_dir=tmp_path / "cfg",
        fifo_path=tmp_path / "mammamiradio.pcm",
        port=3678,
        tmp_dir=tmp_path / "tmp",
    )
    runtime.state_file.parent.mkdir(parents=True, exist_ok=True)
    runtime.state_file.write_text(
        json.dumps(
            {
                "pid": 999999,
                "fingerprint": runtime.fingerprint,
                "go_librespot_bin": runtime.go_librespot_bin,
                "config_dir": str(runtime.config_dir),
            }
        )
    )

    assert read_owned_pid(runtime.state_file, runtime.fingerprint) is None
    assert not runtime.state_file.exists()
