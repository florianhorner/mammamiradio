"""Repair issue helpers for Mamma Mi Radio."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


def create_issue(
    hass: HomeAssistant,
    issue_id: str,
    *,
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    placeholders: dict[str, str] | None = None,
) -> None:
    """Create or update an actionable Repairs issue."""
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=severity,
            translation_key=issue_id,
            translation_placeholders=placeholders,
        )
    except KeyError:
        # Unit tests and partially initialized HA cores can lack the issue
        # registry. Repairs are advisory; never break entity setup/control paths.
        return


def delete_issue(hass: HomeAssistant, issue_id: str) -> None:
    """Clear a Repairs issue if the condition recovered."""
    try:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    except KeyError:
        return
