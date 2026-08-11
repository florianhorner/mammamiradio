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

from mammamiradio.core.song_identity import (
    normalize_artist_identity_text,
    split_artist_feature_credit,
    split_title_feature_credit,
    strip_platform_artist_wrapper,
)
from mammamiradio.core.song_identity import normalize_song_identity_text as normalize_match_text

RequestMode = Literal["artist", "artist_title", "title"]


_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*(?:please\s*[,;:]?\s+)?(?:can|could|would)\s+you\s+"
        r"(?:please\s*[,;:]?\s+)?(?:play|put\s+on)\s+",
        r"^\s*(?:please\s*[,;:]?\s+)?(?:play|put\s+on)\s+",
        r"^\s*i(?:'d|\s+would)?\s+(?:like|want)\s+to\s+(?:hear|listen\s+to)\s+",
        r"^\s*(?:per\s+favore\s*[,;:]?\s+)?(?:(?:mi|ci)\s+)?(?:puoi|potresti|potete|potreste)\s+"
        r"(?:per\s+favore\s*[,;:]?\s+)?"
        r"(?:mettere|suonare|far(?:mi|ci)\s+sentire)\s+",
        r"^\s*(?:per\s+favore\s*[,;:]?\s+)?"
        r"(?:metti|mettete|suona|suonate|fammi\s+sentire|facci\s+sentire)\s+",
        r"^\s*(?:voglio|vorrei|vogliamo|vorremmo)\s+sentire\s+",
    )
)

_RADIO_ADDRESS_RE = re.compile(
    r"^\s*(?:(?:dear|hello|hey|hi|cara|caro|ciao|ehi)\s+radio|radio)\s*[,;:!.-]?\s*",
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
    "radio_edit": ("radio edit",),
}
_ALL_QUALIFIERS = {**_FORBIDDEN_QUALIFIERS, **_SEMANTIC_QUALIFIERS}
_VARIANT_PREFIX_RE = re.compile(
    r"^(?:a\s+)?(?:live|dal\s+vivo|acoustic|acustic[ao]|unplugged|karaoke|instrumental|cover|"
    r"tribute|tributo|reaction|reazione|remix(?:ed)?|remaster(?:ed)?|rimasterizzat[ao]|"
    r"radio\s+edit|sped\s+up|speed\s+up|slowed(?:\s+down)?|nightcore)"
    r"\s+(?:version\s+|versione\s+)?(?:of|di)\s+",
    re.IGNORECASE,
)
_LIVE_VARIANT_LABEL_PATTERN = r"live(?:\s+(?:version(?:\s+\d{4})?|performance|session|\d{4}|(?:at|from|in|on)\s+.+))?"
_ITALIAN_LIVE_VARIANT_LABEL_PATTERN = r"dal\s+vivo(?:\s+(?:versione(?:\s+\d{4})?|\d{4}|(?:a|da|in)\s+.+))?"
_VARIANT_SUFFIX_RE = re.compile(
    r"\s*[-\u2013\u2014|]\s*"
    rf"(?:{_LIVE_VARIANT_LABEL_PATTERN}|{_ITALIAN_LIVE_VARIANT_LABEL_PATTERN}|"
    r"acoustic(?:\s+version)?|acustic[ao](?:\s+versione)?|"
    r"unplugged|(?:[^-\u2013\u2014|()]*\s+)?remix|(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|"
    r"(?:\d{4}\s+)?rimasterizzat[ao](?:\s+\d{4})?|"
    r"radio\s+edit|karaoke|instrumental|cover|tribute|tributo|reaction|reazione|sped\s+up|speed\s+up|"
    r"slowed(?:\s+down)?|nightcore)\s*$",
    re.IGNORECASE,
)
_LONGFORM_IDENTITY_WRAPPER_RE = re.compile(
    r"^(?:dj\s+set|full\s+(?:concert|show|album)|complete\s+(?:album|set)|"
    r"album\s+completo|concerto\s+completo|mix\s+completo)$",
    re.IGNORECASE,
)
_BRACKETED_VARIANT_GROUP_RE = re.compile(
    rf"^(?:{_LIVE_VARIANT_LABEL_PATTERN}|{_ITALIAN_LIVE_VARIANT_LABEL_PATTERN}|"
    r"acoustic(?:\s+version)?|acustic[ao](?:\s+versione)?|unplugged|"
    r"(?:[^()]+\s+)?remix|remixed|"
    r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?|"
    r"(?:\d{4}\s+)?rimasterizzat[ao](?:\s+\d{4})?|"
    r"radio\s+edit|karaoke|instrumental|cover(?:\s+version)?|"
    r"tribute|tributo|reaction|reazione|sped\s+up|speed\s+up|"
    r"slowed(?:\s+down)?|nightcore"
    r")$",
    re.IGNORECASE,
)
_REQUEST_FEATURE_CREDIT_RE = re.compile(
    r"^(?P<base>.+?)\s+(?P<marker>feat(?:uring)?\.?|ft\.?)\s+(?P<guest>.+)$",
    re.IGNORECASE,
)
_REQUEST_IDENTITY_EDGE_CHARS = " \"'.,;:!?-\u2013\u2014"
_BY_TITLE_TAILS = frozenset({"her", "him", "his", "it", "me", "my", "our", "them", "their", "us", "you", "your"})
_QUOTE_PAIRS = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
_QUALITY_IDENTITY_TOKENS = frozenset({"4k", "8k", "hd", "hq", "sd"})
_QUALITY_TOKEN_RE = re.compile(r"\b(?:4k|8k|hd|hq|sd)\b", re.IGNORECASE)
_PLATFORM_PACKAGING_SUFFIX_RE = re.compile(
    # Bare trailing words remain possible song identity (``Digital Audio``).
    # Packaging is removed only when its wording or separator corroborates it.
    r"(?:"
    r"\s+(?:official\s+(?:(?:music|lyric)\s+)?video|official\s+audio|lyric\s+video|music\s+video)"
    r"|\s*[-\u2013\u2014|]\s*(?:official\s+(?:(?:music|lyric)\s+)?video|official\s+audio|"
    r"lyric\s+video|lyrics?|visualizer|music\s+video|audio|(?:4|8)k|hd|hq|sd)"
    r")\s*$",
    re.IGNORECASE,
)
_GENERIC_ARTIST_LABELS = frozenset({"various artists", "various artist", "multiple artists"})


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


