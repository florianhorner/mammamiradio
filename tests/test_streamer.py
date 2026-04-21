"""Tests for streamer bitrate sourcing, runtime config helper, and ingress support."""

from __future__ import annotations

import io
from pathlib import Path

from mammamiradio.config import load_config, runtime_json
from mammamiradio.streamer import _inject_ingress_prefix, _skip_id3_and_xing_header


def test_streamer_uses_audio_bitrate_for_throttle():
    """run_playback_loop reads config.audio.bitrate, not a station-level field."""
    import ast

    src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
    tree = ast.parse(src)
    # Find bytes_per_sec assignment inside run_playback_loop
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_playback_loop":
            body_src = ast.get_source_segment(src, node)
            assert "config.audio.bitrate" in body_src
            assert "config.station.bitrate" not in body_src
            break
    else:
        raise AssertionError("run_playback_loop not found")


def test_icy_br_uses_audio_bitrate():
    """The /stream ICY header must reference audio.bitrate."""
    src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
    assert "config.audio.bitrate" in src
    assert "config.station.bitrate" not in src


def test_runtime_json_output():
    """runtime_json returns expected keys from the loaded config."""
    toml_path = Path(__file__).parent.parent / "radio.toml"
    config = load_config(str(toml_path))
    result = runtime_json(config)
    assert set(result.keys()) == {
        "bind_host",
        "port",
        "tmp_dir",
    }
    assert result["bind_host"] == config.bind_host
    assert result["port"] == config.port


def test_legacy_station_bitrate_migrated(tmp_path, monkeypatch):
    """If radio.toml has station.bitrate but no audio.bitrate, it migrates."""
    toml_content = """
[station]
name = "Test"
language = "it"
bitrate = 128

[[hosts]]
name = "Host"
voice = "it-IT-DiegoNeural"
style = "test"
"""
    toml_file = tmp_path / "radio.toml"
    toml_file.write_text(toml_content)
    config = load_config(str(toml_file))
    assert config.audio.bitrate == 128
    assert not hasattr(config.station, "bitrate")


# --- Ingress prefix injection tests ---


def test_inject_ingress_prefix_empty():
    """Empty prefix should return HTML unchanged."""
    html = """<script>fetch('/stream')</script>"""
    assert _inject_ingress_prefix(html, "") is html


def test_inject_ingress_prefix_rewrites_html_attributes():
    """Non-empty prefix should rewrite static HTML attributes only."""
    prefix = "/api/hassio_ingress/abc123"
    # Static HTML attributes are rewritten
    assert f'href="{prefix}/listen"' in _inject_ingress_prefix('href="/listen"', prefix)
    assert f'src="{prefix}/stream"' in _inject_ingress_prefix('src="/stream"', prefix)


def test_inject_ingress_prefix_does_not_rewrite_js_strings():
    """Single-quoted JS strings must NOT be rewritten — _base handles them."""
    prefix = "/api/hassio_ingress/abc123"
    # JS patterns like _base + '/stream' must stay untouched
    js = "_base + '/stream'"
    assert _inject_ingress_prefix(js, prefix) == js
    js2 = "_base + '/status'"
    assert _inject_ingress_prefix(js2, prefix) == js2
    js3 = "fetch(_base + '/api/skip')"
    assert _inject_ingress_prefix(js3, prefix) == js3


def test_inject_ingress_prefix_no_false_positives():
    """Prefix injection should not affect non-matching patterns."""
    html = "some random text with /stream in prose"
    result = _inject_ingress_prefix(html, "/prefix")
    assert result == html


def test_inject_ingress_prefix_rewrites_static_paths():
    """Ingress prefix should rewrite /static/ asset references."""
    prefix = "/api/hassio_ingress/abc123"
    assert f'"{prefix}/static/manifest.json"' in _inject_ingress_prefix('href="/static/manifest.json"', prefix)
    assert f'"{prefix}/static/icon-192.svg"' in _inject_ingress_prefix('href="/static/icon-192.svg"', prefix)


def test_inject_ingress_prefix_rewrites_sw_path():
    """Ingress prefix should rewrite /sw.js reference."""
    prefix = "/api/hassio_ingress/abc123"
    assert f"'{prefix}/sw.js'" in _inject_ingress_prefix("register('/sw.js')", prefix)


# --- Safari banter-cutoff guard: _skip_id3_and_xing_header ---
#
# Regression fixtures for the H2 fix. The producer emits each banter/news
# segment with an ID3v2 tag + a leading Xing/Info metadata frame that declares
# the file's duration. When those land mid-stream Safari treats the duration
# as end-of-track and fires `ended` after ~9 s, chopping the segment short.
# The helper strips both so the stream reads as a continuous ICECast feed.
# MPEG-1 Layer III, 192 kbps, 48 kHz stereo — matches _MP3_OUTPUT_ARGS.

_L3_HEADER = bytes([0xFF, 0xFB, 0xB4, 0x00])  # MPEG-1 L3, 192kbps, 48kHz, stereo, no pad
_L3_FRAME_LEN = 576
_L3_SIDE_INFO_LEN = 32  # offset 4..36 inside frame for stereo


