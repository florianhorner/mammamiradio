"""Shared exact-equivalence normalization for song and artist identities."""

from __future__ import annotations

import re
import unicodedata

_INTRA_WORD_PUNCTUATION_RE = re.compile(r"(?<=\w)['\u2019\u02bc./\-]+(?=\w)", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_song_identity_text(value: object) -> str:
    """Normalize identity text without fuzzy or transliteration guesses."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    joined = _INTRA_WORD_PUNCTUATION_RE.sub("", unaccented)
    return _SPACE_RE.sub(" ", re.sub(r"[^\w]+", " ", joined.casefold(), flags=re.UNICODE)).strip()


def normalize_artist_identity_text(value: object) -> str:
    """Normalize an artist using the compact alias equivalence accepted by matching."""
    return normalize_song_identity_text(value).replace(" ", "")
