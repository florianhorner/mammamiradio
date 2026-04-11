"""Tests for host personality sliders: model, config, prompt modifiers, and API routes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from mammamiradio.config import load_config
from mammamiradio.models import (
    HostPersonality,
    PersonalityAxes,
    StationState,
    Track,
)
from mammamiradio.scriptwriter import _build_system_prompt, _personality_modifier
from mammamiradio.streamer import LiveStreamHub, router

TOML_PATH = str(Path(__file__).parent.parent / "radio.toml")


# ---------------------------------------------------------------------------
# PersonalityAxes model tests
# ---------------------------------------------------------------------------


class TestPersonalityAxes:
    def test_defaults_are_50(self):
        axes = PersonalityAxes()
        assert axes.energy == 50
        assert axes.chaos == 50
        assert axes.warmth == 50
        assert axes.verbosity == 50
        assert axes.nostalgia == 50

    def test_to_dict(self):
        axes = PersonalityAxes(energy=80, chaos=20)
        d = axes.to_dict()
        assert d == {"energy": 80, "chaos": 20, "warmth": 50, "verbosity": 50, "nostalgia": 50}

    def test_from_dict_clamps_values(self):
        axes = PersonalityAxes.from_dict({"energy": 150, "chaos": -10, "warmth": 50})
        assert axes.energy == 100
        assert axes.chaos == 0
        assert axes.warmth == 50

    def test_from_dict_ignores_unknown_keys(self):
        axes = PersonalityAxes.from_dict({"energy": 70, "unknown_axis": 99})
        assert axes.energy == 70
        assert axes.chaos == 50  # untouched default

    def test_from_dict_empty(self):
        axes = PersonalityAxes.from_dict({})
        assert axes == PersonalityAxes()

    def test_roundtrip(self):
        original = PersonalityAxes(energy=10, chaos=90, warmth=30, verbosity=70, nostalgia=55)
        restored = PersonalityAxes.from_dict(original.to_dict())
        assert restored == original


# ---------------------------------------------------------------------------
# HostPersonality with personality field
# ---------------------------------------------------------------------------


class TestHostPersonalityModel:
    def test_default_personality(self):
        host = HostPersonality(name="Marco", voice="v", style="s")
        assert host.personality == PersonalityAxes()

    def test_custom_personality(self):
        p = PersonalityAxes(energy=90, chaos=80)
        host = HostPersonality(name="Marco", voice="v", style="s", personality=p)
        assert host.personality.energy == 90
        assert host.personality.chaos == 80


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigPersonality:
    def test_radio_toml_loads_personality(self):
        config = load_config(TOML_PATH)
        marco = next(h for h in config.hosts if h.name == "Marco")
        assert marco.personality.energy == 100
        assert marco.personality.chaos == 100
        assert marco.personality.nostalgia == 75

        giulia = next(h for h in config.hosts if h.name == "Giulia")
        assert giulia.personality.energy == 72
        assert giulia.personality.chaos == 92
        assert giulia.personality.warmth == 20


# ---------------------------------------------------------------------------
# Prompt modifier tests
# ---------------------------------------------------------------------------


class TestPersonalityModifier:
    def test_neutral_produces_no_modifier(self):
        axes = PersonalityAxes()  # all 50
        result = _personality_modifier("Marco", axes)
        assert result == ""

    def test_high_energy_produces_manic_text(self):
        axes = PersonalityAxes(energy=90)
        result = _personality_modifier("Marco", axes)
        assert "Manic" in result or "fast" in result.lower()
        assert "Marco" in result

    def test_low_energy_produces_calm_text(self):
        axes = PersonalityAxes(energy=10)
        result = _personality_modifier("Marco", axes)
        assert "slowly" in result.lower() or "calm" in result.lower()

    def test_high_chaos(self):
        axes = PersonalityAxes(chaos=90)
        result = _personality_modifier("Giulia", axes)
        assert "tangent" in result.lower()

    def test_low_chaos(self):
        axes = PersonalityAxes(chaos=10)
        result = _personality_modifier("Giulia", axes)
        assert "topic" in result.lower() or "structured" in result.lower()

    def test_high_warmth(self):
        axes = PersonalityAxes(warmth=85)
        result = _personality_modifier("Host", axes)
        assert "affectionate" in result.lower() or "gushing" in result.lower()

    def test_low_warmth(self):
        axes = PersonalityAxes(warmth=10)
        result = _personality_modifier("Host", axes)
        assert "sarcastic" in result.lower() or "dry" in result.lower()

    def test_high_verbosity(self):
        axes = PersonalityAxes(verbosity=90)
        result = _personality_modifier("Host", axes)
        assert "long" in result.lower() or "stories" in result.lower()

    def test_low_verbosity(self):
        axes = PersonalityAxes(verbosity=10)
        result = _personality_modifier("Host", axes)
        assert "short" in result.lower() or "punchy" in result.lower()

    def test_high_nostalgia(self):
        axes = PersonalityAxes(nostalgia=90)
        result = _personality_modifier("Host", axes)
        assert "remember" in result.lower() or "nostalgia" in result.lower()

    def test_low_nostalgia(self):
        axes = PersonalityAxes(nostalgia=10)
        result = _personality_modifier("Host", axes)
        assert "present" in result.lower() or "current" in result.lower()

    def test_multiple_axes_combined(self):
        axes = PersonalityAxes(energy=90, chaos=90, warmth=10)
        result = _personality_modifier("Marco", axes)
        # Should include guidance for all three deviating axes
        assert "fast" in result.lower() or "manic" in result.lower()
        assert "tangent" in result.lower()
        assert "sarcastic" in result.lower() or "dry" in result.lower()

    def test_borderline_values_produce_no_modifier(self):
        """Values within threshold (35-65) of neutral should produce nothing."""
        axes = PersonalityAxes(energy=40, chaos=60, warmth=45, verbosity=55, nostalgia=50)
        result = _personality_modifier("Host", axes)
        assert result == ""


class TestBuildSystemPrompt:
    def test_prompt_includes_personality_modifiers(self):
        config = load_config(TOML_PATH)
        # Marco has extreme energy and nostalgia — both should influence the prompt
        prompt = _build_system_prompt(config)
        assert "Marco" in prompt
        # Should contain personality guidance for Marco's high nostalgia
        assert "remember" in prompt.lower() or "nostalgia" in prompt.lower()

    def test_prompt_still_includes_base_style(self):
        config = load_config(TOML_PATH)
        prompt = _build_system_prompt(config)
        assert "manic energy" in prompt  # Marco's base style
        assert "razor-sharp sarcasm" in prompt  # Giulia's base style


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


def _make_test_app(*, admin_password: str = "", admin_token: str = "") -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    config = load_config(TOML_PATH)
    config.admin_password = admin_password
    config.admin_token = admin_token
    state = StationState(
        playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="t1")],
    )
    app.state.queue = asyncio.Queue()
    app.state.skip_event = asyncio.Event()
    app.state.stream_hub = LiveStreamHub()
    app.state.station_state = state
    app.state.config = config
    app.state.start_time = time.time()
    return app


@pytest.mark.asyncio
class TestHostsAPI:
    async def test_get_hosts_returns_all_hosts(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/hosts")
        assert r.status_code == 200
        data = r.json()
        assert "hosts" in data
        assert len(data["hosts"]) >= 2
        host = data["hosts"][0]
        assert "name" in host
        assert "personality" in host
        assert set(host["personality"].keys()) == {"energy", "chaos", "warmth", "verbosity", "nostalgia"}

    async def test_get_hosts_requires_auth_from_public_ip(self):
        app = _make_test_app(admin_password="secret")
        transport = httpx.ASGITransport(app=app, client=("203.0.113.50", 9999))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/hosts")
        assert r.status_code == 401

    async def test_patch_personality_updates_axis(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/api/hosts/Marco/personality",
                json={"energy": 95, "chaos": 10},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["personality"]["energy"] == 95
        assert data["personality"]["chaos"] == 10
        # Other axes unchanged
        assert data["personality"]["warmth"] == 55  # Marco's configured default

    async def test_patch_personality_clamps_values(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/api/hosts/Marco/personality",
                json={"energy": 200, "chaos": -50},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["personality"]["energy"] == 100
        assert data["personality"]["chaos"] == 0

    async def test_patch_personality_unknown_host_404(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/api/hosts/NonExistent/personality", json={"energy": 50})
        assert r.status_code == 404

    async def test_patch_personality_no_valid_axes_400(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/api/hosts/Marco/personality", json={"invalid": 50})
        assert r.status_code == 400

    async def test_patch_personality_case_insensitive(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch("/api/hosts/marco/personality", json={"energy": 77})
        assert r.status_code == 200
        assert r.json()["personality"]["energy"] == 77

    async def test_reset_personality(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            # First change something
            await c.patch("/api/hosts/Marco/personality", json={"energy": 99})
            # Then reset
            r = await c.post("/api/hosts/Marco/personality/reset")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        # All axes back to 50
        for val in data["personality"].values():
            assert val == 50

    async def test_reset_unknown_host_404(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/hosts/Nobody/personality/reset")
        assert r.status_code == 404

    async def test_personality_persists_across_requests(self):
        """Changes should persist in the running config across multiple requests."""
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            await c.patch("/api/hosts/Marco/personality", json={"warmth": 5})
            r = await c.get("/api/hosts")
        marco = next(h for h in r.json()["hosts"] if h["name"] == "Marco")
        assert marco["personality"]["warmth"] == 5

    async def test_patch_ignores_non_numeric_values(self):
        app = _make_test_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.patch(
                "/api/hosts/Marco/personality",
                json={"energy": "high", "chaos": 20},
            )
        assert r.status_code == 200
        # "energy" should be ignored (non-numeric), only chaos updated
        assert r.json()["personality"]["chaos"] == 20
