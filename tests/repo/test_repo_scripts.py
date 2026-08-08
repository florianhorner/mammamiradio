from __future__ import annotations

import hashlib
import json
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
ADDON_BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "addon-build.yml"
TEST_ADDON_LOCAL = ROOT / "scripts" / "test-addon-local.sh"
HA_GREEN_PERF_SMOKE = ROOT / "scripts" / "ha-green-perf-smoke.py"
HA_GREEN_LAUNCH_SMOKE = ROOT / "scripts" / "ha-green-launch-smoke.py"
REQUIRED_RECOVERY_ASSETS = ("continuity_1.mp3", "emergency_tone.mp3")


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
    root_changelog: str | None = None,
) -> None:
    """Minimal repo layout that scripts/pre-release-check.sh inspects, in a state that
    passes every check. Override one field to exercise a single gate.

    Both changelogs are written because the cut folds both and the script checks
    both. `root_changelog` defaults to a bracketed heading at `version`, matching
    the root file's house style."""
    _write(tmp_path / "ha-addon/mammamiradio/config.yaml", f"version: {version}\n")
    _write(tmp_path / "pyproject.toml", f'[project]\nname = "mammamiradio"\nversion = "{version}"\n')
    if manifest_version is not None:
        _write(
            tmp_path / "custom_components/mammamiradio/manifest.json",
            '{\n  "domain": "mammamiradio",\n  "version": "' + manifest_version + '"\n}\n',
        )
    _write(tmp_path / "ha-addon/mammamiradio/CHANGELOG.md", addon_changelog)
    if root_changelog is None:
        root_changelog = f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-01-01\n"
    _write(tmp_path / "CHANGELOG.md", root_changelog)
    _write(
        tmp_path / "mammamiradio/audio/normalizer.py",
        'music_eq_chain = (\n    "equalizer=f=200"\n    "equalizer=f=3000"\n)\n',
    )
    _write(tmp_path / "mammamiradio/web/streamer.py", "QUEUE_FALLBACK_WAIT_SECONDS = 5.0\n")
    _write(tmp_path / "mammamiradio/scheduling/producer.py", "# producer recovery ladder\n")
    demo_assets = ROOT / "mammamiradio/assets/demo"
    spoken_manifest_path = demo_assets / "spoken_assets.json"
    spoken_manifest = json.loads(spoken_manifest_path.read_text(encoding="utf-8"))
    for entry in spoken_manifest["assets"]:
        asset_path = Path(entry["path"])
        target = tmp_path / "mammamiradio/assets/demo" / asset_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(demo_assets / asset_path, target)
    shutil.copy2(spoken_manifest_path, tmp_path / "mammamiradio/assets/demo/spoken_assets.json")
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


@pytest.fixture
def fake_ffprobe_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep repository-script tests independent of host multimedia packages."""

    bin_dir = tmp_path / ".test-bin"
    ffprobe = bin_dir / "ffprobe"
    _write(
        ffprobe,
        """#!/usr/bin/env python3
from pathlib import Path
import sys

payload = Path(sys.argv[-1]).read_bytes()
is_mp3 = payload.startswith(b"ID3") or (
    len(payload) >= 2 and payload[0] == 0xFF and payload[1] & 0xE0 == 0xE0
)
if not is_mp3:
    print("invalid audio", file=sys.stderr)
    raise SystemExit(1)
