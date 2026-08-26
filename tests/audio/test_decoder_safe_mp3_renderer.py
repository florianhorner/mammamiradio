"""Unit contract for atomic, decoder-safe standalone MP3 rendering."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import mammamiradio.audio.normalizer as normalizer


def _staging_files(directory: Path) -> list[Path]:
    return list(directory.glob(".*.decoder-window-*.part"))


def test_render_decoder_safe_mp3_window_trims_samples_and_publishes_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "decoder-input.mp3"
    source.write_bytes(b"source")
    output = tmp_path / "public.mp3"
    output.write_bytes(b"old")
    invocation: dict[str, object] = {}

    def _render(cmd: list[str], description: str, **kwargs: object) -> None:
        invocation.update(cmd=cmd, description=description, kwargs=kwargs)
        staging_path = Path(cmd[-1])
        assert staging_path != output
        assert output.read_bytes() == b"old"
        staging_path.write_bytes(b"standalone-mp3")

    monkeypatch.setattr(normalizer, "_run_ffmpeg", _render)

    result = normalizer.render_decoder_safe_mp3_window(
        source,
        output,
        preroll_samples=11_520,
        sample_count=57_600,
        sample_rate=48_000,
        bitrate_kbps=160,
    )

    assert result == output
    assert output.read_bytes() == b"standalone-mp3"
    assert not _staging_files(tmp_path)
    cmd = invocation["cmd"]
    assert isinstance(cmd, list)
    assert cmd[cmd.index("-filter:a") + 1] == ("atrim=start_sample=11520:end_sample=69120,asetpts=PTS-STARTPTS")
    assert cmd[cmd.index("-ar") + 1] == "48000"
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "160k"
    assert cmd[cmd.index("-write_xing") + 1] == "1"
    assert cmd[cmd.index("-f") + 1] == "mp3"
    assert invocation["kwargs"] == {"background": True}


@pytest.mark.parametrize(
    "tool_error",
    [
        subprocess.CalledProcessError(1, ["ffmpeg"]),
        subprocess.TimeoutExpired(["ffmpeg"], 180),
    ],
)
def test_render_decoder_safe_mp3_window_preserves_output_and_cleans_partial_on_tool_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_error: Exception,
) -> None:
    source = tmp_path / "decoder-input.mp3"
    source.write_bytes(b"source")
    output = tmp_path / "public.mp3"
    output.write_bytes(b"old")

    def _fail(cmd: list[str], *_args: object, **_kwargs: object) -> None:
        Path(cmd[-1]).write_bytes(b"partial")
        raise tool_error

    monkeypatch.setattr(normalizer, "_run_ffmpeg", _fail)

    with pytest.raises(normalizer.DecoderSafeMp3RenderError) as caught:
        normalizer.render_decoder_safe_mp3_window(
            source,
            output,
            preroll_samples=1152,
            sample_count=2304,
            sample_rate=48_000,
            bitrate_kbps=192,
        )

    assert caught.value.__cause__ is tool_error
    assert output.read_bytes() == b"old"
    assert not _staging_files(tmp_path)


def test_render_decoder_safe_mp3_window_rejects_empty_ffmpeg_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "decoder-input.mp3"
    source.write_bytes(b"source")
    output = tmp_path / "public.mp3"
    monkeypatch.setattr(normalizer, "_run_ffmpeg", lambda *_args, **_kwargs: None)

    with pytest.raises(normalizer.DecoderSafeMp3RenderError, match="empty"):
        normalizer.render_decoder_safe_mp3_window(
            source,
            output,
            preroll_samples=0,
            sample_count=1152,
            sample_rate=48_000,
            bitrate_kbps=192,
        )

    assert not output.exists()
    assert not _staging_files(tmp_path)


def test_render_decoder_safe_mp3_window_cleans_staging_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "decoder-input.mp3"
    source.write_bytes(b"source")
    output = tmp_path / "public.mp3"
    output.write_bytes(b"old")

    def _render(cmd: list[str], *_args: object, **_kwargs: object) -> None:
        Path(cmd[-1]).write_bytes(b"complete")

    def _fail_replace(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(normalizer, "_run_ffmpeg", _render)
    monkeypatch.setattr(normalizer.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="disk full"):
        normalizer.render_decoder_safe_mp3_window(
            source,
            output,
            preroll_samples=0,
            sample_count=1152,
            sample_rate=48_000,
            bitrate_kbps=192,
        )

    assert output.read_bytes() == b"old"
    assert not _staging_files(tmp_path)


@pytest.mark.parametrize(
    ("preroll_samples", "sample_count", "sample_rate", "bitrate_kbps"),
    [
        (-1, 1152, 48_000, 192),
        (0, 0, 48_000, 192),
        (0, 1152, 0, 192),
        (0, 1152, 48_000, 0),
        (False, 1152, 48_000, 192),
        (0, True, 48_000, 192),
        (0, 1152, 48_000.0, 192),
        (0, 1152, 48_000, 192.0),
    ],
)
def test_render_decoder_safe_mp3_window_rejects_invalid_sample_bounds_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preroll_samples: Any,
    sample_count: Any,
    sample_rate: Any,
    bitrate_kbps: Any,
) -> None:
    run_ffmpeg = pytest.fail
    monkeypatch.setattr(normalizer, "_run_ffmpeg", run_ffmpeg)
    output = tmp_path / "nested" / "public.mp3"

    with pytest.raises(ValueError):
        normalizer.render_decoder_safe_mp3_window(
            tmp_path / "source.mp3",
            output,
            preroll_samples=preroll_samples,
            sample_count=sample_count,
            sample_rate=sample_rate,
            bitrate_kbps=bitrate_kbps,
        )

    assert not output.parent.exists()
