"""Release-beat campaign state for post-update cold opens.

This module owns the small durable state machine that turns a packaged release
beat into a bounded on-air campaign. It deliberately does not depend on the
optional provenance ledger: stream delivery evidence can update this campaign
even when Show Memory is disabled.
"""

from __future__ import annotations

import json
import logging
import os
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

from mammamiradio.core.release_beat_schema import RUNTIME_CONSUMED_KEYS

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LEDGER_FILENAME = "release_campaign_ledger.json"
RELEASE_BEAT_RESOURCE = ("assets", "release", "release_beat.toml")
RELEASE_BEAT_RUNTIME_KEYS = RUNTIME_CONSUMED_KEYS

ACTIVE = "active"
QUEUED_ATTEMPT = "queued_attempt"
AIRED_ATTEMPT = "aired_attempt"
RETIRED = "retired"

DEFAULT_MAX_AIRINGS = 5
DEFAULT_WINDOW_SECONDS = 72 * 60 * 60
DEFAULT_MIN_SECONDS_BETWEEN_AIRINGS = 45 * 60
DEFAULT_MIN_SEGMENTS_BETWEEN_AIRINGS = 6


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, list | tuple):
        out = []
        for item in value:
            text = _as_str(item)
            if text:
                out.append(text)
        return tuple(out)
    return ()


def _as_joined_str(value: Any) -> str:
    if isinstance(value, list | tuple):
        return "; ".join(_as_str_tuple(value))
    return _as_str(value)


def _as_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return default


def _delivered(*, bytes_sent: int, was_skipped: bool, listeners: int) -> bool:
    return not was_skipped and bytes_sent > 0 and listeners > 0


