"""Producer-level tests for the Callback Director wiring.

The pure ledger logic is covered in tests/hosts/test_verbal_gag_ledger.py and
tests/home/test_gag_select.py. These tests prove the producer:
  - retires a gag ONLY when the model reports it landed (queue-time != used),
  - never retires a gag when the segment is DISCARDED before it airs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Segment, SegmentType, StationState, Track
from mammamiradio.hosts.verbal_gag_ledger import VerbalGagLedger
from mammamiradio.scheduling.producer import run_producer

MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _make_state() -> StationState:
    state = StationState(
        playlist=[
            Track(title="Uno", artist="A", duration_ms=200_000, spotify_id="d1"),
            Track(title="Due", artist="A", duration_ms=180_000, spotify_id="d2"),
        ],
        listeners_active=1,
    )
    state.verbal_gag_ledger = VerbalGagLedger()
    state.verbal_gag_ledger.add_gag("bathroom fans", punch=5, now=100.0)
    return state


def _make_config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    config.anthropic_api_key = "test-key"
    return config


async def _run_until_queued(queue, state, config, timeout: float = 5.0):
    task = asyncio.create_task(run_producer(queue, state, config))
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while queue.qsize() == 0:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("Producer did not queue a segment in time")
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_callback_lands_retires_gag(tmp_path):
    """Model reports it used the gag -> the gag is retired (pruned) at queue time."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)
    gid = next(iter(state.verbal_gag_ledger.gags))
    gag = state.verbal_gag_ledger.gags[gid]

    async def _flash(state_arg, config_arg, callback_gag=None):
        if callback_gag:
            state_arg.pending_callback_landed = True  # model landed it
        return (host, "Flash con un ventilatore!", "sports")

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(f"{SCRIPTWRITER_MODULE}.write_news_flash", side_effect=_flash),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch.object(VerbalGagLedger, "offer", return_value=(gid, gag)),
    ):
        await _run_until_queued(queue, state, config)

    assert state.verbal_gag_ledger.gags == {}, "a landed gag must be retired (pruned)"


@pytest.mark.asyncio
async def test_callback_ignored_does_not_retire(tmp_path):
    """Model ignores the gag (callback_used false) -> the gag stays fresh."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)
    gid = next(iter(state.verbal_gag_ledger.gags))
    gag = state.verbal_gag_ledger.gags[gid]

    async def _flash(state_arg, config_arg, callback_gag=None):
        if callback_gag:
            state_arg.pending_callback_landed = False  # model ignored it
        return (host, "Flash senza riferimenti.", "sports")

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(f"{SCRIPTWRITER_MODULE}.write_news_flash", side_effect=_flash),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch.object(VerbalGagLedger, "offer", return_value=(gid, gag)),
    ):
        await _run_until_queued(queue, state, config)

    assert gid in state.verbal_gag_ledger.gags, "an ignored gag must not be retired"
    assert not state.verbal_gag_ledger.gags[gid].traveled


@pytest.mark.asyncio
async def test_discarded_segment_does_not_retire(tmp_path):
    """A flash discarded by a source switch never reaches the success callback,
    so its gag stays fresh (queue-time mutation is past the discard checks)."""
    state = _make_state()
    config = _make_config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    flash_path = tmp_path / "flash.mp3"
    flash_path.write_bytes(b"\x00" * 2048)
    gid = next(iter(state.verbal_gag_ledger.gags))
    gag = state.verbal_gag_ledger.gags[gid]

    offer_calls = 0

    def _offer(**_kwargs):
        nonlocal offer_calls
        offer_calls += 1
        return (gid, gag) if offer_calls == 1 else None  # only the discarded one carries the gag

    async def _flash(state_arg, config_arg, callback_gag=None):
        if callback_gag:
            state_arg.pending_callback_landed = True  # would retire IF it queued
            state_arg.playlist_revision += 1  # source switch mid-generation -> discard
        return (host, "Flash che verra scartato.", "sports")

    with (
        patch(f"{MODULE}.next_segment_type", return_value=SegmentType.NEWS_FLASH),
        patch(f"{SCRIPTWRITER_MODULE}.write_news_flash", side_effect=_flash),
        patch(f"{MODULE}.synthesize", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}._try_crossfade", new_callable=AsyncMock, return_value=flash_path),
        patch(f"{MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch.object(VerbalGagLedger, "offer", side_effect=_offer),
    ):
        await _run_until_queued(queue, state, config)

    assert gid in state.verbal_gag_ledger.gags, "a discarded segment must not retire its gag"
    assert not state.verbal_gag_ledger.gags[gid].traveled
