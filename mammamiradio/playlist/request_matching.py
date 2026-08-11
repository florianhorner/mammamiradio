"""Deterministic parsing and relevance checks for listener song requests.

The matcher deliberately does not guess.  A YouTube result is useful only when
its metadata contains exact, normalized evidence for the requested artist/title.
This keeps search ranking from being mistaken for identity.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

RequestMode = Literal["artist", "artist_title", "title"]


_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(?:please\s+)?(?:can|could|would)\s+you\s+(?:please\s+)?(?:play|put\s+on)\s+",
        r"^\s*(?:please\s+)?(?:play|put\s+on)\s+",
        r"^\s*i(?:'d|\s+would)?\s+(?:like|want)\s+to\s+(?:hear|listen\s+to)\s+",
        r"^\s*(?:per\s+favore\s+)?(?:(?:mi|ci)\s+)?(?:puoi|potresti|potete|potreste)\s+"
        r"(?:per\s+favore\s+)?"
        r"(?:mettere|suonare|far(?:mi|ci)\s+sentire)\s+",
        r"^\s*(?:per\s+favore\s+)?(?:metti|mettete|suona|suonate|fammi\s+sentire|facci\s+sentire)\s+",
        r"^\s*(?:voglio|vorrei|vogliamo|vorremmo)\s+sentire\s+",
    )
)

_RADIO_ADDRESS_RE = re.compile(
    r"^\s*(?:(?:dear|hello|hey|hi)\s+radio|(?:cara|caro|ciao|ehi)\s+radio)\s*[,;:!.-]?\s*",
    re.IGNORECASE,
)

_GENERIC_TITLES = {
    "anything",
    "any song",
    "a song",
    "one song",
    "some music",
    "something",
    "something nice",
    "un brano",
    "una canzone",
    "una canzone qualsiasi",
    "della musica",
    "qualcosa",
    "qualcosa di bello",
}

_DEDICATION_TAIL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\s*[,;]\s*(?:for|per)\s+.+$",
        r"\s+(?:dedicated\s+to|dedicat[oa]\s+(?:a|al|alla))\s+.+$",
        r"\s+(?:for\s+(?:my|our)\s+(?:mother|mom|mum|father|dad|parents?|family|wife|husband|daughter|son))\s*$",
        r"\s+(?:per\s+(?:mia|mio|nostra|nostro)\s+"
        r"(?:madre|mamma|padre|papa|pap\u00e0|famiglia|moglie|marito|figlia|figlio))\s*$",
        r"\s+(?:per\s+(?:mamma|papa|pap\u00e0))\s*$",
    )
)

_CREDIT_SEPARATOR_RE = re.compile(r"\s[-\u2013\u2014|]\s", re.UNICODE)
_COLLABORATOR_RE = re.compile(r"\s+(?:feat(?:uring)?\.?|ft\.?|x|and|e)\s+|\s*&\s*|\s*,\s*", re.IGNORECASE)
_BRACKET_RE = re.compile(r"\s*[\[(]([^\])]+)[\])]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

_NOISE_WORDS = {
    "4k",
    "8k",
    "audio",
    "hd",
    "hq",
    "lyric",
    "lyrics",
    "music",
    "official",
    "sd",
    "video",
    "visualizer",
    "with",
}
_FORBIDDEN_QUALIFIERS = {
    "cover": ("cover",),
    "karaoke": ("karaoke", "instrumental"),
    "tribute": ("tribute", "tributo"),
    "reaction": ("reaction", "reacts", "reazione"),
    "sped_up": ("sped up", "speed up"),
    "slowed": ("slowed", "slowed down"),
    "nightcore": ("nightcore",),
}
_SEMANTIC_QUALIFIERS = {
    "live": ("live", "dal vivo"),
    "acoustic": ("acoustic", "acustico", "acustica", "unplugged"),
    "remix": ("remix", "remixed"),
    "remaster": ("remaster", "remastered", "rimasterizzato", "rimasterizzata"),
}
_ALL_QUALIFIERS = {**_FORBIDDEN_QUALIFIERS, **_SEMANTIC_QUALIFIERS}
_VARIANT_PREFIX_RE = re.compile(
    r"^(?:a\s+)?(?:live|dal\s+vivo|acoustic|acustic[ao]|unplugged|karaoke|instrumental|cover|"
    r"tribute|tributo|reaction|reazione|remix(?:ed)?|remaster(?:ed)?|rimasterizzat[ao]|"
    r"sped\s+up|speed\s+up|slowed(?:\s+down)?|nightcore)"
    r"\s+(?:version\s+|versione\s+)?(?:of|di)\s+",
    re.IGNORECASE,
)
_VARIANT_SUFFIX_RE = re.compile(
    r"\s*[-\u2013\u2014|]\s*"
    r"(?:live(?:\b.*)?|dal\s+vivo(?:\b.*)?|acoustic(?:\s+version)?|acustic[ao](?:\s+versione)?|"
    r"unplugged|(?:[^-\u2013\u2014|()]*\s+)?remix|(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|"
    r"(?:\d{4}\s+)?rimasterizzat[ao](?:\s+\d{4})?|"
    r"karaoke|instrumental|cover|tribute|tributo|reaction|reazione|sped\s+up|speed\s+up|"
    r"slowed(?:\s+down)?|nightcore)\s*$",
    re.IGNORECASE,
)
_LONGFORM_IDENTITY_WRAPPER_RE = re.compile(
    r"^(?:dj\s+set|full\s+(?:concert|show|album)|complete\s+(?:album|set)|"
    r"album\s+completo|concerto\s+completo|mix\s+completo)$",
    re.IGNORECASE,
)
_BY_TITLE_TAILS = frozenset({"her", "him", "his", "it", "me", "my", "our", "them", "their", "us", "you", "your"})
_FEATURE_CREDIT_RE = re.compile(r"^(?:feat(?:uring)?\.?|ft\.?)\s+.+$", re.IGNORECASE)
_FEATURE_SUFFIX_RE = re.compile(
    r"\s+(?:[-\u2013\u2014|]\s*)?(?:feat(?:uring)?\.?|ft\.?)\s+.+$",
    re.IGNORECASE,
)
_INTRA_WORD_PUNCTUATION_RE = re.compile(r"(?<=\w)['\u2019\u02bc./\-]+(?=\w)", re.UNICODE)
_QUOTE_PAIRS = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}


def normalize_match_text(value: object) -> str:
    """Normalize identity text without fuzzy or transliteration guesses."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    joined = _INTRA_WORD_PUNCTUATION_RE.sub("", unaccented)
    return _SPACE_RE.sub(" ", re.sub(r"[^\w]+", " ", joined.casefold(), flags=re.UNICODE)).strip()