def _bracketed_qualifiers(value: object) -> frozenset[str]:
    """Return qualifiers only when the complete bracket group is a label."""
    contents = _SPACE_RE.sub(" ", str(value or "")).strip()
    if not contents or _BRACKETED_VARIANT_GROUP_RE.fullmatch(contents) is None:
        return frozenset()
    return _qualifiers(contents)


def _recording_qualifiers(value: object) -> frozenset[str]:
    """Find variant labels only where a title convention marks them as labels."""
    title = clean_candidate_title(value)
    found: set[str] = set()
    for bracketed in _BRACKET_RE.finditer(title):
        found.update(_bracketed_qualifiers(bracketed.group(1)))
    prefix = _VARIANT_PREFIX_RE.match(title)
    if prefix:
        found.update(_qualifiers(prefix.group(0)))
    suffix = _VARIANT_SUFFIX_RE.search(title)
    if suffix and suffix.start() > 0:
        found.update(_qualifiers(suffix.group(0)))
    return frozenset(found)


def _split_trailing_recording_qualifiers(value: str) -> tuple[str, str]:
    """Peel terminal variant labels while retaining their original syntax."""
    title = _SPACE_RE.sub(" ", value).strip()
    trailers: list[str] = []
    while title:
        bracket = re.search(r"\s*[\[(]([^\])]+)[\])]\s*$", title)
        if bracket is not None and _bracketed_qualifiers(bracket.group(1)):
            trailers.insert(0, bracket.group(0).strip())
            title = title[: bracket.start()].strip()
            continue
        suffix = _VARIANT_SUFFIX_RE.search(title)
        if suffix is not None and suffix.start() > 0:
            trailers.insert(0, suffix.group(0).strip())
            title = title[: suffix.start()].strip()
            continue
        break
    return title, " ".join(trailers)


def _split_unbracketed_feature_credit(value: str) -> tuple[str, str, str] | None:
    """Parse one ambiguous feature phrase while preserving its marker spelling."""
    without_trailer, trailer = _split_trailing_recording_qualifiers(value)
    match = _REQUEST_FEATURE_CREDIT_RE.fullmatch(without_trailer)
    if match is None:
        return None
    base = match.group("base").strip(_REQUEST_IDENTITY_EDGE_CHARS)
    guest = match.group("guest").strip(_REQUEST_IDENTITY_EDGE_CHARS)
    if not base or not guest or re.search(r"\s+(?:by|di|da|for|per)\s+", guest, flags=re.IGNORECASE):
        return None
    if trailer:
        base = f"{base} {trailer}"
    return base, guest, match.group("marker")


def _split_composed_title_feature_credit(value: object) -> tuple[str, str] | None:
    """Split an explicit feature credit even when a variant follows it."""
    title = clean_candidate_title(value)
    direct = split_title_feature_credit(title)
    if direct is not None:
        return direct

    without_trailer, trailer = _split_trailing_recording_qualifiers(title)
    if not trailer:
        return None
    feature_credit = split_title_feature_credit(without_trailer)
    if feature_credit is None:
        return None
    base, guest = feature_credit
    return f"{base} {trailer}".strip(), guest


def _split_contextual_candidate_feature_credit(value: object) -> tuple[str, str] | None:
    """Return a lower-case, title-side credit that needs request corroboration.

    Unpunctuated ``feat``/``ft`` suffixes are common platform metadata, but the
    same words occur in real titles.  They become a credit interpretation only
    after matching code proves the named guest; title-cased phrases such as
    ``A Feat of Strength`` and ``Welcome to Ft. Lauderdale`` remain literal.
    """
    parsed = _split_unbracketed_feature_credit(clean_candidate_title(value))
    if parsed is None:
        return None
    base, guest, marker = parsed
    if marker != marker.casefold():
        return None
    return base, guest


