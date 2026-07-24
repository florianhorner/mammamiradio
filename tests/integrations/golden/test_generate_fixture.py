"""Exit-code contract for the golden-fixture generator/checker.

The contract-drift CI trusts these exit codes: 0 means the committed fixture
matches the serializer, 1 means drift of any kind. Pin every failure mode so
a generator refactor cannot silently turn CI green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integrations.golden import generate_fixture


@pytest.fixture
def fixture_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect FIXTURE_PATH into tmp_path so tests never touch the real fixture."""
    path = tmp_path / "v1_now_playing.json"
    monkeypatch.setattr(generate_fixture, "FIXTURE_PATH", path)
    return path


def test_write_then_check_passes(fixture_path: Path) -> None:
    assert generate_fixture.main([]) == 0
    assert fixture_path.exists()
    assert generate_fixture.main(["--check"]) == 0


def test_check_fails_when_fixture_missing(fixture_path: Path) -> None:
    assert generate_fixture.main(["--check"]) == 1


def test_check_fails_on_semantic_drift(fixture_path: Path) -> None:
    assert generate_fixture.main([]) == 0
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["now_playing"]["title"] = "A Different Song"
    fixture_path.write_bytes(generate_fixture.fixture_bytes(payload))
    assert generate_fixture.main(["--check"]) == 1


def test_check_fails_on_volatile_field_type_drift(fixture_path: Path) -> None:
    assert generate_fixture.main([]) == 0
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload["changed_at"] = "not-a-timestamp"
    # Bypass fixture_bytes: normalization must refuse this payload at check time.
    fixture_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert generate_fixture.main(["--check"]) == 1


def test_check_fails_on_non_canonical_encoding(fixture_path: Path) -> None:
    assert generate_fixture.main([]) == 0
    fixture_path.write_bytes(fixture_path.read_bytes() + b"\n")
    assert generate_fixture.main(["--check"]) == 1


def test_check_fails_on_invalid_json(fixture_path: Path) -> None:
    fixture_path.write_text("{not json", encoding="utf-8")
    assert generate_fixture.main(["--check"]) == 1


def test_check_fails_on_non_object_root(fixture_path: Path) -> None:
    fixture_path.write_text('["not", "an", "object"]\n', encoding="utf-8")
    assert generate_fixture.main(["--check"]) == 1
