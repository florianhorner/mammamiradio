"""Tests for the budgeted HA context refresh (PR2: HA Green context budget).

`_refresh_home_context_budgeted` is the single refresh path the producer uses for
BANTER, AD, and NEWS_FLASH segments, so these tests cover all three seg types at
once — NEWS_FLASH is not on a separate unbudgeted path. The budget protects audio
continuity (INSTANT AUDIO): a slow/hung HA degrades to stale-then-empty context
instead of stalling segment production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.home.authorization import HomeAuthorization, HomeAuthorizationMode
from mammamiradio.home.ha_context import HomeContext
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import (
    _HA_CONTEXT_COLD_LOAD_TIMEOUT,
    _refresh_home_context_budgeted,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _config(timeout: float = 2.0):
    config = load_config(TOML_PATH)
    config.homeassistant.enabled = True
    config.homeassistant.url = "http://ha.local"
    config.ha_token = "tok"
    config.homeassistant.context_refresh_timeout = timeout
    return config


@pytest.mark.asyncio
async def test_refresh_returns_fresh_context_on_success():
    fresh = HomeContext(summary="fresh")

    async def _fast(**_kwargs):
        return fresh

    with patch.object(producer, "fetch_home_context", _fast):
        out = await _refresh_home_context_budgeted(_config(), HomeContext(summary="old"))
    assert out is fresh


@pytest.mark.asyncio
async def test_refresh_falls_back_to_stale_cache_on_timeout():
    stale = HomeContext(summary="stale")

    async def _slow(**_kwargs):
        await asyncio.sleep(1.0)
        return HomeContext(summary="fresh")

    with patch.object(producer, "fetch_home_context", _slow):
        out = await _refresh_home_context_budgeted(_config(timeout=0.01), stale)
    assert out is not stale
    assert out.summary == "stale"
    assert stale.summary == "stale"


@pytest.mark.asyncio
async def test_refresh_falls_back_to_empty_when_no_cache_anywhere():
    async def _slow(**_kwargs):
        await asyncio.sleep(1.0)
        return HomeContext(summary="fresh")

    # No cache anywhere takes the cold warm-up budget, so shrink it too — this is
    # the fully-hung-HA-at-startup case where even the warm-up deadline blows.
    with (
        patch.object(producer, "fetch_home_context", _slow),
        patch.object(producer, "get_cached_home_context", lambda **_kwargs: None),
        patch.object(producer, "_HA_CONTEXT_COLD_LOAD_TIMEOUT", 0.01),
    ):
        out = await _refresh_home_context_budgeted(_config(timeout=0.01), None)
    assert isinstance(out, HomeContext)
    assert out.summary == ""


@pytest.mark.asyncio
async def test_cold_load_gets_longer_budget_than_steady_state():
    # A refresh that takes longer than the tight budget but well under the cold
    # warm-up budget. Warm (cache present) times out to stale; cold (no cache
    # anywhere) gets the longer budget and completes — proving the two tiers.
    async def _slow(**_kwargs):
        await asyncio.sleep(0.05)
        return HomeContext(summary="fresh")

    with (
        patch.object(producer, "fetch_home_context", _slow),
        patch.object(producer, "get_cached_home_context", lambda **_kwargs: None),
    ):
        warm = await _refresh_home_context_budgeted(_config(timeout=0.01), HomeContext(summary="stale"))
        assert warm.summary == "stale"  # tight budget timed out, aired on stale

        cold = await _refresh_home_context_budgeted(_config(timeout=0.01), None)
        assert cold.summary == "fresh"  # cold warm-up budget let it complete

    assert _HA_CONTEXT_COLD_LOAD_TIMEOUT > 2.0


def test_has_real_home_context():
    assert not producer._has_real_home_context(None)
    assert not producer._has_real_home_context(HomeContext())  # empty timeout fallback
    assert producer._has_real_home_context(HomeContext(timestamp=1.0))
    assert producer._has_real_home_context(HomeContext(summary="something"))


@pytest.mark.asyncio
async def test_empty_fallback_does_not_poison_cold_budget():
    # Simulates the next refresh after a prior cold timeout aired the empty
    # HomeContext(): an empty cache must NOT be treated as warm, or a slow-but-
    # healthy registry warm-up would be stuck on the tight budget forever.
    async def _slow(**_kwargs):
        await asyncio.sleep(0.05)
        return HomeContext(summary="fresh")

    with (
        patch.object(producer, "fetch_home_context", _slow),
        patch.object(producer, "get_cached_home_context", lambda **_kwargs: None),
    ):
        out = await _refresh_home_context_budgeted(_config(timeout=0.01), HomeContext())
    assert out.summary == "fresh"  # empty cache still gets the cold warm-up budget


@pytest.mark.asyncio
async def test_refresh_uses_module_cache_when_passed_cache_is_none():
    module_cache = HomeContext(summary="module")

    async def _slow(**_kwargs):
        await asyncio.sleep(1.0)
        return HomeContext(summary="fresh")

    # No passed cache, but the module cache exists -> treated as warm (tight
    # budget) and the timeout fallback returns the module cache, not empty.
    with (
        patch.object(producer, "fetch_home_context", _slow),
        patch.object(producer, "get_cached_home_context", lambda **_kwargs: module_cache),
    ):
        out = await _refresh_home_context_budgeted(_config(timeout=0.01), None)
    assert out is not module_cache
    assert out.summary == "module"
    assert module_cache.summary == "module"


@pytest.mark.asyncio
async def test_refresh_timeout_fallback_still_honors_a_mute_applied_mid_flight(tmp_path):
    """A mute saved while a refresh is in flight must not resurface via the
    timeout fallback — this bypasses fetch_home_context's own mute filtering
    entirely by reusing a context built before the mute existed."""
    from mammamiradio.home.entity_policy import set_entity_muted

    muted_entity = "switch.bar_kaffeemaschine_steckdose"
    stale = HomeContext(
        raw_states={muted_entity: {"state": "on", "attributes": {}}},
        summary="- Coffee machine: on",
    )

    async def _slow(**_kwargs):
        set_entity_muted(tmp_path, muted_entity, True, label="Coffee machine")
        await asyncio.sleep(1.0)
        return HomeContext(summary="fresh")

    config = _config(timeout=0.01)
    config.cache_dir = tmp_path
    with patch.object(producer, "fetch_home_context", _slow):
        out = await _refresh_home_context_budgeted(config, stale)

    assert muted_entity not in out.raw_states
    assert "caff" not in out.summary.lower()


@pytest.mark.asyncio
async def test_narrow_timeout_never_reuses_legacy_passed_or_module_cache():
    legacy_passed = HomeContext(
        summary="PRIVATE PASSED CACHE",
        timestamp=1.0,
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )
    legacy_module = HomeContext(
        summary="PRIVATE MODULE CACHE",
        timestamp=1.0,
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )

    async def _slow(**_kwargs):
        await asyncio.sleep(1.0)
        return HomeContext(summary="fresh")

    with (
        patch.object(producer, "fetch_home_context", _slow),
        patch.object(producer, "get_cached_home_context", return_value=legacy_module),
        patch.object(producer, "_HA_CONTEXT_COLD_LOAD_TIMEOUT", 0.01),
    ):
        out = await _refresh_home_context_budgeted(
            _config(timeout=0.01),
            legacy_passed,
            authorization=HomeAuthorization.narrow(),
        )

    assert out.authorization_mode == HomeAuthorizationMode.NARROW.value
    assert out.summary == ""


@pytest.mark.asyncio
async def test_narrow_refresh_discards_successful_legacy_stamped_result():
    wrong_mode = HomeContext(
        summary="PRIVATE LEGACY RESULT",
        timestamp=1.0,
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )

    async def _wrong_mode(**_kwargs):
        return wrong_mode

    with (
        patch.object(producer, "fetch_home_context", _wrong_mode),
        patch.object(producer, "get_cached_home_context", return_value=None),
    ):
        out = await _refresh_home_context_budgeted(
            _config(),
            None,
            authorization=HomeAuthorization.narrow(),
        )

    assert out.authorization_mode == HomeAuthorizationMode.NARROW.value
    assert out.summary == ""
