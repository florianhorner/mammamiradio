"""Moment Receipts — the durable trail behind every ritual-recipe moment.

The ritual-recipes engine reacts to the home on air, but until now the reaction
evaporated the moment it played: a listener could not tell a home-triggered
line from generic banter, and an operator could not answer "why did the host
say that" (or "why did nothing happen"). This module records each moment from
match through confirmed air:

    ELECTED  — the match won its delivery lane (directive slot, interrupt fired,
               or a running-gag bucket was offered into banter)
    DROPPED  — the match cleared the recipe matcher but lost at the producer
               (directive slot busy, interrupt inside the global cooldown, or
               the generation that carried it fell back to stock copy)
    AIRING   — the carrying segment started streaming (provisional: send-start,
               not proof of delivery)
    final    — the true outcome from ``classify_stream_outcome`` verbatim
               (``aired`` / ``skipped`` / ``no_listeners`` / ``not_streamed`` /
               ``fallback_rescue``); only ``aired`` earns a listener-facing row.

Public/admin split: ``to_public_rows`` emits only the generic
``public_family_label`` plus a coarse age — no entity ids, no confidence, no
spoken lines — matching the exposure `/public-status` already accepts for
``ha_moments["ritual_families"]``. ``to_admin_rows`` carries the full trail and
stays behind admin auth.

Persistence mirrors ``EveningLedger``: a small bounded JSON file in
``cache_dir`` (atomic temp + rename, corrupt-tolerant load, dirty-gated save,
listed in the downloader's ``_CACHE_PROTECTED``). Writes happen only from the
producer's save site — the streamer just mutates in memory and marks the store
dirty, so the playback loop never does disk I/O. Every mutator is best-effort
and must never raise into the audio path.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("mammamiradio.moment_receipts")

STORE_FILENAME = "moments.json"
_SCHEMA_VERSION = 1

# Bounds: the store is a recency surface, not an archive. The row cap dominates
# in practice; retention keeps a quiet station's file from carrying stale weeks.
MAX_ROWS = 100
RETENTION_SECONDS = 7 * 24 * 60 * 60

# Row lifecycle states owned by this module. Final states come verbatim from
# core.segment_status.classify_stream_outcome and are not enumerated here.
STATUS_ELECTED = "elected"
STATUS_DROPPED = "dropped"
STATUS_AIRING = "airing"
_LIVE_STATUSES = {STATUS_ELECTED, STATUS_AIRING}


@dataclass
class MomentRow:
    """One ritual-recipe moment's trail from election to outcome."""

    id: str
    ts: float
    lane: str
    family: str
    public_label: str
    entity_id: str = ""
    confidence: float | None = None
    count: int = 0
    status: str = STATUS_ELECTED
    drop_reason: str = ""
    airing_ts: float = 0.0
    final_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "lane": self.lane,
            "family": self.family,
            "public_label": self.public_label,
            "entity_id": self.entity_id,
            "confidence": self.confidence,
            "count": self.count,
            "status": self.status,
            "drop_reason": self.drop_reason,
            "airing_ts": self.airing_ts,
            "final_ts": self.final_ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> MomentRow:
        confidence_raw = data.get("confidence")
        confidence_value = (
            float(confidence_raw)
            if isinstance(confidence_raw, int | float) and not isinstance(confidence_raw, bool)
            else None
        )
        return cls(
            id=str(data.get("id", "")),
            ts=float(data.get("ts", 0.0) or 0.0),
            lane=str(data.get("lane", "")),
            family=str(data.get("family", "")),
            public_label=str(data.get("public_label", "")),
            entity_id=str(data.get("entity_id", "")),
            confidence=confidence_value,
            count=int(data.get("count", 0) or 0),
            status=str(data.get("status", STATUS_ELECTED)),
            drop_reason=str(data.get("drop_reason", "")),
            airing_ts=float(data.get("airing_ts", 0.0) or 0.0),
            final_ts=float(data.get("final_ts", 0.0) or 0.0),
        )


