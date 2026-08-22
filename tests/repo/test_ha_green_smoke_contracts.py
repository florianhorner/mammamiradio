"""Contract tests for the HA Green launch and runtime smoke gates."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LAUNCH_SMOKE = ROOT / "scripts" / "ha-green-launch-smoke.py"
PERF_SMOKE = ROOT / "scripts" / "ha-green-perf-smoke.py"
PI_SMOKE_WORKFLOW = ROOT / ".github" / "workflows" / "pi-smoke.yml"
ADDON_RUN_SCRIPT = ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh"


def _load_script(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_perf_smoke() -> ModuleType:
    return _load_script(PERF_SMOKE, "ha_green_perf_smoke_contract_test")


def test_launch_smoke_names_the_listener_timing_boundary_honestly() -> None:
    launch_body = LAUNCH_SMOKE.read_text(encoding="utf-8")
    workflow_body = PI_SMOKE_WORKFLOW.read_text(encoding="utf-8")

    assert "fresh-process listener-to-first-byte" in launch_body
    assert "does not claim process-spawn-to-audio" in launch_body
    assert "Fresh-process listener-to-first-byte smoke (<= 2s)" in workflow_body
    assert "request-to-first-byte, not" in workflow_body


def test_pi_smoke_keeps_required_check_name_when_arm_work_is_path_gated() -> None:
    document = yaml.safe_load(PI_SMOKE_WORKFLOW.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert "pi-smoke" in jobs
    assert jobs["pi-smoke"]["if"] == "always() && !cancelled()"
    assert jobs["pi-smoke"]["needs"] == ["changes", "pi-smoke-run"]
    assert jobs["pi-smoke-run"]["if"] == "needs.changes.outputs.audio == 'true'"
    assert jobs["pi-smoke-run"]["runs-on"] == "ubuntu-24.04-arm"


def test_pi_smoke_cancels_superseded_pr_runs_only() -> None:
    document = yaml.safe_load(PI_SMOKE_WORKFLOW.read_text(encoding="utf-8"))
    concurrency = document["concurrency"]
    assert concurrency["group"] == "${{ github.workflow }}-${{ github.event.pull_request.number || github.sha }}"
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_pi_smoke_btbn_fallback_tracks_supported_assets_and_verifies_digest() -> None:
    workflow_body = PI_SMOKE_WORKFLOW.read_text(encoding="utf-8")
    asset_pattern = (
        r"^ffmpeg-n(7\\.[0-9]+-latest-linuxarm64-gpl-7\\.[0-9]+|"
        r"8\\.[0-9]+-latest-linuxarm64-gpl-8\\.[0-9]+)\\.tar\\.xz$"
    )

    assert "Install static ffmpeg compatibility build (arm64)" in workflow_body
    assert "api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest" in workflow_body
    assert f'test("{asset_pattern}")' in workflow_body
    assert "[.browser_download_url, .digest]" in workflow_body
    assert '[[ ! "$asset_digest" =~ ^sha256:[0-9a-f]{64}$ ]]' in workflow_body
    assert "sha256sum --check --strict -" in workflow_body
    assert workflow_body.count("--retry-all-errors") >= 3
    assert "BtbN/FFmpeg-Builds/releases/download/latest/" not in workflow_body
    assert "BtbN/FFmpeg-Builds/releases/download/autobuild-" not in workflow_body
    assert "matching HA addon runtime" not in workflow_body


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "runtime_health": {
                    "queue_empty_elapsed_s": 0,
                    "silence_with_listeners": False,
                }
            },
            "boolean session_stopped",
        ),
        (
            {
                "session_stopped": "false",
                "runtime_health": {
                    "queue_empty_elapsed_s": 0,
                    "silence_with_listeners": False,
                },
            },
            "boolean session_stopped",
        ),
        ({"session_stopped": False}, "object runtime_health"),
        ({"session_stopped": False, "runtime_health": []}, "object runtime_health"),
        (
            {
                "session_stopped": False,
                "runtime_health": {"silence_with_listeners": False},
            },
            "numeric queue_empty_elapsed_s",
        ),
        (
            {
                "session_stopped": False,
                "runtime_health": {
                    "queue_empty_elapsed_s": "0",
                    "silence_with_listeners": False,
                },
            },
            "numeric queue_empty_elapsed_s",
        ),
        (
            {
                "session_stopped": False,
                "runtime_health": {"queue_empty_elapsed_s": 0},
            },
            "boolean silence_with_listeners",
        ),
        (
            {
                "session_stopped": False,
                "runtime_health": {
                    "queue_empty_elapsed_s": 0,
                    "silence_with_listeners": 0,
                },
            },
            "boolean silence_with_listeners",
        ),
    ],
)
def test_perf_smoke_rejects_missing_or_wrong_typed_public_status_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict,
    message: str,
) -> None:
    smoke = _load_perf_smoke()
    monkeypatch.setattr(smoke, "_fetch_json", lambda _path: (200, payload))

    with pytest.raises(SystemExit, match="1"):
        smoke._check_public_status(expect_stopped=False)

    assert message in capsys.readouterr().err


def test_launch_seed_marker_matches_runtime_recovery_marker() -> None:
    launch_body = LAUNCH_SMOKE.read_text(encoding="utf-8")
    run_body = ADDON_RUN_SCRIPT.read_text(encoding="utf-8")

    marker = ".provider_recovery_checked_v2"
    assert marker in launch_body
    assert marker in run_body
    assert '(data_dir / ".provider_recovery_checked")' not in launch_body
