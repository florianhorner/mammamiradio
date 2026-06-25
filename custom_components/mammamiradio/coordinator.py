"""Polling coordinator for the Mamma Mi Radio now-playing contract."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    HTTP_TIMEOUT,
    ISSUE_STATION_UNREACHABLE,
    NOW_PLAYING_PATH,
    UNREACHABLE_MIN_FAILURES,
    UNREACHABLE_MIN_SECONDS,
    UPDATE_INTERVAL,
)
from .repairs import create_issue, delete_issue

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RadioStatus:
    """A normalized snapshot of the station's now-playing contract."""

    session_state: str  # "live" | "stopped" | "empty_queue"
    segment_type: str | None  # "music" | "banter" | "ad" | "news_flash" | ...
    segment_class: str | None  # "music" | "voice" | "interstitial"
    title: str | None
    artist: str | None
    artwork: str | None
    started_at: float | None
    duration: float | None
    host: str | None
    station_name: str

    @classmethod
    def from_payload(cls, payload: dict) -> RadioStatus:
        """Build a status from the v1 now-playing JSON (defensive on every field)."""
        station = payload.get("station") or {}
        now = payload.get("now_playing") or {}
        return cls(
            session_state=str(payload.get("session_state") or "stopped"),
            segment_type=now.get("segment_type"),
            segment_class=now.get("segment_class"),
            title=now.get("title"),
            artist=now.get("artist"),
            artwork=now.get("artwork"),
            started_at=_as_float(now.get("started_at")),
            duration=_as_float(now.get("duration_estimate_sec")),
            host=now.get("host"),
            station_name=str(station.get("name") or "Mamma Mi Radio"),
        )


def _as_float(value: object) -> float | None:
    """Coerce a numeric field to float, tolerating None/garbage from the wire."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class MammaRadioCoordinator(DataUpdateCoordinator[RadioStatus]):
    """Polls the add-on's read contract every few seconds."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, base_url: str) -> None:
        try:
            super().__init__(
                hass,
                _LOGGER,
                config_entry=entry,
                name="Mamma Mi Radio",
                update_interval=UPDATE_INTERVAL,
            )
        except TypeError:
            super().__init__(
                hass,
                _LOGGER,
                name="Mamma Mi Radio",
                update_interval=UPDATE_INTERVAL,
            )
        self._base_url = base_url.rstrip("/")
        self._session = async_get_clientsession(hass)
        self._consecutive_failures = 0
        self._first_failure_at: float | None = None

    @property
    def consecutive_failures(self) -> int:
        """Return consecutive poll failures since the last successful refresh."""
        return self._consecutive_failures

    async def _async_update_data(self) -> RadioStatus:
        """Fetch and parse the now-playing contract."""
        url = f"{self._base_url}{NOW_PLAYING_PATH}"
        try:
            async with asyncio.timeout(HTTP_TIMEOUT):
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        self._record_failure()
                        raise UpdateFailed(f"now-playing returned HTTP {resp.status}")
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            self._record_failure()
            raise UpdateFailed(f"cannot reach the station: {err}") from err
        if not isinstance(payload, dict):
            self._record_failure()
            raise UpdateFailed("now-playing returned a non-object payload")
        status = RadioStatus.from_payload(payload)
        self._record_success()
        return status

    def _record_failure(self) -> None:
        """Track failures and raise an actionable repair only on a sustained outage."""
        now = self.hass.loop.time()
        if self._first_failure_at is None:
            self._first_failure_at = now
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= UNREACHABLE_MIN_FAILURES
            and now - self._first_failure_at >= UNREACHABLE_MIN_SECONDS
        ):
            create_issue(self.hass, ISSUE_STATION_UNREACHABLE)

    def _record_success(self) -> None:
        """Clear failure state after a successful poll."""
        self._consecutive_failures = 0
        self._first_failure_at = None
        delete_issue(self.hass, ISSUE_STATION_UNREACHABLE)
