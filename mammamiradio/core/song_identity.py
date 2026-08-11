"""Shared exact-equivalence normalization for song and artist identities."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection

_INTRA_WORD_PUNCTUATION_RE = re.compile(
    r"(?<=\w)['_\u2010\u2011\u2019\u02bc./\-]+(?=\w)",
    re.UNICODE,
)
_PLATFORM_ARTIST_SUFFIX_PATTERNS = (
    re.compile(r"\s+-\s+Topic\s*$", re.IGNORECASE),
    # Stored policy keys are already case-folded. A separated VEVO label is
    # explicit; for the glued channel convention require a two-character base
    # so ordinary names such as ``Avevo`` stay intact while ``U2VEVO`` works.
    re.compile(r"(?:\s+|(?<=\w{2}))VEVO\s*$", re.IGNORECASE),
    re.compile(r"\s+Official\s+Channel\s*$", re.IGNORECASE),
    re.compile(r"(?<=.{4})\s+Official\s*$", re.IGNORECASE),
)
_ARTIST_FEATURE_CREDIT_RE = re.compile(
    r"^(?P<base>.+?)\s+(?:feat(?:uring)?\.?|ft\.?)\s+(?P<guest>.+)$",
    re.IGNORECASE,
)
_BRACKETED_TITLE_FEATURE_CREDIT_RE = re.compile(
    r"[\[(]\s*(?:feat(?:uring)?\.?|ft\.?)\s+(?P<guest>[^\])]+?)[\])]",
    re.IGNORECASE,
)
_SEPARATED_TITLE_FEATURE_CREDIT_RE = re.compile(
    r"^(?P<base>.+?)\s+[-\u2013\u2014|]\s*(?:feat(?:uring)?\.?|ft\.?)\s+(?P<guest>.+)$",
    re.IGNORECASE,
)
_UNBRACKETED_TITLE_FEATURE_CREDIT_RE = re.compile(
    # A full stop makes these abbreviations recognisable credit syntax rather
    # than ordinary title words (``A Feat of Strength`` / ``Featuring
    # Tomorrow``). ``Ft.`` plus a single word remains ambiguous with place
    # names such as ``Ft. Lauderdale`` and therefore stays part of the title.
    r"^(?P<base>.+?)\s+(?P<marker>feat\.|ft\.)\s+(?P<guest>.+)$",
    re.IGNORECASE,
)
_AMBIGUOUS_FEATURE_GUEST_TAIL_RE = re.compile(r"(?:\s[-\u2013\u2014|]\s|[\[(][^\])]+[\])]\s*$)")
_SPACE_RE = re.compile(r"\s+")


def strip_platform_artist_wrapper(value: object) -> str:
    """Remove explicit video-platform channel suffixes from artist metadata."""
    artist = _SPACE_RE.sub(" ", str(value or "")).strip()
    previous = None
    while artist and artist != previous:
        previous = artist
        for pattern in _PLATFORM_ARTIST_SUFFIX_PATTERNS:
            candidate = pattern.sub("", artist).strip(" -\u2013\u2014")
            if candidate:
                artist = candidate
    return artist


def split_artist_feature_credit(value: object) -> tuple[str, str] | None:
    """Split an explicit feature credit in artist metadata into base and guest."""
    match = _ARTIST_FEATURE_CREDIT_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    base = match.group("base").strip()
    guest = match.group("guest").strip()
    return (base, guest) if base and guest else None


def split_title_feature_credit(value: object) -> tuple[str, str] | None:
    """Split a syntactically explicit title-side feature group or suffix.

    Exact identity accepts a bracketed feature group, terminal separator-led
    credit, or conservative punctuated abbreviation. Standalone feature words
    and compound separator tails remain part of the title because their
    boundaries are ambiguous.
    """
    title = str(value or "").strip()
    bracketed_matches = tuple(_BRACKETED_TITLE_FEATURE_CREDIT_RE.finditer(title))
    if bracketed_matches:
        match = bracketed_matches[-1]
        base = f"{title[: match.start()]} {title[match.end() :]}".strip(" -\u2013\u2014|")
        base = _SPACE_RE.sub(" ", base)
        guest = match.group("guest").strip()
        if base and guest:
            return base, guest

    separator_match = _SEPARATED_TITLE_FEATURE_CREDIT_RE.fullmatch(title)
    if separator_match is not None:
        base = separator_match.group("base").strip(" -\u2013\u2014|")
        guest = separator_match.group("guest").strip()
        if _AMBIGUOUS_FEATURE_GUEST_TAIL_RE.search(guest):
            return None
        if base and guest:
            return base, guest

    unbracketed_match = _UNBRACKETED_TITLE_FEATURE_CREDIT_RE.fullmatch(title)
    if unbracketed_match is not None:
        base = unbracketed_match.group("base").strip(" -\u2013\u2014|")
        marker = unbracketed_match.group("marker").casefold()
        guest = unbracketed_match.group("guest").strip()
        if _AMBIGUOUS_FEATURE_GUEST_TAIL_RE.search(guest):
            return None
        if marker == "ft." and len(guest.split()) < 2:
            return None
        if base and guest:
            return base, guest
    return None


def normalize_song_identity_text(value: object) -> str:
    """Normalize identity text without fuzzy or transliteration guesses."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    joined = _INTRA_WORD_PUNCTUATION_RE.sub("", unaccented)
    return _SPACE_RE.sub(" ", re.sub(r"[^\w]+", " ", joined.casefold(), flags=re.UNICODE)).strip()


def normalize_artist_identity_text(value: object) -> str:
    """Normalize an artist using compact aliases and primary feature-credit identity."""
    artist = strip_platform_artist_wrapper(value)
    feature_credit = split_artist_feature_credit(artist)
    primary_artist = strip_platform_artist_wrapper(feature_credit[0]) if feature_credit is not None else artist
    return normalize_song_identity_text(primary_artist).replace(" ", "")


def _normalize_title_identity_text(value: object) -> str:
    """Normalize a title and remove an explicit feature group or suffix."""
    feature_credit = split_title_feature_credit(value)
    title = feature_credit[0] if feature_credit is not None else value
    return normalize_song_identity_text(title)


def normalize_song_identity_key(key: tuple[str, str]) -> tuple[str, str]:
    """Return the shared exact-equivalence form of an ``(artist, title)`` key."""
    return normalize_artist_identity_text(key[0]), _normalize_title_identity_text(key[1])


def song_identity_keys_match(left: tuple[str, str], right: tuple[str, str]) -> bool:
    """Return whether two ``(artist, title)`` keys identify the same song."""
    return normalize_song_identity_key(left) == normalize_song_identity_key(right)


def song_identity_key_is_blocklisted(
    candidate: tuple[str, str],
    blocklist: Collection[tuple[str, str]],
) -> bool:
    """Return whether any supplied policy key is exactly equivalent to ``candidate``."""
    return any(song_identity_keys_match(candidate, blocked) for blocked in blocklist)
