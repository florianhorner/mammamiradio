from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "stream_watch_server.py"
    spec = importlib.util.spec_from_file_location("stream_watch_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_summary_marks_ai_working_from_recent_generated_banter(monkeypatch):
    module = _load_module()
    seen: list[str] = []

    fixtures = {
        "/public-status": {
            "now_streaming": {"type": "music", "label": "Song"},
            "current_source": {"label": "Demo", "kind": "demo"},
            "upcoming": [{"label": "Next Song"}],
            "stream_log": [
                {"type": "banter", "timestamp": 123.0, "metadata": {"canned": False, "lines": [{"host": "Marco"}]}}
            ],
            "golden_path": {
                "stage": "needs_spotify_connect",
                "headline": "Connect Spotify",
                "detail": "Waiting",
                "blocking": True,
                "steps": ["Select the device"],
            },
        },
        "/healthz": {"status": "ok", "uptime_s": 120.0},
        "/readyz": {"status": "ready", "queue_depth": 2, "uptime_s": 120.0},
    }

    def fake_fetch(path: str):
        seen.append(path)
        return fixtures[path]

    monkeypatch.setattr(module, "_fetch_json", fake_fetch)
    summary = module._build_summary()

    assert seen == ["/public-status", "/healthz", "/readyz"]
    assert summary["ai"]["status"] == "working"
    assert summary["ai"]["last_banter_canned"] is False
    assert summary["spotify"]["connected"] is False
    assert summary["spotify"]["next_step"] == "Select the device"
    assert summary["music"]["upcoming"] == ["Next Song"]
    assert summary["music"]["readiness"] == "ready"
    assert summary["music"]["queue_depth"] == 2
    assert summary["music"]["illusion_window_minutes"] == 2.0


def test_upstream_base_url_uses_runtime_port(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_PORT", "9310")
    module = _load_module()
    assert module._upstream_base_url() == "http://127.0.0.1:9310"
