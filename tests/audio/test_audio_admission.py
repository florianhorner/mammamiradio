"""Admission tests for the shared ffmpeg/ffprobe concurrency gate."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from unittest.mock import patch

import pytest

from mammamiradio.audio import admission, audio_quality, normalizer
from mammamiradio.core.models import SegmentType, Track
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


@pytest.mark.parametrize(
    ("attr", "width"),
    [("_NORM_SEM", 2), ("_BACKGROUND_SEM", 1), ("_RESCUE_SEM", 1)],
)
def test_admission_sems_stay_bounded(attr, width):
    sem = getattr(admission, attr)
    assert isinstance(sem, BoundedSemaphore)
    assert sem._initial_value == width
    assert sem._value == width


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


def test_background_ffmpeg_does_not_block_foreground(monkeypatch):
    background_started = threading.Event()
    foreground_started = threading.Event()
    release_background = threading.Event()

    def fake_run(cmd, *args, **kwargs):
        if "background" in cmd:
            background_started.set()
            assert release_background.wait(1)
        if "foreground" in cmd:
            foreground_started.set()
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        background = pool.submit(
            normalizer._run_ffmpeg,
            ["ffmpeg", "background"],
            "background prefetch",
            background=True,
        )
        assert background_started.wait(1)
        foreground = pool.submit(normalizer._run_ffmpeg, ["ffmpeg", "foreground"], "next-to-air")
        assert foreground_started.wait(0.2)
        release_background.set()
        foreground.result(timeout=1)
        background.result(timeout=1)


@pytest.mark.parametrize(
    ("lane_kwargs", "label"),
    [({"background": True}, "background"), ({"rescue": True}, "rescue")],
)
def test_lane_serializes_same_lane_work(monkeypatch, lane_kwargs, label):
    first_started = threading.Event()
    release_first = threading.Event()
    starts: list[str] = []
    lock = threading.Lock()

    def fake_run(cmd, *args, **kwargs):
        name = cmd[1]
        with lock:
            starts.append(name)
        if name == f"{label}-1":
            first_started.set()
            assert release_first.wait(1)
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            normalizer._run_ffmpeg,
            ["ffmpeg", f"{label}-1"],
            f"{label} 1",
            **lane_kwargs,
        )
        assert first_started.wait(1)
        second = pool.submit(
            normalizer._run_ffmpeg,
            ["ffmpeg", f"{label}-2"],
            f"{label} 2",
            **lane_kwargs,
        )
        time.sleep(0.05)
        assert starts == [f"{label}-1"]
        release_first.set()
        first.result(timeout=1)
        second.result(timeout=1)

    assert starts == [f"{label}-1", f"{label}-2"]


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
    with patch.object(admission, "_NORM_SEM", sem):
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
    assert admission._NORM_SEM.acquire(blocking=False)
    assert admission._NORM_SEM.acquire(blocking=False)
    try:
        thread = threading.Thread(target=normalizer.generate_tone, args=(tmp_path / "tone.mp3",), daemon=True)
        thread.start()
        assert not normal_started.wait(0.05)

        normalizer._run_ffmpeg(["ffmpeg", "rescue"], "emergency rescue", rescue=True)
        assert rescue_started.is_set()
        assert not normal_started.is_set()
    finally:
        admission._NORM_SEM.release()
        admission._NORM_SEM.release()

    thread.join(timeout=1)
    assert normal_started.is_set()


def test_rescue_proceeds_ungated_after_acquire_timeout(monkeypatch):
    """A wedged rescue render must not delay the next emergency fill past the
    short acquire timeout — the second rescue proceeds ungated (INSTANT AUDIO)."""
    monkeypatch.setattr(admission, "_RESCUE_ACQUIRE_TIMEOUT_SEC", 0.05)
    ran = threading.Event()

    def fake_run(cmd, *args, **kwargs):
        ran.set()
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert admission._RESCUE_SEM.acquire(blocking=False)  # simulate a wedged rescue holder
    try:
        t0 = time.perf_counter()
        normalizer._run_ffmpeg(["ffmpeg", "rescue"], "second rescue", rescue=True)
        elapsed = time.perf_counter() - t0
    finally:
        admission._RESCUE_SEM.release()

    assert ran.is_set()
    assert elapsed < 1.0


def test_combined_admission_ceiling_stays_at_three(monkeypatch):
    """Foreground + background + rescue lanes together never exceed 3 concurrent
    ffmpeg runs — the gated worst case the Pi CPU budget is sized against."""
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
    jobs = [
        {"rescue": False, "background": False},
        {"rescue": False, "background": False},
        {"rescue": False, "background": False},
        {"background": True},
        {"background": True},
        {"rescue": True},
        {"rescue": True},
    ]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [
            pool.submit(normalizer._run_ffmpeg, ["ffmpeg", f"job-{i}"], f"job {i}", **kwargs)
            for i, kwargs in enumerate(jobs)
        ]
        for future in futures:
            future.result(timeout=5)

    assert max_active <= 3


def test_post_restart_rescue_renders_despite_starved_gates(tmp_path, monkeypatch):
    """Scenario 3 (post-restart): admission state is process-local and fresh after a
    restart; even with the whole ordinary pipeline starved (both norm slots and the
    background slot held), an error-recovery rescue silence render must start
    immediately so a listener connecting after a restart still gets audio."""
    monkeypatch.setattr(admission, "_NORM_SEM", BoundedSemaphore(2))
    monkeypatch.setattr(admission, "_BACKGROUND_SEM", BoundedSemaphore(1))
    monkeypatch.setattr(admission, "_RESCUE_SEM", BoundedSemaphore(1))
    started = threading.Event()

    def fake_run(cmd, *args, **kwargs):
        started.set()
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert admission._NORM_SEM.acquire(blocking=False)
    assert admission._NORM_SEM.acquire(blocking=False)
    assert admission._BACKGROUND_SEM.acquire(blocking=False)
    try:
        normalizer.generate_silence(tmp_path / "rescue.mp3", 5.0, rescue=True)
    finally:
        admission._NORM_SEM.release()
        admission._NORM_SEM.release()
        admission._BACKGROUND_SEM.release()

    assert started.is_set()


def test_download_sync_direct_url_validates_with_background(tmp_path, monkeypatch):
    """The prefetch download chain threads background admission down to the
    direct-url ffprobe leg — guards the downloader-internal calls the producer
    boundary mocks cannot see."""
    track = Track(
        title="Song",
        artist="Artist",
        duration_ms=180_000,
        source="jamendo",
        direct_url="https://storage.jamendo.com/tracks/1.mp3",
    )
    seen: dict[str, bool] = {}

    monkeypatch.setattr(
        downloader._NO_REDIRECT_OPENER,
        "open",
        lambda url, timeout=10: io.BytesIO(b"x" * (600 * 1024)),
    )

    def fake_validate(path, *, background=False):
        seen["background"] = background
        return True, ""

    monkeypatch.setattr(downloader, "validate_download", fake_validate)
    result = downloader._download_sync(track, tmp_path, tmp_path / "music", background=True)

    assert seen["background"] is True
    assert result.exists()


def test_download_sync_silence_fallback_is_background(tmp_path, monkeypatch):
    """The last-resort silence render inherits the background flag so prefetch
    never takes a foreground slot for placeholder audio."""
    monkeypatch.delenv("MAMMAMIRADIO_ALLOW_YTDLP", raising=False)
    track = Track(title="Song", artist="Artist", duration_ms=180_000, source="youtube")
    seen: dict[str, bool] = {}

    def fake_run_ffmpeg(cmd, description, *, rescue=False, background=False):
        seen["background"] = background
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(downloader, "_run_ffmpeg", fake_run_ffmpeg)
    result = downloader._download_sync(track, tmp_path, tmp_path / "music", background=True)

    assert seen["background"] is True
    assert result.name.startswith("_silence_")


def test_ordinary_silence_generation_stays_bounded(tmp_path, monkeypatch):
    started = threading.Event()

    def fake_run(cmd, *args, **kwargs):
        started.set()
        return _completed(list(cmd), text=bool(kwargs.get("text")))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert admission._NORM_SEM.acquire(blocking=False)
    assert admission._NORM_SEM.acquire(blocking=False)
    try:
        thread = threading.Thread(target=normalizer.generate_silence, args=(tmp_path / "silence.mp3",), daemon=True)
        thread.start()
        assert not started.wait(0.05)
    finally:
        admission._NORM_SEM.release()
        admission._NORM_SEM.release()

    thread.join(timeout=1)
    assert started.is_set()
