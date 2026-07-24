"""Tests for producer-owned HA late-reply recovery.

The coordinator protects audio by limiting the foreground *wait*, while one
owned request continues long enough to recover a useful source snapshot.  These
tests deliberately scale wall-clock values down; their relationships mirror the
2s warm / 20s cold / 30s total production policy.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState
from mammamiradio.home.authorization import HomeAuthorization, HomeAuthorizationMode
from mammamiradio.home.entity_policy import set_entity_muted
from mammamiradio.home.ha_context import HomeContext, HomeRegistrySnapshot, _HomeContextFetchOutcome
from mammamiradio.home.ha_enrichment import HomeEvent
from mammamiradio.home.radio_events import RadioEventMatch
from mammamiradio.scheduling import producer
from mammamiradio.scheduling.producer import _HAContextRefreshCoordinator

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _config(tmp_path: Path, *, timeout: float = 0.005, poll_interval: float = 0.01):
    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    config.homeassistant.enabled = True
    config.homeassistant.url = "http://ha.local"
    config.ha_token = "tok"
    config.homeassistant.context_refresh_timeout = timeout
    # Production validates this as an integer; sub-second test scaling keeps
    # the same cadence relationship without sleeping for minutes.
    config.homeassistant.poll_interval = poll_interval  # type: ignore[assignment]
    return config


def _outcome(
    context: HomeContext,
    *,
    kind: Literal["fresh", "cached", "failed"] = "fresh",
    duration: float = 0.0,
) -> _HomeContextFetchOutcome:
    now = time.time()
    return _HomeContextFetchOutcome(
        kind=kind,
        context=context,
        snapshot_timestamp=context.timestamp,
        attempt_started_at=now - duration,
        attempt_finished_at=now,
        duration_seconds=duration,
    )


def _snapshot(summary: str, *, age: float = 0.0, **kwargs) -> HomeContext:
    return HomeContext(summary=summary, timestamp=time.time() - age, **kwargs)


def _states_response(states: list[dict]) -> MagicMock:
    response = MagicMock()
    response.content = json.dumps(states).encode("utf-8")
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_projection_worker_keeps_loop_live_and_publishes_only_when_coordinator_drains(tmp_path):
    import mammamiradio.home.ha_context as ha_context

    config = _config(tmp_path, timeout=0.005, poll_interval=0.01)
    state = StationState(home_authorization=HomeAuthorization.legacy())
    observer = MagicMock()
    state.home_entity_ids_observer = observer
    prior = _snapshot("old ambient", age=0.02, authorization_mode=HomeAuthorizationMode.LEGACY.value)
    response = _states_response(
        [
            {
                "entity_id": "switch.bar_kaffeemaschine_steckdose",
                "state": "on",
                "attributes": {},
            }
        ]
    )
    client = AsyncMock()
    client.get.return_value = response
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()

    def _blocked_catalog(cache_dir):
        assert threading.current_thread().name.startswith("ha-projection")
        worker_started.set()
        release_worker.wait(timeout=1.0)
        try:
            return {}
        finally:
            worker_finished.set()

    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.02
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0)

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(ha_context, "_get_ha_client", return_value=client),
        patch.object(
            ha_context,
            "_fetch_ha_registry_snapshot",
            new_callable=AsyncMock,
            return_value=HomeRegistrySnapshot(source="empty_fallback"),
        ),
        patch.object(ha_context, "fetch_weather_forecast", new_callable=AsyncMock, return_value=""),
        patch.object(ha_context, "load_catalog_snapshot", side_effect=_blocked_catalog),
        patch.object(producer, "_publish_home_context_outcome", return_value=True) as publish,
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            fallback, fresh = await coordinator.prepare_for_segment()
            assert worker_started.wait(timeout=0.2)
            assert fallback.summary == "old ambient"
            assert not fresh
            assert state.ha_context_refresh_stage == "projection"

            await _heartbeat()
            assert ticks > 1
            assert coordinator.current_context is prior
            publish.assert_not_called()

            release_worker.set()
            retained = coordinator.in_flight_task
            assert retained is not None
            await asyncio.wait_for(asyncio.shield(retained), timeout=0.5)
            assert state.ha_context_refresh_stage == "idle"
            publish.assert_not_called()
            observer.assert_not_called()

            adopted, fresh = await coordinator.prepare_for_segment()
            assert fresh
            assert "switch.bar_kaffeemaschine_steckdose" in adopted.raw_states
            publish.assert_called_once()
            observer.assert_called_once_with(frozenset({"switch.bar_kaffeemaschine_steckdose"}))
        finally:
            release_worker.set()
            await coordinator.close()
    assert worker_finished.wait(timeout=0.5)


@pytest.mark.asyncio
async def test_close_while_projection_worker_runs_ignores_late_candidate_and_clears_stage(tmp_path):
    import mammamiradio.home.ha_context as ha_context

    config = _config(tmp_path, timeout=0.004, poll_interval=0.01)
    state = StationState(home_authorization=HomeAuthorization.legacy())
    prior = _snapshot("safe", age=0.02, authorization_mode=HomeAuthorizationMode.LEGACY.value)
    client = AsyncMock()
    client.get.return_value = _states_response(
        [{"entity_id": "switch.bar_kaffeemaschine_steckdose", "state": "on", "attributes": {}}]
    )
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    real_projection = ha_context._project_home_context

    def _blocked_projection(projection_input):
        worker_started.set()
        release_worker.wait(timeout=1.0)
        try:
            return real_projection(projection_input)
        finally:
            worker_finished.set()

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(ha_context, "_get_ha_client", return_value=client),
        patch.object(
            ha_context,
            "_fetch_ha_registry_snapshot",
            new_callable=AsyncMock,
            return_value=HomeRegistrySnapshot(source="empty_fallback"),
        ),
        patch.object(ha_context, "fetch_weather_forecast", new_callable=AsyncMock, return_value=""),
        patch.object(ha_context, "_project_home_context", side_effect=_blocked_projection),
        patch.object(producer, "_publish_home_context_outcome", return_value=True) as publish,
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        await coordinator.prepare_for_segment()
        assert worker_started.wait(timeout=0.2)
        assert state.ha_context_refresh_stage == "projection"

        await coordinator.close()
        assert state.ha_context_refresh_stage == "idle"
        assert not state.ha_context_refresh_in_flight
        assert coordinator.current_context is prior
        publish.assert_not_called()

        release_worker.set()
        assert worker_finished.wait(timeout=0.5)
        await asyncio.sleep(0)
        assert state.ha_context_refresh_stage == "idle"
        assert coordinator.current_context is prior
        publish.assert_not_called()


@pytest.mark.asyncio
async def test_warm_two_second_foreground_fallback_keeps_one_late_request_and_adopts_it(tmp_path):
    """A simulated 2.5s reply is not thrown away after the 2s foreground wait."""
    config = _config(tmp_path)
    stale_but_prompt_safe = _snapshot("old ambient", age=0.02)
    state = StationState()
    calls = 0

    async def _late_fetch(**_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.025)
        return _outcome(_snapshot("late fresh"), duration=0.025)

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: stale_but_prompt_safe),
        patch.object(producer, "_fetch_home_context_outcome", _late_fetch),
        patch.object(producer, "_publish_home_context_outcome", return_value=True),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            first_context, first_handoff = await coordinator.prepare_for_segment()
            retained = coordinator.in_flight_task
            assert first_context.summary == "old ambient"
            assert not first_handoff
            assert retained is not None
            assert state.ha_context_refresh_active_foreground_timed_out

            # A second eligible segment while the request runs must reuse the
            # same mailbox slot rather than make another full HA request or
            # make audio wait through another foreground deadline.
            second_started = time.monotonic()
            second_context, second_handoff = await coordinator.prepare_for_segment()
            second_elapsed = time.monotonic() - second_started
            assert second_context.summary == "old ambient"
            assert not second_handoff
            assert coordinator.in_flight_task is retained
            assert calls == 1
            assert second_elapsed < config.homeassistant.context_refresh_timeout / 2

            await asyncio.sleep(0.03)
            ready = coordinator.read_refresh_mailbox_status()
            assert ready["in_flight"] is False
            assert ready["adoption_pending"] is True
            assert ready["last_result"] == "success"
            assert ready["last_result_used_background"] is True
            adopted, fresh_handoff = await coordinator.prepare_for_segment()
            assert adopted.summary == "late fresh"
            assert fresh_handoff
            assert calls == 1
            assert state.ha_context_refresh_last_result == "success"
            assert state.ha_context_refresh_last_result_used_background
            assert state.ha_context_refresh_last_result_duration_ms is not None
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_cold_start_keeps_the_longer_foreground_wait_then_recovers_late_result(tmp_path):
    """The initial (simulated 20s) warm-up wait remains longer than warm 2s."""
    config = _config(tmp_path)
    state = StationState()

    async def _late_fetch(**_kwargs):
        await asyncio.sleep(0.025)
        return _outcome(_snapshot("first snapshot"), duration=0.025)

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: None),
        patch.object(producer, "_fetch_home_context_outcome", _late_fetch),
        patch.object(producer, "_publish_home_context_outcome", return_value=True),
        patch.object(producer, "_HA_CONTEXT_COLD_LOAD_TIMEOUT", 0.02),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            started = time.monotonic()
            fallback, handoff = await coordinator.prepare_for_segment()
            elapsed = time.monotonic() - started
            assert fallback.summary == ""
            assert not handoff
            # The cold path used its special budget, not the 5ms warm budget.
            assert elapsed >= 0.015
            assert state.ha_context_refresh_active_foreground_timed_out

            await asyncio.sleep(0.01)
            adopted, fresh_handoff = await coordinator.prepare_for_segment()
            assert adopted.summary == "first snapshot"
            assert fresh_handoff
            assert state.ha_context_refresh_last_result_used_background
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_total_cap_cancels_owned_request_and_retries_only_after_poll_cadence(tmp_path):
    config = _config(tmp_path, timeout=0.004, poll_interval=0.2)
    state = StationState()
    prior = _snapshot("old", age=0.21)
    calls = 0
    cancelled = asyncio.Event()

    async def _hung_fetch(**_kwargs):
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _hung_fetch),
        patch.object(producer, "_HA_CONTEXT_BACKGROUND_TIMEOUT", 0.012),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            await coordinator.prepare_for_segment()
            await asyncio.sleep(0.016)
            terminal = coordinator.read_refresh_mailbox_status()
            assert terminal["in_flight"] is False
            assert terminal["adoption_pending"] is False
            assert terminal["last_result"] == "background_timeout"
            assert terminal["last_result_used_background"] is True
            # The producer can be busy with music for a while before its next
            # eligible boundary. Terminal timing must remain the 30s cap, not
            # expand to the delayed mailbox-drain time.
            await asyncio.sleep(0.06)

            # Draining the 30s-cap equivalent records its terminal result. It
            # must not immediately launch another request just because a voice
            # segment asks for context again.
            fallback, handoff = await coordinator.prepare_for_segment()
            assert fallback.summary == "old"
            assert not handoff
            assert cancelled.is_set()
            assert state.ha_context_refresh_last_result == "background_timeout"
            assert state.ha_context_refresh_in_flight is False
            assert calls == 1
            assert 5 <= state.ha_context_refresh_last_result_duration_ms <= 25

            await coordinator.prepare_for_segment()
            assert calls == 1

            await asyncio.sleep(0.15)
            await coordinator.prepare_for_segment()
            assert calls == 2
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_late_success_started_before_the_stale_threshold_keeps_its_one_shots(tmp_path):
    """Crossing the threshold in flight is not a stale-gap resynchronization."""
    config = _config(tmp_path)
    state = StationState()
    prior = _snapshot("almost stale", age=119.99)
    event = HomeEvent("switch.lamp", "Lamp", "off", "on", time.time())
    match = RadioEventMatch("lamp", "directive", "say it once", event, 60, time.time())

    async def _late_fetch(**_kwargs):
        await asyncio.sleep(0.016)
        return _outcome(_snapshot("fresh", events=deque([event], maxlen=20), radio_events=[match]), duration=0.016)

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _late_fetch),
        patch.object(producer, "_publish_home_context_outcome", return_value=True),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            fallback, handoff = await coordinator.prepare_for_segment()
            assert fallback.summary == "almost stale"
            assert not handoff
            await asyncio.sleep(0.02)

            adopted, fresh_handoff = await coordinator.prepare_for_segment()
            assert fresh_handoff
            assert list(adopted.events) == [event]
            assert adopted.radio_events == [match]
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_completed_mailbox_aged_past_threshold_is_withheld_at_adoption(tmp_path):
    """A completed reply is only prompt-safe if it is still fresh when drained."""
    config = _config(tmp_path)
    state = StationState(
        ha_pending_directive="old home directive",
        ha_pending_directive_source="ha",
    )
    clock = [1_010.0]
    prior = HomeContext(summary="safe prior", timestamp=1_000.0)
    event = HomeEvent("switch.lamp", "Lamp", "off", "on", 1_010.0)
    radio_baseline = {"switch.lamp": {"state": "on"}}
    ritual_baseline = {"ritual.lamp": {"state": "on"}}
    published: list[_HomeContextFetchOutcome] = []
    completed = asyncio.Event()

    async def _completed_then_held(**_kwargs):
        await completed.wait()
        context = HomeContext(
            summary="late household detail",
            events=deque([event], maxlen=20),
            timestamp=1_010.0,
        )
        return _HomeContextFetchOutcome(
            kind="fresh",
            context=context,
            snapshot_timestamp=context.timestamp,
            attempt_started_at=1_010.0,
            attempt_finished_at=1_010.0,
            duration_seconds=0.001,
            radio_event_state_baseline=radio_baseline,
            ritual_recipe_state_baseline=ritual_baseline,
        )

    def _record_published(outcome: _HomeContextFetchOutcome) -> bool:
        published.append(outcome)
        return True

    with (
        patch.object(producer.time, "time", side_effect=lambda: clock[0]),
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _completed_then_held),
        patch.object(producer, "_publish_home_context_outcome", side_effect=_record_published),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            fallback, first_handoff = await coordinator.prepare_for_segment()
            assert fallback.summary == "safe prior"
            assert not first_handoff

            completed.set()
            task = coordinator.in_flight_task
            assert task is not None
            await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            assert task.done()

            # The result was fresh when fetched, but not when the next safe
            # host boundary finally drains the mailbox.
            clock[0] = 1_131.0
            mailbox = coordinator.read_refresh_mailbox_status()
            assert mailbox["in_flight"] is False
            assert mailbox["adoption_pending"] is False
            assert mailbox["last_result"] == "stale"

            prompt_context, fresh_handoff = await coordinator.prepare_for_segment()
            assert prompt_context.timestamp == 1_010.0
            assert prompt_context.summary == ""
            assert list(prompt_context.events) == []
            assert not fresh_handoff
            assert state.ha_context_refresh_last_result == "stale"
            assert state.ha_context_refresh_stale
            assert state.ha_pending_directive == ""
            assert state.ha_pending_directive_source == ""
            assert not coordinator.home_event_handoffs_allowed
            assert len(published) == 1
            assert published[0].snapshot_timestamp == 1_010.0
            assert published[0].radio_event_state_baseline == radio_baseline
            assert published[0].ritual_recipe_state_baseline == ritual_baseline
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_normal_late_success_hands_unmuted_one_shots_to_exactly_one_boundary(tmp_path):
    config = _config(tmp_path, poll_interval=1.0)
    state = StationState()
    prior = _snapshot("old", age=1.1)
    muted_id = "switch.muted"
    live_id = "switch.live"
    now = time.time()
    muted_event = HomeEvent(muted_id, "Muted", "off", "on", now)
    live_event = HomeEvent(live_id, "Live", "off", "on", now)
    muted_match = RadioEventMatch("muted", "directive", "never use", muted_event, 60, now)
    live_match = RadioEventMatch("live", "directive", "use this once", live_event, 60, now)
    calls = 0

    async def _late_fetch(**_kwargs):
        nonlocal calls
        calls += 1
        set_entity_muted(tmp_path, muted_id, True, label="Muted")
        await asyncio.sleep(0.016)
        return _outcome(
            _snapshot(
                "fresh",
                raw_states={
                    muted_id: {"state": "on", "attributes": {}},
                    live_id: {"state": "on", "attributes": {}},
                },
                events=deque([muted_event, live_event], maxlen=20),
                radio_events=[muted_match, live_match],
            ),
            duration=0.016,
        )

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _late_fetch),
        patch.object(producer, "_publish_home_context_outcome", return_value=True),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            fallback, first_handoff = await coordinator.prepare_for_segment()
            assert fallback.summary == "old"
            assert not first_handoff
            await asyncio.sleep(0.025)

            adopted, fresh_handoff = await coordinator.prepare_for_segment()
            assert fresh_handoff
            assert [event.entity_id for event in adopted.events] == [live_id]
            assert [match.event.entity_id for match in adopted.radio_events] == [live_id]
            assert muted_id not in adopted.raw_states

            # Subsequent cache views are safe for prompts but may never replay
            # the radio-event/ritual one-shot handoff.
            cached, cached_handoff = await coordinator.prepare_for_segment()
            assert not cached_handoff
            assert cached.radio_events == []
            assert calls == 1
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_stale_gap_resynchronizes_ambient_context_without_delayed_events(tmp_path):
    config = _config(tmp_path)
    state = StationState()
    prior = _snapshot("too old", age=121.0)
    now = time.time()
    delayed_event = HomeEvent("switch.lamp", "Lamp", "off", "on", now)
    delayed_match = RadioEventMatch("lamp", "directive", "late directive", delayed_event, 60, now)

    async def _fresh_after_gap(**_kwargs):
        return _outcome(
            _snapshot(
                "resynchronized ambient",
                events=deque([delayed_event], maxlen=20),
                radio_events=[delayed_match],
                ritual_public_families=["Late ritual"],
            )
        )

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _fresh_after_gap),
        patch.object(producer, "_publish_home_context_outcome", return_value=True),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            adopted, fresh_handoff = await coordinator.prepare_for_segment()
            assert adopted.summary == "resynchronized ambient"
            assert not fresh_handoff
            assert list(adopted.events) == []
            assert adopted.radio_events == []
            assert adopted.ritual_recipe_matches == []
            assert adopted.ritual_public_families == []
            assert not state.ha_context_refresh_stale
            assert not coordinator.home_event_handoffs_allowed

            # The next ordinary poll is the first one allowed to hand events
            # to the producer again.
            await asyncio.sleep(0.02)
            _next, next_handoff = await coordinator.prepare_for_segment()
            assert next_handoff
            assert coordinator.home_event_handoffs_allowed
        finally:
            await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("directive_source", ["ha", "ha:person.florian_horner"])
async def test_stale_fallback_withholds_pending_ha_directives_and_running_gags(tmp_path, directive_source):
    config = _config(tmp_path)
    state = StationState(
        ha_pending_directive="old home event",
        ha_pending_directive_source=directive_source,
        ha_running_gag="an old house gag",
        ha_running_gag_key="old-home-event",
    )
    prior = _snapshot("too old", age=121.0)

    async def _hung_fetch(**_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _hung_fetch),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            prompt_context, handoff = await coordinator.prepare_for_segment()
            assert prompt_context.summary == ""
            assert not handoff
            assert state.ha_pending_directive == ""
            assert state.ha_pending_directive_source == ""
            assert state.ha_running_gag == ""
            assert state.ha_running_gag_key == ""
        finally:
            await coordinator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("directive", "source"),
    [
        ("timer pasta is done", "timer"),
        ("operator instruction", "operator"),
        ("listener skip bit", "skip_bit"),
    ],
)
async def test_stale_fallback_preserves_non_ha_directive_sources(tmp_path, directive, source):
    config = _config(tmp_path)
    state = StationState(
        ha_pending_directive=directive,
        ha_pending_directive_source=source,
    )
    prior = _snapshot("too old", age=121.0)

    async def _hung_fetch(**_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _hung_fetch),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            await coordinator.prepare_for_segment()
            assert state.ha_pending_directive == directive
            assert state.ha_pending_directive_source == source
        finally:
            await coordinator.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_late_task_without_post_shutdown_state_writes(tmp_path):
    config = _config(tmp_path)
    state = StationState()
    prior = _snapshot("old", age=0.02)
    cancelled = asyncio.Event()

    async def _slow_fetch(**_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "_fetch_home_context_outcome", _slow_fetch),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        await coordinator.prepare_for_segment()
        assert state.ha_context_refresh_in_flight

        await coordinator.close()
        state_after_close = (
            state.ha_context_refresh_in_flight,
            state.ha_context_refresh_active_foreground_timed_out,
            state.ha_context_refresh_last_result,
            state.ha_context_refresh_last_result_duration_ms,
        )
        await asyncio.sleep(0.01)

    assert cancelled.is_set()
    assert state_after_close == (
        state.ha_context_refresh_in_flight,
        state.ha_context_refresh_active_foreground_timed_out,
        state.ha_context_refresh_last_result,
        state.ha_context_refresh_last_result_duration_ms,
    )


def test_has_real_home_context():
    assert not producer._has_real_home_context(None)
    assert not producer._has_real_home_context(HomeContext())
    assert producer._has_real_home_context(HomeContext(timestamp=1.0))
    assert producer._has_real_home_context(HomeContext(summary="something"))


@pytest.mark.asyncio
async def test_narrow_coordinator_never_adopts_legacy_module_cache(tmp_path):
    """A cold/narrow install must not seed its coordinator from a legacy-stamped
    module cache, nor surface it as a timeout fallback."""
    import mammamiradio.home.ha_context as ha_context_mod

    config = _config(tmp_path)
    state = StationState()
    state.home_authorization = HomeAuthorization.narrow()
    legacy_module = HomeContext(
        summary="PRIVATE MODULE CACHE",
        timestamp=time.time(),
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )

    async def _hung_fetch(**_kwargs):
        await asyncio.sleep(1.0)
        return _outcome(_snapshot("late"))

    with (
        patch.object(ha_context_mod, "_ha_cache", legacy_module),
        patch.object(producer, "_fetch_home_context_outcome", _hung_fetch),
        patch.object(producer, "_HA_CONTEXT_COLD_LOAD_TIMEOUT", 0.01),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            ctx, _fresh = await coordinator.prepare_for_segment()
        finally:
            await coordinator.close()

    # get_cached_home_context() rejects the wrong-mode cache, so the coordinator
    # starts empty and the foreground timeout falls back to an empty context.
    assert "PRIVATE MODULE CACHE" not in (ctx.summary or "")
    assert ctx.authorization_mode != HomeAuthorizationMode.LEGACY.value


@pytest.mark.asyncio
async def test_narrow_coordinator_discards_legacy_stamped_fresh_result(tmp_path):
    """Even a *successful* fetch whose result is stamped for the other mode must
    be discarded — authorization is install-scoped and never crosses."""
    config = _config(tmp_path)
    state = StationState()
    state.home_authorization = HomeAuthorization.narrow()
    wrong_mode = HomeContext(
        summary="PRIVATE LEGACY RESULT",
        timestamp=time.time(),
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )

    async def _wrong_mode_fetch(**_kwargs):
        return _outcome(wrong_mode)

    with (
        patch.object(producer, "get_cached_home_context", lambda *_a, **_k: None),
        patch.object(producer, "_fetch_home_context_outcome", _wrong_mode_fetch),
        patch.object(producer, "_publish_home_context_outcome", return_value=True),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            ctx, _fresh = await coordinator.prepare_for_segment()
        finally:
            await coordinator.close()

    assert "PRIVATE LEGACY RESULT" not in (ctx.summary or "")
    assert ctx.authorization_mode != HomeAuthorizationMode.LEGACY.value


@pytest.mark.asyncio
async def test_inflight_mute_then_unmute_discards_the_pre_mute_candidate(tmp_path):
    """A hard mute is a temporal boundary, not merely the current policy view."""
    import mammamiradio.home.ha_context as ha_context

    config = _config(tmp_path, poll_interval=1.0)
    state = StationState(home_authorization=HomeAuthorization.legacy())
    private_id = "switch.private"
    live_id = "switch.live"
    prior = _snapshot(
        "safe prior",
        raw_states={private_id: {"state": "off", "attributes": {}}},
        authorization_mode=HomeAuthorizationMode.LEGACY.value,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def _gated_legacy_fetch(**_kwargs):
        started.set()
        await release.wait()
        now = time.time()
        private_event = HomeEvent(private_id, "Private", "off", "on", now)
        live_event = HomeEvent(live_id, "Live", "off", "on", now)
        private_radio = RadioEventMatch("private", "directive", "private cue", private_event, 60, now)
        live_radio = RadioEventMatch("live", "directive", "live cue", live_event, 60, now)
        private_ritual = SimpleNamespace(
            entity_id=private_id,
            recipe=SimpleNamespace(public_family_label="Private ritual"),
        )
        live_ritual = SimpleNamespace(
            entity_id=live_id,
            recipe=SimpleNamespace(public_family_label="Live ritual"),
        )
        return HomeContext(
            raw_states={
                private_id: {"state": "on", "attributes": {}},
                live_id: {"state": "on", "attributes": {}},
            },
            events=deque([private_event, live_event], maxlen=20),
            radio_events=[private_radio, live_radio],
            ritual_recipe_matches=[private_ritual, live_ritual],
            ritual_public_families=["Private ritual", "Live ritual"],
            timestamp=now,
            authorization_mode=HomeAuthorizationMode.LEGACY.value,
        )

    with (
        patch.object(producer, "get_cached_home_context", lambda *_args, **_kwargs: prior),
        patch.object(producer, "fetch_home_context", _gated_legacy_fetch),
        patch.object(ha_context, "_ha_cache", None),
        patch.object(ha_context, "_radio_event_state_cache", {}),
        patch.object(ha_context, "_ritual_recipe_state_cache", {}),
        patch.object(ha_context, "_home_context_invalidation_generation", 0),
        patch.object(ha_context, "_home_context_entity_invalidation_generations", {}),
    ):
        coordinator = _HAContextRefreshCoordinator(config, state)
        try:
            fallback, first_handoff = await coordinator.prepare_for_segment()
            assert fallback.summary == ""
            assert not first_handoff
            await asyncio.wait_for(started.wait(), timeout=0.1)

            set_entity_muted(tmp_path, private_id, True, label="Private switch")
            ha_context.invalidate_home_context_entity_baselines({private_id})
            coordinator.invalidate_muted_entities({private_id})
            # The physical state changes while the hard mute is active; opening
            # the policy again must not replay this in-flight transition.
            set_entity_muted(tmp_path, private_id, False)

            release.set()
            task = coordinator.in_flight_task
            assert task is not None
            await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            adopted, fresh_handoff = await coordinator.prepare_for_segment()
        finally:
            await coordinator.close()

    assert fresh_handoff
    assert private_id not in adopted.raw_states
    assert [event.entity_id for event in adopted.events] == [live_id]
    assert [match.event.entity_id for match in adopted.radio_events] == [live_id]
    assert [match.entity_id for match in adopted.ritual_recipe_matches] == [live_id]
    assert adopted.ritual_public_families == ["Live ritual"]
