from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECK_COMMIT_MSG = ROOT / "scripts" / "check-commit-msg.sh"
CHECK_VERSION_SYNC = ROOT / "scripts" / "check-version-sync.sh"
CHECK_CHANGELOG_SYNC = ROOT / "scripts" / "check-changelog-sync.sh"
CHECK_CHANGELOG_LINT = ROOT / "scripts" / "check-changelog-lint.sh"
PRE_RELEASE_CHECK = ROOT / "scripts" / "pre-release-check.sh"
VALIDATE_ADDON = ROOT / "scripts" / "validate-addon.sh"
TEST_ADDON_LOCAL = ROOT / "scripts" / "test-addon-local.sh"
HA_GREEN_PERF_SMOKE = ROOT / "scripts" / "ha-green-perf-smoke.py"
HA_GREEN_LAUNCH_SMOKE = ROOT / "scripts" / "ha-green-launch-smoke.py"


def _run(cmd: list[str], cwd: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _init_git_repo(path: Path) -> None:
    _run(["git", "init", "-q"], cwd=path)
    _run(["git", "config", "user.email", "tests@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    _run(["git", "config", "core.hooksPath", "/dev/null"], cwd=path)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_release_check_repo(
    tmp_path: Path,
    *,
    version: str = "1.1.0",
    manifest_version: str | None = "1.1.0",
    addon_changelog: str = "# Changelog\n\n## Unreleased\n\n## 1.1.0\n",
) -> None:
    """Minimal repo layout that scripts/pre-release-check.sh inspects, in a state that
    passes every check. Override one field to exercise a single gate."""
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", f"version: {version}\n")
    _write(tmp_path / "pyproject.toml", f'[project]\nname = "mammamiradio"\nversion = "{version}"\n')
    if manifest_version is not None:
        _write(
            tmp_path / "custom_components/mammamiradio/manifest.json",
            '{\n  "domain": "mammamiradio",\n  "version": "' + manifest_version + '"\n}\n',
        )
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", addon_changelog)
    _write(
        tmp_path / "mammamiradio/audio/normalizer.py",
        'music_eq_chain = (\n    "equalizer=f=200"\n    "equalizer=f=3000"\n)\n',
    )
    _write(tmp_path / "mammamiradio/web/streamer.py", "QUEUE_FALLBACK_WAIT_SECONDS = 5.0\n")
    _write(tmp_path / "tests/test_fallback.py", "_pick_canned_clip return_value=None\nsession_stopped\n")
    _write(
        tmp_path / "Makefile",
        "perf-smoke:\n\tpython scripts/ha-green-perf-smoke.py\n"
        "launch-smoke:\n\tpython scripts/ha-green-launch-smoke.py\n",
    )
    _write(tmp_path / "scripts/ha-green-perf-smoke.py", "#!/usr/bin/env python3\n")
    os.chmod(tmp_path / "scripts/ha-green-perf-smoke.py", 0o755)
    _write(tmp_path / "scripts/ha-green-launch-smoke.py", "#!/usr/bin/env python3\n")
    os.chmod(tmp_path / "scripts/ha-green-launch-smoke.py", 0o755)


def _load_ha_green_perf_smoke() -> types.ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ha_green_perf_smoke", HA_GREEN_PERF_SMOKE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ha_green_launch_smoke() -> types.ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("ha_green_launch_smoke", HA_GREEN_LAUNCH_SMOKE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_commit_msg_accepts_conventional_prefix(tmp_path: Path) -> None:
    msg_file = tmp_path / "message.txt"
    _write(msg_file, "fix(addon): enforce staged version sync\n")

    result = _run(["bash", str(CHECK_COMMIT_MSG), str(msg_file)], cwd=tmp_path)

    assert result.returncode == 0


def test_check_commit_msg_rejects_non_conventional_prefix(tmp_path: Path) -> None:
    msg_file = tmp_path / "message.txt"
    _write(msg_file, "update addon hook docs\n")

    result = _run(["bash", str(CHECK_COMMIT_MSG), str(msg_file)], cwd=tmp_path)

    assert result.returncode == 1
    assert "Commit message must start with a conventional prefix" in result.stdout


def test_check_version_sync_uses_staged_versions(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(
        tmp_path / "custom_components/mammamiradio/manifest.json",
        '{\n  "domain": "mammamiradio",\n  "version": "1.0.0"\n}\n',
    )
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "custom_components/mammamiradio/manifest.json",
        ],
        cwd=tmp_path,
    )
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml"], cwd=tmp_path)
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')

    result = _run(["bash", str(CHECK_VERSION_SYNC)], cwd=tmp_path)

    assert result.returncode == 1
    assert "ERROR: Version mismatch!" in result.stdout
    assert "ha-addon/mammamiradio/config.yaml: 1.1.0" in result.stdout
    assert "pyproject.toml: 1.0.0" in result.stdout


def test_check_version_sync_passes_when_index_matches(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(
        tmp_path / "custom_components/mammamiradio/manifest.json",
        '{\n  "domain": "mammamiradio",\n  "version": "1.0.0"\n}\n',
    )
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "custom_components/mammamiradio/manifest.json",
        ],
        cwd=tmp_path,
    )
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(
        tmp_path / "custom_components/mammamiradio/manifest.json",
        '{\n  "domain": "mammamiradio",\n  "version": "1.1.0"\n}\n',
    )
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "custom_components/mammamiradio/manifest.json",
        ],
        cwd=tmp_path,
    )

    result = _run(["bash", str(CHECK_VERSION_SYNC)], cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""


def test_check_version_sync_detects_manifest_drift(tmp_path: Path) -> None:
    # The HACS integration manifest rides the release number; the pre-commit hook must
    # catch a bump that leaves it behind, not only a config.yaml ↔ pyproject.toml mismatch.
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(
        tmp_path / "custom_components/mammamiradio/manifest.json",
        '{\n  "domain": "mammamiradio",\n  "version": "1.0.0"\n}\n',
    )
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "custom_components/mammamiradio/manifest.json",
        ],
        cwd=tmp_path,
    )
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    # Bump config + pyproject to 1.1.0 but leave the manifest at 1.0.0 in the index.
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml", "pyproject.toml"], cwd=tmp_path)

    result = _run(["bash", str(CHECK_VERSION_SYNC)], cwd=tmp_path)

    assert result.returncode == 1
    assert "ERROR: Version mismatch!" in result.stdout
    assert "custom_components/mammamiradio/manifest.json: 1.0.0" in result.stdout


