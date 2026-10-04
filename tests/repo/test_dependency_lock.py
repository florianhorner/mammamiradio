"""The runtime lock must describe the environment that actually runs our tests."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]


def _runtime_pins() -> dict[str, Requirement]:
    return {
        canonicalize_name(requirement.name): requirement
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line and line[0].isalnum()
        for requirement in [Requirement(line.rstrip("\\").strip())]
        if requirement.marker is None or requirement.marker.evaluate()
    }


@pytest.mark.parametrize("pin", list(_runtime_pins().values()), ids=str)
def test_test_environment_uses_the_runtime_lock(pin: Requirement) -> None:
    installed = importlib.metadata.version(pin.name)
    assert installed in pin.specifier, f"{pin}: installed {installed}; install requirements.txt before testing"


def test_lock_satisfies_source_requirements() -> None:
    """Check direct source constraints even in source-resolved local environments."""
    pins = _runtime_pins()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for raw in project["dependencies"]:
        requirement = Requirement(raw)
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        assert name in pins, f"Runtime dependency {requirement} is missing from requirements.txt"
        pinned_version = next(iter(pins[name].specifier)).version
        assert pinned_version in requirement.specifier, f"{requirement} conflicts with locked {name}=={pinned_version}"


def test_lock_satisfies_source_requirements_and_their_extras() -> None:
    """Check actual metadata edges, including uvicorn[standard], without network access."""
    pins = _runtime_pins()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pending = [Requirement(raw) for raw in project["dependencies"]]
    visited: set[tuple[str, frozenset[str]]] = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        name = canonicalize_name(requirement.name)
        assert name in pins, f"Runtime dependency {requirement} is missing from requirements.txt"
        pinned_version = next(iter(pins[name].specifier)).version
        assert pinned_version in requirement.specifier, f"{requirement} conflicts with locked {name}=={pinned_version}"
        key = (name, frozenset(requirement.extras))
        if key in visited:
            continue
        visited.add(key)
        for raw in importlib.metadata.requires(name) or []:
            child = Requirement(raw)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in {"", *requirement.extras}
            ):
                # The parent extras selected this edge. Do not re-evaluate its
                # marker later with the child's (different) extras context.
                child.marker = None
                pending.append(child)


def _assert_locked_install_order(commands: str, *, development: bool) -> None:
    locked = commands.index("--force-reinstall --require-hashes -r requirements.txt")
    source = commands.index("--no-deps")
    checked = commands.index("pip check")
    assert locked < source < checked
    if development:
        assert commands.index("-r requirements-dev.txt") < locked


def test_quality_setup_installs_dev_tools_before_locked_runtime() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/setup-python-ci/action.yml").read_text())
    install = next(step["run"] for step in action["runs"]["steps"] if step.get("name") == "Install dependencies")
    _assert_locked_install_order(install, development=True)


@pytest.mark.parametrize("workflow", ["addon-build.yml", "addon-release.yml"])
def test_media_proof_uses_the_same_runtime_lock(workflow: str) -> None:
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    install = next(
        step["run"]
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Install full media-proof dependencies"
    )
    _assert_locked_install_order(install, development=True)


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "ha-addon/mammamiradio/Dockerfile"])
def test_images_install_hashed_runtime_before_source(dockerfile: str) -> None:
    source = (ROOT / dockerfile).read_text()
    assert "COPY requirements.txt " in source
    _assert_locked_install_order(source.replace("pip3", "pip"), development=False)


@pytest.mark.parametrize(
    ("script", "development"),
    [("scripts/bootstrap-conductor.sh", True), ("Start Radio.command", False)],
)
def test_local_setup_scripts_install_the_same_lock(script: str, development: bool) -> None:
    # Conductor setup and the Mac launcher used to resolve the editable install's
    # dependencies, which upgraded the runtime past requirements.txt.
    source = (ROOT / script).read_text()
    _assert_locked_install_order(source, development=development)
    editable = [line for line in source.splitlines() if "install" in line and " -e " in f"{line} "]
    assert editable
    assert all("--no-deps" in line for line in editable)


def test_standalone_image_build_runs_on_prs_for_all_image_inputs() -> None:
    workflow = yaml.load((ROOT / ".github/workflows/docker.yml").read_text(), Loader=yaml.BaseLoader)
    paths = workflow["on"]["pull_request"]["paths"]
    for source in (
        ".github/workflows/docker.yml",
        "Dockerfile",
        ".dockerignore",
        "requirements.txt",
        "pyproject.toml",
        "mammamiradio/main.py",
        "mammamiradio/assets/example.mp3",
        "radio.toml",
        "model_registry.toml",
        "scripts/docker-entrypoint.sh",
    ):
        assert any(fnmatchcase(source, pattern) for pattern in paths), f"No PR image build for {source}"


def test_standalone_image_pr_build_has_no_publication_permissions() -> None:
    workflow = yaml.load((ROOT / ".github/workflows/docker.yml").read_text(), Loader=yaml.BaseLoader)
    smoke = workflow["jobs"]["standalone-smoke"]
    release = workflow["jobs"]["build-and-push"]
    assert smoke["if"] == "github.event_name == 'pull_request'"
    assert release["if"] == "github.event_name != 'pull_request'"
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert "workflow_dispatch" in workflow["on"]
    assert "pull_request_target" not in workflow["on"]
    assert smoke["permissions"] == {"contents": "read"}
    checkout = next(step for step in smoke["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
    assert "secrets." not in str(smoke)
    assert "login-action" not in str(smoke)


@pytest.mark.parametrize("failed_command", ["", "build", "run"], ids=["success", "build-failure", "check-failure"])
def test_standalone_image_pr_smoke_propagates_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_command: str
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["standalone-smoke"]["steps"] if "run" in step)
    calls = tmp_path / "docker-calls"
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_TEST_CALLS"\n'
        'if [ "$1" = "$DOCKER_TEST_FAIL" ]; then exit 42; fi\nexit 0\n'
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("DOCKER_TEST_CALLS", str(calls))
    monkeypatch.setenv("DOCKER_TEST_FAIL", failed_command)
    result = subprocess.run(
        ["/bin/bash", "-e", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    expected = ["build --platform linux/amd64 --tag mammamiradio:pr-smoke ."]
    if failed_command != "build":
        expected.append("run --rm --network none --entrypoint python mammamiradio:pr-smoke -m pip check")
    assert calls.read_text().splitlines() == expected
    assert result.returncode == (42 if failed_command else 0), result.stderr


def test_uv_never_manages_the_locked_environment() -> None:
    # A managed project lets `uv run` and `uv sync` install uv's own resolution
    # into .venv, replacing the versions requirements.txt pins.
    tool = tomllib.loads((ROOT / "pyproject.toml").read_text()).get("tool", {})
    assert tool.get("uv", {}).get("managed") is False, "keep [tool.uv] managed = false in pyproject.toml"


needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")


# A runtime dependency, planted at a version uv would never resolve to.
LOCKED_DIST, LOCKED_VERSION = "python-dotenv", "0.0.1"


def _run_uv(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # A private cache and no user-level uv config, so only the project decides.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "UV_CACHE_DIR": os.fspath(cwd.parent / f"{cwd.name}-uv-cache"),
        "UV_NO_CONFIG": "1",
    }
    return subprocess.run(["uv", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=120, check=False)


def _listing(directory: Path) -> list[str]:
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*"))


def _build_requirements_installed() -> bool:
    for raw in tomllib.loads((ROOT / "pyproject.toml").read_text())["build-system"]["requires"]:
        requirement = Requirement(raw)
        try:
            if importlib.metadata.version(requirement.name) not in requirement.specifier:
                return False
        except importlib.metadata.PackageNotFoundError:
            return False
    return True


def _write_wheel(directory: Path, name: str, version: str) -> Path:
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}.py": f"VALUE = {version!r}\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rows = []
    for path, body in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(body.encode()).digest()).rstrip(b"=").decode()
        rows.append(f"{path},sha256={digest},{len(body)}")
    files[f"{dist}/RECORD"] = "\n".join([*rows, f"{dist}/RECORD,,"]) + "\n"
    wheel = directory / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return wheel


@needs_uv
def test_uv_run_leaves_the_environment_alone(tmp_path: Path) -> None:
    # The behaviour the setting promises, on whichever uv this machine has.
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    result = _run_uv(["run", "--offline", "--python", sys.executable, "python", "-c", "pass"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / ".venv").exists(), "uv run created a project environment"
    assert not (tmp_path / "uv.lock").exists(), "uv run wrote a lockfile"

    sync = _run_uv(["sync", "--offline"], tmp_path)
    assert sync.returncode != 0, "uv sync should refuse an unmanaged project"
    assert "unmanaged" in sync.stderr, sync.stderr
    assert not (tmp_path / ".venv").exists()


@needs_uv
@pytest.mark.parametrize("extra", [False, True], ids=["plain", "with-extra"])
def test_uv_run_reuses_the_existing_locked_environment(tmp_path: Path, extra: bool) -> None:
    # The original failure: `uv run` from the repository root synced .venv to uv's own resolution.
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    # A managed `uv run` only replaces a package the project depends on.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {canonicalize_name(Requirement(raw).name) for raw in project["dependencies"]}
    assert canonicalize_name(LOCKED_DIST) in declared, f"{LOCKED_DIST} is no longer a runtime dependency; plant another"
    venv = tmp_path / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", os.fspath(venv)], check=True)
    dist_info = f"{LOCKED_DIST.replace('-', '_')}-{LOCKED_VERSION}.dist-info"
    locked = next(venv.glob("lib/python*/site-packages")) / dist_info
    locked.mkdir()
    (locked / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {LOCKED_DIST}\nVersion: {LOCKED_VERSION}\n")
    (locked / "RECORD").write_text(f"{locked.name}/METADATA,,\n{locked.name}/RECORD,,\n")
    before = _listing(venv)

    command = ["run", "--offline"]
    probe = f"import importlib.metadata as m; print(m.version({LOCKED_DIST!r}))"
    if extra:
        command += ["--with", os.fspath(_write_wheel(tmp_path, "withdemo", "1.0"))]
        probe += "; import withdemo"
    result = _run_uv([*command, "python", "-c", probe], tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == LOCKED_VERSION, "uv run replaced a locked package"
    assert _listing(venv) == before, "uv run changed the existing environment"
    assert not (tmp_path / "uv.lock").exists(), "uv run wrote a lockfile"


@needs_uv
@pytest.mark.skipif(
    not _build_requirements_installed(),
    reason="--no-build-isolation needs every [build-system] requirement installed",
)
def test_uv_build_ignores_the_unmanaged_setting(tmp_path: Path) -> None:
    # scripts/media-proof.py builds the wheel and sdist with `uv build`.
    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text())
    (tmp_path / "mammamiradio").mkdir()
    (tmp_path / "mammamiradio" / "__init__.py").write_text("")
    args = ["build", "--wheel", "--sdist", "--offline", "--no-build-isolation", "--python", sys.executable]
    result = _run_uv([*args, "--out-dir", "dist"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(list((tmp_path / "dist").glob("*.whl"))) == 1
    assert len(list((tmp_path / "dist").glob("*.tar.gz"))) == 1
    assert not (tmp_path / ".venv").exists()
    assert not (tmp_path / "uv.lock").exists()


def test_addon_build_contexts_stage_the_canonical_runtime_lock() -> None:
    workflow = (ROOT / ".github/workflows/addon-build.yml").read_text()
    validator = (ROOT / "scripts/validate-addon.sh").read_text()
    assert "cp requirements.txt ha-addon/mammamiradio/" in workflow
    assert 'cp requirements.txt "$TMPCTX/"' in validator
    assert not (ROOT / "ha-addon/mammamiradio/requirements.txt").exists()