print("audio")
""",
    )
    ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


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


def test_pre_release_check_skips_unreleased_addon_changelog_heading(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    _write_release_check_repo(tmp_path)

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode == 0
    assert "CHANGELOG latest version (## 1.1.0) matches config.yaml (1.1.0)" in result.stdout
    assert "manifest.json (1.1.0) matches config.yaml (1.1.0)" in result.stdout


def test_pre_release_check_fails_on_manifest_version_mismatch(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    # The HACS integration manifest must ride the release number (docs/release-process.md).
    _write_release_check_repo(tmp_path, version="1.1.0", manifest_version="1.0.0")

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "manifest.json version is '1.0.0' but config.yaml is 1.1.0" in result.stdout


def test_pre_release_check_fails_cleanly_on_unreadable_manifest(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    # Malformed manifest must produce a clean [FAIL], never a Python traceback that
    # aborts the release gate.
    _write_release_check_repo(tmp_path, manifest_version=None)
    _write(tmp_path / "custom_components/mammamiradio/manifest.json", "{ not valid json ")

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "manifest.json version is 'unreadable'" in result.stdout


def test_pre_release_check_accepts_dated_addon_changelog_heading(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    # Guards the dated-header parse: "## 1.1.0 - 2026-06-21" reduces to "1.1.0".
    _write_release_check_repo(
        tmp_path,
        addon_changelog="# Changelog\n\n## Unreleased\n\n## 1.1.0 - 2026-06-21\n",
    )

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode == 0
    assert "CHANGELOG latest version (## 1.1.0) matches config.yaml (1.1.0)" in result.stdout


def test_pre_release_check_rejects_stale_root_changelog_heading(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    """A cut that folds the ha-addon changelog but not the root one must fail here.

    addon-release.yml's tag pre-flight also checks the root file, but that fires
    inside the open cut window: the mismatch would pass every pre-merge gate,
    merge, leave main advertising an unpublished image, and only then fail. This
    moves the failure to the cut PR, where the fix is an edit not a revert.
    """
    _write_release_check_repo(
        tmp_path,
        root_changelog="# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n",
    )

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "root CHANGELOG latest version is ## 1.0.0 but config.yaml is 1.1.0" in result.stdout


def test_pre_release_check_accepts_bracketed_root_changelog_heading(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    """The root file's bracketed house style must satisfy the same extractor."""
    _write_release_check_repo(
        tmp_path,
        root_changelog="# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2026-06-21\n",
    )

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode == 0
    assert "root CHANGELOG latest version (## 1.1.0) matches config.yaml (1.1.0)" in result.stdout


