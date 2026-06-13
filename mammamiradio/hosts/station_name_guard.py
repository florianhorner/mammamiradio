"""Station-name illusion guard.

The single hardest illusion break (leadership principle #1) is a competitor /
hallucinated radio-station name reaching a human surface. The LLM bleeds
training-data station names ("Radio Kiss Kiss Moosach") into banter, and home
context can seed an invented one ("Radio <housemate's name> Sensatione").

Two surfaces, two behaviours:

- **Spoken script text** — a sentence that names a station *should* name ours,
  so we *replace* the wrong name with our station name
  (:func:`sanitize_spoken_station_name`).
- **Now-playing metadata** (HA ``media_artist`` / ``media_title``, the listener
  UI label) — a song's artist/title is never "Radio X", so substituting our
  name would be wrong. We *strip* a foreign station name and let the caller
  fall back (:func:`strip_foreign_station_name`).

Both share one detection vocabulary so the two surfaces never drift apart.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Match station-name-like phrases. Inline (?i:…) makes "Radio" / "siamo su"
# case-insensitive while requiring Title Case on the proper-noun words that
# follow, which stops the match before Italian function words like "e", "la".
_WRONG_STATION_PATTERN = re.compile(
    r"\b(?i:Radio)(?:\s+[A-Z]\w*){1,3}|\b(?i:siamo\s+su)(?:\s+[A-Z]\w*){1,5}",
)

# Anchored variant for METADATA fields. It fires when the *entire* value is a
# station-name-like phrase, or when the value *begins* with one followed by a
# separator (the rescue display form "Radio X - Song"). "Radiohead" (a single
# token, no following Title-Case word) and "The Radio Dept." (does not start
# with "Radio") are safe.
#
# This is deliberately aggressive, NOT perfectly conservative: a real band
# literally named "Radio <Word>" (e.g. "Radio Birdman", "Radio Futura") also
# matches, and in the artist field is relabeled to our station name. That trade
# is intentional — a foreign improvised station name reaching the now-playing
# line is a hard illusion break (leadership #1); a rare real "Radio X" band shown
# as the station is benign beside it. Pinned by tests in test_station_name_guard.
_FULL_STATION_PATTERN = re.compile(
    r"(?i:Radio)(?:\s+[A-Z]\w*){1,3}|(?i:siamo\s+su)(?:\s+[A-Z]\w*){1,5}",
)
_LEADING_STATION_PREFIX = re.compile(
    r"^(?i:Radio)(?:\s+[A-Z]\w*){1,3}\s*[–—-]\s*",
)


def sanitize_spoken_station_name(text: str, station_name: str) -> str:
    """Replace any radio station name that isn't ours with the correct one.

    Guards against LLM training-data bleed where it writes competitor station
    names (e.g. 'Radio Kiss Kiss Moosach') into spoken script text — the single
    hardest illusion break.
    """
    station_lower = station_name.lower()

    def _replace(m: re.Match) -> str:
        s = m.group(0)
        # Keep the match if our station name is in it
        if station_lower in s.lower():
            return s
        # "siamo su <wrong>" → "siamo su <ours>"
        if s.lower().startswith("siamo su "):
            logger.warning("Replaced wrong station name in script: %r", s)
            return f"siamo su {station_name}"
        # "Radio <wrong>" → station name
        if s.lower().startswith("radio "):
            logger.warning("Replaced wrong station name in script: %r", s)
            return station_name
        return s

    return _WRONG_STATION_PATTERN.sub(_replace, text)


def strip_foreign_station_name(value: str | None, station_name: str, *, prefix_only: bool = False) -> str:
    """Strip a foreign 'Radio X' station name out of a now-playing metadata field.

    For ``media_artist`` / ``media_title`` and the listener-UI label, a foreign
    station name must never surface — but a song's real artist/title is not a
    station, so we drop the foreign name and let the caller fall back rather than
    substituting our own name as the artist.

    Returns ``""`` when the whole value is a foreign station name, strips a
    leading "Radio X - " prefix off the rescue display form, and otherwise
    returns the value unchanged. "Radiohead" and "The Radio Dept." are left
    intact. NOTE: matching is deliberately aggressive — a real band named
    "Radio <Word>" (e.g. "Radio Birdman") is also stripped, and the artist then
    falls back to the station name; this is an accepted trade so a foreign
    improvised station name can never surface (see the module-level note).

    ``prefix_only=True`` keeps the leading-prefix strip but skips the
    whole-value match, so the value is never emptied. Use it for the **title**
    field, where a real song can legitimately be named "Radio Ga Ga" / "Radio
    Free Europe" — blanking those would itself break the now-playing line. The
    artist field uses the default (full) mode because its fallback chain ends in
    our own station name, never a blank.
    """
    if not value:
        return ""
    v = value.strip()
    station_lower = station_name.lower()

    # Whole value IS a foreign station name → drop it entirely (artist field only).
    if not prefix_only:
        full = _FULL_STATION_PATTERN.fullmatch(v)
        if full and station_lower not in v.lower():
            logger.warning("Stripped foreign station name from now-playing field: %r", v)
            return ""

    # Rescue display form "Foreign Radio Name - Song" -> keep the song only.
    prefix = _LEADING_STATION_PREFIX.match(v)
    if prefix and station_lower not in prefix.group(0).lower():
        remainder = v[prefix.end() :].strip()
        logger.warning("Stripped foreign station prefix from now-playing field: %r", v)
        return remainder

    return v
