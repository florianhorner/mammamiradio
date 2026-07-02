from __future__ import annotations

import ast
import importlib.util
import inspect
import textwrap
from pathlib import Path
from typing import Any

from mammamiradio.core.release_beat_schema import (
    ALLOWED_KEYS,
    ID_RE,
    RUNTIME_CONSUMED_KEYS,
    SEMVER_RE,
    SHA_RE,
    VALID_CHANNELS,
    VALID_PRIORITIES,
    VALIDATOR_ONLY_KEYS,
)
from mammamiradio.release_campaign import ReleaseBeatManifest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "validate-release-beat.py"


def _load_validator() -> Any:
    spec = importlib.util.spec_from_file_location("validate_release_beat", VALIDATOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_manifest_payload() -> dict[str, Any]:
    return {
        "enabled": True,
        "schema": 1,
        "id": "edge-4a15270-hans-guenther",
        "channel": "edge",
        "build_sha": "4a1527080692eed5541e72a5a2b0f2c344e3ca9a",
        "semver": "2.15.0",
        "priority": "normal",
        "title": "Studio crate",
        "facts": ["Hans Guenther can now wait in the studio hallway."],
        "props": ["a human-sized crate labeled HANS GUENTHER"],
        "copy": ["There is a crate in Studio B, and everyone is pretending that is normal."],
        "copy_guidance": "Keep it in-world.",
        "avoid": ["claiming the listener updated successfully before boot"],
        "forbidden_terms": ["software update"],
        "listener_safe_terms": [],
        "max_airings": 3,
        "campaign_window_seconds": 3600,
        "min_seconds_between_airings": 60,
        "min_segments_between_airings": 2,
    }


def _write_pyproject(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        "\n".join(
            [
                "[project]",
                'name = "mammamiradio"',
                'version = "2.15.0"',
                "",
                "[tool.setuptools.package-data]",
                'mammamiradio = ["assets/**/*"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_schema_key_sets_are_intentionally_partitioned() -> None:
    assert RUNTIME_CONSUMED_KEYS <= ALLOWED_KEYS
    assert VALIDATOR_ONLY_KEYS <= ALLOWED_KEYS
    assert VALIDATOR_ONLY_KEYS.isdisjoint(RUNTIME_CONSUMED_KEYS)
    assert ALLOWED_KEYS == RUNTIME_CONSUMED_KEYS | VALIDATOR_ONLY_KEYS


def test_validator_reexports_shared_schema_objects() -> None:
    validator = _load_validator()

    assert validator.ALLOWED_KEYS is ALLOWED_KEYS
    assert validator.ID_RE is ID_RE
    assert validator.SHA_RE is SHA_RE
    assert validator.SEMVER_RE is SEMVER_RE
    assert validator.VALID_CHANNELS is VALID_CHANNELS
    assert validator.VALID_PRIORITIES is VALID_PRIORITIES


def test_manifest_with_every_allowed_key_passes_unknown_key_gate(tmp_path: Path) -> None:
    validator = _load_validator()
    payload = _valid_manifest_payload()

    assert set(payload) == ALLOWED_KEYS
    errors = validator._validate_enabled_manifest(
        payload,
        Path("mammamiradio/assets/release/release_beat.toml"),
        _write_pyproject(tmp_path),
        target_channel=None,
        target_sha=None,
        target_semver=None,
    )

    assert not [error for error in errors if "unknown key(s)" in error]
    assert errors == []


def test_runtime_scheduling_knob_is_accepted_by_validator(tmp_path: Path) -> None:
    validator = _load_validator()
    payload = _valid_manifest_payload()
    payload["max_airings"] = 3

    errors = validator._validate_enabled_manifest(
        payload,
        Path("mammamiradio/assets/release/release_beat.toml"),
        _write_pyproject(tmp_path),
        target_channel=None,
        target_sha=None,
        target_semver=None,
    )

    assert not [error for error in errors if "unknown key(s)" in error]
    assert errors == []


def test_newly_admitted_scalar_text_fields_are_listener_safety_scanned(tmp_path: Path) -> None:
    validator = _load_validator()
    for field in ("title", "copy_guidance"):
        payload = _valid_manifest_payload()
        payload[field] = "Now shipping the GitHub pull request"

        errors = validator._validate_enabled_manifest(
            payload,
            Path("mammamiradio/assets/release/release_beat.toml"),
            _write_pyproject(tmp_path),
            target_channel=None,
            target_sha=None,
            target_semver=None,
        )

        assert any(f"release_beat.{field} contains listener-unsafe term(s)" in error for error in errors), (
            f"{field} was not listener-safety scanned"
        )


def test_scalar_text_field_can_opt_into_machine_terms(tmp_path: Path) -> None:
    validator = _load_validator()
    payload = _valid_manifest_payload()
    payload["title"] = "Studio crate, no GitHub required"
    payload["listener_safe_terms"] = ["github"]

    errors = validator._validate_enabled_manifest(
        payload,
        Path("mammamiradio/assets/release/release_beat.toml"),
        _write_pyproject(tmp_path),
        target_channel=None,
        target_sha=None,
        target_semver=None,
    )

    assert errors == []


def test_release_manifest_loader_reads_only_declared_runtime_keys() -> None:
    source = inspect.getsource(ReleaseBeatManifest.from_dict)
    tree = ast.parse(textwrap.dedent(source))
    read_keys = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }

    assert read_keys == RUNTIME_CONSUMED_KEYS


def test_release_manifest_loader_reflects_all_declared_runtime_keys() -> None:
    payload = _valid_manifest_payload()

    assert set(payload) >= RUNTIME_CONSUMED_KEYS
    beat = ReleaseBeatManifest.from_dict(payload)

    assert beat.enabled is True
    assert beat.id == payload["id"]
    assert beat.channel == payload["channel"]
    assert beat.build_sha == payload["build_sha"]
    assert beat.semver == payload["semver"]
    assert beat.title == payload["title"]
    assert beat.facts == tuple(payload["facts"])
    assert beat.props == tuple(payload["props"])
    assert beat.copy_guidance == payload["copy_guidance"]
    assert beat.forbidden_terms == tuple(payload["forbidden_terms"])
    assert beat.max_airings == payload["max_airings"]
    assert beat.campaign_window_seconds == payload["campaign_window_seconds"]
    assert beat.min_seconds_between_airings == payload["min_seconds_between_airings"]
    assert beat.min_segments_between_airings == payload["min_segments_between_airings"]
