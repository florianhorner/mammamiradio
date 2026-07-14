"""Deterministic language policy for spoken host and advertising copy.

Normal Mode is an English-led show even though the station itself is Italian.
The language policy intentionally lives outside the LLM prompt builder so every
speech surface can use the same small, deterministic guard.  The scorer is not
intended to be a general-purpose language detector: it counts a conservative
set of language-bearing words and leaves names, titles, and other unknown words
unclassified.  The acceptance ratio is therefore always calculated from the
classified English and Italian words only.

Super Italian Mode remains a separate personality switch.  Callers can pass
``super_italian=True`` to :func:`normal_mode_language_ok` to preserve the
existing all-Italian behavior without making the scorer responsible for
validating Italian text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Product contract for Normal Mode.  The target is deliberately separate from
# the acceptance band: generated copy can vary around the target without
# causing needless retries, while still preventing Italian-heavy output.
NORMAL_MODE_ENGLISH_TARGET = 0.75
NORMAL_MODE_ENGLISH_MIN = 0.70
NORMAL_MODE_ENGLISH_MAX = 0.85

# A short transition cannot reliably hit an exact 75/25 word ratio.  It must,
# however, contain English and may not silently bypass the guard when it is
# entirely Italian.
NORMAL_MODE_SHORT_COPY_LIMIT = 8
NORMAL_MODE_SHORT_COPY_MIN_ENGLISH = 0.50

LANGUAGE_TOKEN_RE = re.compile(r"[a-zA-ZÀ-ÖØ-öø-ÿ']+")

# These marker banks are intentionally plain data.  They are shared with the
# scriptwriter facade while it is migrated to the central policy module.
_NORMAL_MODE_ENGLISH_MARKERS = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "all",
        "and",
        "anyway",
        "are",
        "back",
        "be",
        "because",
        "been",
        "before",
        "but",
        "by",
        "can",
        "can't",
        "could",
        "did",
        "do",
        "does",
        "don't",
        "english",
        "exactly",
        "feel",
        "for",
        "from",
        "had",
        "has",
        "have",
        "hear",
        "here",
        "home",
        "i'm",
        "in",
        "is",
        "it",
        "it's",
        "just",
        "keep",
        "let",
        "let's",
        "listeners",
        "listening",
        "little",
        "more",
        "music",
        "next",
        "no",
        "not",
        "now",
        "of",
        "on",
        "or",
        "our",
        "out",
        "radio",
        "right",
        "room",
        "say",
        "show",
        "song",
        "sponsors",
        "stay",
        "still",
        "that",
        "that's",
        "the",
        "then",
        "there",
        "this",
        "through",
        "to",
        "tonight",
        "track",
        "up",
        "very",
        "was",
        "we",
        "we're",
        "what",
        "welcome",
        "with",
        "word",
        "you",
        "your",
    }
)

_NORMAL_MODE_ITALIAN_MARKERS = frozenset(
    {
        "abbiamo",
        "adesso",
        "al",
        "alla",
        "allora",
        "amici",
        "anche",
        "ancora",
        "ascolta",
        "ascoltatori",
        "avete",
        "bene",
        "benissimo",
        "benvenuti",
        "benvenuto",
        "brano",
        "calma",
        "canzone",
        "casa",
        "che",
        "ciao",
        "ci",
        "come",
        "con",
        "continua",
        "cosa",
        "dai",
        "da",
        "del",
        "della",
        "di",
        "dovete",
        "e",
        "era",
        "finisce",
        "fretta",
        "fuori",
        "grazie",
        "gli",
        "il",
        "italiano",
        "la",
        "le",
        "lo",
        "ma",
        "mamma",
        "mia",
        "messaggio",
        "musica",
        "nel",
        "nella",
        "nessuna",
        "non",
        "notte",
        "oggi",
        "ora",
        "parliamo",
        "parlare",
        "per",
        "pezzo",
        "più",
        "piano",
        "poi",
        "prossima",
        "prossimo",
        "pubblicita",
        "pubblicità",
        "qui",
        "ricorda",
        "ricordate",
        "respira",
        "restiamo",
        "sentiamo",
        "sentite",
        "senza",
        "si",
        "sì",
        "siamo",
        "sono",
        "stasera",
        "studio",
        "subito",
        "tornando",
        "torniamo",
        "tutti",
        "tutta",
        "un",
        "una",
        "va",
        "voci",
        "vuoi",
    }
)

# These short words are too ambiguous to count as either language in a
# mixed-language sentence (``in`` and ``no`` are especially common in Italian
# copy, while ``i`` is both an English pronoun and an Italian article).
_NORMAL_MODE_AMBIGUOUS_ENGLISH_MARKERS = frozenset({"a", "in", "no"})
_NORMAL_MODE_AMBIGUOUS_MARKERS = frozenset({"i", "in", "no", "a"})


@dataclass(frozen=True, slots=True)
class LanguageAssessment:
    """Counts and ratios for one or more spoken text fields."""

    total_tokens: int
    english_tokens: int
    italian_tokens: int
    unclassified_tokens: int

    @property
    def classified_tokens(self) -> int:
        """Number of words for which the marker bank identified a language."""

        return self.english_tokens + self.italian_tokens

    @property
    def english_share(self) -> float:
        """English share among classified words, or ``0`` when none were found."""

        if not self.classified_tokens:
            return 0.0
        return self.english_tokens / self.classified_tokens

    @property
    def italian_share(self) -> float:
        """Italian share among classified words, or ``0`` when none were found."""

        if not self.classified_tokens:
            return 0.0
        return self.italian_tokens / self.classified_tokens

    @property
    def is_short(self) -> bool:
        """Whether this is short enough for the relaxed transition rule."""

        return self.classified_tokens < NORMAL_MODE_SHORT_COPY_LIMIT

    @property
    def is_empty(self) -> bool:
        """Whether no lexical tokens were present."""

        return self.total_tokens == 0


def _coerce_texts(texts: str | Iterable[str]) -> str:
    if isinstance(texts, str):
        return texts
    return " ".join(text.strip() for text in texts if isinstance(text, str) and text.strip())


def assess_language(texts: str | Iterable[str]) -> LanguageAssessment:
    """Count known English and Italian words in ``texts``.

    Unknown words are retained in ``total_tokens`` but excluded from the
    language ratio.  This keeps brand names, song titles, and invented station
    vocabulary from skewing the target while leaving the caller an explicit
    ``unclassified_tokens`` count for observability.
    """

    tokens = [token.casefold() for token in LANGUAGE_TOKEN_RE.findall(_coerce_texts(texts))]
    english_tokens = 0
    italian_tokens = 0
    for token in tokens:
        if token in _NORMAL_MODE_AMBIGUOUS_MARKERS:
            continue
        if token in _NORMAL_MODE_ITALIAN_MARKERS or any(char in token for char in "àèéìòù"):
            italian_tokens += 1
        elif token in _NORMAL_MODE_ENGLISH_MARKERS and token not in _NORMAL_MODE_AMBIGUOUS_ENGLISH_MARKERS:
            english_tokens += 1

    classified_tokens = english_tokens + italian_tokens
    return LanguageAssessment(
        total_tokens=len(tokens),
        english_tokens=english_tokens,
        italian_tokens=italian_tokens,
        unclassified_tokens=len(tokens) - classified_tokens,
    )


def normal_mode_language_ok(
    texts: str | Iterable[str],
    *,
    super_italian: bool = False,
) -> bool:
    """Return whether spoken text satisfies the Normal Mode language contract.

    Super Italian deliberately bypasses this Normal Mode check.  Empty text is
    accepted for compatibility with schema-level validation elsewhere.  Any
    non-empty copy with no classified language words fails closed, and short
    copy must contain at least one English word; this removes the old short-copy
    all-Italian bypass.  Long copy targets a 70-85% English band around the 75%
    product target.
    """

    if super_italian:
        return True

    assessment = assess_language(texts)
    if assessment.is_empty:
        return True
    if not assessment.classified_tokens:
        return False
    if assessment.is_short:
        return assessment.english_tokens > 0 and assessment.english_share >= NORMAL_MODE_SHORT_COPY_MIN_ENGLISH
    return NORMAL_MODE_ENGLISH_MIN <= assessment.english_share <= NORMAL_MODE_ENGLISH_MAX


__all__ = [
    "LANGUAGE_TOKEN_RE",
    "NORMAL_MODE_ENGLISH_MAX",
    "NORMAL_MODE_ENGLISH_MIN",
    "NORMAL_MODE_ENGLISH_TARGET",
    "NORMAL_MODE_SHORT_COPY_LIMIT",
    "NORMAL_MODE_SHORT_COPY_MIN_ENGLISH",
    "_NORMAL_MODE_AMBIGUOUS_ENGLISH_MARKERS",
    "_NORMAL_MODE_ENGLISH_MARKERS",
    "_NORMAL_MODE_ITALIAN_MARKERS",
    "LanguageAssessment",
    "assess_language",
    "normal_mode_language_ok",
]
