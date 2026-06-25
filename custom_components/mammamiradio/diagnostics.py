"""Diagnostics support for the Mamma Mi Radio integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MammaRadioConfigEntry
from .const import CONF_ADMIN_TOKEN
from .coordinator import MammaRadioCoordinator

TO_REDACT = {CONF_ADMIN_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: MammaRadioConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator: MammaRadioCoordinator | None = getattr(entry, "runtime_data", None)
    status = coordinator.data if coordinator is not None else None
    return {
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator": {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "consecutive_failures": getattr(coordinator, "consecutive_failures", None),
        },
        "station": asdict(status) if status is not None else None,
    }