def test_pre_release_check_requires_each_recovery_asset(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    _write_release_check_repo(tmp_path)
    (tmp_path / "mammamiradio/assets/demo/recovery/emergency_tone.mp3").unlink()

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "emergency_tone.mp3" in result.stdout


def test_pre_release_check_rejects_recovery_asset_hash_drift(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    _write_release_check_repo(tmp_path)
    asset = tmp_path / "mammamiradio/assets/demo/recovery/emergency_tone.mp3"
    asset.write_bytes(asset.read_bytes() + b"tampered")

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "packaged spoken-asset manifest/hash/transcript validation failed" in result.stdout
    assert "recovery/emergency_tone.mp3 sha256 does not match" in result.stderr


def test_pre_release_check_rejects_manifest_approved_non_audio(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    _write_release_check_repo(tmp_path)
    asset = tmp_path / "mammamiradio/assets/demo/recovery/emergency_tone.mp3"
    payload = b"x" * 2048
    asset.write_bytes(payload)
    manifest_path = tmp_path / "mammamiradio/assets/demo/spoken_assets.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["assets"] if item["path"] == "recovery/emergency_tone.mp3")
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "emergency_tone.mp3 is not ffprobe-readable audio" in result.stdout


def test_pre_release_check_rejects_manifest_approved_unsafe_transcript(
    tmp_path: Path,
    fake_ffprobe_on_path: None,
) -> None:
    """The release gate must enforce listener truth, not only asset paths and hashes."""

    _write_release_check_repo(tmp_path)
    manifest_path = tmp_path / "mammamiradio/assets/demo/spoken_assets.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["assets"] if item["path"] == "recovery/continuity_1.mp3")
    audio_path = tmp_path / "mammamiradio/assets/demo/recovery/continuity_1.mp3"
    assert entry["sha256"] == hashlib.sha256(audio_path.read_bytes()).hexdigest()
    entry["transcript"] = "Bentornato, ti stavamo aspettando."
    manifest_path.write_text(json.dumps(manifest))

    result = _run(["bash", str(PRE_RELEASE_CHECK)], cwd=tmp_path)

    assert result.returncode != 0
    assert "packaged spoken-asset manifest/hash/transcript validation failed" in result.stdout
    assert "transcript contains listener arrival/return copy" in result.stderr


def test_ha_green_perf_smoke_script_has_runtime_quality_gates() -> None:
    body = HA_GREEN_PERF_SMOKE.read_text()

    assert "MAMMAMIRADIO_PERF_BASE_URL" in body
    assert "MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S" in body
    assert "MAX_QUEUE_EMPTY_S" in body
    assert "status=failing" in body
    assert "silence_with_listeners" in body
    assert "queue_empty_elapsed_s" in body
    assert "/stream" in body


def test_ha_green_launch_smoke_covers_warm_and_cold_offline_starts() -> None:
    body = HA_GREEN_LAUNCH_SMOKE.read_text()

    # Launches a real process (the perf smoke does not) ...
    assert "uvicorn" in body
    assert "mammamiradio.main:app" in body
    # ... on throwaway state, once with warm norm-cache music and once with
    # only packaged recovery audio ...
    assert "TemporaryDirectory" in body
    assert "MAMMAMIRADIO_CACHE_DIR" in body
    assert "warm norm-cache" in body
    assert "cold packaged-only" in body
    assert "_seed_warm_norm_cache" in body
    # ... while every external network-backed source is disabled and a child
    # process socket guard enforces that contract.
    assert "_write_network_guard" in body
    assert "external network disabled by launch smoke" in body
    assert '"MAMMAMIRADIO_ALLOW_YTDLP": "false"' in body
    assert '"JAMENDO_CLIENT_ID": ""' in body
    assert '"HA_ENABLED": "false"' in body
    # ... and asserts a STRICT first-byte bound (default 2s, not the perf
    # smoke's 8s already-running budget).
    assert "MAMMAMIRADIO_LAUNCH_FIRST_BYTE_S" in body
    assert '"2.0"' in body
    assert "MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S" in body
    # CI can exercise the exact pulled image without overlaying repository
    # source: isolated /data volumes, the image's default /run.sh command,
    # Docker-level network denial, and listener-byte checks over loopback.
    assert "--image" in body
    assert '"--network"' in body
    assert '"none"' in body
    assert '"volume"' in body
    assert "_image_launch_command" in body
    assert "_image_perf_command" in body
    assert "docker" in body
    assert "/public-status" in body


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


def test_ha_green_launch_smoke_env_clears_network_sources(tmp_path: Path) -> None:
    smoke = _load_ha_green_launch_smoke()

    env = smoke._launch_env(
        port=8123,
        cache_dir=str(tmp_path / "cache"),
        tmp_dir=str(tmp_path / "tmp"),
        guard_dir=str(tmp_path / "guard"),
    )

    assert env["MAMMAMIRADIO_ALLOW_YTDLP"] == "false"
    assert env["JAMENDO_CLIENT_ID"] == ""
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["OPENAI_API_KEY"] == ""
    assert env["AZURE_SPEECH_KEY"] == ""
    assert env["ELEVENLABS_API_KEY"] == ""
    assert env["HA_ENABLED"] == "false"
    assert env["HA_TOKEN"] == ""
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert env["PYTHONPATH"].split(os.pathsep)[:2] == [str(tmp_path / "guard"), str(ROOT)]


def test_ha_green_launch_smoke_builds_isolated_exact_image_commands() -> None:
    smoke = _load_ha_green_launch_smoke()
    image = "ghcr.io/example/mammamiradio-addon-amd64:sha"
    volume = "mmr-launch-volume"
    container = "mmr-launch-container"

    warm_seed = smoke._image_seed_command(image, volume, seed_warm_cache=True)
    cold_seed = smoke._image_seed_command(image, volume, seed_warm_cache=False)
    launch = smoke._image_launch_command(image, volume, container)
    perf = smoke._image_perf_command(container)

    assert warm_seed[:5] == ["run", "--rm", "--network", "none", "--volume"]
    assert f"{volume}:/data" in warm_seed
    assert "MAMMAMIRADIO_SMOKE_WARM=1" in warm_seed
    assert "MAMMAMIRADIO_SMOKE_WARM=0" in cold_seed
    seed_entrypoint = warm_seed.index("--entrypoint")
    assert warm_seed[seed_entrypoint : seed_entrypoint + 3] == [
        "--entrypoint",
        "python3",
        image,
    ]
    assert warm_seed[-2] == "-c"
    assert warm_seed.count(image) == 1

    assert launch[:4] == ["run", "--detach", "--name", container]
    assert launch[-1] == image
    assert launch[4:6] == ["--network", "none"]
    assert f"{volume}:/data" in launch
    assert "SUPERVISOR_TOKEN=smoke-ci" in launch
    assert not any(arg.startswith("-p") or arg == "--publish" for arg in launch)
    # The image reference is last: Docker executes its baked default /run.sh,
    # not a repository-mounted or caller-supplied app command.
    assert launch.count(image) == 1

    assert perf[:2] == ["exec", "--interactive"]
    assert container in perf
    assert perf[-2:] == ["python3", "-"]
    assert f"MAMMAMIRADIO_PERF_FIRST_BYTE_TIMEOUT_S={smoke.FIRST_BYTE_S}" in perf


def test_ha_green_launch_smoke_rejects_emergency_tone_as_valid_cold_open() -> None:
    smoke = _load_ha_green_launch_smoke()
    cold = next(scenario for scenario in smoke._LAUNCH_SCENARIOS if scenario[0] == "cold packaged-only")

    assert "packaged_recovery" in cold[2]
    assert "emergency_tone" not in cold[2]


@pytest.mark.parametrize(
    ("statuses", "message"),
    [
        (
            {
                "/healthz": (503, {"status": "failing"}),
                "/readyz": (200, {"status": "ready", "ready": True}),
                "/public-status": (200, {"session_stopped": False, "now_streaming": {"type": "music"}}),
            },
            "/healthz",
        ),
        (
            {
                "/healthz": (200, {"status": "ok"}),
                "/readyz": (503, {"status": "starting", "ready": False}),
                "/public-status": (200, {"session_stopped": False, "now_streaming": {"type": "music"}}),
            },
            "/readyz",
        ),
        (
            {
                "/healthz": (200, {"status": "ok"}),
                "/readyz": (200, {"status": "ready", "ready": True}),
                "/public-status": (200, {"session_stopped": True, "now_streaming": {"type": "stopped"}}),
            },
            "/public-status",
        ),
    ],
)
def test_ha_green_launch_smoke_rejects_false_post_stream_health(
    statuses: dict[str, tuple[int, dict]],
    message: str,
) -> None:
    smoke = _load_ha_green_launch_smoke()

    with pytest.raises(RuntimeError, match=message):
        smoke._assert_post_stream_status(statuses)


def test_ha_green_launch_smoke_accepts_truthful_post_stream_health() -> None:
    smoke = _load_ha_green_launch_smoke()

    smoke._assert_post_stream_status(
        {
            "/healthz": (200, {"status": "ok"}),
            "/readyz": (200, {"status": "ready", "ready": True}),
            "/public-status": (
                200,
                {"session_stopped": False, "now_streaming": {"type": "music"}},
            ),
        }
    )


def test_ha_green_launch_smoke_image_scenario_cleans_exact_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_ha_green_launch_smoke()
    docker_calls: list[list[str]] = []

    def fake_docker(
        args: list[str],
        *,
        input_text: str | None = None,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del input_text, capture_output
        command = list(args)
        docker_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(smoke, "_run_docker", fake_docker)
    monkeypatch.setattr(smoke, "_seed_image_volume", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_wait_for_image_server", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(smoke, "_run_image_perf_smoke", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        smoke,
        "_image_post_stream_statuses",
        lambda *_args, **_kwargs: {
            "/healthz": (200, {"status": "ok"}),
            "/readyz": (200, {"status": "ready", "ready": True}),
            "/public-status": (200, {"session_stopped": False, "now_streaming": {}}),
        },
    )
    monkeypatch.setattr(smoke, "_image_current_audio_source", lambda *_args, **_kwargs: ("norm_cache", {}))

    assert smoke._run_image_launch_scenario(
        "example/image:sha",
        "warm norm-cache",
        seed_warm_cache=True,
        expected_sources=frozenset({"norm_cache"}),
    )

    volume_create = next(call for call in docker_calls if call[:2] == ["volume", "create"])
    launch = next(call for call in docker_calls if call[:2] == ["run", "--detach"])
    container_remove = next(call for call in docker_calls if call[:2] == ["rm", "--force"])
    volume_remove = next(call for call in docker_calls if call[:3] == ["volume", "rm", "--force"])

    volume_name = volume_create[-1]
    container_name = launch[launch.index("--name") + 1]
    assert f"{volume_name}:/data" in launch
    assert launch[-1] == "example/image:sha"
    assert container_remove[-1] == container_name
    assert volume_remove[-1] == volume_name


def test_ha_green_launch_smoke_network_guard_blocks_external_dns(tmp_path: Path) -> None:
    smoke = _load_ha_green_launch_smoke()
    smoke._write_network_guard(tmp_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)

    result = _run(
        [
            sys.executable,
            "-c",
            (
                "import socket\n"
                "socket.getaddrinfo('127.0.0.1', 8000)\n"
                "try:\n"
                "    socket.getaddrinfo('example.com', 443)\n"
                "except OSError as exc:\n"
                "    assert 'external network disabled by launch smoke' in str(exc)\n"
                "else:\n"
                "    raise SystemExit('external lookup was not blocked')\n"
            ),
        ],
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0, result.stderr


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


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {"status": "ready", "ready": True}, "ready"),
        (503, {"status": "starting", "ready": False}, "starting"),
        (
            503,
            {"status": "stopped", "ready": False, "session_stopped": True},
            "stopped",
        ),
    ],
)
def test_ha_green_perf_smoke_classifies_truthful_readiness(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: dict,
    expected: str,
) -> None:
    smoke = _load_ha_green_perf_smoke()
    monkeypatch.setattr(smoke, "_fetch_json", lambda _path: (status, payload))

    assert smoke._check_readiness() == expected


def test_ha_green_perf_smoke_rejects_unconfirmed_stopped_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_ha_green_perf_smoke()
    monkeypatch.setattr(
        smoke,
        "_fetch_json",
        lambda _path: (503, {"status": "stopped", "ready": False}),
    )

    with pytest.raises(SystemExit):
        smoke._check_readiness()


@pytest.mark.parametrize(("expect_stopped", "session_stopped"), [(False, True), (True, False)])
def test_ha_green_perf_smoke_rejects_public_stop_disagreement(
    monkeypatch: pytest.MonkeyPatch,
    expect_stopped: bool,
    session_stopped: bool,
) -> None:
    smoke = _load_ha_green_perf_smoke()
    monkeypatch.setattr(
        smoke,
        "_fetch_json",
        lambda _path: (
            200,
            {
                "session_stopped": session_stopped,
                "runtime_health": {
                    "queue_empty_elapsed_s": 0,
                    "silence_with_listeners": False,
                },
            },
        ),
    )

    with pytest.raises(SystemExit):
        smoke._check_public_status(expect_stopped=expect_stopped)


@pytest.mark.parametrize(("readiness", "stream_expected"), [("ready", True), ("starting", True), ("stopped", False)])
def test_ha_green_perf_smoke_skips_stream_only_for_intentional_stop(
    monkeypatch: pytest.MonkeyPatch,
    readiness: str,
    stream_expected: bool,
) -> None:
    smoke = _load_ha_green_perf_smoke()
    calls: list[object] = []
    monkeypatch.setattr(smoke, "_wait_for_health", lambda: calls.append("health"))
    monkeypatch.setattr(smoke, "_check_readiness", lambda: readiness)
    monkeypatch.setattr(
        smoke,
        "_check_public_status",
        lambda *, expect_stopped: calls.append(("public", expect_stopped)),
    )
    monkeypatch.setattr(smoke, "_check_first_stream_byte", lambda: calls.append("stream"))

    assert smoke.main() == 0
    assert ("public", readiness == "stopped") in calls
    assert ("stream" in calls) is stream_expected


def test_release_invariants_guard_ha_green_perf_budget() -> None:
    release_body = (ROOT / "scripts" / "check-release-invariants.sh").read_text()
    pre_release_body = (ROOT / "scripts" / "pre-release-check.sh").read_text()

    for body in (release_body, pre_release_body):
        assert "REQUIRED_RECOVERY_ASSETS=(" in body
        assert '"continuity_1.mp3"' in body
        assert '"emergency_tone.mp3"' in body
        assert "manifest/hash" in body
        assert (
            "is_approved_packaged_audio_asset" in body
            or "hashlib.sha256" in body
            or "validate-spoken-assets.py" in body
        )
        assert "ffprobe" in body
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


VALIDATE_ADDON_BACKUP_CONTRACT = (
    "backup: hot",
    "backup_exclude:",
    '  - "tmp"',
    '  - "cache/.ytdlp_tmp"',
    '  - "cache/restart_handoff"',
    '  - "cache/clips"',
    '  - "cache/*.mp3"',
    '  - "cache/*.mp3.json"',
    '  - "cache/*.m4a"',
    '  - "cache/*.webm"',
    '  - "*.part"',
    '  - "*.ytdl"',
    '  - "*.tmp"',
)


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
                *VALIDATE_ADDON_BACKUP_CONTRACT,
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
    _write(
        tmp_path / "model_registry.toml",
        """
[models]
default_profile = "balanced"

[models.catalog.anthropic]
opus = "anthropic-creative"
haiku = "anthropic-fast"

[models.catalog.openai]
large = "openai-creative"
small = "openai-fast"

[models.routing]
banter = "creative"
news_flash = "creative"
ad = "creative"
transition = "fast"
home_mood = "fast"
memory_extract = "fast"
direction = "creative"

[models.profiles.balanced]
anthropic = { creative = "opus", fast = "haiku" }
openai = { creative = "large", fast = "small" }

[tts.openai]
model = "openai-tts"

[pricing]
fallback_input_per_million = 15.0
fallback_output_per_million = 75.0
""".lstrip(),
    )
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


def test_validate_addon_requires_the_canonical_model_registry(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="def _inject_ingress_prefix(html: str, prefix: str) -> str:\n    return html\n",
    )
    (tmp_path / "model_registry.toml").unlink()

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "Missing canonical model_registry.toml" in result.stdout


def test_validate_addon_rejects_a_committed_addon_registry_copy(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="def _inject_ingress_prefix(html: str, prefix: str) -> str:\n    return html\n",
    )
    _write(tmp_path / "ha-addon/mammamiradio/model_registry.toml", "[models]\n")

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "must not be committed" in result.stdout


def test_validate_addon_rejects_registry_that_runtime_cannot_route(tmp_path: Path) -> None:
    env = _create_validate_addon_repo(
        tmp_path,
        streamer_body="def _inject_ingress_prefix(html: str, prefix: str) -> str:\n    return html\n",
    )
    _write(tmp_path / "model_registry.toml", "[models]\ndefault_profile = 'balanced'\n")

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "model_registry.toml parse/schema error" in result.stdout


def test_cut_edge_release_image_paths_mirror_addon_build_triggers() -> None:
    import re

    workflow = (ROOT / ".github" / "workflows" / "addon-build.yml").read_text()
    script = (ROOT / "scripts" / "cut-edge-release.sh").read_text()

    trigger_section_match = re.search(r"\bon:\s*\n(.*?)(?=\njobs:)", workflow, re.DOTALL)
    assert trigger_section_match, "Could not locate addon-build.yml `on:` block"
    trigger_paths = {
        line.split("-", 1)[1].strip().strip('"').strip("'").removesuffix("/**")
        for line in trigger_section_match.group(0).splitlines()
        if line.lstrip().startswith("- ")
    }
    image_paths_match = re.search(r'^IMAGE_PATHS="([^"]+)"$', script, re.MULTILINE)
    assert image_paths_match, "scripts/cut-edge-release.sh must declare IMAGE_PATHS"
    image_paths = set(image_paths_match.group(1).split())

    assert image_paths == trigger_paths


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
                *VALIDATE_ADDON_BACKUP_CONTRACT,
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
    assert "options keys must follow schema order" in result.stdout


def test_validate_addon_allows_optional_schema_only_keys(tmp_path: Path) -> None:
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
                "stage: stable",
                "image: ghcr.io/florianhorner/mammamiradio-addon-{arch}",
                "timeout: 300",
                "host_network: true",
                "ingress_port: 8000",
                *VALIDATE_ADDON_BACKUP_CONTRACT,
                "options:",
                '  station_name: "Test"',
                '  quality_profile: "balanced"',
                "schema:",
                "  station_name: str?",
                "  quality_profile: list(premium|balanced|economy)?",
                "  legacy_api_key: password?",
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
                "station_name=${station_name:-}",
                "quality_profile=${quality_profile:-}",
                "legacy_api_key=${legacy_api_key:-}",
                "",
            ]
        ),
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/translations/en.yaml",
        "\n".join(
            [
                "configuration:",
                "  station_name: key",
                "  quality_profile: key",
                "  legacy_api_key: key",
                "",
            ]
        ),
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode == 0
    assert "options keys follow schema order; schema-only keys are optional" in result.stdout


