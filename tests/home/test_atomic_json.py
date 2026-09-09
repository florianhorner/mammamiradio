"""Owner-only atomic JSON helper: mode at creation, cleanup, byte threading."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.home.atomic_json import (
    atomic_write_json,
    chmod_owner_only,
    unlink_legacy_fixed_tmp,
)


def _recording_mkstemp(names: list[str], modes: list[int]):
    real = tempfile.mkstemp

    def recording(*args, **kwargs):
        fd, name = real(*args, **kwargs)
        names.append(Path(name).name)
        modes.append(os.stat(name).st_mode & 0o777)
        return fd, name

    return recording


def test_atomic_write_json_temp_is_created_owner_only(tmp_path):
    destination = tmp_path / "household.json"
    names: list[str] = []
    modes: list[int] = []
    previous_umask = os.umask(0o000)
    try:
        with patch(
            "mammamiradio.home.atomic_json.tempfile.mkstemp",
            side_effect=_recording_mkstemp(names, modes),
        ):
            atomic_write_json(destination, {"k": "v"}, ensure_ascii=True)
    finally:
        os.umask(previous_umask)

    assert modes == [0o600]
    assert destination.stat().st_mode & 0o777 == 0o600
    assert names[0].startswith(".household.json.") and names[0].endswith(".tmp")


def test_atomic_write_json_replace_failure_raises_and_leaves_destination(tmp_path):
    destination = tmp_path / "household.json"
    previous = '{"kept": true}'
    destination.write_text(previous, encoding="utf-8")
    with (
        patch("mammamiradio.home.atomic_json.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        atomic_write_json(destination, {"k": "v"}, ensure_ascii=True)
    assert destination.read_text(encoding="utf-8") == previous
    assert list(tmp_path.glob(".household.json.*.tmp")) == []


def test_atomic_write_json_runtimeerror_still_cleans_scratch(tmp_path):
    destination = tmp_path / "household.json"
    with (
        patch("mammamiradio.home.atomic_json.os.replace", side_effect=RuntimeError("boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        atomic_write_json(destination, {"k": "v"}, ensure_ascii=True)
    assert not destination.exists()
    assert list(tmp_path.glob(".household.json.*.tmp")) == []


def test_atomic_write_json_unencodable_payload_never_creates_temp(tmp_path):
    destination = tmp_path / "household.json"
    with (
        patch("mammamiradio.home.atomic_json.tempfile.mkstemp", side_effect=AssertionError("mkstemp")),
        pytest.raises(TypeError),
    ):
        atomic_write_json(destination, {"k": object()}, ensure_ascii=True)
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_json_missing_parent_raises(tmp_path):
    destination = tmp_path / "gone" / "household.json"
    with pytest.raises(OSError):
        atomic_write_json(destination, {"k": "v"}, ensure_ascii=True)


def test_atomic_write_json_threads_ensure_ascii_indent_sort_keys(tmp_path):
    destination = tmp_path / "household.json"
    atomic_write_json(destination, {"b": "à", "a": 1}, ensure_ascii=True, indent=2, sort_keys=True)
    escaped = destination.read_text(encoding="utf-8")
    assert escaped.index('"a"') < escaped.index('"b"')
    assert "\\u00e0" in escaped
    atomic_write_json(destination, {"b": "à", "a": 1}, ensure_ascii=False, indent=None, sort_keys=False)
    compact = json.loads(destination.read_text(encoding="utf-8"))
    assert compact == {"b": "à", "a": 1}
    assert "à" in destination.read_text(encoding="utf-8")


def test_atomic_write_json_temp_names_are_unique(tmp_path):
    destination = tmp_path / "household.json"
    names: list[str] = []
    modes: list[int] = []
    with patch(
        "mammamiradio.home.atomic_json.tempfile.mkstemp",
        side_effect=_recording_mkstemp(names, modes),
    ):
        atomic_write_json(destination, {"n": 1}, ensure_ascii=True)
        atomic_write_json(destination, {"n": 2}, ensure_ascii=True)
    assert len(names) == 2
    assert names[0] != names[1]
    assert all(n.startswith(".household.json.") and n.endswith(".tmp") for n in names)


def test_chmod_owner_only_tightens_existing_file(tmp_path):
    path = tmp_path / "leftover.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    chmod_owner_only(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_chmod_owner_only_missing_path_does_not_raise(tmp_path):
    chmod_owner_only(tmp_path / "missing.json")


def test_unlink_legacy_fixed_tmp_removes_shared_scratch(tmp_path):
    path = tmp_path / "moments.json"
    leftover = path.with_suffix(".json.tmp")
    leftover.write_text("stale", encoding="utf-8")
    unlink_legacy_fixed_tmp(path)
    assert not leftover.exists()
