"""Tests for the synthetic audio cache helper."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path

import pytest

from mammamiradio.audio import synth_cache
from mammamiradio.audio.synth_cache import duration_bucket_sec, materialize_synth_mp3, next_synth_variant


def _write(path: Path, payload: bytes = b"mp3") -> Path:
    path.write_bytes(payload)
    return path


def test_materialize_synth_mp3_miss_then_hit_reuses_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    calls = 0

    def _generator(path: Path) -> Path:
        nonlocal calls
        calls += 1
        return _write(path, b"cached")

    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"

    assert materialize_synth_mp3(cache_dir, "bed", first, {"mood": "lounge"}, _generator) == first
    assert materialize_synth_mp3(cache_dir, "bed", second, {"mood": "lounge"}, _generator) == second

    assert calls == 1
    assert first.read_bytes() == b"cached"
    assert second.read_bytes() == b"cached"
    assert len(list(cache_dir.glob("synth_bed_*.mp3"))) == 1


def test_materialize_synth_mp3_publishes_without_leaving_staging_files(tmp_path):
    cache_dir = tmp_path / "cache"
    out = tmp_path / "out.mp3"

    materialize_synth_mp3(cache_dir, "motif", out, {"signature": "chime"}, lambda path: _write(path, b"motif"))

    assert out.read_bytes() == b"motif"
    assert len(list(cache_dir.glob("synth_motif_*.mp3"))) == 1
    assert list(cache_dir.glob(".*.tmp*")) == []


def test_materialize_synth_mp3_empty_generation_is_not_cached(tmp_path):
    cache_dir = tmp_path / "cache"
    out = tmp_path / "out.mp3"

    materialize_synth_mp3(cache_dir, "foley", out, {"environment": "unknown"}, lambda path: path)

    assert not out.exists()
    assert list(cache_dir.glob("synth_foley_*.mp3")) == []


def test_materialize_synth_mp3_ignores_empty_existing_cache_entry(tmp_path):
    cache_dir = tmp_path / "cache"
    calls = 0

    def _generator(path: Path) -> Path:
        nonlocal calls
        calls += 1
        return _write(path, b"fresh")

    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"

    materialize_synth_mp3(cache_dir, "bed", first, {"mood": "lounge"}, _generator)
    cache_file = next(cache_dir.glob("synth_bed_*.mp3"))
    cache_file.write_bytes(b"")

    materialize_synth_mp3(cache_dir, "bed", second, {"mood": "lounge"}, _generator)

    assert calls == 2
    assert second.read_bytes() == b"fresh"
    assert cache_file.read_bytes() == b"fresh"


def test_materialize_synth_mp3_cache_setup_failure_generates_directly(tmp_path):
    cache_dir = tmp_path / "not-a-dir"
    cache_dir.write_bytes(b"occupied")
    out = tmp_path / "out.mp3"

    materialize_synth_mp3(cache_dir, "bed", out, {"mood": "lounge"}, lambda path: _write(path, b"direct"))

    assert out.read_bytes() == b"direct"


def test_materialize_synth_mp3_serializes_parallel_first_writers(tmp_path):
    cache_dir = tmp_path / "cache"
    calls = 0

    def _generator(path: Path) -> Path:
        nonlocal calls
        calls += 1
        time.sleep(0.02)
        return _write(path, b"shared")

    outputs = [tmp_path / f"out_{idx}.mp3" for idx in range(5)]

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(
            pool.map(
                lambda out: materialize_synth_mp3(cache_dir, "bed", out, {"mood": "lounge"}, _generator),
                outputs,
            )
        )

    assert calls == 1
    assert all(path.read_bytes() == b"shared" for path in outputs)


def test_materialize_synth_mp3_variant_is_part_of_cache_key(tmp_path):
    cache_dir = tmp_path / "cache"
    out_a = tmp_path / "a.mp3"
    out_b = tmp_path / "b.mp3"

    materialize_synth_mp3(
        cache_dir, "foley", out_a, {"environment": "cafe"}, lambda path: _write(path, b"a"), variant=0
    )
    materialize_synth_mp3(
        cache_dir, "foley", out_b, {"environment": "cafe"}, lambda path: _write(path, b"b"), variant=1
    )

    assert out_a.read_bytes() == b"a"
    assert out_b.read_bytes() == b"b"
    assert len(list(cache_dir.glob("synth_foley_*.mp3"))) == 2


def test_duration_bucket_rounds_up_and_never_returns_zero():
    assert duration_bucket_sec(0.01) == 1
    assert duration_bucket_sec(2.0) == 2
    assert duration_bucket_sec(2.01) == 3


def test_next_synth_variant_rotates_without_immediate_repeat():
    params = {"environment": "unique-test-room", "duration_sec": 7}

    variants = [next_synth_variant("foley", params) for _ in range(5)]

    assert set(variants[:3]) == {0, 1, 2}
    assert all(left != right for left, right in pairwise(variants))


def test_next_synth_variant_pool_size_one_always_zero():
    params = {"environment": "single-variant-room"}

    assert [next_synth_variant("foley", params, pool_size=1) for _ in range(4)] == [0, 0, 0, 0]


def test_materialize_synth_mp3_different_params_make_different_cache_files(tmp_path):
    cache_dir = tmp_path / "cache"

    materialize_synth_mp3(cache_dir, "bed", tmp_path / "a.mp3", {"mood": "lounge"}, lambda p: _write(p, b"a"))
    materialize_synth_mp3(cache_dir, "bed", tmp_path / "b.mp3", {"mood": "dramatic"}, lambda p: _write(p, b"b"))

    assert len(list(cache_dir.glob("synth_bed_*.mp3"))) == 2


def test_materialize_synth_mp3_generator_error_falls_back_to_direct(tmp_path):
    cache_dir = tmp_path / "cache"
    out = tmp_path / "out.mp3"
    calls = 0

    def _flaky(path: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("staging render blew up")
        return _write(path, b"direct")

    assert materialize_synth_mp3(cache_dir, "bed", out, {"mood": "lounge"}, _flaky) == out
    assert out.read_bytes() == b"direct"
    assert calls == 2
    assert list(cache_dir.glob("synth_bed_*.mp3")) == []


def test_materialize_synth_mp3_persistent_generator_error_propagates(tmp_path):
    cache_dir = tmp_path / "cache"

    def _always_fails(path: Path) -> Path:
        raise RuntimeError("ffmpeg unavailable")

    with pytest.raises(RuntimeError, match="ffmpeg unavailable"):
        materialize_synth_mp3(cache_dir, "bed", tmp_path / "out.mp3", {"mood": "lounge"}, _always_fails)


def test_materialize_synth_mp3_normalizes_path_and_unknown_param_values(tmp_path):
    cache_dir = tmp_path / "cache"
    calls = 0

    def _generator(path: Path) -> Path:
        nonlocal calls
        calls += 1
        return _write(path, b"cached")

    # Path values stringify and range() (an unhandled type) falls through to repr(),
    # so two identical calls must resolve to one stable key and reuse the cache.
    params = {"asset": Path("/sfx/chime.wav"), "span": range(3)}
    materialize_synth_mp3(cache_dir, "motif", tmp_path / "a.mp3", params, _generator)
    materialize_synth_mp3(cache_dir, "motif", tmp_path / "b.mp3", params, _generator)

    assert calls == 1
    assert len(list(cache_dir.glob("synth_motif_*.mp3"))) == 1


def test_valid_mp3_swallows_oserror(tmp_path, monkeypatch):
    target = _write(tmp_path / "x.mp3")

    def _boom(self):
        raise OSError("stat failed")

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "stat", _boom)

    assert synth_cache._valid_mp3(target) is False


def test_touch_atime_swallows_oserror(tmp_path, monkeypatch):
    target = _write(tmp_path / "x.mp3")

    def _boom(*_args, **_kwargs):
        raise OSError("utime failed")

    monkeypatch.setattr(synth_cache.os, "utime", _boom)

    # Best-effort atime touch must never raise into the audio path.
    synth_cache._touch_atime(target)
