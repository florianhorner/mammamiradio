"""Chaos Mode scriptwriter prompt and fallback tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import ChaosSubtype, PlayedEntry, StationState, Track
from mammamiradio.hosts import scriptwriter
from mammamiradio.hosts.fallbacks import CHAOS_NORMAL_STOCK_LINES
from mammamiradio.hosts.scriptwriter import CHAOS_MODE_BLOCK, write_banter

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


@pytest.fixture()
def config():
    cfg = load_config(TOML_PATH)
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    return cfg


@pytest.fixture()
def state():
    return StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=180_000, youtube_id="yt1")])


async def _capture_prompt(config, state, subtype: ChaosSubtype) -> str:
    captured = {}
    host = config.hosts[0].name

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": host, "text": "Ciao."}], "new_joke": None}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(side_effect=_fake_generate_json_response),
    ):
        await write_banter(state, config, chaos_subtype=subtype)
    return captured["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subtype", "marker"),
    [
        (ChaosSubtype.FOURTH_WALL, "CHAOS_FOURTH_WALL"),
        (ChaosSubtype.ABANDONED_STORM, "CHAOS_ABANDONED_STORM"),
        (ChaosSubtype.IMPOSSIBLE_RECALL, "CHAOS_IMPOSSIBLE_RECALL"),
        (ChaosSubtype.ICON_MOMENT, "CHAOS_ICON_MOMENT"),
    ],
)
async def test_each_chaos_subtype_adds_its_prompt_fragment(config, state, subtype, marker):
    prompt = await _capture_prompt(config, state, subtype)

    assert CHAOS_MODE_BLOCK.strip() in prompt
    assert marker in prompt


@pytest.mark.asyncio
async def test_impossible_recall_uses_real_30_minute_play_history(config, state):
    old_track = Track(title="Vecchia Canzone", artist="Artista Antico", duration_ms=180_000)
    state.played_track_log.append(PlayedEntry(track=old_track, played_at=time.monotonic() - 31 * 60))

    prompt = await _capture_prompt(config, state, ChaosSubtype.IMPOSSIBLE_RECALL)

    assert "Artista Antico" in prompt
    assert "Vecchia Canzone" in prompt
    assert "RECALL TARGET: earlier" not in prompt


@pytest.mark.asyncio
async def test_impossible_recall_falls_back_to_earlier_without_old_history(config, state):
    recent_track = Track(title="Appena Suonata", artist="Artista", duration_ms=180_000)
    state.played_track_log.append(PlayedEntry(track=recent_track, played_at=time.monotonic() - 60))

    prompt = await _capture_prompt(config, state, ChaosSubtype.IMPOSSIBLE_RECALL)

    assert "RECALL TARGET: earlier" in prompt


@pytest.mark.asyncio
async def test_chaos_mode_block_overrides_axis_chaos_block(config, state):
    state.chaos_mode_active = True
    for host in config.hosts:
        host.personality.chaos = 100

    prompt = await _capture_prompt(config, state, ChaosSubtype.FOURTH_WALL)

    assert "CHAOS MODE IS LIVE" in prompt
    assert "CHAOS DIRECTION:" not in prompt


@pytest.mark.asyncio
async def test_both_llms_down_uses_chaos_stock_line(config, state):
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    lines, commit = await write_banter(state, config, chaos_subtype=ChaosSubtype.ICON_MOMENT)

    texts = [text for _host, text in lines]
    assert commit is None
    assert texts == CHAOS_NORMAL_STOCK_LINES[ChaosSubtype.ICON_MOMENT]
    assert texts != ["E torniamo alla musica!"]
    assert state.chaos_script_fallbacks == 1
    assert state.chaos_last_degraded_reason == "script_fallback"


@pytest.mark.asyncio
async def test_chaos_stock_line_preserves_every_subtype_line(config, state):
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    for subtype, expected_lines in CHAOS_NORMAL_STOCK_LINES.items():
        lines, commit = await write_banter(state, config, chaos_subtype=subtype)

        assert commit is None
        assert [text for _host, text in lines] == expected_lines


@pytest.mark.asyncio
async def test_script_llm_failure_uses_chaos_stock_line(config, state):
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new=AsyncMock(side_effect=RuntimeError("llm down")),
    ):
        lines, _ = await write_banter(state, config, chaos_subtype=ChaosSubtype.ABANDONED_STORM)

    assert [text for _host, text in lines] == CHAOS_NORMAL_STOCK_LINES[ChaosSubtype.ABANDONED_STORM]
    assert state.chaos_last_degraded_reason == "script_fallback"


@pytest.mark.asyncio
async def test_normal_banter_still_uses_generic_stock_without_llm(config, state):
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    lines, _ = await write_banter(state, config)

    assert [text for _host, text in lines] == ["E torniamo alla musica!"]
    assert state.chaos_script_fallbacks == 0


def test_chaos_prompt_fragments_are_public_json_safe():
    # The prompt fragments are plain strings that can be embedded in captured
    # prompt fixtures without custom serializers.
    payload = {subtype.value: block for subtype, block in scriptwriter.CHAOS_SUBTYPE_BLOCKS.items()}
    assert "CHAOS_ICON_MOMENT" in json.dumps(payload)
