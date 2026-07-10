from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_addon_dockerfile_preserves_package_directory():
    dockerfile = (REPO_ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "COPY pyproject.toml ./\nCOPY mammamiradio/ ./mammamiradio/" in dockerfile
    assert "COPY pyproject.toml mammamiradio/ ./" not in dockerfile, "single COPY would flatten package dir"


def test_addon_stages_the_root_model_registry_without_a_duplicate_copy():
    """The image must use the canonical root registry, not an add-on-maintained fork."""
    dockerfile = (REPO_ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert (REPO_ROOT / "model_registry.toml").is_file()
    assert not (REPO_ROOT / "ha-addon" / "mammamiradio" / "model_registry.toml").exists()
    assert "COPY model_registry.toml ./" in dockerfile

    validator = (REPO_ROOT / "scripts" / "validate-addon.sh").read_text()
    assert "ha-addon/mammamiradio/model_registry.toml must not be committed" in validator
    assert 'cp model_registry.toml "$TMPCTX/"' in validator


def test_addon_dockerfile_declares_home_assistant_image_labels():
    dockerfile = (REPO_ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "ARG BUILD_VERSION" in dockerfile
    assert "ARG BUILD_ARCH" in dockerfile
    assert 'io.hass.version="${BUILD_VERSION}"' in dockerfile
    assert 'io.hass.type="app"' in dockerfile
    assert 'io.hass.arch="${BUILD_ARCH}"' in dockerfile


def test_addon_metadata_exposes_stable_channel_and_edge_channel():
    stable = (REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    edge = (REPO_ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml").read_text()

    assert re.search(r"^stage:\s*stable\s*$", stable, re.MULTILINE)
    assert re.search(r"^stage:\s*experimental\s*$", edge, re.MULTILINE)


def test_addon_apparmor_profile_is_shared_with_edge():
    stable = REPO_ROOT / "ha-addon" / "mammamiradio" / "apparmor.txt"
    edge = REPO_ROOT / "ha-addon" / "mammamiradio-edge" / "apparmor.txt"

    stable_text = stable.read_text()
    assert stable_text == edge.read_text()
    assert re.search(r"^profile\s+\S+\s+flags=", stable_text, re.MULTILINE)
    assert "network," in stable_text
    assert "/data/** rwk," in stable_text


def test_addon_port_8000_consistent_across_config_run_and_runtime_defaults():
    """Guard against drift: all addon/runtime port defaults must stay in lockstep."""
    config_yaml = (REPO_ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    run_sh = (REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()
    runtime_config = (REPO_ROOT / "mammamiradio" / "core" / "config.py").read_text()

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


def test_addon_run_sh_uses_guarded_file_backed_provider_secrets_parser():
    run_sh = (REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()

    assert 'SECRETS_FILE="/config/secrets.env"' in run_sh
    assert run_sh.count('python3 -c "') == 1
    assert 'if ! OPTS_EXPORT=$(python3 -c "' in run_sh
    assert "secret_keys = tuple(env_key for _, env_key in provider_option_map)" in run_sh
    assert "cat '$SECRETS_FILE'" not in run_sh


def test_addon_run_sh_preserves_operator_stop_flag():
    run_sh = (REPO_ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()

    assert "session_stopped.flag" not in run_sh, (
        "run.sh must not clear the persisted stop flag at container startup; "
        "only POST /api/resume may resume a deliberately stopped station."
    )
