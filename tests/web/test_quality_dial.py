"""Tests for the AI quality dial (/api/quality) and the model-aware cost counter.

The dial hot-swaps the active model profile with NO restart and NO queue purge —
the current segment must finish airing untouched (leadership principle #1). The
cost counter prices each model the session actually ran and never shows a silent
$0 or crashes on an unpriced model (operator honesty).
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
from mammamiradio.web.streamer import LiveStreamHub, _consumption_cost, _estimate_api_cost, router

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture(autouse=True)
def _isolate_quality_env():
    """The /api/quality handler writes MAMMAMIRADIO_QUALITY into os.environ for
    persistence; restore it around each test so this suite can't leak the active
    profile into other test files' load_config() calls."""
    prev = os.environ.get("MAMMAMIRADIO_QUALITY")
    os.environ.pop("MAMMAMIRADIO_QUALITY", None)
    yield
    if prev is None:
        os.environ.pop("MAMMAMIRADIO_QUALITY", None)
    else:
        os.environ["MAMMAMIRADIO_QUALITY"] = prev


def _make_test_app(*, admin_password: str = "", is_addon: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = ""
    config.is_addon = is_addon
    state = StationState(playlist=[Track(title="S", artist="A", duration_ms=180_000, spotify_id="t1")])
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    hub = LiveStreamHub()
    hub.bind_state(state)
    app.state.stream_hub = hub
    return app


# ── GET /api/quality ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_quality_returns_active_and_profiles(monkeypatch):
    monkeypatch.delenv("MAMMAMIRADIO_QUALITY", raising=False)
    app = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    ) as client:
        resp = await client.get("/api/quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_profile"] == "balanced"
    assert set(body["profiles"]) == {"premium", "balanced", "economy"}


# ── POST /api/quality — live swap, persistence, no queue purge ─────────────
@pytest.mark.asyncio
async def test_post_quality_applies_and_persists_standalone(monkeypatch):
    app = _make_test_app(is_addon=False)
    monkeypatch.delenv("MAMMAMIRADIO_QUALITY", raising=False)
    with patch("mammamiradio.web.streamer._save_dotenv") as save_dotenv:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
        ) as client:
            resp = await client.post("/api/quality", json={"quality_profile": "premium"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "active_profile": "premium"}
    assert app.state.config.models.active_profile == "premium"
    save_dotenv.assert_called_once_with({"MAMMAMIRADIO_QUALITY": "premium"})


@pytest.mark.asyncio
async def test_post_quality_persistence_failure_does_not_change_live_profile(monkeypatch):
    app = _make_test_app(is_addon=False)
    monkeypatch.delenv("MAMMAMIRADIO_QUALITY", raising=False)
    assert app.state.config.models.active_profile == "balanced"
    with patch("mammamiradio.web.streamer._save_dotenv", side_effect=OSError("disk full")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
        ) as client:
            resp = await client.post("/api/quality", json={"quality_profile": "premium"})

    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert app.state.config.models.active_profile == "balanced"
    assert os.environ.get("MAMMAMIRADIO_QUALITY") is None


@pytest.mark.asyncio
async def test_post_quality_swap_does_not_purge_queue():
    """Principle #1: switching the dial must NOT touch the queue or skip the
    current segment — the in-flight segment finishes airing untouched."""
    app = _make_test_app()
    seg = Segment(type=SegmentType.MUSIC, path=Path("/tmp/x.mp3"), ephemeral=False)
    app.state.queue.put_nowait(seg)
    with patch("mammamiradio.web.streamer._save_dotenv"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
        ) as client:
            resp = await client.post("/api/quality", json={"quality_profile": "economy"})
    assert resp.status_code == 200
    assert app.state.queue.qsize() == 1  # queue untouched
    assert not app.state.skip_event.is_set()  # current segment not skipped


@pytest.mark.asyncio
async def test_post_quality_rejects_invalid_profile():
    app = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    ) as client:
        resp = await client.post("/api/quality", json={"quality_profile": "ultra"})
    body = resp.json()
    assert body["ok"] is False
    assert "must be one of" in body["error"]


