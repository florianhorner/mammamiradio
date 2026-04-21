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


def test_inject_ingress_prefix_rewrites_script_src_static():
    """Ingress prefix must rewrite <script src="/static/..."> alongside href= attributes.

    Guards the dashboard.html refactor that moved inline JS into /static/script.js.
    Without this, HA Ingress users hit dead <script> tags and the dashboard loses
    all interactivity under the Supervisor proxy.
    """
    prefix = "/api/hassio_ingress/abc123"
    html = '<script src="/static/script.js" defer></script>'
    expected = f'<script src="{prefix}/static/script.js" defer></script>'
    assert _inject_ingress_prefix(html, prefix) == expected


def test_inject_ingress_prefix_rewrites_sw_path():
    """Ingress prefix should rewrite /sw.js reference."""
    prefix = "/api/hassio_ingress/abc123"
    assert f"'{prefix}/sw.js'" in _inject_ingress_prefix("register('/sw.js')", prefix)
