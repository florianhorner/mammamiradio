"""Track rationale and source narrative system.

Generates playful "Why this track?" explanations that mix plausible reasons
with obviously fake statistics. Used for per-track attribution in the listener
UI and metadata sent through the producer.

Editorial guardrails baked into the copy in this module:
1. No precise dates — say "last winter" not "January 14th"
2. No specific locations — say "your commute" not "the A4 motorway"
3. No sensitive behavior — no "3am listening", no health/mood inference
4. All fake stats stay obviously absurd — 98.3%, not 60% (plausible is creepy)
5. Plausible deniability — "probably" and "we think" over certainty

`_GUARDRAIL_BANNED_PATTERNS` below codifies rules 1 and 2 plus the
surveillance-time slice of rule 3 (e.g. "at 3:14 am") as regexes.
`tests/playlist/test_track_rationale_coverage.py::test_guardrail_patterns_pass_on_current_copy`
scans `_REAL_REASONS`, `_FAKE_REASONS`, and listener-pattern lines against
those patterns — any future copy edit that introduces a banned phrase fails
the test before it ships. Rules 4 and 5 are stylistic and not regex-checkable.
"""

from __future__ import annotations

import random

from mammamiradio.core.models import ListenerProfile, PlaylistSource, Track


def classify_track_crate(track: Track, source: PlaylistSource | None) -> str:
    """Assign a track to a taste crate based on available metadata."""
    if source and source.kind == "demo":
        return "discoveries"
    if track.popularity >= 70:
        return "classics"
    if track.explicit:
        return "guilty_pleasures"
    if track.popularity <= 30 and track.popularity > 0:
        return "deep_cuts"
    # Default: coin flip between discoveries and wildcards
    return random.choice(["discoveries", "wildcards"])


# ---------------------------------------------------------------------------
# "Why this track?" explanations
# ---------------------------------------------------------------------------

# Real-ish reasons (plausible, based on available metadata)
_REAL_REASONS = [
    "You saved this artist in at least one playlist. We checked.",
    "This was in your Liked Songs. Yes, it still counts.",
    "Popularity score: {popularity}/100. You have mainstream taste and that's fine.",
    "This artist showed up {n} times in your library. Coincidence? No.",
    "Album track from {album}. You saved one song, we brought the whole family.",
    "Duration: {duration}. Perfect for whatever you're pretending to do right now.",
    "This was the most-played genre in your collection. Don't fight it.",
    "You liked a song by a similar artist. We took that as permission.",
]

# Obviously fake reasons (absurd stats, safe humor)
_FAKE_REASONS = [
    "98.3% chance you'll deny liking this track in public.",
    "Our algorithm gave this a 'chef's kiss' rating of 11.7 out of 10.",
    "You played this 17 times last winter. Or maybe it was someone else. We'll never tell.",
    "Predicted skip probability: 12%. We're betting on you.",
    "This track has a 94.1% emotional damage potential. Proceed with caution.",
    "Based on your listening pattern, you were going to search for this in about 3 songs.",
    "Compatibility score: 'suspiciously high'. We don't make the rules.",
    "Fun fact: 73% of listeners who like your top artist also like this. The other 27% are wrong.",
    "Our AI classified this as 'objectively a banger'. The AI has questionable taste.",
    "This track was recommended by an algorithm that also thinks pineapple belongs on pizza.",
    "Risk assessment: LOW. Unless you're in a meeting with your boss.",
    "We found this in a playlist called something we can't repeat on air.",
    "Vibe match: 107%. Yes, that's over 100%. We don't know how either.",
    "This is the musical equivalent of comfort food. Zero nutritional value, maximum satisfaction.",
    "Statistically, you'll hum this for the next 48 hours. Sorry in advance.",
]


def generate_track_rationale(
    track: Track,
    source: PlaylistSource | None = None,
    listener: ListenerProfile | None = None,
) -> str:
    """Generate a playful 'Why this track?' explanation for the listener UI."""
    pool: list[str] = []

    # Add real-ish reasons with template substitution
    for tmpl in _REAL_REASONS:
        reason = tmpl.format(
            popularity=track.popularity or random.randint(40, 85),
            album=track.album or "an album we can't pronounce",
            duration=f"{track.duration_ms // 60000}:{(track.duration_ms % 60000) // 1000:02d}",
            n=random.randint(2, 8),
        )
        pool.append(reason)

    # Add all fake reasons
    pool.extend(_FAKE_REASONS)

    # Listener-pattern-aware reasons
    if listener and listener.patterns:
        pats = listener.patterns
        if "restless_skipper" in pats:
            pool.append("We picked this one knowing you'd skip it. Prove us wrong.")
        if "ballad_lover" in pats:
            pool.append("We detected a romantic streak. This one's for the feelings.")
        if "energy_seeker" in pats:
            pool.append("High BPM detected in your preferences. This should keep you moving.")
        if "bails_on_intros" in pats:
            pool.append("This one gets to the point fast. We learned from your impatience.")

    return random.choice(pool)


# ---------------------------------------------------------------------------
# Guardrail enforcement
# ---------------------------------------------------------------------------

_GUARDRAIL_BANNED_PATTERNS = [
    # No precise dates
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",  # DD/MM/YYYY
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d",
    # No specific times that imply surveillance
    r"\bat\s+\d{1,2}:\d{2}\s*(am|pm)?\b",
    # No location specifics
    r"\b(via |strada |piazza |address|GPS|coordinates)\b",
]
