"""The runtime lock must describe the environment that actually runs our tests."""

from __future__ import annotations

import importlib.metadata
import tomllib
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


def test_addon_build_contexts_stage_the_canonical_runtime_lock() -> None:
    workflow = (ROOT / ".github/workflows/addon-build.yml").read_text()
    validator = (ROOT / "scripts/validate-addon.sh").read_text()
    assert "cp requirements.txt ha-addon/mammamiradio/" in workflow
    assert 'cp requirements.txt "$TMPCTX/"' in validator
    assert not (ROOT / "ha-addon/mammamiradio/requirements.txt").exists()
