"""Listener persona persistence — the mythology the hosts build about you."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from mammamiradio.core.listener_session import LISTENER_SESSION_GAP_SECONDS

logger = logging.getLogger(__name__)

# Compatibility export for callers that imported the old threshold.  Session
# boundaries are now decided by the station-level listener state machine.
SESSION_GAP_SECONDS = LISTENER_SESSION_GAP_SECONDS

# Max chars per persona field entry
MAX_FIELD_ENTRY_LEN = 200

# Persona size threshold for compression
PERSONA_SIZE_LIMIT = 2048

# ── Arc phase machine ──────────────────────────────────────────────
# Relationship phases computed from session_count. Never stored — always derived.
_DEFAULT_ARC_THRESHOLDS = (4, 11, 26)
_ARC_THRESHOLDS: list[tuple[int, str]] = []

_ARC_DIRECTIVES: dict[str, str] = {
    "stranger": ("No individual identity is known. Keep the voice curious and playful without implying recognition."),
    "acquaintance": (
        "Use station memory and past music casually to build rapport, without implying a specific person returned."
    ),
    "friend": (
        "Use deeper station callbacks and comfortable inside jokes, while keeping "
        "companionship aggregate and identity-free."
    ),
    "old_friend": (
        "Use legendary station callbacks and a warm shared mythology; never claim to recognize an individual listener."
    ),
}

_MILESTONE_SESSIONS: frozenset[int] = frozenset({1, 5, 10, 25, 50, 100})

_ARC_BUDGETS: dict[str, tuple[int, int]] = {
    # phase -> (callback_budget, joke_budget)
    "stranger": (1, 1),
    "acquaintance": (3, 2),
    "friend": (4, 3),
    "old_friend": (5, 3),
}


def set_arc_thresholds(thresholds: list[int]) -> None:
    """Apply configurable session thresholds for arc phases."""
    global _ARC_THRESHOLDS

    try:
        parsed = [int(value) for value in thresholds]
    except (TypeError, ValueError):
        parsed = list(_DEFAULT_ARC_THRESHOLDS)

    if len(parsed) != 3 or parsed != sorted(parsed) or any(value < 1 for value in parsed):
        logger.warning("Invalid persona.arc_thresholds=%r; using defaults %s", thresholds, _DEFAULT_ARC_THRESHOLDS)
        parsed = list(_DEFAULT_ARC_THRESHOLDS)

    _ARC_THRESHOLDS = [
        (parsed[2], "old_friend"),
        (parsed[1], "friend"),
        (parsed[0], "acquaintance"),
    ]


def compute_arc_phase(session_count: int) -> str:
    """Derive the relationship phase from session count."""
    for threshold, phase in _ARC_THRESHOLDS:
        if session_count >= threshold:
            return phase
    return "stranger"


set_arc_thresholds(list(_DEFAULT_ARC_THRESHOLDS))


# Patterns that look like prompt injection attempts in persona entries
_INSTRUCTION_PATTERNS = (
    "ignore previous",
    "disregard",
    "system override",
    "forget your",
    "you must",
    "you should always",
    "you should never",
    "respond with",
    "instruction:",
    "system:",
)


def _sanitize(text: str) -> str:
    """Strip potentially harmful characters, instruction patterns, and cap length."""
    text = re.sub(r"[<>{}]", "", text)
    text_lower = text.lower()
    for pattern in _INSTRUCTION_PATTERNS:
        if pattern in text_lower:
            return "(filtered)"
    return text[:MAX_FIELD_ENTRY_LEN]


def _sanitize_list(items: list) -> list[str]:
    """Sanitize a list of persona entries."""
    if not isinstance(items, list):
        return []
    return [_sanitize(str(item)) for item in items if item]


@dataclass
class ListenerPersona:
    """In-memory representation of the listener's persona."""

    motifs: list[str] = field(default_factory=list)
    theories: list[str] = field(default_factory=list)
    running_jokes: list[str] = field(default_factory=list)
    callbacks: list[dict] = field(default_factory=list)
    personality_guesses: list[str] = field(default_factory=list)
    session_count: int = 0
    last_session: str = ""
    arc_metadata: dict = field(default_factory=dict)

    @property
    def arc_phase(self) -> str:
        """Current relationship phase, derived from session count."""
        return compute_arc_phase(self.session_count)

    @property
    def callback_budget(self) -> int:
        """How many callbacks the hosts should reference per break."""
        return _ARC_BUDGETS.get(self.arc_phase, (1, 1))[0]

    @property
    def joke_budget(self) -> int:
        """How many jokes the hosts should reference per break."""
        return _ARC_BUDGETS.get(self.arc_phase, (1, 1))[1]

    @property
    def pending_milestone(self) -> int | None:
        """Session number if this is a milestone that hasn't been consumed yet."""
        fired = set(self.arc_metadata.get("milestones_fired", []))
        if self.session_count in _MILESTONE_SESSIONS and self.session_count not in fired:
            return self.session_count
        return None

    def to_prompt_context(self) -> str:
        """Format persona for inclusion in a Claude prompt."""
        parts = []
        if self.motifs:
            parts.append(f"Music motifs: {', '.join(self.motifs[-5:])}")
        if self.theories:
            parts.append(f"Open station theories: {', '.join(self.theories[-5:])}")
        if self.running_jokes:
            parts.append(f"Running jokes: {', '.join(self.running_jokes[-self.joke_budget :])}")
        if self.callbacks and self.callback_budget > 0:
            recent = self.callbacks[-self.callback_budget :]
            cb_strs = [f"{c.get('song', '?')} ({c.get('context', '')})" for c in recent]
            parts.append(f"Past songs to reference: {', '.join(cb_strs)}")
        if self.personality_guesses:
            parts.append(f"Personality guesses: {', '.join(self.personality_guesses[-3:])}")
        parts.append(f"Station sessions so far: {self.session_count}")
        return "\n".join(parts) if parts else "No prior station-session history yet."

    def json_size(self) -> int:
        """Approximate byte size of the persona as JSON."""
        return len(json.dumps(self.__dict__, default=str))