@pytest.mark.asyncio
async def test_post_quality_rejects_malformed_json():
    app = _make_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    ) as client:
        resp = await client.post("/api/quality", content="{bad", headers={"content-type": "application/json"})
    assert resp.json()["ok"] is False  # graceful, not an unhandled 500


@pytest.mark.asyncio
async def test_post_quality_addon_writes_options_json(tmp_path, monkeypatch):
    app = _make_test_app(is_addon=True)
    monkeypatch.delenv("MAMMAMIRADIO_QUALITY", raising=False)
    options_file = tmp_path / "options.json"
    options_file.write_text(json.dumps({"existing": "value"}))
    with patch("mammamiradio.web.persistence.Path", return_value=options_file):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
        ) as client:
            resp = await client.post("/api/quality", json={"quality_profile": "economy"})
    assert resp.status_code == 200
    options = json.loads(options_file.read_text())
    assert options["quality_profile"] == "economy"
    assert options["existing"] == "value"  # single-key patch, didn't clobber


@pytest.mark.asyncio
async def test_quality_requires_admin_for_public_ip():
    app = _make_test_app(admin_password="secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 9)), base_url="http://testserver"
    ) as client:
        get_resp = await client.get("/api/quality")
        post_resp = await client.post("/api/quality", json={"quality_profile": "premium"})
    assert get_resp.status_code == 401
    assert post_resp.status_code == 401


# ── Model-aware cost counter ──────────────────────────────────────────────
def test_cost_counter_prices_each_model():
    state = StationState(playlist=[])
    state.api_tokens_by_model = {
        "claude-opus-4-8": {"input": 1_000_000, "output": 1_000_000},  # 15 + 75 = 90
        "gpt-5.5": {"input": 1_000_000, "output": 1_000_000},  # 5 + 30 = 35 (default-profile creative fallback)
        "gpt-5.4-mini": {"input": 1_000_000, "output": 1_000_000},  # 0.75 + 4.50 = 5.25
    }
    cost, unpriced = _estimate_api_cost(state)
    assert unpriced is False
    assert cost == pytest.approx(130.25, abs=0.01)


def test_cost_counter_unpriced_model_flags_and_uses_conservative_default():
    state = StationState(playlist=[])
    state.api_tokens_by_model = {"brand-new-model-x": {"input": 1_000_000, "output": 1_000_000}}
    cost, unpriced = _estimate_api_cost(state)
    assert unpriced is True
    assert cost == pytest.approx(90.0, abs=0.01)  # highest-tier fallback, never $0


def test_cost_counter_never_zero_without_per_model_data():
    """Legacy/fresh state with only aggregate counters still shows a non-blank cost."""
    state = StationState(playlist=[])
    state.api_input_tokens = 1_000_000
    state.api_output_tokens = 1_000_000
    cost, unpriced = _estimate_api_cost(state)
    assert cost > 0
    assert unpriced is False


def test_cost_counter_includes_tts_characters():
    """Paid TTS characters add a blended estimate on top of the LLM token cost."""
    state = StationState(playlist=[])
    state.api_tokens_by_model = {"gpt-5.4-mini": {"input": 1_000_000, "output": 1_000_000}}  # 5.25
    state.tts_characters = 1_000_000  # * 0.00002 blended = 20.00
    cost, _ = _estimate_api_cost(state)
    assert cost == pytest.approx(5.25 + 20.0, abs=0.01)