def test_validate_addon_rejects_required_schema_only_keys(tmp_path: Path) -> None:
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
                "stage: stable",
                "image: ghcr.io/florianhorner/mammamiradio-addon-{arch}",
                "timeout: 300",
                "host_network: true",
                "ingress_port: 8000",
                *VALIDATE_ADDON_BACKUP_CONTRACT,
                "options:",
                '  station_name: "Test"',
                "schema:",
                "  station_name: str?",
                "  hidden_required: str",
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
                "station_name=${station_name:-}",
                "hidden_required=${hidden_required:-}",
                "",
            ]
        ),
    )
    _write(
        tmp_path / "ha-addon/mammamiradio/translations/en.yaml",
        "\n".join(
            [
                "configuration:",
                "  station_name: key",
                "  hidden_required: key",
                "",
            ]
        ),
    )

    result = _run(["bash", str(VALIDATE_ADDON)], cwd=tmp_path, env=env)

    assert result.returncode != 0
    assert "Schema-only keys must be optional: hidden_required" in result.stdout


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


@pytest.mark.parametrize(
    ("validator_path", "start_marker", "end_marker"),
    [
        (VALIDATE_ADDON, "assert_image_recovery_assets() {", "assert_image_model_registry() {"),
        (
            ADDON_BUILD_WORKFLOW,
            "- name: Assert installed recovery assets",
            "- name: Assert installed model registry",
        ),
    ],
)
def test_addon_image_validator_checks_every_required_recovery_asset(
    validator_path: Path,
    start_marker: str,
    end_marker: str,
) -> None:
    """Image gates must inspect the full required subset, not accept the first good clip."""
    body = validator_path.read_text()
    recovery_block = body.split(start_marker, 1)[1].split(end_marker, 1)[0]

    for asset_name in REQUIRED_RECOVERY_ASSETS:
        assert asset_name in recovery_block, f"{validator_path} does not require {asset_name}"
    assert "resources.files" in recovery_block
    assert "> 1024" in recovery_block or "<= 1024" in recovery_block
    assert "is_approved_packaged_audio_asset" in recovery_block or "validate_spoken_asset_manifest" in recovery_block
    assert "ffprobe" in recovery_block
    assert "raise SystemExit(0)" not in recovery_block


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


