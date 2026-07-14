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
# These are the Normal Mode counterparts to ``_TRANSITION_REWRITE_MAP``.  Keep
# the Italian map under its historical name: a few callers and hot-reload tests
# inspect that symbol directly.  The mode-aware helper below is the only path
# new code should use when selecting a rewrite.
_TRANSITION_REWRITE_MAP_NORMAL: dict[str, list[str]] = {
    "banter": [
        "Hold on, amici — there is one more thing to settle.",
        "Wait a second, amici — this deserves one more thought.",
        "Stay with me, amici — the real point is coming now.",
        "Good catch, amici — we have one more detail to unpack.",
    ],
    "ad": [
        "Hold on, amici — a quick word from our sponsors.",
        "One second, amici — the sponsors are stepping in.",
        "Before we carry on, amici — here comes the ad break.",
        "Stay with us, amici — a brief sponsor message is next.",
    ],
    "news_flash": [
        "One second, amici — a fresh bulletin just reached the desk.",
        "Hold that thought, amici — something just landed in the newsroom.",
        "Wait, amici — we may have an update worth hearing.",
        "Stand by, amici — the desk has a new bulletin.",
    ],
}
_BORING_TRANSITION_STEMS = {"che pezzo", "eh non", "bellissima", "allora", "e adesso"}
_TRANSITION_STOCK_COPY: dict[bool, dict[str, str]] = {
    False: {
        "banter": "Stay with us, amici — we have one more thing to settle.",
        "ad": "Stay close, amici — a quick word from our sponsors.",
        "news_flash": "Hold that thought, amici — a bulletin just reached the desk.",
    },
    True: {
        "banter": "Restate con noi, amici — c'è ancora qualcosa da chiarire.",
        "ad": "Restate con noi, amici — un messaggio dai nostri sponsor.",
        "news_flash": "Attenzione, amici — è arrivato un aggiornamento in redazione.",
    },
}
_TERMINAL_CUTOFF_MARKERS = ("—", "–", "--", "-", "...", "…")
_TRAILING_DIALOGUE_CLOSERS = "\"'”’)]}»"


def _transition_text_usable(text: object) -> bool:
    """Return whether generated transition copy is safe to put on air.

    A transition is a handoff, not an interrupted conversation. Keep malformed,
    tiny, and visibly cut-off model output on the deterministic stock-copy path.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped.split()) < 3:
        return False
    # A model can wrap a cut-off thought in dialogue punctuation, sometimes with
    # whitespace between the closer and the unfinished marker.  Strip both as a
    # single trailing set so ``-\" )`` is rejected just like ``-\")``.
    spoken_end = stripped.rstrip(_TRAILING_DIALOGUE_CLOSERS + " \t\r\n")
    return not spoken_end.endswith(_TERMINAL_CUTOFF_MARKERS)


def _transition_stock_fallbacks(*, super_italian: bool) -> dict[str, str]:
    """Return a copy of the complete stock handoffs for the active spoken mode."""
    return dict(_TRANSITION_STOCK_COPY[super_italian])


def _transition_stock_copy(next_segment: str, *, super_italian: bool) -> str:
    """Select a complete deterministic handoff for a transition exit path."""
    fallbacks = _TRANSITION_STOCK_COPY[super_italian]
    return fallbacks.get(next_segment, fallbacks["banter"])


def _transition_stem(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    words = [w for w in cleaned.split() if w]
    return " ".join(words[:2])


def _massage_transition_text(
    text: str,
    next_segment: str,
    recent_texts: list[str],
    *,
    super_italian: bool = True,
) -> str:
    """Replace stale opener patterns when the LLM falls into a rut.

    ``super_italian`` defaults to ``True`` for compatibility with the original
    three-argument helper, which always used the Italian rewrite inventory.  The
    normal-mode caller must pass its active spoken mode explicitly; otherwise a
    perfectly good English-led transition can be replaced with Italian after
    the language guard has already accepted it.
    """
    stem = _transition_stem(text)
    recent_stems = [_transition_stem(item) for item in recent_texts if item]
    repeated = recent_stems.count(stem) >= 1 and stem in _BORING_TRANSITION_STEMS
    if not repeated:
        return text.strip()

    rewrite_map = _TRANSITION_REWRITE_MAP if super_italian else _TRANSITION_REWRITE_MAP_NORMAL
    for candidate in rewrite_map.get(next_segment, rewrite_map["banter"]):
        if _transition_stem(candidate) not in recent_stems:
            return candidate
    return rewrite_map.get(next_segment, rewrite_map["banter"])[0]
