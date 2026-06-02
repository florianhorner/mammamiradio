"""Transition-line rewrite data and the stem/massage helpers.

Extracted verbatim from ``hosts/scriptwriter.py`` (god-module split). Holds the
canned transition openers and the de-duplication helpers that keep DJ transitions
from falling into a rut. Self-contained — no imports back into scriptwriter.
Reloaded ahead of the scriptwriter facade by /api/hot-reload so edits here take
effect without a stream gap.
"""

from __future__ import annotations

import re

_TRANSITION_REWRITE_MAP: dict[str, list[str]] = {
    "banter": [
        "Mamma mia... adesso si litiga davvero.",
        "Aspetta un secondo, perche qui c'e da dire una cosa.",
        "No, ma senti questa, perche adesso parte il casino vero.",
        "Madonna, fermati un attimo, perche qui c'e materiale.",
    ],
    "ad": [
        "Aspetta, ma prima ci tocca la pubblicita.",
        "Un secondo solo, che arrivano gli sponsor peggiori d'Italia.",
        "No, no, fermi tutti, prima passa la pubblicita.",
        "Prima di continuare, c'e una pausa che nessuno ha chiesto.",
    ],
    "news_flash": [
        "Un secondo, mi stanno urlando qualcosa in cuffia.",
        "Aspetta, aspetta, qui c'e aria di notizia improvvisa.",
        "No, ferma tutto, mi dicono che sta succedendo qualcosa.",
        "Un attimo, questa sembra una notizia vera. Purtroppo.",
    ],
}
_BORING_TRANSITION_STEMS = {"che pezzo", "eh non", "bellissima", "allora", "e adesso"}


def _transition_stem(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    words = [w for w in cleaned.split() if w]
    return " ".join(words[:2])


def _massage_transition_text(text: str, next_segment: str, recent_texts: list[str]) -> str:
    """Replace stale opener patterns when the LLM falls into a rut."""
    stem = _transition_stem(text)
    recent_stems = [_transition_stem(item) for item in recent_texts if item]
    repeated = recent_stems.count(stem) >= 1 and stem in _BORING_TRANSITION_STEMS
    if not repeated:
        return text.strip()

    for candidate in _TRANSITION_REWRITE_MAP.get(next_segment, _TRANSITION_REWRITE_MAP["banter"]):
        if _transition_stem(candidate) not in recent_stems:
            return candidate
    return _TRANSITION_REWRITE_MAP.get(next_segment, _TRANSITION_REWRITE_MAP["banter"])[0]
