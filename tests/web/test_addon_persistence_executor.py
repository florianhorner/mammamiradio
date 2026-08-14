"""Regression coverage for isolated HA add-on persistence execution."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mammamiradio.web import persistence, streamer


def test_blocked_addon_persistence_does_not_starve_default_executor():
    addon_started = threading.Event()
    release_addon = threading.Event()
    addon_thread_names: list[str] = []

    def blocked_addon_write() -> None:
        addon_thread_names.append(threading.current_thread().name)
        addon_started.set()
        if not release_addon.wait(timeout=2):
            raise TimeoutError("test did not release blocked add-on persistence")

    async def exercise() -> str:
        addon_task = asyncio.create_task(streamer._run_addon_persistence(blocked_addon_write))
        try:
            for _ in range(100):
                if addon_started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert addon_started.is_set()

            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: threading.current_thread().name),
                timeout=0.5,
            )
        finally:
            release_addon.set()
            await asyncio.wait_for(addon_task, timeout=1)

    loop = asyncio.new_event_loop()
    default_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="test-default",
    )
    loop.set_default_executor(default_executor)
    try:
        default_thread_name = loop.run_until_complete(exercise())
    finally:
        release_addon.set()
        default_executor.shutdown(wait=True, cancel_futures=True)
        loop.close()

    assert addon_thread_names == ["addon-persistence_0"]
    assert default_thread_name == "test-default_0"


async def test_addon_credentials_use_addon_persistence_helper(monkeypatch):
    config = SimpleNamespace(is_addon=True)
    station_state = object()
    app_state = SimpleNamespace(config=config, station_state=station_state)
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    updates = {"ANTHROPIC_API_KEY": "sk-addon"}
    submit = AsyncMock(return_value=persistence._SECRET_WRITE_DURABLE)
    apply_live = Mock()
    provider_verdict = AsyncMock(return_value=None)
    monkeypatch.setattr(streamer, "_run_addon_persistence", submit)
    monkeypatch.setattr(streamer, "_apply_live_credentials", apply_live)
    monkeypatch.setattr(streamer, "_run_provider_verdict", provider_verdict)

    await streamer._persist_and_apply_credentials(request, updates, use_addon_options=True)
    await app_state.provider_verdict_task

    submit.assert_awaited_once_with(streamer._save_addon_options, updates)
    apply_live.assert_called_once_with(station_state, config, updates)
    provider_verdict.assert_awaited_once_with(app_state)


async def test_addon_credential_unconfirmed_durability_is_not_treated_as_success(monkeypatch):
    """A committed-but-unconfirmed write must fail loud, not silently apply live credentials."""
    config = SimpleNamespace(is_addon=True)
    station_state = object()
    app_state = SimpleNamespace(config=config, station_state=station_state)
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    updates = {"ANTHROPIC_API_KEY": "sk-addon"}
    submit = AsyncMock(return_value=persistence._SECRET_WRITE_COMMITTED_UNCONFIRMED)
    apply_live = Mock()
    monkeypatch.setattr(streamer, "_run_addon_persistence", submit)
    monkeypatch.setattr(streamer, "_apply_live_credentials", apply_live)

    with pytest.raises(persistence._AddonPersistenceError, match="Unable to confirm the add-on credential save"):
        await streamer._persist_and_apply_credentials(request, updates, use_addon_options=True)

    submit.assert_awaited_once_with(streamer._save_addon_options, updates)
    apply_live.assert_not_called()
    assert not hasattr(app_state, "provider_verdict_task")


async def test_addon_credential_precommit_failure_leaves_live_state_untouched(monkeypatch):
    config = SimpleNamespace(is_addon=True)
    station_state = object()
    app_state = SimpleNamespace(config=config, station_state=station_state)
    request = SimpleNamespace(app=SimpleNamespace(state=app_state))
    updates = {"ANTHROPIC_API_KEY": "sk-addon"}
    submit = AsyncMock(side_effect=persistence._AddonPersistenceError("Unable to persist add-on credentials"))
    apply_live = Mock()
    monkeypatch.setattr(streamer, "_run_addon_persistence", submit)
    monkeypatch.setattr(streamer, "_apply_live_credentials", apply_live)

    with pytest.raises(persistence._AddonPersistenceError, match="Unable to persist"):
        await streamer._persist_and_apply_credentials(request, updates, use_addon_options=True)

    submit.assert_awaited_once_with(streamer._save_addon_options, updates)
    apply_live.assert_not_called()
    assert not hasattr(app_state, "provider_verdict_task")