def _strip_dedication_tail(value: str) -> str:
    cleaned = value.strip()
    for pattern in _DEDICATION_TAIL_PATTERNS:
        cleaned = pattern.sub("", cleaned).strip()
    return cleaned.strip(" ,;-\u2013\u2014")


def _ambiguous_dedication_title(value: str) -> str:
    """Return the possible title before an unpunctuated recipient.

    The full phrase remains the primary title because ``Waiting for You`` and
    ``Love for Sale`` are valid identities. Candidate metadata may validate this
    stripped alternative without letting the parser guess it into first place.
    """
    cleaned = value.strip()
    if not (
        len(cleaned) >= 2
        and cleaned[0] in {'"', "'", "\u201c", "\u2018"}
        and cleaned[-1] in {'"', "'", "\u201d", "\u2019"}
    ):
        recipient_match = re.search(r"\s+(?:for|per)\s+(?P<recipient>[^,;]+)$", cleaned, flags=re.IGNORECASE)
        if recipient_match:
            recipient = recipient_match.group("recipient").strip()
            title_prefix = cleaned[: recipient_match.start()].strip()
            if normalize_match_text(recipient) in {"me", "us", "noi"}:
                return title_prefix
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
                return title_prefix
    return ""


def _ambiguous_politeness_title(value: str) -> str:
    """Return a possible title before a conversational trailing courtesy."""
    stripped = re.sub(
        r"(?:\s*,\s*|\s+)(?:please|per\s+favore)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip(_REQUEST_IDENTITY_EDGE_CHARS)
    return stripped if stripped and stripped != value else ""


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
    remainder = body[close_index + 1 :].strip().rstrip(".,;:!?").strip()
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
    requested_feature_artist: str = ""
    preserve_title_feature_syntax: bool = False


@dataclass(frozen=True)
class SongRequestIntent:
    """Primary request identity plus ranked, metadata-verifiable ambiguities."""

    primary_identity: SongRequestIdentity
    original: str = ""
    alternative_identities: tuple[SongRequestIdentity, ...] = ()

    @property
    def mode(self) -> RequestMode:
        return self.primary_identity.mode

    @property
    def artist(self) -> str:
        return self.primary_identity.artist

    @property
    def title(self) -> str:
        return self.primary_identity.title

    @property
    def requested_qualifiers(self) -> frozenset[str]:
        return self.primary_identity.requested_qualifiers

    @property
    def requested_feature_artist(self) -> str:
        return self.primary_identity.requested_feature_artist

    @property
    def preserve_title_feature_syntax(self) -> bool:
        return self.primary_identity.preserve_title_feature_syntax

    @property
    def search_query(self) -> str:
        primary = self.primary_identity
        identity_parts = [part for part in (primary.artist, primary.title) if part]
        if primary.requested_feature_artist:
            identity_parts.extend(("feat.", primary.requested_feature_artist))
        query_identity = " ".join(identity_parts).strip()
        return f"{query_identity} official audio".strip()


def _split_ambiguous_request_feature_credit(value: str) -> tuple[str, str] | None:
    """Return a possible unpunctuated title-side guest credit.

    The full phrase remains a higher-ranked identity because words such as
    ``featuring`` and ``feat`` can be literal title text. This alternative is
    useful only when candidate metadata independently proves the guest.
    """
    if _split_composed_title_feature_credit(value) is not None:
        return None
    parsed = _split_unbracketed_feature_credit(value)
    return parsed[:2] if parsed is not None else None


def _request_identity(
    mode: RequestMode,
    *,
    artist: str = "",
    title: str = "",
    parse_title_feature: bool = True,
) -> SongRequestIdentity:
    clean_artist = artist.strip(_REQUEST_IDENTITY_EDGE_CHARS)
    clean_title = title.strip(_REQUEST_IDENTITY_EDGE_CHARS)
    requested_feature_artist = ""
    preserve_title_feature_syntax = not parse_title_feature
    artist_feature = split_artist_feature_credit(clean_artist)
    if artist_feature is not None:
        clean_artist, requested_feature_artist = artist_feature
    if mode == "artist":
        clean_title = ""
    elif parse_title_feature:
        feature_credit = _split_composed_title_feature_credit(clean_title)
        if feature_credit is not None:
            feature_title, title_guest = feature_credit
            if not requested_feature_artist or normalize_artist_identity_text(
                requested_feature_artist
            ) == normalize_artist_identity_text(title_guest):
                clean_title = feature_title
                requested_feature_artist = title_guest
            else:
                # Both credits are explicit and may name distinct collaborators.
                # Keep the title-side credit literal while the artist-side guest
                # remains a separate evidence requirement; stripping either one
                # would allow a different credited recording to verify.
                preserve_title_feature_syntax = True
    return SongRequestIdentity(
        mode=mode,
        artist=clean_artist,
        title=clean_title,
        requested_qualifiers=_recording_qualifiers(clean_title),
        requested_feature_artist=requested_feature_artist,
        preserve_title_feature_syntax=preserve_title_feature_syntax,
    )


def _derived_request_identity(
    identity: SongRequestIdentity,
    *,
    mode: RequestMode | None = None,
    artist: str | None = None,
    title: str | None = None,
    requested_feature_artist: str = "",
    preserve_title_feature_syntax: bool | None = None,
) -> SongRequestIdentity | None:
    """Create a narrower interpretation while retaining existing constraints."""
    derived = _request_identity(
        mode or identity.mode,
        artist=identity.artist if artist is None else artist,
        title=identity.title if title is None else title,
        parse_title_feature=not (
            identity.preserve_title_feature_syntax
            if preserve_title_feature_syntax is None
            else preserve_title_feature_syntax
        ),
    )
    guests = [
        guest
        for guest in (
            identity.requested_feature_artist,
            derived.requested_feature_artist,
            requested_feature_artist,
        )
        if guest
    ]
    if guests:
        canonical_guests = {normalize_artist_identity_text(guest) for guest in guests}
        if len(canonical_guests) != 1:
            return None
        derived = SongRequestIdentity(
            mode=derived.mode,
            artist=derived.artist,
            title=derived.title,
            requested_qualifiers=derived.requested_qualifiers,
            requested_feature_artist=guests[0],
            preserve_title_feature_syntax=derived.preserve_title_feature_syntax,
        )
    return derived


def _expand_request_identities(
    identities: Sequence[SongRequestIdentity],
) -> tuple[SongRequestIdentity, ...]:
    """Compose candidate-verified interpretations without guessing one early."""
    queue = list(identities)
    expanded: list[SongRequestIdentity] = []
    seen: set[SongRequestIdentity] = set()

    while queue:
        identity = queue.pop(0)
        if identity in seen:
            continue
        seen.add(identity)
        expanded.append(identity)

        dedication_title = _ambiguous_dedication_title(identity.title)
        if dedication_title:
            derived = _derived_request_identity(identity, title=dedication_title)
            if derived is not None and derived not in seen:
                queue.append(derived)

        polite_title = _ambiguous_politeness_title(identity.title)
        if polite_title:
            derived = _derived_request_identity(identity, title=polite_title)
            if derived is not None and derived not in seen:
                queue.append(derived)

        if identity.mode == "title":
            separator_parts = _CREDIT_SEPARATOR_RE.split(identity.title, maxsplit=1)
            if len(separator_parts) == 2 and all(part.strip(_REQUEST_IDENTITY_EDGE_CHARS) for part in separator_parts):
                derived = _derived_request_identity(
                    identity,
                    mode="artist_title",
                    artist=separator_parts[0],
                    title=separator_parts[1],
                )
                if derived is not None and derived not in seen:
                    queue.append(derived)

        ambiguous_feature = _split_ambiguous_request_feature_credit(identity.title)
        if ambiguous_feature is not None:
            feature_title, feature_artist = ambiguous_feature
            derived = _derived_request_identity(
                identity,
                title=feature_title,
                requested_feature_artist=feature_artist,
                preserve_title_feature_syntax=False,
            )
            if derived is not None and derived not in seen:
                queue.append(derived)
    return tuple(expanded)


def _clean_request_artist_credit(value: str) -> str:
    cleaned = _strip_artist_dedication_tail(_strip_dedication_tail(value))
    polite = _ambiguous_politeness_title(cleaned)
    if polite:
        cleaned = polite
    return cleaned.strip(_REQUEST_IDENTITY_EDGE_CHARS)


def parse_song_request(message: str) -> SongRequestIntent | None:
    """Parse supported English/Italian song commands into exact search intent.

    ``None`` means the text is not a supported song command.  It does not mean
    that a supported command has no matching catalogue result.
    """
    original = str(message or "").strip()
    raw_body = _strip_command(original)
    if raw_body is None:
        return None
    # Quote boundaries are authoritative before any conversational-tail
    # cleanup. Otherwise punctuation inside ``"Waiting, for You"`` looks like
    # a dedication separator and destroys the exact title before it is parsed.
    quoted_identity = _split_quoted_identity(raw_body)
    body = raw_body if quoted_identity is not None else _strip_dedication_tail(raw_body)
    artist_credit_has_courtesy = bool(
        re.search(
            r"\s+(?:by|di|da)\s+.+(?:\s*,\s*|\s+)(?:please|per\s+favore)\s*[.!?]*$",
            raw_body,
            flags=re.IGNORECASE,
        )
    )
    if not body:
        return None

    primary = _request_identity("title", title=body)
    alternatives: list[SongRequestIdentity] = []

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
                        parse_title_feature=False,
                    )
                    if _looks_like_by_title_tail(possible_artist):
                        primary = full_title_identity
                        alternatives.append(credit_identity)
                    else:
                        primary = credit_identity
                        alternatives.append(full_title_identity)
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
                            parse_title_feature=False,
                        )
                        explicit_artist_feature = split_artist_feature_credit(possible_artist) is not None
                        if (
                            explicit_artist_feature
                            or artist_credit_has_courtesy
                            or (title_word_count > 0 and artist_word_count > 0 and same_arity_class)
                        ):
                            primary = credit_identity
                            alternatives.append(full_title_identity)
                        else:
                            primary = full_title_identity
                            alternatives.append(credit_identity)

    identities = (primary,) if quoted_identity is not None else _expand_request_identities((primary, *alternatives))
    primary, alternative_identities = identities[0], identities[1:]

    if not primary.title and not primary.artist:
        return None
    return SongRequestIntent(
        primary_identity=primary,
        original=original,
        alternative_identities=alternative_identities,
    )


