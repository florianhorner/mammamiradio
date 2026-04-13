"""Listener persona persistence — the mythology the hosts build about you."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Session gap threshold — 10 minutes of no listeners = new session
SESSION_GAP_SECONDS = 600

# Max chars per persona field entry
MAX_FIELD_ENTRY_LEN = 200

# Persona size threshold for compression
PERSONA_SIZE_LIMIT = 2048

# ── Arc phase machine ──────────────────────────────────────────────
# Relationship phases computed from session_count. Never stored — always derived.
_ARC_THRESHOLDS: list[tuple[int, str]] = [
    (26, "old_friend"),
    (11, "friend"),
    (4, "acquaintance"),
]

_ARC_DIRECTIVES: dict[str, str] = {
    "stranger": (
        "You don't know this listener yet. Be curious. Observe. Seed theories. "
        "Don't pretend familiarity you haven't earned."
    ),
    "acquaintance": (
        "You're getting to know this listener. Reference past sessions casually. Test which jokes land. Build rapport."
    ),
    "friend": ('This is a regular. Deep callbacks, inside jokes, comfortable silence. "Remember when..." is natural.'),
    "old_friend": (
        "This is family. You've been through things together. Legendary callbacks. "
        "The station wouldn't be the same without them."
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


def compute_arc_phase(session_count: int) -> str:
    """Derive the relationship phase from session count."""
    for threshold, phase in _ARC_THRESHOLDS:
        if session_count >= threshold:
            return phase
    return "stranger"


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
            parts.append(f"Theories about the listener: {', '.join(self.theories[-5:])}")
        if self.running_jokes:
            parts.append(f"Running jokes: {', '.join(self.running_jokes[-self.joke_budget :])}")
        if self.callbacks and self.callback_budget > 0:
            recent = self.callbacks[-self.callback_budget :]
            cb_strs = [f"{c.get('song', '?')} ({c.get('context', '')})" for c in recent]
            parts.append(f"Past songs to reference: {', '.join(cb_strs)}")
        if self.personality_guesses:
            parts.append(f"Personality guesses: {', '.join(self.personality_guesses[-3:])}")
        parts.append(f"Sessions so far: {self.session_count}")
        return "\n".join(parts) if parts else "First-time listener. No history yet."

    def json_size(self) -> int:
        """Approximate byte size of the persona as JSON."""
        return len(json.dumps(self.__dict__, default=str))


class PersonaStore:
    """Async SQLite-backed persona storage."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._last_listener_at: float = 0.0
        self._session_id: str = ""

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
            new_jokes = _sanitize_list(updates.get("new_jokes", []))
            callbacks_used = updates.get("callbacks_used", [])

            if new_theories:
                persona.theories = (persona.theories + new_theories)[-10:]
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
                    "Persona updated: +%d theories, +%d jokes",
                    len(new_theories),
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
        """Record a track play in history with optional skip/duration data."""
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

    def maybe_new_session(self) -> bool:
        """Check if enough time has passed to consider this a new session.

        Returns True if session_count should be incremented.
        """
        now = time.time()
        if self._last_listener_at == 0.0 or (now - self._last_listener_at) > SESSION_GAP_SECONDS:
            self._last_listener_at = now
            self._session_id = f"s{int(now)}"
            return True
        self._last_listener_at = now
        return False

    async def increment_session(self) -> None:
        """Bump session_count and detect milestones."""
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                await db.execute(
                    "UPDATE listener_persona SET session_count = session_count + 1, "
                    "last_session = datetime('now') WHERE id = 1"
                )
                await db.commit()

            persona = await self.get_persona()
            phase = persona.arc_phase
            milestone = persona.pending_milestone
            logger.info(
                "Listener session #%d started (phase: %s%s)",
                persona.session_count,
                phase,
                f", milestone #{milestone}" if milestone else "",
            )
        except Exception:
            logger.exception("Failed to increment session")

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