def test_cost_breakdown_prices_model_aware_categories_and_reconciles_units():
    state = StationState(playlist=[])
    state.record_llm_usage("script_banter", "claude-opus-4-8", 1_000_000, 1_000_000)
    state.record_llm_usage("script_banter", "gpt-5.4-mini", 1_000_000, 1_000_000)
    state.record_llm_usage("script_transition", "gpt-5.4-mini", 10_000, 10_000)
    state.record_llm_usage("script_ads", "gpt-5.5", 100_000, 100_000)
    state.record_tts_usage(50_000)

    payload = _consumption_cost(state)
    breakdown = payload["cost_breakdown"]
    cats = breakdown["categories"]

    assert breakdown["available"] is True
    assert breakdown["total_usd"] == payload["api_cost_estimate_usd"]
    assert cats["script_banter"]["raw_cost_usd"] == pytest.approx(95.25, abs=0.0001)
    assert cats["tts"]["raw_cost_usd"] == pytest.approx(1.0, abs=0.0001)
    assert sum(cat["calls"] for cat in cats.values()) == state.api_calls
    assert sum(cat["input_tokens"] for cat in cats.values()) == state.api_input_tokens
    assert sum(cat["output_tokens"] for cat in cats.values()) == state.api_output_tokens
    assert sum(cat["characters"] for cat in cats.values()) == state.tts_characters


def test_cost_breakdown_flags_unpriced_category_and_total():
    state = StationState(playlist=[])
    state.record_llm_usage("script_ads", "brand-new-model-x", 1000, 1000)

    payload = _consumption_cost(state)
    breakdown = payload["cost_breakdown"]

    assert payload["api_cost_unpriced_model"] is True
    assert breakdown["unpriced_model"] is True
    assert breakdown["categories"]["script_ads"]["unpriced"] is True


def test_cost_breakdown_marks_legacy_aggregate_only_state_unavailable():
    state = StationState(playlist=[])
    state.api_calls = 1
    state.api_input_tokens = 1_000_000
    state.api_output_tokens = 1_000_000

    payload = _consumption_cost(state)
    breakdown = payload["cost_breakdown"]

    assert payload["api_cost_estimate_usd"] > 0
    assert breakdown["available"] is False
    assert all(cat["raw_cost_usd"] == 0 for cat in breakdown["categories"].values())


def test_cost_counter_tts_folds_into_legacy_aggregate_branch():
    """TTS estimate is also added in the no-per-model (legacy/fresh) branch."""
    state = StationState(playlist=[])
    state.api_input_tokens = 0
    state.api_output_tokens = 0
    state.tts_characters = 500_000  # * 0.00002 = 10.00
    cost, unpriced = _estimate_api_cost(state)
    assert cost == pytest.approx(10.0, abs=0.01)
    assert unpriced is False


def test_cost_counter_tts_getattr_safe_on_legacy_state():
    """A state object predating tts_characters must not raise (getattr default 0)."""
    from types import SimpleNamespace

    legacy = SimpleNamespace(api_tokens_by_model={}, api_input_tokens=0, api_output_tokens=0)
    cost, _ = _estimate_api_cost(legacy)
    assert cost == 0.0


@pytest.mark.asyncio
async def test_status_surfaces_unpriced_flag():
    """The unpriced-model flag must reach the /status body (protected-UI regression
    guard, like the token cost counter itself)."""
    app = _make_test_app()
    app.state.station_state.api_calls = 1
    app.state.station_state.api_tokens_by_model = {"brand-new-model": {"input": 1000, "output": 1000}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    ) as client:
        resp = await client.get("/status")
    assert resp.status_code == 200
    consumption = resp.json()["consumption"]
    assert "api_cost_estimate_usd" in consumption  # protected element preserved
    assert consumption["api_cost_unpriced_model"] is True
    assert consumption["cost_breakdown"]["available"] is False


@pytest.mark.asyncio
async def test_status_surfaces_fixed_key_cost_breakdown():
    app = _make_test_app()
    state = app.state.station_state
    state.record_llm_usage("script_banter", "gpt-5.4-mini", 1000, 500)
    state.record_llm_usage("script_transition", "gpt-5.4-mini", 100, 50)
    state.record_llm_usage("script_ads", "gpt-5.5", 200, 100)
    state.record_tts_usage(300)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://testserver"
    ) as client:
        resp = await client.get("/status")

    assert resp.status_code == 200
    breakdown = resp.json()["consumption"]["cost_breakdown"]
    assert breakdown["available"] is True
    assert set(breakdown["categories"]) == {"script_banter", "script_transition", "script_ads", "tts"}
    assert breakdown["categories"]["tts"]["characters"] == 300