def test_check_version_sync_requires_parseable_manifest_version(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(tmp_path / "custom_components/mammamiradio/manifest.json", '{\n  "domain": "mammamiradio"\n}\n')
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "custom_components/mammamiradio/manifest.json",
        ],
        cwd=tmp_path,
    )

    result = _run(["bash", str(CHECK_VERSION_SYNC)], cwd=tmp_path)

    assert result.returncode == 1
    assert "ERROR: Could not parse version from staged files." in result.stdout
    assert "custom_components/mammamiradio/manifest.json: <missing>" in result.stdout


def test_pre_commit_registers_version_sync_hook() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text()

    assert "id: version-sync" in config
    assert "entry: scripts/check-version-sync.sh" in config
    assert "stages: [pre-commit]" in config


def test_check_changelog_sync_requires_both_changelogs_on_version_bump(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n")
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n## [1.1.0]\n")
    _run(["git", "add", "ha-addon/mammamiradio/config.yaml", "pyproject.toml", "CHANGELOG.md"], cwd=tmp_path)

    result = _run(["bash", str(CHECK_CHANGELOG_SYNC)], cwd=tmp_path)

    assert result.returncode == 1
    assert "ha-addon/mammamiradio/CHANGELOG.md" in result.stdout


def test_check_changelog_sync_passes_when_both_changelogs_staged(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.0.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.0.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n")
    _run(["git", "add", "."], cwd=tmp_path)
    _run(["git", "commit", "-qm", "init"], cwd=tmp_path)

    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", 'version: "1.1.0"\n')
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n## [1.1.0]\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n## 1.1.0\n")
    _run(
        [
            "git",
            "add",
            "ha-addon/mammamiradio/config.yaml",
            "pyproject.toml",
            "CHANGELOG.md",
            "ha-addon/mammamiradio/CHANGELOG.md",
        ],
        cwd=tmp_path,
    )

    result = _run(["bash", str(CHECK_CHANGELOG_SYNC)], cwd=tmp_path)

    assert result.returncode == 0


def test_check_changelog_lint_rejects_digit_phase_and_track_labels(tmp_path: Path) -> None:
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n- Phase 1 shipped.\n")
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n- Track B shipped.\n")

    result = _run(["bash", str(CHECK_CHANGELOG_LINT)], cwd=tmp_path)

    assert result.returncode == 1
    assert r"\bPhase [0-9]+\b" in result.stdout
    assert r"\bTrack [A-Z]\b" in result.stdout


def test_pre_release_check_skips_unreleased_addon_changelog_heading(tmp_path: Path) -> None:
    _write_release_check_repo(tmp_path)

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode == 0
    assert "CHANGELOG latest version (## 1.1.0) matches config.yaml (1.1.0)" in result.stdout
    assert "manifest.json (1.1.0) matches config.yaml (1.1.0)" in result.stdout


def test_pre_release_check_fails_on_manifest_version_mismatch(tmp_path: Path) -> None:
    # The HACS integration manifest must ride the release number (docs/release-process.md).
    _write_release_check_repo(tmp_path, version="1.1.0", manifest_version="1.0.0")

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "manifest.json version is '1.0.0' but config.yaml is 1.1.0" in result.stdout


def test_pre_release_check_fails_cleanly_on_unreadable_manifest(tmp_path: Path) -> None:
    # Malformed manifest must produce a clean [FAIL], never a Python traceback that
    # aborts the release gate.
    _write_release_check_repo(tmp_path, manifest_version=None)
    _write(tmp_path / "custom_components/mammamiradio/manifest.json", "{ not valid json ")

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "manifest.json version is 'unreadable'" in result.stdout


def test_pre_release_check_accepts_dated_addon_changelog_heading(tmp_path: Path) -> None:
    # Guards the dated-header parse: "## 1.1.0 - 2026-06-21" reduces to "1.1.0".
    _write_release_check_repo(
        tmp_path,
        addon_changelog="# Changelog\n\n## Unreleased\n\n## 1.1.0 - 2026-06-21\n",
    )

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode == 0
    assert "CHANGELOG latest version (## 1.1.0) matches config.yaml (1.1.0)" in result.stdout


def test_ha_green_perf_smoke_script_has_runtime_quality_gates() -> None:
    body = HA_GREEN_PERF_SMOKE.read_text()

    assert "MAMMAMIRADIO_PERF_BASE_URL" in body
    assert "MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S" in body
    assert "MAX_QUEUE_EMPTY_S" in body
    assert "status=failing" in body
    assert "silence_with_listeners" in body
    assert "queue_empty_elapsed_s" in body
    assert "/stream" in body


def test_ha_green_launch_smoke_is_cold_start_strict() -> None:
    body = HA_GREEN_LAUNCH_SMOKE.read_text()

    # Launches a real process (the perf smoke does not) ...
    assert "uvicorn" in body
    assert "mammamiradio.main:app" in body
    # ... on throwaway temp cache/tmp so no warm state leaks in ...
    assert "TemporaryDirectory" in body
    assert "MAMMAMIRADIO_CACHE_DIR" in body
    # ... and asserts a STRICT first-byte bound (default 2s, not the perf
    # smoke's 8s already-running budget).
    assert "MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S" in body
    assert '"2.0"' in body
    assert "MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S" in body


@pytest.mark.parametrize(
    ("env_name", "env_value", "message"),
    [
        ("MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S", "soon", "must be a float in seconds"),
        ("MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S", "nan", "must be a finite positive float in seconds"),
        ("MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S", "inf", "must be a finite positive float in seconds"),
        ("MAMMAMIRADIO_LAUNCH_STARTUP_S", "soon", "must be a float in seconds"),
        ("MAMMAMIRADIO_LAUNCH_STARTUP_S", "nan", "must be a finite positive float in seconds"),
        ("MAMMAMIRADIO_LAUNCH_STARTUP_S", "inf", "must be a finite positive float in seconds"),
    ],
)
def test_ha_green_launch_smoke_validates_timeout_env_vars(
    monkeypatch: pytest.MonkeyPatch, env_name: str, env_value: str, message: str
) -> None:
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(RuntimeError, match=f"{env_name} {message}"):
        _load_ha_green_launch_smoke()


def test_ha_green_launch_smoke_reports_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    smoke = _load_ha_green_launch_smoke()

    def missing_ffmpeg(*_args, **_kwargs) -> None:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(smoke.subprocess, "run", missing_ffmpeg)

    with pytest.raises(RuntimeError, match=r"ffmpeg is required for scripts/ha-green-launch-smoke\.py"):
        smoke._seed_warm_norm_cache(str(tmp_path))


def test_makefile_has_launch_smoke_target() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "launch-smoke:" in makefile
    assert "ha-green-launch-smoke.py" in makefile


def test_ha_green_perf_smoke_allows_readyz_starting_response() -> None:
    smoke = _load_ha_green_perf_smoke()

    smoke._assert_not_silence_failure(
        "/readyz",
        503,
        {"status": "starting", "silence_with_listeners": False, "queue_empty_elapsed_s": 0},
        allow_starting=True,
    )


def test_ha_green_perf_smoke_rejects_unexpected_readyz_500() -> None:
    smoke = _load_ha_green_perf_smoke()

    try:
        smoke._assert_not_silence_failure(
            "/readyz",
            500,
            {"status": "error", "silence_with_listeners": False, "queue_empty_elapsed_s": 0},
            allow_starting=True,
        )
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("unexpected /readyz 500 must fail the smoke gate")


def test_release_invariants_guard_ha_green_perf_budget() -> None:
    release_body = (ROOT / "scripts" / "check-release-invariants.sh").read_text()
    pre_release_body = (ROOT / "scripts" / "pre-release-check.sh").read_text()

    for body in (release_body, pre_release_body):
        assert "QUEUE_FALLBACK_WAIT_SECONDS" in body
        assert "norm_files\\[0\\]" in body
        assert "ha-green-perf-smoke.py" in body
        assert "ha-green-launch-smoke.py" in body
        assert "launch-smoke:" in body


def test_check_changelog_lint_rejects_internal_process_phrases(tmp_path: Path) -> None:
    _write(
        tmp_path / "CHANGELOG.md",
        "\n".join(
            [
                "# Changelog",
                "",
                "## [Unreleased]",
                "",
                "- CLAUDE.md documented how red tests ride green.",
                "- The earlier patch informed the later cleanup.",
                "",
            ]
        ),
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/CHANGELOG.md",
        "\n".join(
            [
                "# Changelog",
                "",
                "## Unreleased",
                "",
                "- Conductor setup fails when a contributor workflow was superseded.",
                "",
            ]
        ),
    )

    result = _run(["bash", str(CHECK_CHANGELOG_LINT)], cwd=tmp_path)

    assert result.returncode == 1
    assert r"\bCLAUDE\.md\b" in result.stdout
    assert r"\bred tests ride green\b" in result.stdout
    assert r"\binformed the later\b" in result.stdout
    assert r"\bConductor setup fails\b" in result.stdout
    assert r"\bsuperseded\b" in result.stdout


def _create_validate_addon_repo(
    tmp_path: Path,
    *,
    streamer_body: str,
    broken_dotvenv_python: bool = False,
    web_module: str = "mammamiradio/web/pages.py",
) -> dict[str, str]:
    _init_git_repo(tmp_path)
    _run(["git", "remote", "add", "origin", "https://github.com/florianhorner/fakeitaliradio.git"], cwd=tmp_path)

    _write(
        tmp_path / "ha-addon/mammamiradio/config.yaml",
        "\n".join(
            [
                'version: "1.1.0"',
                "stage: stable",
                "image: ghcr.io/florianhorner/mammamiradio-addon-{arch}",
                "timeout: 300",
                "host_network: true",
                "ingress_port: 8000",
                "options:",
                '  anthropic_api_key: ""',
                '  openai_api_key: ""',
                '  station_name: "Test"',
                '  quality_profile: "balanced"',
                "schema:",
                "  anthropic_api_key: password?",
                "  openai_api_key: password?",
                "  station_name: str?",
                "  quality_profile: list(premium|balanced|economy)?",
                "",
            ]
        ),
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/rootfs/run.sh",
        "\n".join(
            [
                "#!/usr/bin/env sh",
                'export MAMMAMIRADIO_PORT="8000"',
                "anthropic_api_key=${anthropic_api_key:-}",
                "openai_api_key=${openai_api_key:-}",
                "station_name=${station_name:-}",
                "quality_profile=${quality_profile:-}",
                "",
            ]
        ),
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/Dockerfile",
        "\n".join(
            [
                "ARG BUILD_FROM=scratch",
                "FROM ${BUILD_FROM}",
                "COPY app /app",
                "ARG BUILD_VERSION",
                "ARG BUILD_ARCH",
                "LABEL \\",
                '  io.hass.version="${BUILD_VERSION}" \\',
                '  io.hass.type="app" \\',
                '  io.hass.arch="${BUILD_ARCH}"',
                "",
            ]
        ),
    )
    _write(tmp_path / "ha-addon/mammamiradio/build.yaml", "build_from: {}\n")
    _write(
        tmp_path / "ha-addon/mammamiradio/apparmor.txt",
        "#include <tunables/global>\n\nprofile mammamiradio flags=(attach_disconnected,mediate_deleted) {\n}\n",
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/translations/en.yaml",
        "\n".join(
            [
                "configuration:",
                "  anthropic_api_key: key",
                "  openai_api_key: key",
                "  station_name: key",
                "  quality_profile: key",
                "",
            ]
        ),
    )
    _write(tmp_path / "mammamiradio/__init__.py", "")
    _write(tmp_path / web_module, streamer_body)
    _write(tmp_path / "radio.toml", "[station]\nname = 'Test'\n")
    _write(tmp_path / "ha-addon/mammamiradio/radio.toml", "[station]\nname = 'Test'\n")
    _write(tmp_path / "pyproject.toml", '[project]\nname = "mammamiradio"\nversion = "1.1.0"\n')
    _write(tmp_path / "repository.yaml", "name: test\n")

    if broken_dotvenv_python:
        _write(tmp_path / ".venv/bin/python3", "#!/usr/bin/env sh\nexit 1\n")
        os.chmod(tmp_path / ".venv/bin/python3", 0o755)

    bin_dir = tmp_path / "bin"
    _write(bin_dir / "gh", "#!/usr/bin/env sh\nexit 1\n")
    os.chmod(bin_dir / "gh", 0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{Path(sys.executable).parent}:{env['PATH']}"
    return env


def test_validate_addon_rejects_single_quoted_js_rewrites(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace("'/api/hosts'", f"'{prefix}/api/hosts'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "rewrites single-quoted JS path" in result.stdout


def test_validate_addon_allows_service_worker_rewrite(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace("'/sw.js'", f"'{prefix}/sw.js'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "Ingress prefix injection only rewrites safe patterns" in result.stdout


def test_validate_addon_resolves_owner_from_repository_yaml_without_remote(tmp_path: Path) -> None:
    """No git remote + no gh: the expected image owner must fall back to
    repository.yaml's url, not an empty owner. Regression for the line-86 bug
    where `sed` exiting 0 on empty input left OWNER="" → ghcr.io//... mismatch.
    """
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
    )
    # Simulate a fresh/no-remote worktree: drop origin (gh is already stubbed to
    # exit 1 by the helper), and have repository.yaml carry the canonical owner.
    _run(["git", "remote", "remove", "origin"], cwd=tmp_path)
    _write(tmp_path / "repository.yaml", "name: test\nurl: https://github.com/florianhorner/mammamiradio\n")

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert "ghcr.io//mammamiradio-addon-{arch}" not in result.stdout
    assert "Image path: ghcr.io/florianhorner/mammamiradio-addon-{arch}" in result.stdout
    assert result.returncode == 0


def test_validate_addon_rejects_options_schema_order_mismatch(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/config.yaml",
        "\n".join(
            [
                'version: "1.1.0"',
                "image: ghcr.io/florianhorner/mammamiradio-addon-{arch}",
                "timeout: 300",
                "host_network: true",
                "ingress_port: 8000",
                "options:",
                '  anthropic_api_key: ""',
                '  openai_api_key: ""',
                '  station_name: "Test"',
                '  quality_profile: "balanced"',
                "schema:",
                "  station_name: str?",
                "  anthropic_api_key: password?",
                "  openai_api_key: password?",
                "  quality_profile: list(premium|balanced|economy)?",
                "",
            ]
        ),
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "options and schema key order differ" in result.stdout


def test_validate_addon_falls_back_when_dotvenv_python_is_broken(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
        broken_dotvenv_python=True,
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "radio.toml is valid TOML" in result.stdout


def test_validate_addon_discovers_helper_in_alternate_module(tmp_path: Path) -> None:
    # Helper lives outside pages.py — discovery must still find and pass it.
    env = _create_validate_addon_repo(
        tmp_path,
        web_module="mammamiradio/web/ingress.py",
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    html = html.replace("'/sw.js'", f"'{prefix}/sw.js'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "Ingress prefix injection only rewrites safe patterns" in result.stdout


def test_validate_addon_rejects_unsafe_rewrite_in_alternate_module(tmp_path: Path) -> None:
    # An unsafe rewrite must fail no matter which web/*.py module holds it.
    env = _create_validate_addon_repo(
        tmp_path,
        web_module="mammamiradio/web/ingress.py",
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace("'/api/hosts'", f"'{prefix}/api/hosts'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "rewrites single-quoted JS path" in result.stdout


def test_validate_addon_scans_all_modules_for_unsafe_rewrite(tmp_path: Path) -> None:
    # Two definitions across modules: a safe one in pages.py and a stale unsafe
    # copy in streamer.py. Scan-all must fail (a first-match-only scan would
    # wrongly pass on the alphabetically-earlier safe pages.py).
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
    )
    _write(
        tmp_path / "mammamiradio/web/streamer.py",
        """
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace("'/api/hosts'", f"'{prefix}/api/hosts'")
    return html
""".strip()
        + "\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "rewrites single-quoted JS path" in result.stdout


def test_validate_addon_warns_when_no_helper_anywhere(tmp_path: Path) -> None:
    # No _inject_ingress_prefix in any web/*.py — the discovery path must emit a
    # soft warning (the "none" sentinel), not a hard failure.
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="# no ingress helper defined in this module\n",
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "No _inject_ingress_prefix found" in result.stdout


def test_validate_addon_fails_when_web_dir_missing(tmp_path: Path) -> None:
    # mammamiradio/web absent entirely — the safety check cannot run and must fail.
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    return html
""".strip()
        + "\n",
    )
    shutil.rmtree(tmp_path / "mammamiradio/web")

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "Missing mammamiradio/web" in result.stdout


def test_validate_addon_skips_unparseable_module_during_discovery(tmp_path: Path) -> None:
    # A syntax error in an unrelated web/*.py module must be skipped, not treated
    # as an ingress-safety failure: the scan still discovers the safe helper in
    # another module and passes (the D2 per-file parse-isolation branch).
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
    )
    _write(tmp_path / "mammamiradio/web/broken.py", "def (:\n")

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "Ingress prefix injection only rewrites safe patterns" in result.stdout


def test_test_addon_local_delegates_to_validate_addon() -> None:
    wrapper = TEST_ADDON_LOCAL.read_text()

    assert "Compatibility wrapper" in wrapper
    assert 'exec "$ROOT/scripts/validate-addon.sh" "$@"' in wrapper


def test_validate_addon_build_passes_home_assistant_label_args() -> None:
    validator = VALIDATE_ADDON.read_text()

    assert '--build-arg BUILD_VERSION="$ADDON_VER"' in validator
    assert '--build-arg BUILD_ARCH="$BUILD_ARCH"' in validator


def test_validate_addon_rejects_dockerfile_missing_hass_labels(tmp_path: Path) -> None:
    """validate-addon.sh must exit non-zero when the Dockerfile lacks io.hass.* labels.

    This is the negative gate for check 11 (Dockerfile safety). A Dockerfile
    that ships without these labels cannot be discovered by the HA Supervisor and
    will silently fail to register the add-on version.
    """
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="""
def _inject_ingress_prefix(html: str, prefix: str) -> str:
    html = html.replace('href="/listen"', f'href="{prefix}/listen"')
    return html
""".strip()
        + "\n",
    )
    # Overwrite Dockerfile with one that has no io.hass.* labels.
    _write(
        tmp_path / "ha-addon/mammamiradio/Dockerfile",
        "\n".join(
            [
                "ARG BUILD_FROM=scratch",
                "FROM ${BUILD_FROM}",
                "COPY app /app",
                "",
            ]
        ),
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "Dockerfile missing required Home Assistant image label" in result.stdout


def test_addon_dockerfile_does_not_drop_root_before_supervisor_mounts() -> None:
    dockerfile = (ROOT / "ha-addon" / "mammamiradio" / "Dockerfile").read_text()

    assert "USER radio" not in dockerfile


def test_port_is_consistent_across_addon_files() -> None:
    """config.yaml ingress_port, run.sh MAMMAMIRADIO_PORT, and config.py default must all match.

    Port drift across these three files causes silent HA add-on breakage:
    the supervisor health-check points at one port, uvicorn binds another.
    """
    import re

    config_yaml = (ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    run_sh = (ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()
    config_py = (ROOT / "mammamiradio" / "core" / "config.py").read_text()

    # ingress_port: 8000
    ingress_match = re.search(r"^ingress_port:\s*(\d+)", config_yaml, re.MULTILINE)
    assert ingress_match, "ingress_port not found in config.yaml"
    ingress_port = ingress_match.group(1)

    # export MAMMAMIRADIO_PORT="8000"
    env_match = re.search(r'MAMMAMIRADIO_PORT=["\']?(\d+)["\']?', run_sh)
    assert env_match, "MAMMAMIRADIO_PORT not found in run.sh"
    env_port = env_match.group(1)

    # --port 8000  (uvicorn CLI flag)
    uvicorn_match = re.search(r"--port\s+(\d+)", run_sh)
    assert uvicorn_match, "--port not found in run.sh"
    uvicorn_port = uvicorn_match.group(1)

    # port: int = 8000  (config.py dataclass default)
    py_match = re.search(r"port:\s*int\s*=\s*(\d+)", config_py)
    assert py_match, "port default not found in config.py"
    py_port = py_match.group(1)

    assert ingress_port == env_port == uvicorn_port == py_port, (
        f"Port mismatch: config.yaml ingress_port={ingress_port}, "
        f"run.sh MAMMAMIRADIO_PORT={env_port}, "
        f"run.sh --port={uvicorn_port}, "
        f"config.py default={py_port}"
    )


def test_dockerfile_port_matches_config() -> None:
    """Standalone Dockerfile ENV, EXPOSE, and CMD --port must match config.py default.

    The Dockerfile hardcodes port in three places independently of the HA addon.
    If someone bumps the default port in config.py without updating the Dockerfile,
    the standalone container silently binds the wrong port.
    """
    import re

    dockerfile = (ROOT / "Dockerfile").read_text()
    config_py = (ROOT / "mammamiradio" / "core" / "config.py").read_text()

    # ENV MAMMAMIRADIO_PORT=8000
    env_match = re.search(r"ENV MAMMAMIRADIO_PORT=(\d+)", dockerfile)
    assert env_match, "ENV MAMMAMIRADIO_PORT not found in Dockerfile"
    env_port = env_match.group(1)

    # EXPOSE 8000
    expose_match = re.search(r"^EXPOSE\s+(\d+)", dockerfile, re.MULTILINE)
    assert expose_match, "EXPOSE not found in Dockerfile"
    expose_port = expose_match.group(1)

    # CMD [..., "--port", "8000"]
    cmd_match = re.search(r"--port[\"',\s]+(\d+)", dockerfile)
    assert cmd_match, "--port not found in Dockerfile CMD"
    cmd_port = cmd_match.group(1)

    # port: int = 8000  (config.py dataclass default)
    py_match = re.search(r"port:\s*int\s*=\s*(\d+)", config_py)
    assert py_match, "port default not found in config.py"
    py_port = py_match.group(1)

    assert env_port == expose_port == cmd_port == py_port, (
        f"Port mismatch in Dockerfile vs config.py: "
        f"ENV MAMMAMIRADIO_PORT={env_port}, "
        f"EXPOSE={expose_port}, "
        f"CMD --port={cmd_port}, "
        f"config.py default={py_port}"
    )