def _clean_artist(value: object) -> str:
    return strip_platform_artist_wrapper(value)


def _station_artist_identity(value: object) -> str:
    """Return the candidate-backed primary artist for station identity.

    A conventional ``feat.`` credit names a guest without changing the base
    artist used by bans, dedupe, queue admission, and cache rescue. Co-billed
    artists joined by ``&``, ``x``, commas, or ``and`` remain intact.
    """
    artist = _clean_artist(value)
    feature_credit = split_artist_feature_credit(artist)
    return _clean_artist(feature_credit[0]) if feature_credit is not None else artist


def _display_matched_artist(requested: str, evidence: str) -> str:
    """Build a readable display label after candidate evidence proves identity."""
    candidate = _clean_artist(evidence)
    requested_normalized = normalize_match_text(requested)
    candidate_normalized = normalize_match_text(candidate)
    if (
        candidate_normalized
        and " " not in candidate_normalized
        and " " in requested_normalized
        and candidate_normalized == requested_normalized.replace(" ", "")
    ):
        # ``LucioBattistiVEVO`` proves identity but is a poor listener-facing
        # label. Request boundaries are safe only for this display value; the
        # station and policy identities remain candidate-backed below.
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


def _is_quality_identity_with_variant(value: str) -> bool:
    """True when a quality-looking token is the song beneath a real variant."""
    title = _SPACE_RE.sub(" ", value).strip()
    prefix = _VARIANT_PREFIX_RE.match(title)
    if prefix and normalize_match_text(title[prefix.end() :]) in _QUALITY_IDENTITY_TOKENS:
        return True

    found_variant = False

    def _remove_variant_group(match: re.Match[str]) -> str:
        nonlocal found_variant
        if _bracketed_qualifiers(match.group(1)):
            found_variant = True
            return ""
        return match.group(0)

    base = _BRACKET_RE.sub(_remove_variant_group, title).strip()
    without_suffix = _VARIANT_SUFFIX_RE.sub("", base).strip()
    if without_suffix != base:
        found_variant = True
        base = without_suffix
    return found_variant and normalize_match_text(base) in _QUALITY_IDENTITY_TOKENS


