"""Tests for streamer bitrate sourcing, runtime config helper, and ingress support."""

from __future__ import annotations

from pathlib import Path

from mammamiradio.config import load_config, runtime_json
from mammamiradio.streamer import _inject_ingress_prefix


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
        "fifo_path",
        "go_librespot_bin",
        "go_librespot_port",
        "tmp_dir",
    }
    assert result["fifo_path"] == config.audio.fifo_path
    assert result["go_librespot_bin"] == config.audio.go_librespot_bin


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


def test_inject_ingress_prefix_rewrites():
    """Non-empty prefix should rewrite all URL patterns."""
    prefix = "/api/hassio_ingress/abc123"
    # Test each pattern individually to avoid cross-replacement issues
    assert f"'{prefix}/stream'" in _inject_ingress_prefix("fetch('/stream')", prefix)
    assert f"'{prefix}/status'" in _inject_ingress_prefix("fetch('/status')", prefix)
    assert f"'{prefix}/public-status'" in _inject_ingress_prefix("fetch('/public-status')", prefix)
    assert f"'{prefix}/api/skip'" in _inject_ingress_prefix("fetch('/api/skip')", prefix)
    assert f'"{prefix}/listen"' in _inject_ingress_prefix('href="/listen"', prefix)
    assert f'href="{prefix}/listen"' in _inject_ingress_prefix('href="/listen"', prefix)
    assert f'src="{prefix}/stream"' in _inject_ingress_prefix('src="/stream"', prefix)


def test_inject_ingress_prefix_no_false_positives():
    """Prefix injection should not affect non-matching patterns."""
    html = "some random text with /stream in prose"
    result = _inject_ingress_prefix(html, "/prefix")
    # Only quoted URL patterns are replaced, not bare text
    assert result == html
