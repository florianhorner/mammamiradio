"""Regression coverage for the local-only staged showreel harness."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts.showreel import capture, mock_ha


@contextmanager
def _mock_homecoming_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_ha.make_handler("homecoming"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request_json(base: str, path: str, data: bytes | None = None) -> dict:
    request = Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _states_by_id(base: str) -> dict[str, dict]:
    return {state["entity_id"]: state for state in _request_json(base, "/api/states")}


def test_homecoming_mock_stages_a_real_unlock_transition() -> None:
    with _mock_homecoming_server() as base:
        assert _states_by_id(base)["lock.example_entry_lock"]["state"] == "locked"

        result = _request_json(
            base,
            "/__set",
            json.dumps({"entity_id": "lock.example_entry_lock", "state": "unlocked"}).encode(),
        )

        assert result == {
            "ok": True,
            "entity_id": "lock.example_entry_lock",
            "old": "locked",
            "new": "unlocked",
        }
        assert _states_by_id(base)["lock.example_entry_lock"]["state"] == "unlocked"


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        (b"{", 400),
        (json.dumps({"entity_id": "lock.example_unknown", "state": "unlocked"}).encode(), 404),
        (json.dumps({"entity_id": "lock.example_entry_lock", "state": "locked"}).encode(), 409),
    ],
)
def test_homecoming_mock_rejects_invalid_or_noop_flips(payload: bytes, expected_status: int) -> None:
    with _mock_homecoming_server() as base:
        with pytest.raises(HTTPError) as error:
            _request_json(base, "/__set", payload)

        assert error.value.code == expected_status
        assert _states_by_id(base)["lock.example_entry_lock"]["state"] == "locked"


class _Process:
    def __init__(self, label: str, events: list[tuple[str, object]]):
        self.label = label
        self.events = events

    def terminate(self) -> None:
        self.events.append(("terminate", self.label))

    def wait(self, timeout: float) -> None:
        self.events.append(("wait", (self.label, timeout)))

    def kill(self) -> None:
        self.events.append(("kill", self.label))


def _configure_capture(monkeypatch, tmp_path: Path):
    events: list[tuple[str, object]] = []
    run_calls: list[list[str]] = []

    monkeypatch.setattr(capture, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(capture.time, "sleep", lambda seconds: events.append(("sleep", seconds)))

    def lead_seen(*_args: object) -> bool:
        events.append(("lead", None))
        return True

    monkeypatch.setattr(capture, "_wait_lead", lead_seen)
    monkeypatch.setattr(capture, "_wait_type", lambda *_args, **_kwargs: 12.0)
    monkeypatch.setattr(capture, "_wait_until_not", lambda *_args, **_kwargs: 36.0)
    monkeypatch.setattr(capture, "_queued_segment_id", lambda _base, segment: f"{segment}-queue-id")

    def fake_popen(command: list[str]) -> _Process:
        output = command[command.index("-o") + 1]
        label = "warm" if output == "/dev/null" else "record"
        events.append(("popen", label))
        return _Process(label, events)

    def fake_run(command: list[str], **_kwargs) -> SimpleNamespace:
        run_calls.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(capture.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    return events, run_calls


def _home_event_args(tmp_path: Path) -> list[str]:
    return [
        "--arc",
        "banter",
        "--home-event",
        "lock.example_entry_lock:unlocked",
        "--raw",
        str(tmp_path / "raw.mp3"),
        "--final",
        str(tmp_path / "final.mp3"),
    ]


def test_default_lead_window_includes_the_warmup_settle_time() -> None:
    assert capture.LEAD_START_WINDOW_SECONDS > capture.DEFAULT_SETTLE_SECONDS


def test_home_event_recipe_sets_the_station_context_ttl() -> None:
    """The capture wait must match the mock station command's context TTL."""
    readme = (Path(__file__).resolve().parents[2] / "scripts/showreel/README.md").read_text()

    assert "MAMMAMIRADIO_HA_CONTEXT_POLL_INTERVAL=15 \\\nHA_ENABLED=true" in readme


@pytest.mark.parametrize(
    "args",
    [
        ["--arc", "news_flash", "--home-event", "lock.example_entry_lock:unlocked"],
        ["--arc", "banter", "--home-event", "lock.example_entry_lock:unlocked", "--ha-poll-interval", "0"],
    ],
)
def test_home_event_cli_contract_fails_closed(args: list[str]) -> None:
    assert capture.main(args) == 1


def test_trigger_waits_for_its_operator_render_before_accepting_a_same_type_queue(monkeypatch) -> None:
    pending = iter(["banter", "banter", None])
    monkeypatch.setattr(capture, "_post", lambda *_args: {"ok": True})
    monkeypatch.setattr(capture, "_operator_force_pending", lambda _base: next(pending))
    monkeypatch.setattr(capture, "_queued", lambda *_args: True)
    monkeypatch.setattr(capture, "_now", lambda *_args: ("music", "lead", 0.0))
    monkeypatch.setattr(capture.time, "sleep", lambda _seconds: None)

    assert capture._trigger_queued("http://127.0.0.1:8077", "banter") is True


