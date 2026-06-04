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
        "marin",
        "cedar",
    }
)

# edge-tts Italian voice IDs exposed by the installed edge-tts package in this
# workspace. The package fronts Microsoft Edge Read Aloud, not the official
# Azure Speech catalog; keep this intentionally narrower than Azure.
EDGE_ITALIAN_VOICES = frozenset(
    {
        "it-IT-DiegoNeural",
        "it-IT-ElsaNeural",
        "it-IT-GiuseppeMultilingualNeural",
        "it-IT-IsabellaNeural",
    }
)

# Official Azure Speech Italian voices that are useful for the station. Azure
# also exposes broader multilingual catalogs and a Voice List API; unknown Azure
# IDs are not rejected at config load because availability varies by region.
AZURE_ITALIAN_VOICES = frozenset(
    {
        "it-IT-Alessio:DragonHDLatestNeural",
        "it-IT-Isabella:DragonHDLatestNeural",
        "it-IT-DiegoNeural",
        "it-IT-ElsaNeural",
        "it-IT-GiuseppeMultilingualNeural",
        "it-IT-IsabellaNeural",
    }
)

EDGE_DEFAULT_FALLBACK_VOICE = "it-IT-DiegoNeural"


def is_openai_voice(voice: str) -> bool:
    """Return True if the voice ID matches a known OpenAI TTS voice."""
    return voice.strip().lower() in OPENAI_VOICES


def is_known_edge_voice(voice: str) -> bool:
    """Return True if the voice ID is in the Italian edge-tts catalog."""
    return voice.strip() in EDGE_ITALIAN_VOICES


def is_known_azure_voice(voice: str) -> bool:
    """Return True if the voice ID is in the curated Azure Italian catalog."""
    return voice.strip() in AZURE_ITALIAN_VOICES