def test_standalone_dockerfile_stages_the_model_registry() -> None:
    """Standalone Dockerfile must copy the canonical model_registry.toml.

    load_config() reads the registry as a sibling of radio.toml
    (/app/model_registry.toml). Without a COPY here, every standalone /
    docker-compose deployment boots with _empty_models() and silently loses all
    LLM script generation and OpenAI TTS routing — no crash, no CI signal. The
    HA add-on stages the same file via addon-build.yml; this guards the
    standalone path that has no build-time hash assert.
    """
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert (ROOT / "model_registry.toml").is_file(), "canonical model_registry.toml missing at repo root"
    assert "COPY model_registry.toml" in dockerfile, (
        "Standalone Dockerfile does not COPY model_registry.toml — "
        "standalone deployments would boot without LLM/TTS routing"
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


def test_addon_schema_has_no_provider_secret_fields() -> None:
    """Provider credentials must never reappear as Supervisor add-on options.

    Keys live in /config/secrets.env (written by the admin setup panel);
    Supervisor options are printed verbatim by `ha addons info`, so a provider
    field in options/schema leaks the credential to routine diagnostics
    (issue #688). run.sh keeps reading legacy options.json values as a boot
    fallback, but the schema — stable and edge — must stay clean.
    """
    provider_fields = (
        "anthropic_api_key",
        "openai_api_key",
        "azure_speech_key",
        "azure_speech_region",
        "elevenlabs_api_key",
    )
    surfaces = (
        ROOT / "ha-addon" / "mammamiradio" / "config.yaml",
        ROOT / "ha-addon" / "mammamiradio" / "translations" / "en.yaml",
        ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml",
        ROOT / "ha-addon" / "mammamiradio-edge" / "translations" / "en.yaml",
    )
    import re

    leaks = [
        f"{path.relative_to(ROOT)}: {field}"
        for path in surfaces
        for field in provider_fields
        if re.search(rf'^\s*["\']?{field}["\']?\s*:', path.read_text(), re.MULTILINE)
    ]
    assert not leaks, (
        "Provider secret fields must not be add-on options; "
        "configure them via /config/secrets.env instead: " + ", ".join(leaks)
    )

    # The inverse contract: run.sh must keep reading these keys from legacy
    # options.json so installs that predate the schema removal still boot
    # with their credentials (secrets.env wins per key once populated).
    run_sh = (ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()
    missing = [field for field in provider_fields if f"'{field}'" not in run_sh]
    assert not missing, "run.sh lost the legacy options.json fallback for: " + ", ".join(missing)


def test_addon_cache_default_is_consistent_across_files() -> None:
    """Check that the add-on cache default is identical in all five config paths.

    A mismatch would leave the Configuration tab and runtime using different values.
    """
    import re

    stable_yaml = (ROOT / "ha-addon" / "mammamiradio" / "config.yaml").read_text()
    edge_yaml = (ROOT / "ha-addon" / "mammamiradio-edge" / "config.yaml").read_text()
    run_sh = (ROOT / "ha-addon" / "mammamiradio" / "rootfs" / "run.sh").read_text()
    config_py = (ROOT / "mammamiradio" / "core" / "config.py").read_text()

    found: dict[str, str] = {}

    for label, text in (("stable config.yaml", stable_yaml), ("edge config.yaml", edge_yaml)):
        m = re.search(r"^\s*norm_cache_mb:\s*(\d+)\s*$", text, re.MULTILINE)
        assert m, f"norm_cache_mb option default not found in {label}"
        found[label] = m.group(1)

    # Both the .get() default and the except-branch fallback in run.sh.
    run_defaults = re.findall(r"norm_cache_mb'\s*,\s*(\d+)\)|cache_mb_int\s*=\s*(\d+)", run_sh)
    flat = [g for pair in run_defaults for g in pair if g]
    assert len(flat) == 2, f"expected 2 cache defaults in run.sh, found {flat}"
    found["run.sh get default"] = flat[0]
    found["run.sh fallback"] = flat[1]

    m = re.search(r"^ADDON_MAX_CACHE_SIZE_MB\s*=\s*(\d+)", config_py, re.MULTILINE)
    assert m, "ADDON_MAX_CACHE_SIZE_MB not found in config.py"
    found["config.py ADDON_MAX_CACHE_SIZE_MB"] = m.group(1)

    assert len(set(found.values())) == 1, f"add-on cache default drifted across files: {found}"


def test_addon_cache_schema_bounds_match_config_py() -> None:
    """The Supervisor schema and runtime must use the same cache bounds."""
    import re

    config_py = (ROOT / "mammamiradio" / "core" / "config.py").read_text()
    lo = re.search(r"^MIN_MAX_CACHE_SIZE_MB\s*=\s*(\d+)", config_py, re.MULTILINE)
    hi = re.search(r"^MAX_MAX_CACHE_SIZE_MB\s*=\s*(\d+)", config_py, re.MULTILINE)
    assert lo and hi, "cache clamp bounds not found in config.py"

    for addon in ("mammamiradio", "mammamiradio-edge"):
        text = (ROOT / "ha-addon" / addon / "config.yaml").read_text()
        m = re.search(r"^\s*norm_cache_mb:\s*int\((\d+),\s*(\d+)\)\?", text, re.MULTILINE)
        assert m, f"norm_cache_mb schema bounds not found in {addon}/config.yaml"
        assert m.group(1) == lo.group(1), f"{addon} schema minimum drifted from MIN_MAX_CACHE_SIZE_MB"
        assert m.group(2) == hi.group(1), f"{addon} schema maximum drifted from MAX_MAX_CACHE_SIZE_MB"