def test_waiting_for_a_segment_id_ends_at_the_next_same_type_segment(monkeypatch) -> None:
    now_ids = iter(["requested", "requested", "preflight"])
    monkeypatch.setattr(capture, "_now", lambda _base: ("banter", "Marco & Giulia", 0.0))
    monkeypatch.setattr(capture, "_now_queue_id", lambda _base: next(now_ids))
    monkeypatch.setattr(capture.time, "sleep", lambda _seconds: None)

    assert capture._wait_until_not("http://127.0.0.1:8077", "banter", 0.0, queue_id="requested") is not None


def test_home_event_capture_primes_before_recording(monkeypatch, tmp_path) -> None:
    events, run_calls = _configure_capture(monkeypatch, tmp_path)
    trigger_calls: list[str] = []

    def trigger(_base: str, segment: str) -> bool:
        trigger_calls.append(segment)
        events.append(("trigger", segment))
        return True

    monkeypatch.setattr(capture, "_trigger_queued", trigger)
    monkeypatch.setattr(
        capture,
        "_post",
        lambda _base, path, _body: (
            events.append(("post", path))
            or {"ok": True, "entity_id": "lock.example_entry_lock", "old": "locked", "new": "unlocked"}
        ),
    )

    assert capture.main(_home_event_args(tmp_path)) == 0
    assert trigger_calls == ["news_flash", "banter", "banter"]
    assert events.index(("popen", "record")) > events.index(("post", "/__set"))
    assert events.index(("popen", "record")) > events.index(("sleep", 16.0))
    assert len(run_calls) == 1


@pytest.mark.parametrize("failure", ["lead", "preflight", "flip", "final_trigger", "missing_arc"])
def test_home_event_capture_fails_closed(monkeypatch, tmp_path, failure: str) -> None:
    events, run_calls = _configure_capture(monkeypatch, tmp_path)
    final = tmp_path / "final.mp3"
    final.write_bytes(b"known-good")

    if failure == "lead":
        monkeypatch.setattr(capture, "_wait_lead", lambda *_args: False)
    if failure == "missing_arc":
        monkeypatch.setattr(capture, "_wait_type", lambda *_args, **_kwargs: None)

    trigger_count = 0

    def trigger(_base: str, _segment: str) -> bool:
        nonlocal trigger_count
        trigger_count += 1
        if failure == "preflight":
            return False
        return not (failure == "final_trigger" and trigger_count == 3)

    monkeypatch.setattr(capture, "_trigger_queued", trigger)
    response = {"ok": True, "entity_id": "lock.example_entry_lock", "old": "locked", "new": "unlocked"}
    if failure == "flip":
        response["old"] = "unlocked"
    monkeypatch.setattr(capture, "_post", lambda *_args: response)

    assert capture.main(_home_event_args(tmp_path)) == 1
    assert final.read_bytes() == b"known-good"
    assert not run_calls
    if failure in {"lead", "preflight", "flip"}:
        assert ("popen", "record") not in events


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8123",
        "http://localhost:8123",
        "https://127.0.0.1:8123",
        "http://127.0.0.1:8123/api",
        "http://user:pass@127.0.0.1:8123",
    ],
)
def test_station_launcher_rejects_non_loopback_origins(url):
    from scripts.showreel.station import validate_mock_url

    with pytest.raises(ValueError):
        validate_mock_url(url)


def test_station_launcher_isolates_inherited_credentials_and_state(tmp_path):
    import os
    import subprocess
    import sys

    script = """
import os
from pathlib import Path
import uvicorn
from scripts.showreel import station
seen = []
def run(app, **kwargs):
    from mammamiradio import main
    config = main.load_config()
    assert config.homeassistant.url == "http://127.0.0.1:18123"
    assert config.ha_token == "synthetic-showreel-token"
    assert config.cache_dir != Path(os.environ["ORIGINAL_CACHE"])
    assert kwargs["host"] == "127.0.0.1"
    seen.append(config.cache_dir.parent)
    result = main.read_home_compatibility(None, None)
    assert result.authorization.bindings.identity == "local-showreel"
uvicorn.run = run
assert station.main(["--mock-ha", "http://127.0.0.1:18123"]) == 0
assert seen and not seen[0].exists()
"""
    environment = dict(
        os.environ,
        SUPERVISOR_TOKEN="synthetic-inherited",
        HASSIO_TOKEN="synthetic-inherited",
        MAMMAMIRADIO_CACHE_DIR=str(tmp_path),
        ORIGINAL_CACHE=str(tmp_path),
    )
    result = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
