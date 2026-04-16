from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_addon_dockerfile_preserves_package_directory():
    dockerfile = (REPO_ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "COPY pyproject.toml ./\nCOPY mammamiradio/ ./mammamiradio/" in dockerfile
    assert "COPY pyproject.toml mammamiradio/ ./" not in dockerfile, "single COPY would flatten package dir"


def test_addon_port_8000_consistent_across_config_run_and_runtime_defaults():
    """Guard against drift: all addon/runtime port defaults must stay in lockstep."""
    config_yaml = (REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    run_sh = (REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()
    runtime_config = (REPO_ROOT / "mammamiradio" / "config.py").read_text()

    ingress_match = re.search(r"^ingress_port:\s*(\d+)\s*$", config_yaml, re.MULTILINE)
    run_env_match = re.search(r'export MAMMAMIRADIO_PORT="(\d+)"', run_sh)
    run_uvicorn_match = re.search(r"--host\s+0\.0\.0\.0\s+--port\s+(\d+)\b", run_sh)
    station_default_match = re.search(r"port:\s*int\s*=\s*(\d+)", runtime_config)
    env_default_match = re.search(r'os\.getenv\("MAMMAMIRADIO_PORT",\s*"(\d+)"\)', runtime_config)

    assert ingress_match and run_env_match and run_uvicorn_match and station_default_match and env_default_match

    ports = {
        int(ingress_match.group(1)),
        int(run_env_match.group(1)),
        int(run_uvicorn_match.group(1)),
        int(station_default_match.group(1)),
        int(env_default_match.group(1)),
    }
    assert ports == {8000}


def test_addon_run_sh_respects_home_assistant_toggle():
    run_sh = (REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()

    # Must NOT use the broken f-string form (double-quotes inside shell double-quoted string
    # cause the shell to mangle the Python code, resulting in NameError: name 'true').
    assert 'export HA_ENABLED={"true" if enabled else "false"}' not in run_sh
    # Must use a shell-safe form that correctly exports HA_ENABLED.
    assert "export HA_ENABLED" in run_sh
    assert 'if [ "${HA_ENABLED:-true}" != "false" ]; then' in run_sh
    assert "Home Assistant integration disabled by add-on option" in run_sh


def test_addon_run_sh_preserves_operator_stop_flag():
    run_sh = (REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()

    assert "session_stopped.flag" not in run_sh, (
        "run.sh must not clear the persisted stop flag at container startup; "
        "only POST /api/resume may resume a deliberately stopped station."
    )