@dataclass(frozen=True)
class ReleaseBeatManifest:
    """Packaged release beat, loaded from source/image metadata."""

    enabled: bool = False
    id: str = ""
    channel: str = ""
    build_sha: str = ""
    semver: str = ""
    title: str = ""
    facts: tuple[str, ...] = ()
    props: tuple[str, ...] = ()
    copy_guidance: str = ""
    forbidden_terms: tuple[str, ...] = ()
    max_airings: int = DEFAULT_MAX_AIRINGS
    campaign_window_seconds: int = DEFAULT_WINDOW_SECONDS
    min_seconds_between_airings: int = DEFAULT_MIN_SECONDS_BETWEEN_AIRINGS
    min_segments_between_airings: int = DEFAULT_MIN_SEGMENTS_BETWEEN_AIRINGS

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.id)

    @classmethod
    def disabled(cls) -> ReleaseBeatManifest:
        return cls(enabled=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseBeatManifest:
        copy_guidance = data.get("copy_guidance", data.get("copy"))
        forbidden_terms = data.get("forbidden_terms", data.get("avoid"))
        return cls(
            enabled=bool(data.get("enabled", False)),
            id=_as_str(data.get("id")),
            channel=_as_str(data.get("channel")),
            build_sha=_as_str(data.get("build_sha")),
            semver=_as_str(data.get("semver")),
            title=_as_str(data.get("title")),
            facts=_as_str_tuple(data.get("facts")),
            props=_as_str_tuple(data.get("props")),
            copy_guidance=_as_joined_str(copy_guidance),
            forbidden_terms=_as_str_tuple(forbidden_terms),
            max_airings=_as_int(data.get("max_airings"), DEFAULT_MAX_AIRINGS),
            campaign_window_seconds=_as_int(
                data.get("campaign_window_seconds"),
                DEFAULT_WINDOW_SECONDS,
                minimum=60,
            ),
            min_seconds_between_airings=_as_int(
                data.get("min_seconds_between_airings"),
                DEFAULT_MIN_SECONDS_BETWEEN_AIRINGS,
                minimum=0,
            ),
            min_segments_between_airings=_as_int(
                data.get("min_segments_between_airings"),
                DEFAULT_MIN_SEGMENTS_BETWEEN_AIRINGS,
                minimum=0,
            ),
        )

    def to_prompt_payload(self, *, variation_index: int = 0) -> dict[str, Any]:
        """Small JSON-safe shape for the scriptwriter prompt."""
        facts = self.facts
        props = self.props
        # Rotate the first visible fact/prop across repeat airings while keeping
        # the full context available. This gives the host a nudge toward variety
        # without hiding the source of truth.
        if facts:
            idx = variation_index % len(facts)
            facts = (facts[idx], *facts[:idx], *facts[idx + 1 :])
        if props:
            idx = variation_index % len(props)
            props = (props[idx], *props[:idx], *props[idx + 1 :])
        return {
            "id": self.id,
            "channel": self.channel,
            "build_sha": self.build_sha,
            "semver": self.semver,
            "title": self.title,
            "facts": list(facts),
            "props": list(props),
            "copy_guidance": self.copy_guidance,
            "forbidden_terms": list(self.forbidden_terms),
        }


@dataclass(frozen=True)
class ReleaseBeatOffer:
    """One attempt offered to the host-generation path."""

    beat_id: str
    attempt_id: str
    prompt_payload: dict[str, Any]

    def segment_metadata(self) -> dict[str, str]:
        return {
            "release_beat_id": self.beat_id,
            "release_beat_attempt_id": self.attempt_id,
        }


@dataclass
class ReleaseCampaignLedger:
    """Durable campaign state for one release beat."""

    schema_version: int = SCHEMA_VERSION
    beat_id: str = ""
    status: str = ACTIVE
    aired_count: int = 0
    first_aired_at: float = 0.0
    last_aired_at: float = 0.0
    last_attempt_at: float = 0.0
    # Wall-clock of the first begin_attempt(), aired or not. Anchors campaign
    # window expiry for a beat the host keeps declining (which never sets
    # first_aired_at), so a never-aired campaign still self-retires.
    first_attempt_at: float = 0.0
    non_release_segments_since_last_airing: int = 0
    retired_reason: str = ""
    attempt_id: str = ""
    queued_segment_id: str = ""
    _dirty: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def fresh(cls, beat_id: str) -> ReleaseCampaignLedger:
        return cls(beat_id=beat_id, status=ACTIVE, _dirty=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleaseCampaignLedger:
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION),
            beat_id=_as_str(data.get("beat_id")),
            status=_as_str(data.get("status")) or ACTIVE,
            aired_count=max(0, int(data.get("aired_count", 0) or 0)),
            first_aired_at=_as_float(data.get("first_aired_at")),
            last_aired_at=_as_float(data.get("last_aired_at")),
            last_attempt_at=_as_float(data.get("last_attempt_at")),
            first_attempt_at=_as_float(data.get("first_attempt_at")),
            non_release_segments_since_last_airing=max(
                0,
                int(data.get("non_release_segments_since_last_airing", 0) or 0),
            ),
            retired_reason=_as_str(data.get("retired_reason")),
            attempt_id=_as_str(data.get("attempt_id")),
            queued_segment_id=_as_str(data.get("queued_segment_id")),
        )

    @classmethod
    def load(cls, cache_dir: Path, *, beat_id: str) -> ReleaseCampaignLedger:
        path = Path(cache_dir) / LEDGER_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls.fresh(beat_id)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Release campaign ledger is unreadable, starting fresh: %s", path)
            return cls.fresh(beat_id)
        if not isinstance(payload, dict):
            logger.warning("Release campaign ledger has unexpected shape, starting fresh: %s", path)
            return cls.fresh(beat_id)
        try:
            ledger = cls.from_dict(payload)
        except (TypeError, ValueError):
            logger.warning("Release campaign ledger has invalid fields, starting fresh: %s", path)
            return cls.fresh(beat_id)
        if ledger.beat_id != beat_id:
            return cls.fresh(beat_id)
        return ledger

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "beat_id": self.beat_id,
            "status": self.status,
            "aired_count": self.aired_count,
            "first_aired_at": self.first_aired_at,
            "last_aired_at": self.last_aired_at,
            "last_attempt_at": self.last_attempt_at,
            "first_attempt_at": self.first_attempt_at,
            "non_release_segments_since_last_airing": self.non_release_segments_since_last_airing,
            "retired_reason": self.retired_reason,
            "attempt_id": self.attempt_id,
            "queued_segment_id": self.queued_segment_id,
        }

    def save_if_dirty(self, cache_dir: Path) -> None:
        if not self._dirty:
            return
        path = Path(cache_dir) / LEDGER_FILENAME
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, path)
            self._dirty = False
        except OSError as exc:
            logger.warning("Could not persist release campaign ledger to %s: %s", path, exc)