def _contains_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = normalize_match_text(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in f" {normalized_text} "


def _qualifiers(value: object) -> frozenset[str]:
    normalized = normalize_match_text(value)
    found = {
        name
        for name, phrases in _ALL_QUALIFIERS.items()
        if any(_contains_phrase(normalized, phrase) for phrase in phrases)
    }
    return frozenset(found)


def _recording_qualifiers(value: object) -> frozenset[str]:
    """Find variant labels only where a title convention marks them as labels."""
    title = clean_candidate_title(value)
    found: set[str] = set()
    for bracketed in _BRACKET_RE.finditer(title):
        found.update(_qualifiers(bracketed.group(1)))
    prefix = _VARIANT_PREFIX_RE.match(title)
    if prefix:
        found.update(_qualifiers(prefix.group(0)))
    suffix = _VARIANT_SUFFIX_RE.search(title)
    if suffix and suffix.start() > 0:
        found.update(_qualifiers(suffix.group(0)))
    return frozenset(found)


def _strip_dedication_tail(value: str) -> str:
    cleaned = value.strip()
    for pattern in _DEDICATION_TAIL_PATTERNS:
        cleaned = pattern.sub("", cleaned).strip()
    # A trailing named recipient is the common unpunctuated request form
    # (``play Albachiara for Anna`` / ``metti Albachiara per Anna``). Quoted
    # titles remain an escape hatch for real song names that contain ``for`` or
    # ``per`` as part of their identity.
    if not (
        len(cleaned) >= 2
        and cleaned[0] in {'"', "'", "\u201c", "\u2018"}
        and cleaned[-1] in {'"', "'", "\u201d", "\u2019"}
    ):
        recipient_match = re.search(r"\s+(?:for|per)\s+(?P<recipient>[^,;]+)$", cleaned, flags=re.IGNORECASE)
        if recipient_match:
            recipient = recipient_match.group("recipient").strip()
            title_prefix = cleaned[: recipient_match.start()].strip()
            recipient_words = recipient.split()
            looks_like_named_recipient = (
                len(title_prefix.split()) == 1
                and 1 <= len(recipient_words) <= 3
                and all(word[:1].isupper() for word in recipient_words)
            )
            if looks_like_named_recipient and not re.search(
                r"\s+(?:by|di|da)\s+",
                recipient,
                flags=re.IGNORECASE,
            ):
                cleaned = title_prefix
    return cleaned.strip(" ,;-\u2013\u2014")


def _strip_artist_dedication_tail(value: str) -> str:
    # Once an explicit ``by``/``di``/``da`` credit has been separated, a final
    # for/per clause cannot be part of the song title and is safe to remove.
    return re.sub(r"\s+(?:for|per)\s+.+$", "", value, flags=re.IGNORECASE).strip()


def _strip_command(message: str) -> str | None:
    message = _RADIO_ADDRESS_RE.sub("", message, count=1)
    for pattern in _COMMAND_PATTERNS:
        match = pattern.match(message)
        if match:
            return message[match.end() :].strip()
    return None


def _split_quoted_identity(body: str) -> tuple[str, str] | None:
    """Return an explicitly quoted title and optional outside artist credit."""
    if not body or body[0] not in _QUOTE_PAIRS:
        return None
    closer = _QUOTE_PAIRS[body[0]]
    close_index = body.rfind(closer)
    if close_index <= 0:
        return None
    title = body[1:close_index].strip()
    remainder = body[close_index + 1 :].strip()
    if not title:
        return None
    if not remainder:
        return title, ""
    if re.fullmatch(r"(?:for|per)\s+.+", remainder, flags=re.IGNORECASE):
        return title, ""
    credit = re.fullmatch(r"(?:by|di|da)\s+(.+)", remainder, flags=re.IGNORECASE)
    if credit:
        return title, credit.group(1).strip()
    return None


def _looks_like_by_title_tail(value: str) -> bool:
    words = normalize_match_text(value).split()
    return bool(words) and words[0] in _BY_TITLE_TAILS


def _is_generic_title(value: str) -> bool:
    normalized = normalize_match_text(value)
    return normalized in _GENERIC_TITLES


@dataclass(frozen=True)
class SongRequestIdentity:
    """One metadata-verifiable interpretation of a listener request."""

    mode: RequestMode
    artist: str = ""
    title: str = ""
    requested_qualifiers: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class SongRequestIntent:
    """Structured identity extracted from a listener's natural-language cue."""

    mode: RequestMode
    artist: str = ""
    title: str = ""
    requested_qualifiers: frozenset[str] = field(default_factory=frozenset)
    original: str = ""
    alternative_identity: SongRequestIdentity | None = None

    @property
    def primary_identity(self) -> SongRequestIdentity:
        return SongRequestIdentity(
            mode=self.mode,
            artist=self.artist,
            title=self.title,
            requested_qualifiers=self.requested_qualifiers,
        )

    @property
    def search_query(self) -> str:
        identity = " ".join(part for part in (self.artist, self.title) if part).strip()
        return f"{identity} official audio".strip()


_REQUEST_IDENTITY_EDGE_CHARS = " \"'.,;:!?-\u2013\u2014"


def _request_identity(mode: RequestMode, *, artist: str = "", title: str = "") -> SongRequestIdentity:
    clean_artist = artist.strip(_REQUEST_IDENTITY_EDGE_CHARS)
    clean_title = title.strip(_REQUEST_IDENTITY_EDGE_CHARS)
    if mode == "artist":
        clean_title = ""
    return SongRequestIdentity(
        mode=mode,
        artist=clean_artist,
        title=clean_title,
        requested_qualifiers=_recording_qualifiers(clean_title),
    )


def _clean_request_artist_credit(value: str) -> str:
    cleaned = _strip_artist_dedication_tail(_strip_dedication_tail(value))
    return cleaned.strip(_REQUEST_IDENTITY_EDGE_CHARS)


def parse_song_request(message: str) -> SongRequestIntent | None:
    """Parse supported English/Italian song commands into exact search intent.

    ``None`` means the text is not a supported song command.  It does not mean
    that a supported command has no matching catalogue result.
    """
    original = str(message or "").strip()
    body = _strip_command(original)
    if body is None:
        return None
    body = _strip_dedication_tail(body)
    if not body:
        return None

    primary = _request_identity("title", title=body)
    alternative: SongRequestIdentity | None = None

    quoted_identity = _split_quoted_identity(body)
    if quoted_identity is not None:
        quoted_title, quoted_artist = quoted_identity
        quoted_artist = _clean_request_artist_credit(quoted_artist)
        primary = _request_identity(
            "artist_title" if quoted_artist else "title",
            artist=quoted_artist,
            title=quoted_title,
        )
    else:
        # An unquoted final ``by`` has two honest interpretations. Keep the
        # established pronoun/possessive heuristic only to choose the primary;
        # metadata must validate either identity before it can match. Explicit
        # quotes above remain authoritative and never create an alternative.
        by_match = re.match(r"^(?P<title>.+)\s+by\s+(?P<artist>.+)$", body, flags=re.IGNORECASE)
        if by_match:
            possible_title = by_match.group("title").strip(_REQUEST_IDENTITY_EDGE_CHARS)
            possible_artist = _clean_request_artist_credit(by_match.group("artist"))
            if possible_title and possible_artist:
                credit_mode: RequestMode = "artist" if _is_generic_title(possible_title) else "artist_title"
                credit_identity = _request_identity(
                    credit_mode,
                    artist=possible_artist,
                    title=possible_title,
                )
                if credit_mode == "artist":
                    primary = credit_identity
                else:
                    full_title_identity = _request_identity(
                        "title",
                        title=f"{possible_title} by {possible_artist}",
                    )
                    if _looks_like_by_title_tail(possible_artist):
                        primary, alternative = full_title_identity, credit_identity
                    else:
                        primary, alternative = credit_identity, full_title_identity
        else:
            # ``di`` is equally ambiguous in Italian: it can introduce an
            # artist or be part of the title. The structural arity rule chooses
            # a deterministic primary, while the other interpretation remains
            # available for exact metadata verification. ``da`` remains the
            # explicit performer form and needs no alternative.
            italian_match = re.match(
                r"^(?P<title>.+)\s+(?P<credit>di|da)\s+(?P<artist>[^,;]+)$",
                body,
                re.IGNORECASE,
            )
            if italian_match:
                possible_title = italian_match.group("title").strip(_REQUEST_IDENTITY_EDGE_CHARS)
                possible_artist = _clean_request_artist_credit(italian_match.group("artist"))
                credit = italian_match.group("credit").casefold()
                generic_request = _is_generic_title(possible_title)
                title_word_count = len(possible_title.split())
                artist_word_count = len(possible_artist.split())
                same_arity_class = (title_word_count == 1) == (artist_word_count == 1)
                if possible_title and possible_artist:
                    credit_mode = "artist" if generic_request else "artist_title"
                    credit_identity = _request_identity(
                        credit_mode,
                        artist=possible_artist,
                        title=possible_title,
                    )
                    if generic_request or credit == "da":
                        primary = credit_identity
                    else:
                        full_title_identity = _request_identity(
                            "title",
                            title=f"{possible_title} di {possible_artist}",
                        )
                        if title_word_count > 0 and artist_word_count > 0 and same_arity_class:
                            primary, alternative = credit_identity, full_title_identity
                        else:
                            primary, alternative = full_title_identity, credit_identity

    if not primary.title and not primary.artist:
        return None
    return SongRequestIntent(
        mode=primary.mode,
        artist=primary.artist,
        title=primary.title,
        requested_qualifiers=primary.requested_qualifiers,
        original=original,
        alternative_identity=alternative,
    )


def _clean_artist(value: object) -> str:
    artist = _SPACE_RE.sub(" ", str(value or "")).strip()
    artist = re.sub(r"\s+-\s+Topic\s*$", "", artist, flags=re.IGNORECASE)
    artist = re.sub(r"\s*VEVO\s*$", "", artist, flags=re.IGNORECASE)
    artist = re.sub(r"\s+Official(?:\s+Channel)?\s*$", "", artist, flags=re.IGNORECASE)
    return artist.strip(" -\u2013\u2014")


def _canonical_matched_artist(requested: str, evidence: str) -> str:
    """Keep candidate spelling while restoring separators hidden by channel branding."""
    candidate = _clean_artist(evidence)
    requested_normalized = normalize_match_text(requested)
    candidate_normalized = normalize_match_text(candidate)
    if (
        candidate_normalized
        and " " not in candidate_normalized
        and " " in requested_normalized
        and candidate_normalized == requested_normalized.replace(" ", "")
    ):
        # ``LucioBattistiVEVO`` proves identity but is poor display/dedupe
        # spelling. Reuse only the request's word boundaries, removing accents
        # and punctuation so listener text cannot create a parallel key.
        requested_words = requested_normalized.split()
        if candidate.isalnum() and sum(len(word) for word in requested_words) == len(candidate):
            parts: list[str] = []
            offset = 0
            for word in requested_words:
                parts.append(candidate[offset : offset + len(word)])
                offset += len(word)
            return " ".join(parts)
        decomposed = unicodedata.normalize("NFKD", requested)
        unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
        return _SPACE_RE.sub(" ", re.sub(r"[^\w&]+", " ", unaccented, flags=re.UNICODE)).strip()
    return candidate


def _is_noise_group(contents: str) -> bool:
    words = set(normalize_match_text(contents).split())
    return bool(words) and words <= _NOISE_WORDS


def clean_candidate_title(value: object) -> str:
    """Remove video-platform packaging while retaining musical variants."""
    original_title = _SPACE_RE.sub(" ", str(value or "")).strip()
    title = original_title
    title = _BRACKET_RE.sub(lambda match: "" if _is_noise_group(match.group(1)) else match.group(0), title)
    title = re.sub(r"\bwith\s+lyrics?\b", "", title, flags=re.IGNORECASE)
    suffix = re.compile(
        # Platform packaging is a separate suffix, never the tail of an
        # identity word. Without this boundary, ``Claudio`` was shortened to
        # ``Cl`` merely because it ends in the letters ``audio``.
        r"(?:\s+|\s*[-\u2013\u2014|]\s*)"
        r"(?:official\s+(?:(?:music|lyric)\s+)?video|official\s+audio|lyric\s+video|lyrics?|visualizer|"
        r"music\s+video|audio|(?:4|8)k|hd|hq|sd)\s*$",
        re.IGNORECASE,
    )
    previous = None
    while title != previous:
        previous = title
        title = suffix.sub("", title).strip()
    # Quality labels are normally platform noise, but they can also be the
    # complete song identity. Remove an adjacent wrapper first so ``HD
    # (Official Audio)`` keeps ``HD``; quality labels elsewhere remain noise.
    if normalize_match_text(title) in {"4k", "8k", "hd", "hq", "sd"}:
        return _SPACE_RE.sub(" ", title).strip(" -\u2013\u2014|")
    title = re.sub(r"\b(?:4k|8k|hd|hq|sd)\b", "", title, flags=re.IGNORECASE)
    cleaned = _SPACE_RE.sub(" ", title).strip(" -\u2013\u2014|")
    # A packaging-looking word can also be the complete, legitimate song
    # identity (``Audio``, ``Lyrics``, ``Visualizer``, ``HD``, ``4K``). Exact
    # structured metadata is subject to this helper too, so an empty cleanup
    # must fall back to that trusted identity instead of making it unmatchable.
    return cleaned or original_title.strip(" -\u2013\u2014|")


def _split_title_credit(raw_title: str) -> tuple[str, str]:
    parts = _CREDIT_SEPARATOR_RE.split(raw_title, maxsplit=1)
    if len(parts) == 2:
        return _clean_artist(parts[0]), clean_candidate_title(parts[1])
    return "", clean_candidate_title(raw_title)


def _same_artist_identity(requested: str, candidate: str) -> bool:
    normalized_requested = normalize_match_text(requested)
    normalized_candidate = normalize_match_text(candidate)
    return bool(normalized_requested) and (
        normalized_candidate == normalized_requested
        or normalized_candidate.replace(" ", "") == normalized_requested.replace(" ", "")
    )


def _matched_artist_identity(requested_artist: str, evidence: object) -> str:
    """Return the verified candidate spelling for the matching artist segment.

    A display credit may contain collaborators (``Lady Gaga feat. Bradley
    Cooper``). The full credit remains useful display metadata, but policy
    checks need the exact collaborator segment that established relevance so a
    base-artist blocklist entry cannot be bypassed.
    """
    requested = normalize_match_text(requested_artist)
    candidate = _clean_artist(evidence)
    if not requested or not candidate:
        return ""
    if _same_artist_identity(requested_artist, candidate):
        return _canonical_matched_artist(requested_artist, candidate)
    for part in _COLLABORATOR_RE.split(candidate):
        if _same_artist_identity(requested_artist, part):
            return _canonical_matched_artist(requested_artist, part)
    return ""


def _credited_artist_identities(display_artist: str, identity_artist: str) -> tuple[str, ...]:
    """Return candidate-backed artist identities needed by policy checks.

    ``identity_artist`` is the exact collaborator segment that verified the
    listener request. When that segment came from a compound display credit,
    every sibling segment is equally real candidate metadata and must reach the
    blocklist gate: requesting Bradley Cooper must not bypass a prior
    Lady Gaga/Shallow ban on ``Lady Gaga feat. Bradley Cooper``.
    """
    display_artist = _clean_artist(display_artist)
    identity_artist = _clean_artist(identity_artist)
    candidates = [display_artist, identity_artist]
    if display_artist and identity_artist and not _same_artist_identity(display_artist, identity_artist):
        candidates.extend(_COLLABORATOR_RE.split(display_artist))

    identities: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _clean_artist(candidate)
        normalized = normalize_match_text(cleaned)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        identities.append(cleaned)
    return tuple(identities)


def _strip_title_variant(value: str) -> str:
    title = clean_candidate_title(value)
    # Bracketed musical variants are meaningful for display but not the base
    # song identity used by the exact matcher.
    title = _BRACKET_RE.sub(
        lambda match: (
            ""
            if (
                _qualifiers(match.group(1))
                or _FEATURE_CREDIT_RE.fullmatch(match.group(1).strip())
                or _LONGFORM_IDENTITY_WRAPPER_RE.fullmatch(match.group(1).strip())
            )
            else match.group(0)
        ),
        title,
    ).strip()
    title = _VARIANT_PREFIX_RE.sub("", title)
    title = _FEATURE_SUFFIX_RE.sub("", title)
    title = _VARIANT_SUFFIX_RE.sub("", title)
    title = re.sub(
        r"\s*(?:[-\u2013\u2014|]\s*|\s+)"
        r"(?:dj\s+set|full\s+(?:concert|show|album)|complete\s+(?:album|set)|"
        r"album\s+completo|concerto\s+completo|mix\s+completo)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return title.strip(" -\u2013\u2014|")


def _candidate_identity(metadata: dict[str, Any]) -> tuple[list[str], str, str]:
    display_title = str(metadata.get("title") or metadata.get("track_title") or "").strip()
    prefix_artist, title_from_display = _split_title_credit(display_title)
    structured_title = clean_candidate_title(metadata.get("track_title") or "")
    title = structured_title or title_from_display
    structured_artist = str(metadata.get("track_artist") or "")
    artist_evidence = [
        structured_artist,
        prefix_artist,
        str(metadata.get("artist") or ""),
        str(metadata.get("uploader") or ""),
        str(metadata.get("channel") or ""),
    ]
    artist_evidence = [value for value in artist_evidence if value]
    # Structured performer metadata is best.  In its absence, a conventional
    # ``Artist - Title`` credit is more trustworthy for display than the
    # uploader/channel identity retained in the legacy ``artist`` field.
    display_artist = _clean_artist(structured_artist) or prefix_artist
    if not display_artist:
        display_artist = next((_clean_artist(value) for value in artist_evidence if _clean_artist(value)), "")
    return artist_evidence, title, display_artist


@dataclass(frozen=True)
class SongCandidateMatch:
    """A relevant search candidate with safe canonical display metadata."""

    metadata: dict[str, Any]
    artist: str
    identity_artist: str
    credited_artists: tuple[str, ...]
    title: str
    identity_title: str
    variant: str
    rank: tuple[int, int, int]


@dataclass(frozen=True)
class SongMatchResult:
    """Ranked relevant candidates or an honest semantic failure."""

    matches: tuple[SongCandidateMatch, ...] = ()
    failure_reason: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.matches)

    @property
    def best(self) -> SongCandidateMatch | None:
        return self.matches[0] if self.matches else None


def _variant_rank(qualifiers: frozenset[str]) -> int:
    if not qualifiers:
        return 0
    if qualifiers <= {"remaster"}:
        return 1
    if qualifiers & _FORBIDDEN_QUALIFIERS.keys():
        return 3
    return 2


def _variant_label(qualifiers: frozenset[str]) -> str:
    for name in (
        "live",
        "acoustic",
        "remix",
        "remaster",
        "cover",
        "karaoke",
        "tribute",
        "reaction",
        "sped_up",
        "slowed",
        "nightcore",
    ):
        if name in qualifiers:
            return name
    return "standard"


def match_song_request_candidates(
    intent: SongRequestIntent,
    metadata_results: Sequence[dict[str, Any]],
) -> SongMatchResult:
    """Return only candidates with exact normalized identity evidence.

    Matches against the parser's primary interpretation rank before matches
    against its explicit ambiguity alternative. Within each interpretation,
    recording variant (standard, remaster, other variants) and then original
    search order break ties. Search ordering alone can therefore never make an
    unrelated video a match.
    """
    matches: list[SongCandidateMatch] = []
    request_identities = [intent.primary_identity]
    if intent.alternative_identity is not None:
        request_identities.append(intent.alternative_identity)
    for index, metadata in enumerate(metadata_results):
        artist_evidence, candidate_title, display_artist = _candidate_identity(metadata)
        # Recording variants belong to the recording title. Artist/channel
        # names such as "Live Nation" are identity evidence, never proof that a
        # standard upload is a live performance.
        _, display_title_evidence = _split_title_credit(str(metadata.get("title") or ""))
        candidate_qualifiers = frozenset().union(
            _recording_qualifiers(display_title_evidence),
            _recording_qualifiers(metadata.get("track_title") or ""),
        )
        for identity_rank, request_identity in enumerate(request_identities):
            requested_qualifiers = request_identity.requested_qualifiers
            if (candidate_qualifiers & _FORBIDDEN_QUALIFIERS.keys()) - requested_qualifiers:
                continue
            if requested_qualifiers and not requested_qualifiers <= candidate_qualifiers:
                continue
            matched_artist = ""
            identity_artist = ""
            if request_identity.artist:
                for value in artist_evidence:
                    identity_artist = _matched_artist_identity(request_identity.artist, value)
                    if identity_artist:
                        matched_artist = _canonical_matched_artist(request_identity.artist, value)
                        break
                if not matched_artist:
                    continue
            if request_identity.title:
                requested_base_title = normalize_match_text(_strip_title_variant(request_identity.title))
                candidate_base_title = normalize_match_text(_strip_title_variant(candidate_title))
                if not requested_base_title or candidate_base_title != requested_base_title:
                    continue

            # Never carry listener spelling into station identity. The matcher
            # may equate accents/punctuation/compact channel names; retaining
            # request text here would bypass canonical dedupe or blocklists.
            canonical_artist = matched_artist or display_artist
            identity_artist = identity_artist or canonical_artist
            credited_artists = _credited_artist_identities(canonical_artist, identity_artist)
            canonical_title = candidate_title
            if not canonical_title:
                continue
            identity_title = _strip_title_variant(candidate_title)
            if not identity_title:
                continue
            matches.append(
                SongCandidateMatch(
                    metadata=dict(metadata),
                    artist=canonical_artist,
                    identity_artist=identity_artist,
                    credited_artists=credited_artists,
                    title=canonical_title,
                    identity_title=identity_title,
                    variant=_variant_label(candidate_qualifiers),
                    rank=(identity_rank, _variant_rank(candidate_qualifiers), index),
                )
            )
            # One search result represents one recording. If it validates both
            # interpretations, retain only its higher-priority identity.
            break

    matches.sort(key=lambda match: match.rank)
    if not matches:
        return SongMatchResult(failure_reason="low_confidence")
    return SongMatchResult(matches=tuple(matches))
