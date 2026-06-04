"""Per-segment correlation context for the provenance ledger.

A producer sets a fresh :class:`CallCollector` before it fans out the LLM calls
for one segment (or one ad break). Each ``_generate_json_response`` running under
that context appends its ``llm_call_id`` (with role + spot_index) to the
collector. After the ``gather`` returns the producer reads the collected ids to
build the Tier-2 ``segment_prepared`` row's ``llm_call_refs``.

Why a ContextVar and not a return value: banter and ad breaks fan calls out with
``asyncio.gather`` (``producer.py:1462`` / ``2024``). ``gather`` runs each
coroutine in a child task that inherits a COPY of the context, but the copy still
references the SAME collector object, so concurrent appends from sibling calls all
land in the one collector — without threading an id back through four call
signatures.

Discipline (codex review): the producer is one long-running task, so it MUST
``token = set_collector(c)`` then ``finally: reset_collector(token)`` per segment,
or the next unrelated segment inherits a stale collector.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field


@dataclass
class CallCollector:
    """Collects the LLM calls made while producing one segment / ad break."""

    attempt_id: str
    ad_break_id: str | None = None
    # Each entry: {"llm_call_id", "role", "spot_index", "ok"}
    calls: list[dict] = field(default_factory=list)


_current: contextvars.ContextVar[CallCollector | None] = contextvars.ContextVar("provenance_collector", default=None)


def set_collector(collector: CallCollector) -> contextvars.Token:
    """Bind a collector to the current context; returns a token for reset()."""
    return _current.set(collector)


def reset_collector(token: contextvars.Token) -> None:
    """Restore the previous collector (call in a finally to avoid leaks)."""
    _current.reset(token)


def get_collector() -> CallCollector | None:
    """The collector for the current segment, or None outside any producer scope."""
    return _current.get()