class ReleaseCampaign:
    """Runtime facade for release beat scheduling and delivery accounting."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        manifest: ReleaseBeatManifest | None = None,
        ledger: ReleaseCampaignLedger | None = None,
        clock=time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.manifest = manifest or load_release_beat_manifest()
        beat_id = self.manifest.id if self.manifest.active else ""
        self.ledger = ledger or ReleaseCampaignLedger.load(self.cache_dir, beat_id=beat_id)
        self._clock = clock
        if self.enabled and self.ledger.status in {QUEUED_ATTEMPT, AIRED_ATTEMPT}:
            self._activate()

    @property
    def enabled(self) -> bool:
        return self.manifest.active

    @classmethod
    def load(cls, cache_dir: Path, *, clock=time.time) -> ReleaseCampaign:
        return cls(cache_dir, manifest=load_release_beat_manifest(), clock=clock)

    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else now)

    def _retire(self, reason: str) -> None:
        if self.ledger.status == RETIRED and self.ledger.retired_reason == reason:
            return
        self.ledger.status = RETIRED
        self.ledger.retired_reason = reason
        self.ledger.attempt_id = ""
        self.ledger.queued_segment_id = ""
        self.ledger._dirty = True

    def _activate(self) -> None:
        self.ledger.status = ACTIVE
        self.ledger.retired_reason = ""
        self.ledger.attempt_id = ""
        self.ledger.queued_segment_id = ""
        self.ledger._dirty = True

    def _maybe_expire(self, now: float) -> None:
        if not self.enabled:
            return
        if self.ledger.status == RETIRED:
            return
        if self.ledger.aired_count >= self.manifest.max_airings:
            self._retire("budget_exhausted")
            return
        # Anchor on the first airing if there was one, else the first offer —
        # so a beat the host keeps declining (first_aired_at stays 0) still
        # retires at the campaign window instead of offering forever.
        anchor = self.ledger.first_aired_at or self.ledger.first_attempt_at
        if anchor and now - anchor > self.manifest.campaign_window_seconds:
            self._retire("window_expired")

    def is_due(self, *, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        current = self._now(now)
        self._maybe_expire(current)
        if self.ledger.status != ACTIVE:
            return False
        if self.ledger.aired_count <= 0:
            # Never aired yet: throttle on time since the last OFFER only. Do NOT
            # fall through to the min_segments branch below — non_release_segments
            # is reset only on a real airing, so for a never-aired beat it latches
            # >= min_segments forever (re-starving music), and min_segments may be
            # configured to 0. The first offer (last_attempt_at == 0) still fires
            # immediately, so the post-update cold open is not delayed.
            return (
                self.ledger.last_attempt_at <= 0
                or current - self.ledger.last_attempt_at >= self.manifest.min_seconds_between_airings
            )
        if self.ledger.aired_count >= self.manifest.max_airings:
            self._retire("budget_exhausted")
            return False
        if current - self.ledger.last_aired_at >= self.manifest.min_seconds_between_airings:
            return True
        return self.ledger.non_release_segments_since_last_airing >= self.manifest.min_segments_between_airings

    def begin_attempt(self, *, now: float | None = None) -> ReleaseBeatOffer | None:
        current = self._now(now)
        if not self.is_due(now=current):
            return None
        attempt_id = uuid4().hex
        self.ledger.status = QUEUED_ATTEMPT
        self.ledger.last_attempt_at = current
        if not self.ledger.first_attempt_at:
            self.ledger.first_attempt_at = current
        self.ledger.attempt_id = attempt_id
        self.ledger.queued_segment_id = ""
        self.ledger._dirty = True
        return ReleaseBeatOffer(
            beat_id=self.manifest.id,
            attempt_id=attempt_id,
            prompt_payload=self.manifest.to_prompt_payload(variation_index=self.ledger.aired_count),
        )

    def mark_generation_result(
        self,
        *,
        attempt_id: str,
        release_beat_used: bool,
        queue_id: str = "",
    ) -> None:
        if self.ledger.attempt_id != attempt_id:
            return
        if not release_beat_used:
            self._activate()
            return
        self.ledger.status = AIRED_ATTEMPT
        self.ledger.queued_segment_id = queue_id
        self.ledger._dirty = True

    def abandon_attempt(self, *, attempt_id: str) -> None:
        """Restore an offered/generated beat that never reached the queue."""
        if self.ledger.attempt_id != attempt_id:
            return
        self._activate()

    def abandon_in_flight(self) -> None:
        """Re-activate an offer begun but never queued (commit-free safety net).

        Covers producer paths that raise after ``begin_attempt`` but before the
        banter is queued and where no commit object survives to run the precise
        ``abandon_attempt`` — e.g. a sibling transition task failing inside the
        transition+banter ``asyncio.gather`` (the tuple unpack never happens, so
        the banter commit is lost). Touches ONLY ``QUEUED_ATTEMPT``, so a beat
        already queued (``AIRED_ATTEMPT``) is never clobbered and still airs.
        """
        if self.ledger.status == QUEUED_ATTEMPT:
            self._activate()

    def record_queue_discard(self, metadata: Mapping[str, Any] | None) -> bool:
        """Restore a release beat queued segment that was discarded before air."""
        if not self.enabled or self.ledger.status not in {QUEUED_ATTEMPT, AIRED_ATTEMPT}:
            return False
        meta = metadata or {}
        if _as_str(meta.get("release_beat_id")) != self.manifest.id:
            return False
        attempt_id = _as_str(meta.get("release_beat_attempt_id"))
        queue_id = _as_str(meta.get("queue_id"))
        if attempt_id and self.ledger.attempt_id and attempt_id != self.ledger.attempt_id:
            return False
        if queue_id and self.ledger.queued_segment_id and queue_id != self.ledger.queued_segment_id:
            return False
        if not attempt_id and not queue_id:
            return False
        self._activate()
        return True

    def record_stream_result(
        self,
        metadata: Mapping[str, Any] | None,
        *,
        bytes_sent: int,
        was_skipped: bool,
        listeners: int,
        now: float | None = None,
    ) -> bool:
        """Record stream evidence. Returns True when this counted an airing."""
        if not self.enabled:
            return False
        current = self._now(now)
        self._maybe_expire(current)
        meta = metadata or {}
        beat_id = _as_str(meta.get("release_beat_id"))
        attempt_id = _as_str(meta.get("release_beat_attempt_id"))
        if beat_id != self.manifest.id:
            # Only track non-release spacing while a campaign is still actively
            # trying (is_due()'s min_segments check is unreachable once status
            # != ACTIVE). Without this guard, a RETIRED campaign keeps dirtying
            # and synchronously saving the ledger on every delivered segment for
            # the rest of the session — pure disk churn on the audio hot path.
            if self.ledger.status == ACTIVE and _delivered(
                bytes_sent=bytes_sent, was_skipped=was_skipped, listeners=listeners
            ):
                self.ledger.non_release_segments_since_last_airing += 1
                self.ledger._dirty = True
                self._maybe_expire(current)
            return False
        if self.ledger.status not in {QUEUED_ATTEMPT, AIRED_ATTEMPT}:
            # No attempt pending (already cleared or never began) — a duplicate
            # or replayed stream-result event for this beat must not reactivate
            # or re-count an airing that already landed.
            logger.debug("Ignoring release beat stream result with no pending attempt")
            return False
        if attempt_id and self.ledger.attempt_id and attempt_id != self.ledger.attempt_id:
            logger.debug("Ignoring release beat stream result for stale attempt %s", attempt_id)
            return False
        if not _delivered(bytes_sent=bytes_sent, was_skipped=was_skipped, listeners=listeners):
            self._activate()
            return False
        self.ledger.aired_count += 1
        if not self.ledger.first_aired_at:
            self.ledger.first_aired_at = current
        self.ledger.last_aired_at = current
        self.ledger.non_release_segments_since_last_airing = 0
        self.ledger.attempt_id = ""
        self.ledger.queued_segment_id = ""
        self.ledger.status = ACTIVE
        self.ledger._dirty = True
        self._maybe_expire(current)
        return True

    def save_if_dirty(self) -> None:
        self.ledger.save_if_dirty(self.cache_dir)


def _manifest_from_toml_bytes(raw: bytes) -> ReleaseBeatManifest:
    payload = tomllib.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        return ReleaseBeatManifest.disabled()
    table = payload.get("release_beat", payload)
    if not isinstance(table, dict):
        return ReleaseBeatManifest.disabled()
    try:
        return ReleaseBeatManifest.from_dict(table)
    except (TypeError, ValueError):
        logger.warning("Release beat manifest has invalid fields; disabling beat")
        return ReleaseBeatManifest.disabled()


def load_release_beat_manifest(path: Path | None = None) -> ReleaseBeatManifest:
    """Load the packaged release beat, or disabled when absent/invalid."""
    try:
        if path is not None:
            return _manifest_from_toml_bytes(Path(path).read_bytes())
        resource = resources.files("mammamiradio").joinpath(*RELEASE_BEAT_RESOURCE)
        return _manifest_from_toml_bytes(resource.read_bytes())
    except FileNotFoundError:
        return ReleaseBeatManifest.disabled()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning("Release beat manifest is unreadable; disabling beat: %s", exc)
        return ReleaseBeatManifest.disabled()
