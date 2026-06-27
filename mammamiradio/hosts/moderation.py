"""Pure helpers for deterministic listener-request moderation."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


def _fold_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(without_marks.casefold().split())


@lru_cache(maxsize=256)
def _entry_pattern(entry: str) -> re.Pattern[str] | None:
    folded = _fold_text(entry.strip())
    if not folded:
        return None
    escaped = re.escape(folded).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)")


def is_blocked(text: str, blocked_names: list[str]) -> bool:
    """Return whether ``text`` contains a configured blocked name or phrase."""
    if not text or not blocked_names:
        return False

    folded_text = _fold_text(text)
    if not folded_text:
        return False

    for entry in blocked_names:
        if not isinstance(entry, str):
            continue
        pattern = _entry_pattern(entry)
        if pattern and pattern.search(folded_text):
            return True
    return False