def clean_candidate_title(value: object) -> str:
    """Remove corroborated platform packaging while retaining song identity."""
    original_title = _SPACE_RE.sub(" ", str(value or "")).strip()

    def _clean_quality_token(match: re.Match[str]) -> str:
        if not original_title[: match.start()].strip():
            return match.group(0)
        tail = original_title[match.end() :]
        bracket = re.match(r"\s*[\[(]([^\])]+)[\])]", tail)
        corroborated = bool(
            _VARIANT_SUFFIX_RE.fullmatch(tail)
            or (bracket is not None and (_is_noise_group(bracket.group(1)) or _bracketed_qualifiers(bracket.group(1))))
        )
        return "" if corroborated else match.group(0)

    title = _QUALITY_TOKEN_RE.sub(_clean_quality_token, original_title)
    title = _BRACKET_RE.sub(lambda match: "" if _is_noise_group(match.group(1)) else match.group(0), title)
    preserve_quality_variant = _is_quality_identity_with_variant(title)
    previous = None
    while title != previous:
        previous = title
        candidate = _PLATFORM_PACKAGING_SUFFIX_RE.sub("", title).strip()
        # In ``live version of HD`` the final token resembles a platform label
        # but is the whole song identity beneath explicit variant syntax.
        if preserve_quality_variant and not _is_quality_identity_with_variant(candidate):
            break
        title = candidate
    # Quality labels are normally platform noise, but they can also be the
    # complete song identity. Remove an adjacent wrapper first so ``HD
    # (Official Audio)`` keeps ``HD``; quality labels elsewhere remain noise.
    if normalize_match_text(title) in _QUALITY_IDENTITY_TOKENS or _is_quality_identity_with_variant(title):
        return _SPACE_RE.sub(" ", title).strip(" -\u2013\u2014|")
    cleaned = _SPACE_RE.sub(" ", title).strip(" -\u2013\u2014|")
    # A packaging-looking word can also be the complete, legitimate song
    # identity (``Audio``, ``Lyrics``, ``Visualizer``, ``HD``, ``4K``). Exact
    # structured metadata is subject to this helper too, so an empty cleanup
    # must fall back to that trusted identity instead of making it unmatchable.
    return cleaned or original_title.strip(" -\u2013\u2014|")