@dataclass
class MomentStore:
    """Bounded, restart-durable record of ritual-recipe moments."""

    rows: list[MomentRow] = field(default_factory=list)
    _dirty: bool = field(default=False, repr=False)

    # --- record ---------------------------------------------------------------

    def record(
        self,
        *,
        lane: str,
        family: str,
        public_label: str,
        entity_id: str = "",
        confidence: float | None = None,
        count: int = 0,
        status: str = STATUS_ELECTED,
        drop_reason: str = "",
        now: float | None = None,
    ) -> str:
        """Append one moment row and return its opaque id.

        The id is what travels through ``Segment.metadata`` — deliberately
        meaningless so it can cross public payload boundaries without leaking
        anything about the home.
        """
        ref_now = time.time() if now is None else now
        row = MomentRow(
            id=uuid.uuid4().hex[:12],
            ts=ref_now,
            lane=lane,
            family=family,
            public_label=public_label,
            entity_id=entity_id,
            confidence=confidence,
            count=count,
            status=status,
            drop_reason=drop_reason,
        )
        self.rows.append(row)
        self._prune(ref_now)
        self._dirty = True
        return row.id

    # --- lifecycle ------------------------------------------------------------

    def _find(self, moment_id: str) -> MomentRow | None:
        if not moment_id:
            return None
        for row in reversed(self.rows):
            if row.id == moment_id:
                return row
        return None

    def mark_airing(self, moment_id: str, *, now: float | None = None) -> None:
        """Flip an elected row to airing (send-start; provisional, idempotent)."""
        row = self._find(moment_id)
        if row is None or row.status not in _LIVE_STATUSES:
            return
        row.status = STATUS_AIRING
        row.airing_ts = time.time() if now is None else now
        self._dirty = True

    def finalize(self, moment_id: str, status: str, *, now: float | None = None) -> None:
        """Record the true outcome (a ``classify_stream_outcome`` value).

        Unknown ids and already-final rows are silent no-ops — the streamer's
        finally block must never be able to corrupt the trail or raise.
        """
        row = self._find(moment_id)
        if row is None or row.status not in _LIVE_STATUSES or not status:
            return
        row.status = str(status)
        row.final_ts = time.time() if now is None else now
        self._dirty = True

    def mark_dropped(self, moment_id: str, reason: str, *, now: float | None = None) -> None:
        """Demote an elected row that never made it into a real segment."""
        row = self._find(moment_id)
        if row is None or row.status != STATUS_ELECTED:
            return
        row.status = STATUS_DROPPED
        row.drop_reason = reason
        row.final_ts = time.time() if now is None else now
        self._dirty = True

    # --- read -----------------------------------------------------------------

    def to_public_rows(
        self,
        *,
        now: float | None = None,
        active_ids: set[str] | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """Listener-safe rows: generic label + coarse age, nothing else.

        ``aired`` rows are always eligible; an ``airing`` row appears only while
        its id is in ``active_ids`` (i.e. the carrying segment is what
        ``now_streaming`` is playing right now) — a send that immediately
        failed or was skipped disappears instead of lingering as a false
        receipt.
        """
        ref_now = time.time() if now is None else now
        self._prune(ref_now)
        active = active_ids or set()
        out: list[dict] = []
        for row in reversed(self.rows):
            if row.status == "aired":
                shown_ts = row.airing_ts or row.final_ts or row.ts
            elif row.status == STATUS_AIRING and row.id in active:
                shown_ts = row.airing_ts or row.ts
            else:
                continue
            out.append(
                {
                    "label": row.public_label,
                    "ago_min": max(1, round(max(0.0, ref_now - shown_ts) / 60)),
                    "status": "airing" if row.status == STATUS_AIRING else "aired",
                }
            )
            if len(out) >= limit:
                break
        return out

    def to_admin_rows(self, *, now: float | None = None, limit: int = 25) -> list[dict]:
        """Full trail for the authenticated admin Moments panel."""
        ref_now = time.time() if now is None else now
        self._prune(ref_now)
        return [row.to_dict() for row in reversed(self.rows[-limit:])] if self.rows else []

    # --- bounds ---------------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - RETENTION_SECONDS
        kept = [row for row in self.rows if row.ts >= cutoff]
        if len(kept) > MAX_ROWS:
            kept = kept[-MAX_ROWS:]
        if len(kept) != len(self.rows):
            self.rows = kept
            self._dirty = True

    # --- persistence (mirrors EveningLedger) -----------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "rows": [row.to_dict() for row in self.rows],
        }

    @classmethod
    def load(cls, cache_dir: Path) -> MomentStore:
        """Corrupt-tolerant load: missing/malformed/wrong-shape → fresh store."""
        path = Path(cache_dir) / STORE_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Moment store is unreadable, starting fresh: %s", path)
            return cls()
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            logger.warning("Moment store has unexpected shape, starting fresh: %s", path)
            return cls()
        rows: list[MomentRow] = []
        for raw in payload["rows"]:
            if not isinstance(raw, dict):
                continue
            try:
                row = MomentRow.from_dict(raw)
            except (TypeError, ValueError):
                continue
            if row.id:
                rows.append(row)
        store = cls(rows=rows)
        # A restart severs every live row's path to air: the pending directive
        # and offered gag live only in memory, and an "airing" row's finalize
        # was lost with the playback loop. Demote them honestly instead of
        # letting the admin panel claim "waiting for its break" / "on air right
        # now" for up to a week about moments that can no longer happen.
        now = time.time()
        for row in store.rows:
            if row.status in _LIVE_STATUSES:
                row.status = STATUS_DROPPED
                row.drop_reason = "restart"
                row.final_ts = now
                store._dirty = True
        store._prune(now)
        # Demotions/prunes leave the store dirty on purpose — the producer's
        # save site persists them on its next cycle.
        return store

    def save_if_dirty(self, cache_dir: Path) -> None:
        """Persist atomically (temp + rename) only when state changed.

        Called from the producer's save site only — never from the playback
        loop, so a slow SD-card write can't put a gap in the stream.
        """
        if not self._dirty:
            return
        path = Path(cache_dir) / STORE_FILENAME
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
            self._dirty = False
        except Exception as exc:  # disk full / permissions / anything — the caller
            # is the producer loop, and this module's contract is that a receipt
            # bug never becomes an audio bug, so the net is wider than OSError.
            logger.warning("Could not persist moment store to %s: %s", path, exc)
