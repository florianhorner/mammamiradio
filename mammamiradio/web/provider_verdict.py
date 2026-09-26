"""Active AI-key validation verdict: probe result -> tri-state StationState verdict.

Extracted from ``web/streamer.py`` (god-module split, mirrors ``web/persistence.py``).
The probe itself lives in ``core/provider_checks``; the route handlers and capability
shaping that consume these verdicts stay in ``streamer``.

A bogus ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` present at boot must read as
"not working" in the admin WITHOUT waiting for a banter segment to 401. These helpers
turn a ``check_provider_keys`` payload into a persisted ``unverified | valid | rejected``
verdict, and run that probe non-blockingly at startup / on key-save / on demand.
"""

from __future__ import annotations

import asyncio
import time

from mammamiradio.core.models import KeyStatus, StationState
from mammamiradio.core.provider_checks import check_provider_keys


def _provider_check_keys(config) -> tuple[str, str, str, str, str]:
    return (
        config.anthropic_api_key,
        config.openai_api_key,
        config.azure_speech_key,
        config.azure_speech_region,
        config.elevenlabs_api_key,
    )


def _provider_check_identity(app_state) -> tuple:
    return (*_provider_check_keys(app_state.config), getattr(app_state, "_provider_check_generation", 0))


def _verdict_from_probe_entry(entry: dict) -> KeyStatus | None:
    """Map a single ``check_provider_keys`` provider entry to a key-status verdict.

    Returns "valid" / "rejected" for a definitive auth answer, or ``None`` when the
    probe was inconclusive (key absent, quota/rate-limit/network error) so the caller
    leaves the prior status untouched rather than overwriting it with a false signal.
    """
    if not isinstance(entry, dict) or not entry.get("configured"):
        return None
    if entry.get("ok"):
        return "valid"
    if entry.get("error_type") == "authentication_error":
        return "rejected"
    # Quota / rate-limit / network / unknown: the key was NOT actively refused, so we
    # cannot claim "rejected". Stay "unverified" (handled by the caller as a no-op).
    return None


def _record_provider_verdict(state: StationState, probe_result: dict) -> None:
    """Persist a ``check_provider_keys`` payload onto StationState key-status fields.

    Only a definitive verdict ("valid"/"rejected") overwrites the prior status; an
    inconclusive probe leaves the existing status as-is.
    """
    providers = (probe_result or {}).get("providers", {})
    now = time.time()

    anthropic_verdict = _verdict_from_probe_entry(providers.get("anthropic", {}))
    if anthropic_verdict is not None:
        state.anthropic_key_status = anthropic_verdict
        state.anthropic_key_checked_at = now

    # OpenAI is keyed by a single OPENAI_API_KEY; the chat endpoint is the canonical
    # signal for the script-generation fallback path, so base the verdict on it.
    openai_verdict = _verdict_from_probe_entry(providers.get("openai_chat", {}))
    if openai_verdict is not None:
        state.openai_key_status = openai_verdict
        state.openai_key_checked_at = now


async def _probe_provider_keys(app_state, *, ai_only: bool = False) -> dict:
    """Share one current-key probe across boot, save, and operator checks."""
    lock = getattr(app_state, "_provider_check_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state._provider_check_lock = lock

    async with lock:
        if getattr(app_state, "_provider_checks_shutting_down", False):
            raise RuntimeError("Provider checks are shutting down")
        keys = _provider_check_identity(app_state)
        cached = getattr(app_state, "_provider_check_cached_result", None)
        if (
            cached is not None
            and getattr(app_state, "_provider_check_cached_keys", None) == keys
            and time.time() - getattr(app_state, "_provider_check_cached_at", 0.0) < 2.0
        ):
            return cached

        task = getattr(app_state, "_provider_check_task", None)
        if getattr(app_state, "_provider_check_task_keys", None) != keys:
            task = None
        if task is not None and task.done():
            app_state._provider_check_task = None
            task = None
        if task is None:
            app_state._provider_check_task_keys = keys
            ai_result = asyncio.get_running_loop().create_future()
            app_state._provider_check_ai_result = ai_result
            task = asyncio.create_task(_perform_provider_probe(app_state, keys, ai_result))
            app_state._provider_check_task = task
            active = getattr(app_state, "background_tasks", None)
            if active is None:
                active = app_state.background_tasks = set()
            active.add(task)

            def forget(finished):
                active.discard(finished)
                if not ai_result.done():
                    ai_result.set_result({"ok": False, "providers": {}})
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(forget)

        else:
            ai_result = app_state._provider_check_ai_result

    try:
        result = await asyncio.shield(ai_result if ai_only else task)
    except BaseException:
        # A cancelled HTTP waiter must not cancel or orphan the shared probe.
        async with lock:
            if not task.done() or getattr(app_state, "_provider_check_task", None) is not task:
                raise
            app_state._provider_check_task = None
        raise

    return result


async def _perform_provider_probe(app_state, keys: tuple[str, ...], ai_result: asyncio.Future) -> dict:
    if keys != _provider_check_identity(app_state) or getattr(app_state, "_provider_checks_shutting_down", False):
        raise RuntimeError("Provider credentials changed before the check started")

    def ai_checked(result: dict) -> None:
        if keys == _provider_check_identity(app_state) and not getattr(
            app_state, "_provider_checks_shutting_down", False
        ):
            _record_provider_verdict(app_state.station_state, result)
        ai_result.set_result(result)

    result = await check_provider_keys(app_state.config, on_ai_checked=ai_checked)
    if not ai_result.done():
        ai_checked(result)  # Also settle mocks and checks with no configured AI key.
    if (
        keys == _provider_check_identity(app_state)
        and not getattr(app_state, "_provider_checks_shutting_down", False)
        and getattr(app_state, "_provider_check_task", None) is asyncio.current_task()
    ):
        app_state._provider_check_cached_result = result
        app_state._provider_check_cached_keys = keys
        app_state._provider_check_cached_at = time.time()
    return result


def _provider_probe_in_flight(app_state) -> bool:
    keys = _provider_check_identity(app_state)
    ai_result = getattr(app_state, "_provider_check_ai_result", None)
    if getattr(app_state, "_provider_check_task_keys", None) == keys and ai_result is not None:
        return not ai_result.done()
    return any(
        (task := getattr(app_state, name, None)) is not None
        and not task.done()
        and (key_name is None or getattr(app_state, key_name, None) == keys)
        for name, key_name in (
            ("provider_verdict_task", None),
            ("_provider_check_task", "_provider_check_task_keys"),
            ("_setup_recheck_provider_task", "_setup_recheck_provider_task_keys"),
        )
    )


async def _run_provider_verdict(app_state) -> None:
    """Schedule a non-blocking shared probe at boot or after a credential save."""
    config = app_state.config
    if not config.anthropic_api_key and not config.openai_api_key:
        return
    try:
        await _probe_provider_keys(app_state, ai_only=True)
    except Exception:
        return