def _title_feature_artists(value: object) -> tuple[str, ...]:
    """Return candidate-backed performers credited on the title side."""
    feature_credit = _split_composed_title_feature_credit(value)
    if feature_credit is None:
        return ()
    guest_artist = _clean_artist(feature_credit[1])
    return (guest_artist,) if guest_artist else ()


def _split_title_credit(raw_title: str) -> tuple[str, str]:
    parts = _CREDIT_SEPARATOR_RE.split(raw_title, maxsplit=1)
    if len(parts) == 2:
        return _clean_artist(parts[0]), clean_candidate_title(parts[1])
    return "", clean_candidate_title(raw_title)


def _same_artist_identity(requested: str, candidate: str) -> bool:
    normalized_requested = normalize_artist_identity_text(requested)
    return bool(normalized_requested) and normalize_artist_identity_text(candidate) == normalized_requested


def _explicit_feature_artist_identities(value: object) -> tuple[str, ...]:
    """Return base and guest artists only for an explicit feature credit."""
    candidate = _clean_artist(value)
    feature_credit = split_artist_feature_credit(candidate)
    if feature_credit is None:
        return ()
    return tuple(artist for value_part in feature_credit if (artist := _clean_artist(value_part)))


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
    if requested == normalize_match_text(candidate):
        return candidate
    for part in _explicit_feature_artist_identities(candidate):
        if _same_artist_identity(requested_artist, part):
            return _clean_artist(part)
    if _same_artist_identity(requested_artist, candidate):
        return candidate
    return ""


def _credited_artist_identities(
    display_artist: str,
    identity_artist: str,
    additional_artists: Sequence[str] = (),
) -> tuple[str, ...]:
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
    candidates.extend(_explicit_feature_artist_identities(display_artist))
    candidates.extend(additional_artists)

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


def _strip_title_variant(value: str, *, strip_feature_credit: bool = True) -> str:
    title = clean_candidate_title(value)
    if strip_feature_credit:
        feature_credit = _split_composed_title_feature_credit(title)
        if feature_credit is not None:
            title = feature_credit[0]
    # Bracketed musical variants are meaningful for display but not the base
    # song identity used by the exact matcher.
    title = _BRACKET_RE.sub(
        lambda match: (
            ""
            if (
                _bracketed_qualifiers(match.group(1)) or _LONGFORM_IDENTITY_WRAPPER_RE.fullmatch(match.group(1).strip())
            )
            else match.group(0)
        ),
        title,
    ).strip()
    title = _VARIANT_PREFIX_RE.sub("", title)
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


@dataclass(frozen=True)
class _CandidateIdentity:
    artist_evidence: tuple[str, ...]
    title: str
    display_artist: str
    authoritative_artist: str
    feature_base_artist: str
    prefix_artist: str
    feature_evidence: tuple[str, ...]
    explicit_feature_artists: tuple[str, ...]
    contextual_feature_credits: tuple[tuple[str, str], ...]
    platform_artists: tuple[str, ...]


def _platform_artist_disproving_prefix(candidate: _CandidateIdentity) -> str:
    """Return one unconflicted platform artist that contradicts a dash prefix."""
    if candidate.authoritative_artist or not candidate.prefix_artist:
        return ""
    usable = [
        artist
        for artist in candidate.platform_artists
        if normalize_match_text(artist) not in _GENERIC_ARTIST_LABELS
        and normalize_match_text(artist) not in {"generic channel", "official channel", "youtube"}
    ]
    if not usable or any(_same_artist_identity(candidate.prefix_artist, artist) for artist in usable):
        return ""
    normalized = {normalize_artist_identity_text(artist) for artist in usable}
    return usable[0] if len(normalized) == 1 else ""


