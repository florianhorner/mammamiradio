"""Owner-only atomic JSON helper: mode at creation, cleanup, byte threading."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.home.atomic_json import (
    atomic_write_json,
    chmod_owner_only,
    prune_stale_atomic_json_tmp_files,
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


def test_atomic_write_json_without_fchmod_fails_closed_and_cleans_scratch(tmp_path, monkeypatch):
    destination = tmp_path / "household.json"
    previous = '{"kept": true}'
    destination.write_text(previous, encoding="utf-8")
    monkeypatch.delattr(os, "fchmod")

    with pytest.raises(OSError, match=r"owner-only atomic JSON writes require os\.fchmod"):
        atomic_write_json(destination, {"k": "v"}, ensure_ascii=True)

    assert destination.read_text(encoding="utf-8") == previous
    assert list(tmp_path.glob(".household.json.*.tmp")) == []


def test_atomic_write_json_write_failure_raises_and_leaves_destination(tmp_path):
    destination = tmp_path / "household.json"
    previous = '{"kept": true}'
    destination.write_text(previous, encoding="utf-8")
    real_fdopen = os.fdopen

    class WriteFailingHandle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._handle.close()

        def write(self, _body):
            raise OSError("disk full during write")

    def failing_fdopen(fd, *args, **kwargs):
        return WriteFailingHandle(real_fdopen(fd, *args, **kwargs))

    with (
        patch("mammamiradio.home.atomic_json.os.fdopen", side_effect=failing_fdopen),
        pytest.raises(OSError, match="disk full during write"),
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


def test_prune_stale_atomic_json_tmp_files_removes_only_owned_old_scratch(tmp_path):
    cache_dir = tmp_path / "cache"
    destinations = (
        cache_dir / "ha_registry.json",
        cache_dir / "state" / "ha_entity_policy.json",
        cache_dir / "moments.json",
        cache_dir / "evening_ledger.json",
    )
    old_mtime = time.time() - 7 * 3600
    old_unique = [path.parent / f".{path.name}.crashed.tmp" for path in destinations]
    old_legacy = [path.with_suffix(".json.tmp") for path in destinations[2:]]
    for path in (*old_unique, *old_legacy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("household data", encoding="utf-8")
        os.utime(path, (old_mtime, old_mtime))

    fresh = cache_dir / ".moments.json.in-flight.tmp"
    fresh.write_text("fresh", encoding="utf-8")
    unrelated = cache_dir / ".other.json.crashed.tmp"
    unrelated.write_text("unrelated", encoding="utf-8")

    assert prune_stale_atomic_json_tmp_files(cache_dir, destinations) == 6
    assert all(not path.exists() for path in (*old_unique, *old_legacy))
    assert fresh.exists()
    assert unrelated.exists()


def test_prune_stale_atomic_json_tmp_files_bounds_scan_and_prune(tmp_path, caplog):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    destination = cache_dir / "moments.json"
    old_mtime = time.time() - 7 * 3600
    candidates = [cache_dir / f".moments.json.{index}.tmp" for index in range(4)]
    for index, path in enumerate(candidates):
        path.write_text("stale", encoding="utf-8")
        os.utime(path, (old_mtime - index, old_mtime - index))

    with (
        patch("mammamiradio.home.atomic_json._MAX_SCRATCH_GLOB_CANDIDATES", 3),
        patch("mammamiradio.home.atomic_json._MAX_SCRATCH_PRUNE_PER_DESTINATION", 2),
    ):
        assert prune_stale_atomic_json_tmp_files(cache_dir, (destination,)) == 2

    assert sum(path.exists() for path in candidates) == 2
    assert "exceeded 3 raw candidates" in caplog.text
    assert "capping this pass at 2" in caplog.text


@pytest.mark.parametrize("max_age_hours", [0, -1, float("nan"), float("inf")])
def test_prune_stale_atomic_json_tmp_files_rejects_unsafe_age(tmp_path, max_age_hours):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    scratch = cache_dir / ".moments.json.crashed.tmp"
    scratch.write_text("stale", encoding="utf-8")

    assert (
        prune_stale_atomic_json_tmp_files(
            cache_dir,
            (cache_dir / "moments.json",),
            max_age_hours=max_age_hours,
        )
        == 0
    )
    assert scratch.exists()


def test_prune_stale_atomic_json_tmp_files_rejects_symlinked_cache_root(tmp_path):
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    scratch = real_cache / ".moments.json.crashed.tmp"
    scratch.write_text("stale", encoding="utf-8")
    old_mtime = time.time() - 7 * 3600
    os.utime(scratch, (old_mtime, old_mtime))
    cache_link = tmp_path / "cache-link"
    cache_link.symlink_to(real_cache, target_is_directory=True)

    assert prune_stale_atomic_json_tmp_files(cache_link, (cache_link / "moments.json",)) == 0
    assert scratch.exists()