def _l3_frame(body_bytes: bytes) -> bytes:
    """Produce a 576-byte frame with the given body at offset 4 (side info + data)."""
    pad = _L3_FRAME_LEN - 4 - len(body_bytes)
    assert pad >= 0, "body too large for one frame"
    return _L3_HEADER + body_bytes + b"\x00" * pad


def _xing_frame(magic: bytes = b"Info") -> bytes:
    """Xing/Info metadata frame: side info zeroed, magic at offset 36."""
    body = b"\x00" * _L3_SIDE_INFO_LEN + magic + b"\x00" * 12  # frames/bytes/toc/quality
    return _l3_frame(body)


def _audio_frame() -> bytes:
    """Plain audio frame — no Xing/Info magic."""
    body = b"\x00" * _L3_SIDE_INFO_LEN + b"DATA" + b"\x00" * 12
    return _l3_frame(body)


def _id3v2(size: int = 0) -> bytes:
    """Empty ID3v2.4 tag with the given payload size encoded sync-safe."""
    b = bytearray(b"ID3\x04\x00\x00")
    b += bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )
    b += b"\x00" * size
    return bytes(b)


def test_skip_id3_and_xing_on_banter_shape():
    """ID3v2 + Xing/Info + audio frame → pointer lands at audio frame start."""
    buf = io.BytesIO(_id3v2(16) + _xing_frame(b"Info") + _audio_frame())
    _skip_id3_and_xing_header(buf)
    tail = buf.read()
    assert tail[:4] == _L3_HEADER
    assert b"DATA" in tail
    # The Xing frame was consumed — only one audio frame remains.
    assert len(tail) == _L3_FRAME_LEN


def test_skip_xing_variant_magic():
    """Both 'Xing' (VBR) and 'Info' (CBR) magics are recognised."""
    buf = io.BytesIO(_xing_frame(b"Xing") + _audio_frame())
    _skip_id3_and_xing_header(buf)
    tail = buf.read()
    assert b"DATA" in tail
    assert len(tail) == _L3_FRAME_LEN


def test_skip_keeps_audio_when_no_xing():
    """ID3v2 + audio (no Xing) → strip only the tag, keep the audio frame."""
    buf = io.BytesIO(_id3v2(8) + _audio_frame() + _audio_frame())
    _skip_id3_and_xing_header(buf)
    tail = buf.read()
    assert tail[:4] == _L3_HEADER
    assert b"DATA" in tail
    # Both audio frames preserved.
    assert len(tail) == _L3_FRAME_LEN * 2


def test_skip_no_id3_no_xing_is_noop():
    """Raw audio stream (no tag, no Xing) → pointer stays at 0."""
    payload = _audio_frame() + _audio_frame()
    buf = io.BytesIO(payload)
    _skip_id3_and_xing_header(buf)
    assert buf.tell() == 0
    assert buf.read() == payload


def test_skip_malformed_header_rewinds_to_zero():
    """Garbage input must not crash or lose bytes — defensive rewind."""
    buf = io.BytesIO(b"not an mp3 at all, just text")
    _skip_id3_and_xing_header(buf)
    assert buf.tell() == 0


def test_skip_truncated_input_rewinds_safely():
    """Input shorter than the ID3 header still rewinds without raising."""
    buf = io.BytesIO(b"\x00\x01\x02")
    _skip_id3_and_xing_header(buf)
    assert buf.tell() == 0


def test_skip_on_live_banter_file_lands_on_audio_frame():
    """If a live banter file is sitting in tmp/, the helper lands on an MP3 sync word.

    Non-fatal if no file present (CI, cold worktrees). When present, this is the
    end-to-end guard: a real ffmpeg-produced banter gets its Xing stripped and the
    next byte is a valid MPEG-1 L3 frame header.
    """
    candidates = list((Path(__file__).parent.parent / "tmp").glob("banter_full_*.mp3"))
    if not candidates:
        return
    with candidates[0].open("rb") as f:
        _skip_id3_and_xing_header(f)
        head = f.read(4)
    assert len(head) == 4
    assert head[0] == 0xFF
    assert (head[1] & 0xE0) == 0xE0, f"expected MP3 sync word, got {head!r}"


def test_run_playback_loop_strips_per_segment_metadata():
    """Guard: the playback loop must call the stripper before the read loop.

    Static check on source — the runtime side is covered by the live-file test
    above. Ensures a refactor can't quietly remove the fix without the test
    suite noticing.
    """
    import ast

    src = (Path(__file__).parent.parent / "mammamiradio" / "streamer.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_playback_loop":
            body_src = ast.get_source_segment(src, node)
            assert "_skip_id3_and_xing_header" in body_src, (
                "run_playback_loop must strip ID3/Xing before broadcasting (Safari banter cutoff regression guard)"
            )
            break
    else:
        raise AssertionError("run_playback_loop not found")
