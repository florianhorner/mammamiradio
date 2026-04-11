"""Track rationale and source narrative system.

Generates playful "Why this track?" explanations that mix plausible reasons
with obviously fake statistics. Powers the onboarding narrative, taste crate
labels, and per-track attribution in the listener UI.

Guardrail rules (enforced in all output):
1. NEVER reference precise dates — say "last winter" not "January 14th"
2. NEVER reference specific locations — say "your commute" not "the A4 motorway"
3. NEVER reference sensitive behavior — no "3am listening", no health/mood inference
4. ALL fake stats must be obviously absurd — 98.3%, not 60% (plausible is creepy)
5. ALWAYS maintain plausible deniability — "probably" and "we think" over certainty
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from mammamiradio.models import ListenerProfile, PlaylistSource, Track

# ---------------------------------------------------------------------------
# Taste crate categories (the "loot screen" metaphor)
# ---------------------------------------------------------------------------


@dataclass
class TasteCrate:
    """A labeled bucket of tracks in the onboarding crate-dig metaphor."""

    key: str
    label_it: str
    label_en: str
    description: str
    icon: str


TASTE_CRATES = [
    TasteCrate(
        key="classics",
        label_it="I Tuoi Classici",
        label_en="Your Classics",
        description="The songs you'd rescue from a burning building. "
        "We found them in your most-played, your oldest playlists, "
        "and the ones you never removed from Liked Songs.",
        icon="vinyl",
    ),
    TasteCrate(
        key="guilty_pleasures",
        label_it="I Piaceri Proibiti",
        label_en="Your Guilty Pleasures",
        description="The songs you skip when someone's watching. "
        "We found them buried three playlists deep where nobody looks. "
        "Your secret is safe. Probably.",
        icon="mask",
    ),
    TasteCrate(
        key="discoveries",
        label_it="Le Nostre Scoperte",
        label_en="Our Discoveries",
        description="Songs you haven't heard yet but statistically should love. "
        "We cross-referenced your taste with 47 imaginary algorithms "
        "and a coin flip. You're welcome.",
        icon="compass",
    ),
    TasteCrate(
        key="deep_cuts",
        label_it="Gli Scavi Profondi",
        label_en="The Deep Cuts",
        description="Album tracks, B-sides, and songs you saved once "
        "and forgot about. We remembered. We always remember.",
        icon="shovel",
    ),
    TasteCrate(
        key="wildcards",
        label_it="Le Carte Pazze",
        label_en="The Wildcards",
        description="Songs that have absolutely no business being here. "
        "But the algorithm had a feeling. "
        "If you skip these, we learn. If you don't, we were right all along.",
        icon="dice",
    ),
]


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
# Onboarding narrative copy
# ---------------------------------------------------------------------------

ONBOARDING_NARRATIVE = {
    "headline": "Stiamo saccheggiando la tua musica.",
    "headline_en": "We're raiding your music.",
    "steps": [
        {
            "label_it": "Collegamento in corso...",
            "label_en": "Connecting...",
            "copy": "We're tuning into the Italian charts. Don't worry — "
            "we have impeccable taste. Well, mostly. "
            "Actually, we judge a little.",
        },
        {
            "label_it": "Analisi dei gusti...",
            "label_en": "Analyzing taste...",
            "copy": "Scanning the charts, trending tracks, and that one genre "
            "you secretly love but won't admit to. We see everything. "
            "We have questions.",
        },
        {
            "label_it": "Classificazione...",
            "label_en": "Sorting the crates...",
            "copy": "Sorting your music into categories: "
            "Undeniable Bangers, Guilty Pleasures You'll Deny, "
            "and Songs You Saved Once And Forgot About. "
            "The last category is surprisingly large.",
        },
        {
            "label_it": "Costruzione della stazione...",
            "label_en": "Building your station...",
            "copy": "We've seen enough. Your station is taking shape. "
            "Two hosts are warming up, the jingle is tuning itself, "
            "and we're making some extremely specific bad decisions together.",
        },
    ],
}


# ---------------------------------------------------------------------------
# Station birth sequence script (20-30s audio)
# ---------------------------------------------------------------------------

STATION_BIRTH_SCRIPT = {
    "duration_target_sec": 25,
    "sequence": [
        {
            "type": "sfx",
            "description": "Static crackle, radio dial scanning through frequencies",
            "duration_sec": 3,
        },
        {
            "type": "sfx",
            "description": "The station jingle motif assembles note by note "
            "(C5... E5... G5... C6 — the Rhodes arpeggio locks in)",
            "duration_sec": 4,
        },
        {
            "type": "voice",
            "host": "Marco",
            "text": "Ah... eccoci. Mamma Mi Radio. Da Windor a Vergen.",
            "note": "Delivered like waking up — groggy, then snapping to life",
            "duration_sec": 4,
        },
        {
            "type": "voice",
            "host": "Giulia",
            "text": "Abbiamo dato un'occhiata alla tua musica. Abbiamo delle domande.",
            "note": "Deadpan. Zero warmth. Maximum intrigue.",
            "duration_sec": 4,
        },
        {
            "type": "voice",
            "host": "Marco",
            "text": "Ma prima — abbiamo visto abbastanza. "
            "Facciamo insieme delle pessime decisioni incredibilmente specifiche.",
            "note": "Building energy. The 'let's go' moment.",
            "duration_sec": 5,
        },
        {
            "type": "sfx",
            "description": "Full jingle sting plays — the station is alive",
            "duration_sec": 3,
        },
        {
            "type": "voice",
            "host": "Giulia",
            "text": "Primo pezzo. Non dirci se ci abbiamo preso. Lo sappiamo già.",
            "note": "Smug confidence. Immediate cut to first track.",
            "duration_sec": 3,
        },
    ],
}


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

GUARDRAIL_RULES = [
    "NEVER reference precise dates — say 'last winter' not 'January 14th'. "
    "Vague temporal anchors only: 'a while back', 'that summer', 'recently'.",
    "NEVER reference specific locations — say 'your commute' not 'the A4'. "
    "Geographic vagueness is funny; geographic precision is surveillance.",
    "NEVER reference sensitive behavior — no '3am listening sessions', "
    "no health/mood inference, no 'you were sad when you played this'. "
    "Stick to music taste, never emotional state.",
    "ALL fake statistics must be obviously absurd — use 98.3%, 107%, 11.7/10. "
    "Plausible numbers (like 62%) feel like real tracking. Absurd numbers feel like a joke.",
    "ALWAYS maintain plausible deniability — 'probably', 'we think', 'our algorithm suggests'. "
    "Never state knowledge as fact. The humor comes from the gap between "
    "'we definitely know' energy and 'we're totally guessing' language.",
]