def _candidate_identity(metadata: dict[str, Any]) -> _CandidateIdentity:
    display_title = str(metadata.get("title") or metadata.get("track_title") or "").strip()
    prefix_artist, title_from_display = _split_title_credit(display_title)
    structured_title = clean_candidate_title(metadata.get("track_title") or "")
    title = structured_title or title_from_display
    structured_artist = _clean_artist(metadata.get("track_artist") or "")
    authoritative_artist = (
        structured_artist if normalize_match_text(structured_artist) not in _GENERIC_ARTIST_LABELS else ""
    )
    base_artist_evidence = [
        authoritative_artist or structured_artist,
        prefix_artist,
        str(metadata.get("artist") or ""),
        str(metadata.get("uploader") or ""),
        str(metadata.get("channel") or ""),
    ]
    title_feature_artists = tuple(
        dict.fromkeys(
            (
                *_title_feature_artists(title_from_display),
                *_title_feature_artists(structured_title),
            )
        )
    )
    artist_credit_evidence = (
        (authoritative_artist,)
        if authoritative_artist
        else (structured_artist, prefix_artist, str(metadata.get("artist") or ""))
    )
    artist_feature_artists = tuple(
        guest
        for value in artist_credit_evidence
        if (feature_credit := split_artist_feature_credit(_clean_artist(value))) is not None
        if (guest := _clean_artist(feature_credit[1]))
    )
    explicit_feature_artists = tuple(dict.fromkeys((*title_feature_artists, *artist_feature_artists)))
    contextual_feature_credits = tuple(
        dict.fromkeys(
            feature_credit
            for value in (title_from_display, structured_title)
            if (feature_credit := _split_contextual_candidate_feature_credit(value)) is not None
        )
    )
    platform_artists = tuple(
        dict.fromkeys(
            cleaned
            for value in (metadata.get("uploader"), metadata.get("channel"))
            if (cleaned := _clean_artist(value))
        )
    )
    # Structured performer metadata is best.  In its absence, a conventional
    # ``Artist - Title`` credit is more trustworthy for display than the
    # uploader/channel identity retained in the legacy ``artist`` field.
    display_artist = structured_artist or prefix_artist
    if not display_artist:
        display_artist = next(
            (_clean_artist(value) for value in base_artist_evidence if _clean_artist(value)),
            "",
        )
    # Only title-prefix or structured performer metadata can establish the
    # primary artist for a title-side guest credit. The legacy ``artist`` field
    # may be an uploader/channel and must never become station identity merely
    # because the guest verified the listener request.
    feature_base_artist = authoritative_artist or prefix_artist or structured_artist
    title_credit_aliases = list(title_feature_artists)
    if feature_base_artist and title_feature_artists:
        title_credit_aliases.append(f"{feature_base_artist} feat. {' & '.join(title_feature_artists)}")
    fallback_artist_evidence = (
        prefix_artist,
        str(metadata.get("artist") or ""),
        str(metadata.get("uploader") or ""),
        str(metadata.get("channel") or ""),
    )
    artist_evidence = tuple(
        value
        for value in (
            authoritative_artist or structured_artist,
            *title_credit_aliases,
            *(fallback_artist_evidence if not authoritative_artist else ()),
        )
        if value
    )
    return _CandidateIdentity(
        artist_evidence=artist_evidence,
        title=title,
        display_artist=display_artist,
        authoritative_artist=authoritative_artist,
        feature_base_artist=feature_base_artist,
        prefix_artist=prefix_artist,
        feature_evidence=tuple(title_credit_aliases),
        explicit_feature_artists=explicit_feature_artists,
        contextual_feature_credits=contextual_feature_credits,
        platform_artists=platform_artists,
    )


