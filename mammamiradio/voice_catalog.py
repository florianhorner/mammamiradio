"""Voice catalog — single source of truth for TTS voice IDs per backend.

Config load validates every configured voice against the catalog for its
backend. Invalid voices are logged once and substituted with a safe default
before the first synthesis attempt, so runtime never floods with repeated
per-segment voice errors.
"""

from __future__ import annotations

# OpenAI gpt-4o-mini-tts voice catalog. Names are case-insensitive on the API
# so we match case-insensitively as well.
OPENAI_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    }
)

# edge-tts Italian voice IDs used by the station. edge-tts ships a larger
# multilingual catalog; we only need to validate the subset the radio actually
# configures. Entries are case-sensitive because edge-tts compares IDs
# verbatim.
EDGE_ITALIAN_VOICES = frozenset(
    {
        "it-IT-DiegoNeural",
        "it-IT-ElsaNeural",
        "it-IT-FabiolaNeural",
        "it-IT-FiammaNeural",
        "it-IT-GianniNeural",
        "it-IT-GiuseppeMultilingualNeural",
        "it-IT-IsabellaNeural",
        "it-IT-PalmiraNeural",
        "it-IT-PierinaNeural",
        "it-IT-RinaldoNeural",
    }
)

EDGE_DEFAULT_FALLBACK_VOICE = "it-IT-DiegoNeural"


def is_openai_voice(voice: str) -> bool:
    """Return True if the voice ID matches a known OpenAI TTS voice."""
    return voice.strip().lower() in OPENAI_VOICES


def is_known_edge_voice(voice: str) -> bool:
    """Return True if the voice ID is in the Italian edge-tts catalog."""
    return voice.strip() in EDGE_ITALIAN_VOICES
