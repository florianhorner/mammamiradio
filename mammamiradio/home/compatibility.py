"""Private Home upgrade decision, evaluated outside the audio startup path."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mammamiradio.home.authorization import HomeAuthorization
from mammamiradio.home.bindings import HomeBindings
from mammamiradio.home.consent import load_ambient_consent
from mammamiradio.home.migration import (
    load_legacy_home_database_preflight_v1,
    load_legacy_home_preflight_v1,
)
from mammamiradio.home.profile import resume_home_profile_v1


def home_compatibility_resolved(status: str) -> bool:
    """Only completed evidence reads may authorize setup or a saved choice."""
    return status in {"verified", "ambient", "needs_consent", "narrow"}


@dataclass(frozen=True)
class HomeCompatibility:
    authorization: HomeAuthorization
    status: str
    requires_consent: bool = False
    consent_granted: bool = False

    @property
    def resolved(self) -> bool:
        return home_compatibility_resolved(self.status)

    @property
    def permits_saved_choice(self) -> bool:
        return self.resolved and (not self.requires_consent or self.consent_granted)


def read_home_compatibility(state_dir: Path, db_path: Path) -> HomeCompatibility:
    """A permanent ambient cap wins over every later profile restoration."""
    consent = load_ambient_consent(db_path)
    if not consent.readable:
        return HomeCompatibility(HomeAuthorization.narrow(), "unavailable")
    if consent.capped:
        return HomeCompatibility(
            HomeAuthorization.narrow(),
            "ambient" if consent.granted else "needs_consent",
            requires_consent=True,
            consent_granted=consent.granted,
        )
    try:
        profile = resume_home_profile_v1(state_dir, db_path, strict_io=True)
        if profile is not None:
            bindings = HomeBindings.from_document(profile.to_dict(), identity=profile.content_digest)
            return HomeCompatibility(HomeAuthorization.legacy(bindings), "verified")
        sidecar = load_legacy_home_preflight_v1(state_dir, strict_io=True)
        database = load_legacy_home_database_preflight_v1(db_path, strict_io=True)
    except (OSError, sqlite3.Error):
        return HomeCompatibility(HomeAuthorization.narrow(), "unavailable")
    if sidecar is not None and sidecar == database and sidecar.durable and not sidecar.database_preexisted:
        # Installs already using the narrow consent flow retain that scope.
        return HomeCompatibility(HomeAuthorization.narrow(), "narrow")
    # Only an explicit narrow choice persists the permanent scope cap.
    # Eligible installations may still restore matching private evidence.
    return HomeCompatibility(HomeAuthorization.narrow(), "needs_consent", requires_consent=True)