@dataclass(frozen=True)
class SongCandidateMatch:
    """A relevant result with separate display and station identities.

    ``artist``/``title`` are listener-facing labels; an exact compact artist
    match may borrow request word boundaries for readability. Candidate-backed
    ``station_artist``/``identity_title`` remain the stable Track identity.
    """

    metadata: dict[str, Any]
    artist: str
    station_artist: str
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
        "radio_edit",
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

    Matches against the parser's primary interpretation rank before its
    composable ambiguity alternatives. Within each interpretation, recording
    variant (standard, remaster, other variants) and then original search order
    break ties. Search ordering alone can therefore never make an unrelated
    video a match.
    """
    matches: list[SongCandidateMatch] = []
    request_identities = [intent.primary_identity, *intent.alternative_identities]
    for index, metadata in enumerate(metadata_results):
        candidate = _candidate_identity(metadata)
        # Recording variants belong to the recording title. Artist/channel
        # names such as "Live Nation" are identity evidence, never proof that a
        # standard upload is a live performance.
        _, display_title_evidence = _split_title_credit(str(metadata.get("title") or ""))
        candidate_qualifiers = frozenset().union(
            _recording_qualifiers(display_title_evidence),
            _recording_qualifiers(metadata.get("track_title") or ""),
        )
        structured_title = clean_candidate_title(metadata.get("track_title") or "")
        full_display_title = clean_candidate_title(metadata.get("title") or "")
        for identity_rank, request_identity in enumerate(request_identities):
            contextual_feature = next(
                (
                    feature_credit
                    for feature_credit in candidate.contextual_feature_credits
                    if (
                        request_identity.requested_feature_artist
                        and _same_artist_identity(request_identity.requested_feature_artist, feature_credit[1])
                    )
                    or (request_identity.artist and _same_artist_identity(request_identity.artist, feature_credit[1]))
                ),
                None,
            )
            contextual_feature_artists = (contextual_feature[1],) if contextual_feature is not None else ()
            active_feature_artists = tuple(
                dict.fromkeys((*candidate.explicit_feature_artists, *contextual_feature_artists))
            )
            if request_identity.requested_feature_artist and not any(
                _same_artist_identity(request_identity.requested_feature_artist, guest)
                for guest in active_feature_artists
            ):
                continue
            feature_base_artist = candidate.feature_base_artist
            feature_evidence = candidate.feature_evidence
            if contextual_feature is not None:
                feature_base_artist = feature_base_artist or candidate.prefix_artist
                contextual_aliases = [contextual_feature[1]]
                if feature_base_artist:
                    contextual_aliases.append(f"{feature_base_artist} feat. {contextual_feature[1]}")
                feature_evidence = tuple(dict.fromkeys((*feature_evidence, *contextual_aliases)))
            matched_artist = ""
            matched_artist_evidence = ""
            matched_artist_from_title_feature = False
            identity_artist = ""
            if request_identity.artist:
                for value in (*candidate.artist_evidence, *feature_evidence):
                    identity_artist = _matched_artist_identity(request_identity.artist, value)
                    if identity_artist:
                        matched_artist = _display_matched_artist(request_identity.artist, value)
                        matched_artist_evidence = _clean_artist(value)
                        matched_artist_from_title_feature = any(
                            _same_artist_identity(value, evidence) for evidence in feature_evidence
                        )
                        break
                if not matched_artist:
                    continue

            title_interpretations = [(candidate.title, candidate_qualifiers)]
            if contextual_feature is not None:
                title_interpretations = [(contextual_feature[0], candidate_qualifiers)]
            structured_artist_disproves_prefix = bool(
                candidate.authoritative_artist
                and candidate.prefix_artist
                and not _same_artist_identity(candidate.authoritative_artist, candidate.prefix_artist)
            )
            request_artist_disproves_prefix = bool(
                request_identity.artist
                and matched_artist
                and not _matched_artist_identity(request_identity.artist, candidate.prefix_artist)
                and not matched_artist_from_title_feature
            )
            platform_artist_disproving_prefix = _platform_artist_disproving_prefix(candidate)
            if (
                not structured_title
                and full_display_title
                and full_display_title != candidate.title
                and (
                    structured_artist_disproves_prefix
                    or request_artist_disproves_prefix
                    or platform_artist_disproving_prefix
                )
            ):
                # A separately verified performer means the first dash can
                # belong to the title (``High - Live`` or ``Bang Bang - My
                # Baby Shot Me Down``), rather than an uncorroborated artist
                # prefix. The uncorroborated split is not a second candidate:
                # accepting it would let a request for "Live" match "High -
                # Live" merely because the real performer arrived separately.
                title_interpretations = [(full_display_title, _recording_qualifiers(full_display_title))]

            matched_title = ""
            matched_qualifiers: frozenset[str] = frozenset()
            requested_qualifiers = request_identity.requested_qualifiers
            strip_feature_credit = not request_identity.preserve_title_feature_syntax
            requested_base_title = normalize_match_text(
                _strip_title_variant(
                    request_identity.title,
                    strip_feature_credit=strip_feature_credit,
                )
            )
            for interpreted_title, interpreted_qualifiers in title_interpretations:
                if (interpreted_qualifiers & _FORBIDDEN_QUALIFIERS.keys()) - requested_qualifiers:
                    continue
                if requested_qualifiers and not requested_qualifiers <= interpreted_qualifiers:
                    continue
                if request_identity.title:
                    candidate_base_title = normalize_match_text(
                        _strip_title_variant(
                            interpreted_title,
                            strip_feature_credit=strip_feature_credit,
                        )
                    )
                    if not requested_base_title or candidate_base_title != requested_base_title:
                        continue
                matched_title = interpreted_title
                matched_qualifiers = interpreted_qualifiers
                break
            if not matched_title:
                continue

            # This value is display-only and may restore request word boundaries
            # after an exact compact match. Station and policy identity below
            # deliberately use candidate evidence instead.
            candidate_artist = (
                feature_base_artist or matched_artist
                if active_feature_artists
                else matched_artist or platform_artist_disproving_prefix or candidate.display_artist
            )
            identity_artist = identity_artist or candidate_artist
            credited_evidence = feature_evidence
            if active_feature_artists and not feature_base_artist and candidate.display_artist:
                credited_evidence = (*credited_evidence, candidate.display_artist)
            credited_artists = _credited_artist_identities(
                candidate_artist,
                identity_artist,
                credited_evidence,
            )
            # Station identity must come from candidate metadata, never from
            # spacing or punctuation reconstructed from the listener request.
            station_artist_evidence = (
                feature_base_artist or matched_artist_evidence
                if matched_artist_from_title_feature
                else matched_artist_evidence or platform_artist_disproving_prefix or candidate.display_artist
            )
            station_artist = _station_artist_identity(station_artist_evidence)
            identity_title = _strip_title_variant(
                matched_title,
                strip_feature_credit=not request_identity.preserve_title_feature_syntax,
            )
            if not identity_title:
                continue
            matches.append(
                SongCandidateMatch(
                    metadata=dict(metadata),
                    artist=candidate_artist,
                    station_artist=station_artist,
                    identity_artist=identity_artist,
                    credited_artists=credited_artists,
                    title=matched_title,
                    identity_title=identity_title,
                    variant=_variant_label(matched_qualifiers),
                    rank=(identity_rank, _variant_rank(matched_qualifiers), index),
                )
            )
            # One search result represents one recording. If it validates both
            # interpretations, retain only its higher-priority identity.
            break

    matches.sort(key=lambda match: match.rank)
    if not matches:
        return SongMatchResult(failure_reason="low_confidence")
    return SongMatchResult(matches=tuple(matches))