class PersonaStore:
    """Async SQLite-backed persona storage."""

    def __init__(self, db_path: Path, *, process_token: str | None = None):
        self.db_path = db_path
        self._session_id: str = ""
        self._recorded_session_ids: set[str] = set()
        token = str(process_token or uuid.uuid4().hex).strip()
        if not token:
            raise ValueError("process_token must not be empty")
        self._process_token = token
        self._listener_receipts_prepared = False

    async def get_persona(self) -> ListenerPersona:
        """Read the current listener persona from SQLite."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM listener_persona WHERE id = 1")
            row = await cursor.fetchone()

            if not row:
                return ListenerPersona()

            # arc_metadata may not exist yet (pre-migration DB)
            try:
                arc_meta_raw = row["arc_metadata"] or "{}"
            except (IndexError, KeyError):
                arc_meta_raw = "{}"
            return ListenerPersona(
                motifs=json.loads(row["motifs"] or "[]"),
                theories=json.loads(row["theories"] or "[]"),
                running_jokes=json.loads(row["running_jokes"] or "[]"),
                callbacks=json.loads(row["callbacks"] or "[]"),
                personality_guesses=json.loads(row["personality_guesses"] or "[]"),
                session_count=row["session_count"] or 0,
                last_session=row["last_session"] or "",
                arc_metadata=json.loads(arc_meta_raw or "{}"),
            )

    async def update_persona(self, updates: dict) -> None:
        """Apply persona_updates from a Claude response. Validates and sanitizes."""
        if not isinstance(updates, dict):
            logger.error("Invalid persona_updates type: %s", type(updates))
            return

        try:
            persona = await self.get_persona()

            new_theories = _sanitize_list(updates.get("new_theories", []))
            new_guesses = _sanitize_list(updates.get("new_personality_guesses", []))
            new_jokes = _sanitize_list(updates.get("new_jokes", []))
            callbacks_used = updates.get("callbacks_used", [])

            if new_theories:
                persona.theories = (persona.theories + new_theories)[-10:]
            if new_guesses:
                persona.personality_guesses = (persona.personality_guesses + new_guesses)[-5:]
            if new_jokes:
                persona.running_jokes = (persona.running_jokes + new_jokes)[-5:]
            if isinstance(callbacks_used, list):
                for cb in callbacks_used:
                    if isinstance(cb, str):
                        persona.callbacks.append(
                            {
                                "song": _sanitize(cb),
                                "context": "",
                                "date": time.strftime("%Y-%m-%d"),
                                "session": persona.session_count,
                            }
                        )
                    elif isinstance(cb, dict) and cb.get("song"):
                        persona.callbacks.append(
                            {
                                "song": _sanitize(str(cb["song"])),
                                "context": _sanitize(str(cb.get("context", ""))),
                                "date": time.strftime("%Y-%m-%d"),
                                "session": persona.session_count,
                            }
                        )
                persona.callbacks = persona.callbacks[-20:]

            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    """INSERT INTO listener_persona (id, motifs, theories, running_jokes,
                       callbacks, personality_guesses, session_count, updated_at)
                       VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now'))
                       ON CONFLICT(id) DO UPDATE SET
                       theories = excluded.theories,
                       running_jokes = excluded.running_jokes,
                       callbacks = excluded.callbacks,
                       personality_guesses = excluded.personality_guesses,
                       updated_at = excluded.updated_at""",
                    (
                        json.dumps(persona.motifs),
                        json.dumps(persona.theories),
                        json.dumps(persona.running_jokes),
                        json.dumps(persona.callbacks),
                        json.dumps(persona.personality_guesses),
                        persona.session_count,
                    ),
                )
                await db.commit()
                logger.info(
                    "Persona updated: +%d theories, +%d guesses, +%d jokes",
                    len(new_theories),
                    len(new_guesses),
                    len(new_jokes),
                )

        except Exception:
            logger.exception("Failed to update persona")

    async def record_motif(self, artist: str, title: str) -> None:
        """Append a played track to the motif history, keeping the last 20."""
        motif = _sanitize(f"{artist} – {title}")
        try:
            persona = await self.get_persona()
            persona.motifs = [*persona.motifs, motif][-20:]
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    "UPDATE listener_persona SET motifs = ?, updated_at = datetime('now') WHERE id = 1",
                    (json.dumps(persona.motifs),),
                )
                await db.commit()
        except Exception:
            logger.warning("Failed to record motif", exc_info=True)

    async def record_play(
        self,
        track_youtube_id: str,
        session_id: str,
        host_script: str | None = None,
        *,
        skipped: bool = False,
        listen_duration_s: float | None = None,
    ) -> None:
        """Record a completed or skipped track play with optional duration data."""
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute("SELECT id FROM tracks WHERE youtube_id = ?", (track_youtube_id,))
                row = await cursor.fetchone()
                track_id = row[0] if row else None

                await db.execute(
                    "INSERT INTO play_history (track_id, session_id, host_script, skipped, listen_duration_s) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (track_id, session_id, host_script, int(skipped), listen_duration_s),
                )
                await db.commit()
        except Exception:
            logger.exception("Failed to record play")

    async def get_recent_plays(self, n: int = 10) -> list[dict]:
        """Get recent play history for dedup and host context."""
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """SELECT ph.played_at, t.title, t.artist, t.youtube_id
                       FROM play_history ph
                       LEFT JOIN tracks t ON ph.track_id = t.id
                       ORDER BY ph.id DESC LIMIT ?""",
                    (n,),
                )
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
        except Exception:
            logger.exception("Failed to get recent plays")
            return []

    async def prepare_listener_session_process(self) -> bool:
        """Verify the durable receipt ledger is ready for this process.

        Receipts are intentionally append-only.  An older process may retry
        after a newer process starts, so deleting its committed receipt would
        turn that retry into a second persona increment.
        """

        if self._listener_receipts_prepared:
            return True
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute("SELECT 1 FROM listener_session_receipts LIMIT 1")
                await cursor.fetchone()
        except Exception:
            logger.exception("Failed to prepare listener-session receipts")
            return False
        self._listener_receipts_prepared = True
        return True

    async def start_session(self, session_id: str) -> bool:
        """Durably increment one logical station epoch once per process.

        The process-unique anonymous token prevents an epoch number reused after
        restart from colliding with a previous run.  The receipt and persona
        increment commit in one transaction; the in-memory acknowledgement is
        deliberately last so a post-commit interruption can retry safely.
        """
        normalized_id = str(session_id or "").strip()
        if not normalized_id:
            return False
        if normalized_id in self._recorded_session_ids:
            return True
        if not await self.prepare_listener_session_process():
            return False

        receipt_id = f"{self._process_token}:{normalized_id}"

        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("INSERT OR IGNORE INTO listener_persona (id) VALUES (1)")
                await db.execute(
                    "INSERT OR IGNORE INTO listener_session_receipts "
                    "(receipt_id, process_token, logical_session_id) VALUES (?, ?, ?)",
                    (receipt_id, self._process_token, normalized_id),
                )
                cursor = await db.execute("SELECT changes()")
                changes = await cursor.fetchone()
                inserted = bool(changes and int(changes[0]) == 1)
                if inserted:
                    await db.execute(
                        "UPDATE listener_persona SET session_count = session_count + 1, "
                        "last_session = datetime('now'), updated_at = datetime('now') WHERE id = 1"
                    )
                await db.commit()
        except Exception:
            logger.exception("Failed to commit listener session %s", normalized_id)
            return False

        try:
            self._acknowledge_session_receipt(normalized_id)
        except Exception:
            logger.exception("Listener session committed but acknowledgement was interrupted (%s)", normalized_id)
            return False
        logger.info("Listener session committed (%s)", normalized_id)
        return True

    def _acknowledge_session_receipt(self, normalized_id: str) -> None:
        """Acknowledge only after the receipt transaction is durable."""

        self._recorded_session_ids.add(normalized_id)
        self._session_id = normalized_id

    async def _increment_session_row(self) -> bool:
        """Increment the legacy row for explicit/manual callers."""
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute("INSERT OR IGNORE INTO listener_persona (id) VALUES (1)")
                await db.execute(
                    "UPDATE listener_persona SET session_count = session_count + 1, "
                    "last_session = datetime('now'), updated_at = datetime('now') WHERE id = 1"
                )
                await db.commit()
            return True
        except Exception:
            logger.exception("Failed to increment session")
            return False

    async def increment_session(self) -> None:
        """Bump session_count for an explicit/manual compatibility caller."""
        await self._increment_session_row()

    async def consume_milestone(self) -> None:
        """Mark the current milestone as fired so it won't repeat."""
        try:
            persona = await self.get_persona()
            milestone = persona.pending_milestone
            if milestone is None:
                return
            fired = set(persona.arc_metadata.get("milestones_fired", []))
            fired.add(milestone)
            persona.arc_metadata["milestones_fired"] = sorted(fired)
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    "UPDATE listener_persona SET arc_metadata = ? WHERE id = 1",
                    (json.dumps(persona.arc_metadata),),
                )
                await db.commit()
        except Exception:
            logger.exception("Failed to consume milestone")
