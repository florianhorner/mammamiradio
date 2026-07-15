"""Truth checks for listener-facing copy.

These checks are intentionally narrow: aggregate companionship and a separately
authorized named Home return are allowed, but a stream connection may never be
turned into an arrival, return, or first-listener claim. The final producer
boundary uses this leaf without importing prompt assembly.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

_AUTHORIZED_HOME_RETURN_SOURCES = {
    "ha:person.florian_horner": "Florian",
    "ha:person.sabrina": "Sabrina",
}
_CURATED_RESIDENT_NAMES_PATTERN = "|".join(
    re.escape(name) for name in sorted(set(_AUTHORIZED_HOME_RETURN_SOURCES.values()))
)
_GENERIC_AUDIENCE_PATTERN = re.compile(
    r"\b(?:you|your|listener\w*|audience|everyone|everybody|anyone|"
    r"friends?|folks?|guys?|all|voi|tu|tutti|chiunque|amici?|ragazz\w*|gente|ascoltator\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HomeReturnAuthority:
    """Narrow fact-bound authority for one named resident-return line."""

    fact_id: str
    source_entity_id: str
    resident_name: str

    def allows(self, text: str) -> bool:
        """Allow one return phrase bound to the named resident, and nothing else.

        The authority is line-scoped, but it must not turn the whole line into
        an exemption.  Remove only the exact authorized phrase and run the
        ordinary truth guard over the remainder so a second resident, listener,
        or generic return claim in the same line still fails closed.
        """

        if not isinstance(text, str) or _GENERIC_AUDIENCE_PATTERN.search(text):
            return False
        name = re.escape(self.resident_name)
        addressed_name = rf"{name}(?=\s*(?:[,!?.;:—–-]|$))"
        patterns = (
            rf"\bwelcome\s+back\b[,\s:—–-]*{addressed_name}",
            rf"\b(?:bentornat[oa]|ben\s+ritrovat[oa])\b[,\s:—–-]*{addressed_name}",
            rf"\b(?:glad|nice|good|great|happy|lovely|wonderful)\s+to\s+"
            rf"(?:have|see)\s+{name}\s+back\b",
            rf"\b{name}\b\s+(?:(?:is|'s)\s+(?:finally\s+)?back|"
            r"(?:(?:has|'s)\s+)?(?:just\s+)?returned|"
            r"(?:has\s+)?(?:just\s+)?come\s+back|(?:just\s+)?came\s+back)\b",
            rf"\b{name}\b\s+(?:è\s+)?(?:appena\s+)?tornat\w*\b",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match is None:
                continue
            remainder = f"{text[: match.start()]} {text[match.end() :]}"
            return find_unsafe_listener_claim(remainder) is None
        return False


def home_return_authority_for_directive(source: str, directive: str) -> HomeReturnAuthority | None:
    """Derive authority only from the two curated HA person-state triggers."""

    resident_name = _AUTHORIZED_HOME_RETURN_SOURCES.get(str(source or ""))
    if resident_name is None or resident_name.casefold() not in str(directive or "").casefold():
        return None
    digest = hashlib.sha256(f"{source}\0{directive}".encode()).hexdigest()[:16]
    return HomeReturnAuthority(
        fact_id=f"resident-return-{digest}",
        source_entity_id=source.removeprefix("ha:"),
        resident_name=resident_name,
    )


_UNSAFE_LISTENER_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "english_arrival",
        re.compile(
            r"\b(?:someone|somebody|the\s+listener|a\s+listener|our\s+listener)\s+(?:has\s+)?(?:just\s+)?"
            r"(?:tuned\s+in|joined|connected|arrived|is\s+(?:listening|here))\b"
            r"|\b(?:just|right\s+now|finally)\s+(?:tuned\s+in|joined|connected|arrived)\b"
            r"|\byou(?:'ve|\s+have|\s+are|'re)?\s+(?:just\s+)?"
            r"(?:tuned\s+in|joined(?:\s+us)?|connected|arrived)\b"
            r"|\bthanks\s+for\s+(?:tuning\s+in|joining\s+us|connecting)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "english_listener_arrival_label",
        re.compile(r"\b(?:a|the|our)\s+(?:new|first)\s+listener\b|\b(?:new|first)\s+listener\b", re.IGNORECASE),
    ),
    (
        "english_return",
        re.compile(
            rf"\b(?:welcome\s+back|(?:good|glad|nice|great|happy|lovely|wonderful)\s+to\s+"
            rf"(?:have|see)\s+(?:you|{_CURATED_RESIDENT_NAMES_PATTERN})\s+"
            r"(?:back|with\s+us\s+again)|back\s+with\s+us)\b"
            r"|\b(?:good|glad|nice|great|happy|lovely|wonderful)\s+(?:having|seeing)\s+you\s+"
            r"(?:back|here\s+again)\b"
            r"|\b(?:good|glad|nice|great|happy|lovely|wonderful)\s+you(?:'re|\s+are)\s+back\b"
            r"|\b(?:you(?:'re|\s+are)\s+back|you(?:'ve|\s+have)?\s+(?:come|came)\s+back)\b"
            r"|\byou(?:(?:'ve|\s+have)\s+)?(?:just\s+)?returned\b"
            r"|\byou(?:'ve|\s+have)\s+rejoined\s+us\b"
            r"|\byou(?:'re|\s+are)\s+(?:here|with\s+us)\s+again\b"
            r"|\bthanks\s+for\s+(?:coming\s+back|returning|rejoining(?:\s+us)?)\b"
            r"|\b(?:we|the\s+station)\s+(?:have|got)\s+you\s+back\b"
            rf"|\b(?:someone|somebody|the\s+listener|a\s+listener|our\s+listener|"
            rf"{_CURATED_RESIDENT_NAMES_PATTERN})\s+(?:"
            r"(?:(?:has|'s)\s+)?(?:just\s+)?returned|"
            r"(?:(?:has|'s)\s+)?(?:just\s+)?rejoined\s+us|"
            r"(?:has\s+)?(?:just\s+)?come\s+back|"
            r"(?:just\s+)?came\s+back|"
            r"(?:is|'s)\s+(?:here|with\s+us)\s+again|"
            r"(?:is|'s)\s+(?:finally\s+)?back)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "italian_arrival",
        re.compile(
            r"\bqualcuno\s+(?:si\s+è\s+)?(?:appena\s+)?"
            r"(?:sintonizzat\w*|collegat\w*|arrivat\w*|unit\w*(?:\s+a\s+noi)?)\b"
            r"|\b(?:appena|proprio\s+ora)\s+(?:sei|siete)\s+"
            r"(?:sintonizzat\w*|collegat\w*|arrivat\w*)\b"
            r"|\b(?:tu\s+)?(?:sei|ti\s+sei)\s+(?:appena\s+)?"
            r"(?:sintonizzat\w*|collegat\w*|arrivat\w*)\b"
            r"|\b(?:voi\s+)?(?:siete|vi\s+siete)\s+(?:appena\s+)?"
            r"(?:sintonizzat\w*|collegat\w*|arrivat\w*)\b"
            r"|\bsi\s+è\s+(?:appena\s+)?(?:sintonizzat\w*|collegat\w*|arrivat\w*)\s+"
            r"(?:qualcuno|un[oa]\s+ascoltator\w*)\b"
            r"|\bci\s+(?:ha|hanno)\s+(?:appena\s+)?raggiunt\w*(?:\s+qualcuno)?\b"
            r"|\bqualcuno\s+ci\s+(?:ha|hanno)\s+(?:appena\s+)?raggiunt\w*\b"
            r"|\bgrazie\s+(?:per|di)\s+(?:esserti|esservi|essere)\s+"
            r"(?:sintonizzat\w*|collegat\w*|unit\w*)\b"
            r"|\bun[oa]?\s+(?:nuov[oa]\s+)?arriv\w*\b",
            re.IGNORECASE,
        ),
    ),
    (
        "italian_listener_arrival_label",
        re.compile(
            r"\b(?:nuov[oa]|prim[oa])\s+ascoltator\w*\b"
            r"|\bfinalmente\s+qualcuno\s+(?:ci\s+)?ascolt\w*\b"
            r"|\bqualcuno\s+ci\s+ascolt\w*\b",
            re.IGNORECASE,
        ),
    ),
    (
        "italian_return",
        re.compile(
            r"\b(?:benvenut[oa]|bentornat[oaie]|ben\s+ritrovat[oaie])\b"
            r"|\b(?:sei|siete)\s+(?:di\s+nuovo|ancora)\s+(?:con\s+noi|qui)\b"
            r"|\b(?:sei|siete)\s+(?:qui|con\s+noi)\s+(?:di\s+nuovo|ancora)\b"
            r"|\b(?:di\s+nuovo|ancora)\s+(?:qui\s+)?con\s+noi\b"
            r"|\b(?:sei|siete|ti\s+sei|vi\s+siete)\s+(?:appena\s+)?tornat\w*\b"
            r"|\becco(?:ti|vi)\s+(?:qui\s+)?di\s+nuovo\b"
            r"|\bgrazie\s+(?:per|di)\s+(?:esserti|esservi|essere)\s+tornat\w*\b"
            r"|\b(?:che\s+bello|che\s+piacere|felic[ei]|content[ioe]|bellissimo)\s+(?:di\s+)?"
            r"(?:riaver(?:ti|vi)|riveder(?:ti|vi)|ritrovar(?:ti|vi))(?:\s+(?:qui|con\s+noi))?\b"
            r"|\b(?:è|fa)\s+(?:bello|piacere)\s+"
            r"(?:riaver(?:ti|vi)|riveder(?:ti|vi)|ritrovar(?:ti|vi))\b"
            rf"|\b(?:che\s+bello|che\s+piacere|felic[ei]|content[ioe])\s+(?:di\s+)?"
            rf"(?:riavere|rivedere|ritrovare)\s+(?:{_CURATED_RESIDENT_NAMES_PATTERN})"
            r"(?:\s+(?:qui|con\s+noi))?\b"
            rf"|\b(?:qualcuno|un[oa]?\s+ascoltator\w*|l['’]ascoltator\w*|"
            rf"{_CURATED_RESIDENT_NAMES_PATTERN})\s+(?:"
            r"(?:è\s+)?(?:appena\s+)?tornat\w*|"
            r"(?:è\s+)?(?:di\s+nuovo|ancora)\s+(?:qui|con\s+noi))\b",
            re.IGNORECASE,
        ),
    ),
)


def find_unsafe_listener_claim(text: str) -> str | None:
    """Return the first unsafe claim category, or ``None`` for safe copy."""

    if not isinstance(text, str):
        return "non_text"
    for category, pattern in _UNSAFE_LISTENER_CLAIM_PATTERNS:
        if pattern.search(text):
            return category
    return None


def contains_unsafe_listener_claims(
    texts: str | Iterable[str],
    *,
    return_authority: HomeReturnAuthority | None = None,
) -> bool:
    """Return whether final copy exceeds listener/home-return truth authority."""

    if isinstance(texts, str):
        texts = (texts,)
    authority_used = False
    for text in texts:
        category = find_unsafe_listener_claim(text)
        if category is None:
            continue
        if (
            category in {"english_return", "italian_return"}
            and return_authority is not None
            and not authority_used
            and return_authority.allows(text)
        ):
            authority_used = True
            continue
        return True
    return False
