"""Real-MP3 proof for Moment Picker frame indexing and frozen capture output."""

from __future__ import annotations

import array
import math
import subprocess
import sys
from collections import deque
from pathlib import Path

import httpx
import pytest

from mammamiradio.core.models import Segment, SegmentType
from mammamiradio.web.mp3_frames import Mp3FrameIndexError, build_mpeg1_layer3_frame_index
from mammamiradio.web.streamer import _append_clip_chunk, _mark_clip_segment_start
from tests.web.test_streamer_routes import _make_test_app

pytestmark = pytest.mark.requires_ffmpeg


def _render_mp3(path: Path, *, vbr: bool) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "12",
        "-c:a",
        "libmp3lame",
    ]
    command.extend(["-q:a", "4"] if vbr else ["-b:a", "128k"])
    command.append(str(path))
    subprocess.run(command, check=True)


def _decode(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "null", "-"],
        check=True,
    )


def _render_transition_mp3(path: Path, *, vbr: bool) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=220:sample_rate=44100:duration=16",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:sample_rate=44100:duration=18",
        "-filter_complex",
        "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",
    ]
    command.extend(["-q:a", "4"] if vbr else ["-b:a", "128k"])
    command.append(str(path))
    subprocess.run(command, check=True)


def _decode_pcm(path: Path) -> list[int]:
    rendered = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "44100",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    )
    samples = array.array("h")
    samples.frombytes(rendered.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tolist()


def _tone_power(samples: list[int], frequency: float, sample_rate: int = 44_100) -> float:
    mean = sum(samples) / max(1, len(samples))
    real = sum(
        (sample - mean) * math.cos(2 * math.pi * frequency * n / sample_rate) for n, sample in enumerate(samples)
    )
    imag = sum(
        (sample - mean) * math.sin(2 * math.pi * frequency * n / sample_rate) for n, sample in enumerate(samples)
    )
    return (real * real + imag * imag) / max(1, len(samples) ** 2)


@pytest.mark.parametrize("vbr", [False, True])
def test_frame_index_tracks_complete_real_cbr_and_vbr_frames(tmp_path: Path, vbr: bool) -> None:
    source = tmp_path / ("vbr.mp3" if vbr else "cbr.mp3")
    _render_mp3(source, vbr=vbr)
    raw = source.read_bytes()

    index = build_mpeg1_layer3_frame_index(raw)

    assert len(index.frames) > 400
    assert index.data_start > 0  # leading ID3/Xing bytes are not a fake frame
    assert index.duration_sec > 11.5
    assert all(frame.duration_sec == pytest.approx(1152 / 44_100) for frame in index.frames)
    assert all(a.byte_end == b.byte_start for a, b in zip(index.frames, index.frames[1:], strict=False))
    if vbr:
        assert len({frame.byte_end - frame.byte_start for frame in index.frames}) > 2


def test_frame_index_discards_only_partial_edges_and_rejects_bad_interior(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    _render_mp3(source, vbr=True)
    raw = source.read_bytes()
    retained = raw[137:-113]  # intentionally begin/end inside real frames

    index = build_mpeg1_layer3_frame_index(retained)
    assert index.data_start > 0
    assert index.data_end < len(retained)
    aligned = tmp_path / "aligned.mp3"
    aligned.write_bytes(retained[index.data_start : index.data_end])
    _decode(aligned)

    broken = bytearray(retained)
    interior = index.frames[len(index.frames) // 2].byte_start
    broken[interior : interior + 4] = b"\0\0\0\0"
    with pytest.raises(Mp3FrameIndexError):
        build_mpeg1_layer3_frame_index(bytes(broken))


@pytest.mark.asyncio
@pytest.mark.parametrize("vbr", [False, True])
async def test_capture_and_final_commit_decode_after_mid_frame_retention(tmp_path: Path, vbr: bool) -> None:
    source = tmp_path / ("source-vbr.mp3" if vbr else "source-cbr.mp3")
    _render_mp3(source, vbr=vbr)
    # Both retained edges are deliberately non-frame-aligned. The endpoint must
    # emit only the parser's complete internal frame run, never synthesize one.
    retained = source.read_bytes()[137:-113]
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    app.state.clip_ring_buffer = deque()
    app.state.clip_buffer_max_bytes = len(retained) + 4096
    app.state.clip_buffer_bytes = 0
    app.state.clip_buffer_start_byte = 0
    app.state.clip_bytes_total = 0
    app.state.clip_generation = 0
    app.state.clip_marks = []
    _mark_clip_segment_start(
        app,
        Segment(
            type=SegmentType.BANTER,
            path=source,
            metadata={"title": "Frame-perfect moment", "clip_audio_class": "speech"},
        ),
    )
    for offset in range(0, len(retained), 4096):
        _append_clip_chunk(app, retained[offset : offset + 4096])

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        capture_response = await client.post("/api/clip/capture")
        assert capture_response.status_code == 201
        capture = capture_response.json()
        preview_response = await client.get(capture["audio_path"])
        assert preview_response.status_code == 200
        preview = tmp_path / "preview.mp3"
        preview.write_bytes(preview_response.content)
        _decode(preview)

        committed = await client.post(
            "/api/clip/commit",
            json={"capture_id": capture["capture_id"], "choice_id": "moment"},
        )
        assert committed.status_code == 201
        final_path = app.state.config.cache_dir / "clips" / f"{committed.json()['clip_id']}.mp3"
    _decode(final_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("vbr", [False, True])
async def test_capture_choice_onset_excludes_decoder_preroll(tmp_path: Path, vbr: bool) -> None:
    source = tmp_path / ("transition-vbr.mp3" if vbr else "transition-cbr.mp3")
    _render_transition_mp3(source, vbr=vbr)
    retained = source.read_bytes()[137:-113]
    app = _make_test_app()
    app.state.config.cache_dir = tmp_path / "cache"
    app.state.clip_ring_buffer = deque()
    app.state.clip_buffer_max_bytes = len(retained) + 4096
    app.state.clip_buffer_bytes = 0
    app.state.clip_buffer_start_byte = 0
    app.state.clip_bytes_total = 0
    app.state.clip_generation = 0
    app.state.clip_marks = []
    _mark_clip_segment_start(
        app,
        Segment(
            type=SegmentType.BANTER,
            path=source,
            metadata={"title": "Clean onset", "clip_audio_class": "speech"},
        ),
    )
    for offset in range(0, len(retained), 4096):
        _append_clip_chunk(app, retained[offset : offset + 4096])

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        captured = (await client.post("/api/clip/capture")).json()
        moment = next(choice for choice in captured["choices"] if choice["choice_id"] == "moment")
        preview_response = await client.get(captured["audio_path"])
        preview = tmp_path / "transition-preview.mp3"
        preview.write_bytes(preview_response.content)
        committed = await client.post(
            "/api/clip/commit",
            json={"capture_id": captured["capture_id"], "choice_id": "moment"},
        )
        final_path = app.state.config.cache_dir / "clips" / f"{committed.json()['clip_id']}.mp3"

    preview_pcm = _decode_pcm(preview)
    final_pcm = _decode_pcm(final_path)
    onset_samples = int(0.2 * 44_100)
    preview_start = round(moment["in_sec"] * 44_100)
    preview_onset = preview_pcm[preview_start : preview_start + onset_samples]
    final_onset = final_pcm[:onset_samples]
    for onset in (preview_onset, final_onset):
        rms = math.sqrt(sum(sample * sample for sample in onset) / len(onset))
        assert rms > 500
        assert _tone_power(onset, 880) > _tone_power(onset, 220) * 4

    final_duration = len(final_pcm) / 44_100
    assert final_duration == pytest.approx(moment["duration_sec"], abs=1152 / 44_100)
