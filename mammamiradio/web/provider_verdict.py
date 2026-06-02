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

import time

from mammamiradio.core.models import KeyStatus, StationState
from mammamiradio.core.provider_checks import check_provider_keys


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


async def _run_provider_verdict(app_state) -> None:
    """Probe configured AI keys and persist the verdict onto StationState.

    Non-blocking by contract: callers schedule this via ``asyncio.create_task`` and
    never await it, so it can never delay boot or the first audio (Leadership
    Principle #2). All exceptions are swallowed — a flaky network must never crash
    startup or a key-save; the status simply stays "unverified".
    """
    config = app_state.config
    state = app_state.station_state
    if not config.anthropic_api_key and not config.openai_api_key:
        return
    # Snapshot the keys we're validating. If a concurrent save_keys swaps a key
    # mid-probe, a late-finishing stale probe must not clobber the fresh verdict —
    # its sibling save-scheduled probe already owns the new key's result.
    anthropic_key = config.anthropic_api_key
    openai_key = config.openai_api_key
    try:
        result = await check_provider_keys(config)
    except BaseException:
        return
    if config.anthropic_api_key != anthropic_key or config.openai_api_key != openai_key:
        return  # key changed mid-flight; the save-scheduled probe owns the verdict now
    _record_provider_verdict(state, result)
