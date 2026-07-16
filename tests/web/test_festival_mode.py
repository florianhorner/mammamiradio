"""Tests for Festival Mode — /api/party endpoints and prompt injection.

Three mandatory scenarios per the audio delivery test coverage rule:
  Scenario 1 — Normal: toggle on/off, prompt uses FESTIVAL_MODE_BLOCK, queue purged.
  Scenario 2 — Empty fallback: LLM down, festival still arms, errors are handled.
  Scenario 3 — Post-restart: party_mode=festival in config survives across cold boot.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.web.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _make_test_app(*, admin_password: str = "", is_addon: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = ""
    config.is_addon = is_addon
    config.party_mode = None
    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


# ---------------------------------------------------------------------------
# GET /api/party
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_party_returns_inactive_by_default():
    app = _make_test_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/api/party")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"active": False, "mode": None}


@pytest.mark.asyncio
async def test_get_party_returns_active_when_festival_set():
    app = _make_test_app()
    app.state.config.party_mode = "festival"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.get("/api/party")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"active": True, "mode": "festival"}


# ---------------------------------------------------------------------------
# Scenario 1 — Normal: enable → first-strike banter, prompt injection, disable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_party_enable_sets_festival_mode_purges_queue_and_arms_banter(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    for idx in range(3):
        f = tmp_path / f"old-{idx}.mp3"
        f.write_bytes(b"old")
        app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=f, ephemeral=True))

    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    body = resp.json()
    state = app.state.station_state
    config = app.state.config
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["active"] is True
    assert body["mode"] == "festival"
    assert config.party_mode == "festival"
    assert state.force_next == SegmentType.BANTER
    assert app.state.queue.qsize() == 1
    assert app.state.queue._queue[0].metadata["continuity_reservation"] is True
    assert os.environ["MAMMAMIRADIO_FESTIVAL_MODE"] == "true"
    save_dotenv.assert_called_once_with({"MAMMAMIRADIO_FESTIVAL_MODE": "true"})


@pytest.mark.asyncio
async def test_post_party_enable_preserves_ready_head_when_replacement_is_unavailable(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    state = app.state.station_state
    head_path = tmp_path / "festival-head.mp3"
    tail_path = tmp_path / "festival-tail.mp3"
    head_path.write_bytes(b"head")
    tail_path.write_bytes(b"tail")
    head = Segment(type=SegmentType.MUSIC, path=head_path, duration_sec=180.0, metadata={"title": "Head"})
    tail = Segment(type=SegmentType.BANTER, path=tail_path, duration_sec=10.0, metadata={"title": "Tail"})
    app.state.queue.put_nowait(head)
    app.state.queue.put_nowait(tail)
    state.queued_segments = [{"type": "music", "label": "Head"}, {"type": "banter", "label": "Tail"}]
    state.continuity_epoch = 6

    with (
        patch("mammamiradio.web.streamer._save_dotenv"),
        patch("mammamiradio.web.streamer._DEMO_ASSETS_DIR", tmp_path / "missing-demo-assets"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    assert resp.status_code == 200
    assert resp.json()["active"] is True
    assert app.state.config.party_mode == "festival"
    assert state.force_next is SegmentType.BANTER
    assert list(app.state.queue._queue) == [head]
    assert len(state.queued_segments) == 1
    assert state.continuity_epoch == 7
    assert head_path.exists()
    assert not tail_path.exists()


@pytest.mark.asyncio
async def test_post_party_enable_clears_shadow_queue(tmp_path, monkeypatch):
    """Regression: enabling Festival Mode must clear the UI shadow queue, not just
    the real audio queue. The enable path drained the real queue but forgot the
    shadow, leaving the 'Up Next' panel showing segments that no longer existed
    (the queue-shadow drift seen when Festival Mode was toggled mid-stream)."""
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    state = app.state.station_state
    # Populate BOTH the real queue and the shadow projection, as during live playback.
    for idx in range(3):
        f = tmp_path / f"old-{idx}.mp3"
        f.write_bytes(b"old")
        app.state.queue.put_nowait(Segment(type=SegmentType.MUSIC, path=f, ephemeral=True))
        state.queued_segments.append({"type": "music", "label": f"Old {idx}"})
    assert len(state.queued_segments) == 3

    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    assert resp.status_code == 200
    # Both views rebuild together — no stale "Up Next" rows survive the toggle.
    assert app.state.queue.qsize() == len(state.queued_segments) == 1
    assert state.queued_segments[0]["reason"] == "Protected continuity audio."


@pytest.mark.asyncio
async def test_post_party_disable_clears_festival_mode_without_purging_queue(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.setenv("MAMMAMIRADIO_FESTIVAL_MODE", "true")
    app.state.config.party_mode = "festival"
    keep = tmp_path / "keep.mp3"
    keep.write_bytes(b"keep")
    app.state.queue.put_nowait(Segment(type=SegmentType.BANTER, path=keep, ephemeral=True))

    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "disable"})

    body = resp.json()
    config = app.state.config
    assert resp.status_code == 200
    assert body["ok"] is True
    assert body["active"] is False
    assert config.party_mode is None
    # Queue not purged on disable — in-flight festival segment plays out
    assert app.state.queue.qsize() == 1
    assert keep.exists()
    assert os.environ["MAMMAMIRADIO_FESTIVAL_MODE"] == "false"


@pytest.mark.asyncio
async def test_festival_block_injected_before_return_json_in_banter_prompt():
    """FESTIVAL_MODE_BLOCK must appear before 'Return JSON:' in the write_banter prompt."""
    from mammamiradio.hosts.scriptwriter import write_banter

    config = load_config(TOML_PATH)
    config.party_mode = "festival"
    config.anthropic_api_key = "test-key"
    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000)],
    )

    captured_prompts: list[str] = []

    async def fake_generate(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"lines": [{"host": config.hosts[0].name, "text": "Magnifico!"}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=fake_generate):
        await write_banter(state, config)

    assert captured_prompts, "write_banter did not call _generate_json_response"
    prompt = captured_prompts[0]
    festival_pos = prompt.find("FESTIVAL MODE")
    return_json_pos = prompt.find("Return JSON:")
    assert festival_pos != -1, "FESTIVAL_MODE_BLOCK not found in prompt"
    assert return_json_pos != -1, "'Return JSON:' not found in prompt"
    assert festival_pos < return_json_pos, (
        f"FESTIVAL_MODE_BLOCK at {festival_pos} appears AFTER 'Return JSON:' at {return_json_pos}"
    )


@pytest.mark.asyncio
async def test_festival_block_absent_when_festival_mode_off():
    from mammamiradio.hosts.scriptwriter import write_banter

    config = load_config(TOML_PATH)
    config.party_mode = None
    config.anthropic_api_key = "test-key"
    state = StationState(
        playlist=[Track(title="Test Song", artist="Test Artist", duration_ms=180_000)],
    )

    captured_prompts: list[str] = []

    async def fake_generate(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"lines": [{"host": config.hosts[0].name, "text": "Ciao!"}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=fake_generate):
        await write_banter(state, config)

    assert captured_prompts
    assert "FESTIVAL MODE" not in captured_prompts[0]


# ---------------------------------------------------------------------------
# Scenario 2 — Empty fallback: LLM down, festival mode arms without silent error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_festival_enable_succeeds_even_when_llm_unavailable(tmp_path, monkeypatch):
    """Enabling festival mode must not fail if the LLM is not configured.

    The endpoint succeeds; the banter that eventually plays may fall back to
    stock copy, but the mode change and queue purge are never gated on LLM
    availability.
    """
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    # Simulate LLM-down by blanking both API keys
    app.state.config.anthropic_api_key = ""
    app.state.config.openai_api_key = ""

    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["active"] is True
    assert app.state.config.party_mode == "festival"
    assert app.state.station_state.force_next == SegmentType.BANTER


# ---------------------------------------------------------------------------
# Scenario 3 — Post-restart: party_mode=festival in config survives restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_festival_mode_persists_across_config_reload(tmp_path, monkeypatch):
    """party_mode=festival loaded from env at boot time must be present in config."""
    monkeypatch.setenv("MAMMAMIRADIO_FESTIVAL_MODE", "true")
    config = load_config(TOML_PATH)
    assert config.party_mode == "festival", (
        "party_mode should be 'festival' when MAMMAMIRADIO_FESTIVAL_MODE=true at boot"
    )


@pytest.mark.asyncio
async def test_festival_mode_off_at_cold_boot_without_env(monkeypatch):
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    config = load_config(TOML_PATH)
    assert config.party_mode is None


@pytest.mark.asyncio
async def test_festival_mode_env_false_clears_party_mode(monkeypatch):
    """MAMMAMIRADIO_FESTIVAL_MODE=false must explicitly clear party_mode to None."""
    monkeypatch.setenv("MAMMAMIRADIO_FESTIVAL_MODE", "false")
    config = load_config(TOML_PATH)
    assert config.party_mode is None


@pytest.mark.asyncio
async def test_festival_mode_env_zero_clears_party_mode(monkeypatch):
    """MAMMAMIRADIO_FESTIVAL_MODE=0 (FALSY variant) must explicitly clear party_mode."""
    monkeypatch.setenv("MAMMAMIRADIO_FESTIVAL_MODE", "0")
    config = load_config(TOML_PATH)
    assert config.party_mode is None


@pytest.mark.asyncio
async def test_festival_mode_banter_uses_festival_block_after_post_restart(monkeypatch):
    """After a cold boot with MAMMAMIRADIO_FESTIVAL_MODE=true, the next banter
    must include FESTIVAL_MODE_BLOCK — same guarantee as live toggle, no silent reset."""
    from mammamiradio.hosts.scriptwriter import write_banter

    monkeypatch.setenv("MAMMAMIRADIO_FESTIVAL_MODE", "true")
    config = load_config(TOML_PATH)
    assert config.party_mode == "festival"
    config.anthropic_api_key = "test-key"

    state = StationState(
        playlist=[Track(title="Post Restart Song", artist="Artist", duration_ms=180_000)],
    )

    captured_prompts: list[str] = []

    async def fake_generate(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"lines": [{"host": config.hosts[0].name, "text": "Benvenuti!"}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=fake_generate):
        await write_banter(state, config)

    assert captured_prompts
    prompt = captured_prompts[0]
    assert "FESTIVAL MODE" in prompt
    assert prompt.find("FESTIVAL MODE") < prompt.find("Return JSON:")


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_enable_is_idempotent(tmp_path, monkeypatch):
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    app.state.config.party_mode = "festival"

    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    # No side-effects: save_dotenv not called, force_next not armed
    save_dotenv.assert_not_called()
    assert app.state.station_state.force_next is None


@pytest.mark.asyncio
async def test_double_disable_is_idempotent(monkeypatch):
    app = _make_test_app()
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    # Already off
    assert app.state.config.party_mode is None

    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "disable"})

    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    save_dotenv.assert_not_called()


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"action": "start"},
        {"action": "enable", "mode": "hitster"},
        {"action": "enable"},
        {},
    ],
)
async def test_post_party_rejects_invalid_payloads(payload):
    app = _make_test_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.post("/api/party", json=payload)

    assert resp.status_code == 422
    assert resp.json()["ok"] is False


@pytest.mark.asyncio
async def test_post_party_rejects_malformed_json():
    app = _make_test_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://testserver",
    ) as client:
        resp = await client.post("/api/party", content="{bad", headers={"content-type": "application/json"})

    assert resp.status_code == 422
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_party_endpoints_require_admin_for_public_ip():
    app = _make_test_app(admin_password="secret")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.50", 9999)),
        base_url="http://testserver",
    ) as client:
        get_resp = await client.get("/api/party")
        post_resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    assert get_resp.status_code == 401
    assert post_resp.status_code == 401


# ---------------------------------------------------------------------------
# HA addon persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_party_addon_mode_writes_options_json(tmp_path, monkeypatch):
    app = _make_test_app(is_addon=True)
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"existing": "value"}))

    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    assert resp.status_code == 200
    options = json.loads(options_file.read_text())
    assert options["festival_mode"] is True
    assert options["existing"] == "value"


# ---------------------------------------------------------------------------
# Festival + Chaos stacking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_festival_and_chaos_blocks_both_appear_in_prompt():
    """When both festival mode and chaos mode are active, both prompt blocks must be
    present, with chaos appearing first and festival after — both before 'Return JSON:'."""
    from mammamiradio.hosts.scriptwriter import write_banter

    config = load_config(TOML_PATH)
    config.party_mode = "festival"
    config.anthropic_api_key = "test-key"
    state = StationState(
        playlist=[Track(title="Stacked Song", artist="Artist", duration_ms=180_000)],
        chaos_mode_active=True,
    )

    captured_prompts: list[str] = []

    async def fake_generate(prompt, **kwargs):
        captured_prompts.append(prompt)
        return {"lines": [{"host": config.hosts[0].name, "text": "CHAOS + FESTIVAL!"}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=fake_generate):
        await write_banter(state, config)

    assert captured_prompts
    prompt = captured_prompts[0]
    chaos_pos = prompt.find("CHAOS MODE IS LIVE")
    festival_pos = prompt.find("FESTIVAL MODE")
    return_json_pos = prompt.find("Return JSON:")

    assert chaos_pos != -1, "CHAOS_MODE_BLOCK not found in stacked prompt"
    assert festival_pos != -1, "FESTIVAL_MODE_BLOCK not found in stacked prompt"
    assert return_json_pos != -1
    assert chaos_pos < festival_pos < return_json_pos, (
        f"Expected chaos({chaos_pos}) < festival({festival_pos}) < return_json({return_json_pos})"
    )


# ---------------------------------------------------------------------------
# Standalone dotenv persistence path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_festival_standalone_persists_to_dotenv(tmp_path, monkeypatch):
    app = _make_test_app(is_addon=False)
    monkeypatch.delenv("MAMMAMIRADIO_FESTIVAL_MODE", raising=False)

    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            resp = await client.post("/api/party", json={"action": "enable", "mode": "festival"})

    assert resp.status_code == 200
    save_dotenv.assert_called_once_with({"MAMMAMIRADIO_FESTIVAL_MODE": "true"})
