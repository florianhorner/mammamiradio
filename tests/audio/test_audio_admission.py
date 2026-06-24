"""Admission tests for the shared ffmpeg/ffprobe concurrency gate."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from unittest.mock import patch

import pytest

from mammamiradio.audio import audio_quality, normalizer
from mammamiradio.core.models import SegmentType
from mammamiradio.playlist import downloader


@pytest.fixture(autouse=True)
def _restore_audio_admission_globals():
    reconcile = normalizer._loudness_reconcile
    reconcile_args = list(normalizer._reconcile_output_args)
    broadcast_args = None if normalizer._broadcast_output_args is None else list(normalizer._broadcast_output_args)
    normalizer._loudness_reconcile = None
    normalizer._reconcile_output_args = list(normalizer._MP3_OUTPUT_ARGS)
    normalizer._broadcast_output_args = None
    yield
    normalizer._loudness_reconcile = reconcile
    normalizer._reconcile_output_args = reconcile_args
    normalizer._broadcast_output_args = broadcast_args


def _completed(cmd: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
    stdout: str = ""
    stderr: str = ""
    joined = " ".join(cmd)

    if cmd[0] == "ffprobe" and "-print_format" in cmd:
        stdout = json.dumps({"format": {"duration": "45.0"}})
    elif cmd[0] == "ffprobe":
        stdout = "10.0\n"
    elif "ebur128=peak=true" in joined:
        stderr = "Integrated loudness:\n    I:         -16.0 LUFS\n"
    elif "volumedetect" in joined:
        stderr = "mean_volume: -20.0 dB\nmax_volume: -6.0 dB\n"
    elif "silencedetect" in joined:
        stderr = ""

    if not text:
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout.encode(), stderr=stderr.encode())
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=stderr)


def test_norm_sem_stays_bounded_semaphore_two():
    assert isinstance(normalizer._NORM_SEM, BoundedSemaphore)
    assert normalizer._NORM_SEM._initial_value == 2
    assert normalizer._NORM_SEM._value == 2


def test_global_ffmpeg_admission_caps_non_rescue_paths(tmp_path, monkeypatch):
    """All normal ffmpeg/ffprobe paths share one 2-wide leaf gate."""
    small = tmp_path / "small.mp3"
    small.write_bytes(b"x" * 2048)
    big = tmp_path / "download.mp3"
    big.write_bytes(b"x" * (600 * 1024))

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run(cmd, *args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            return _completed(list(cmd), text=bool(kwargs.get("text")))
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(subprocess, "run", fake_run)
    calls = [
        lambda: normalizer.generate_tone(tmp_path / "tone.mp3"),
        lambda: normalizer.generate_sweep(tmp_path / "sweep.mp3"),
        lambda: normalizer.concat_files([small, small], tmp_path / "concat.mp3"),
        lambda: normalizer.mix_voice_with_bed(small, small, tmp_path / "bed.mp3"),
        lambda: normalizer.crossfade_voice_over_music(small, small, tmp_path / "crossfade.mp3"),
        lambda: normalizer.measure_lufs(small),
        lambda: normalizer.probe_duration_sec(small),
        lambda: audio_quality.validate_segment_audio(small, SegmentType.BANTER),
        lambda: downloader.validate_download(big),
    ]

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        list(pool.map(lambda fn: fn(), calls))

    assert max_active <= 2


class _NonNestingSemaphore:
    def __init__(self) -> None:
        self.depth = 0
        self.entries = 0

    def __enter__(self):
        if self.depth:
            raise AssertionError("ffmpeg slot acquired while already held")
        self.depth += 1
        self.entries += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        return False


def test_composite_paths_do_not_nest_ffmpeg_slots(tmp_path, monkeypatch):
    """Former composite wrappers would double-acquire once the leaf gate exists."""
    small = tmp_path / "small.mp3"
    small.write_bytes(b"x" * 2048)
    sem = _NonNestingSemaphore()

    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: _completed(list(cmd), text=bool(kw.get("text"))))
    normalizer.configure_loudness_reconcile(-18.0, -17.0)
    with patch.object(normalizer, "_NORM_SEM", sem):
        normalizer.normalize(small, tmp_path / "norm.mp3")
        normalizer.mix_voice_with_bed(small, small, tmp_path / "bed.mp3")
        normalizer.mix_voice_with_sting(small, small, tmp_path / "sting.mp3")
        normalizer.concat_files([small, small], tmp_path / "concat.mp3")

    assert sem.entries > 4


def test_rescue_ffmpeg_bypasses_full_gate_but_normal_helpers_do_not(tmp_path, monkeypatch):
    normal_started = threading.Event()
    rescue_started = threading.Event()

    def fake_run(cmd, *args, **kwargs):
        if "rescue" in cmd:
            rescue_started.set()
        else:
            normal_started.set()
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert normalizer._NORM_SEM.acquire(blocking=False)
    assert normalizer._NORM_SEM.acquire(blocking=False)
    try:
        thread = threading.Thread(target=normalizer.generate_tone, args=(tmp_path / "tone.mp3",), daemon=True)
        thread.start()
        assert not normal_started.wait(0.05)

        normalizer._run_ffmpeg(["ffmpeg", "rescue"], "emergency rescue", rescue=True)
        assert rescue_started.is_set()
        assert not normal_started.is_set()
    finally:
        normalizer._NORM_SEM.release()
        normalizer._NORM_SEM.release()

    thread.join(timeout=1)
    assert normal_started.is_set()


def test_ordinary_silence_generation_stays_bounded(tmp_path, monkeypatch):
    started = threading.Event()

    def fake_run(cmd, *args, **kwargs):
        started.set()
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert normalizer._NORM_SEM.acquire(blocking=False)
    assert normalizer._NORM_SEM.acquire(blocking=False)
    try:
        thread = threading.Thread(target=normalizer.generate_silence, args=(tmp_path / "silence.mp3",), daemon=True)
        thread.start()
        assert not started.wait(0.05)
    finally:
        normalizer._NORM_SEM.release()
        normalizer._NORM_SEM.release()

    thread.join(timeout=1)
    assert started.is_set()
