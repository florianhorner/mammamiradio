"""Shared classifiers for segment fallback state and stream outcome.

Single source of truth, extracted so that BOTH the listener-profile bookkeeping
in ``StationState.on_stream_segment`` and the provenance ledger's Tier-3
``stream_result`` row classify a segment the same way. Duplicating the flag list
is the documented failure mode ``producer-rescue-paths-miss-fallback-flag``:
producer rescue clips set ``queue_drain_recovery`` / ``silence_fallback`` /
``resume_bridge`` / ``idle_bridge`` WITHOUT ``fallback:True``, so a naive
``metadata["fallback"]`` check silently mislabels exactly the dead-air rescues
you most want to see.

This module is a dependency-free leaf (stdlib only) so it can be imported by
``core.models`` and ``web.streamer`` without import cycles.

    metadata ──► is_fallback_active() ──► bool (was this a rescue / fallback?)

    send-loop results ──► classify_stream_outcome() ──► aired_status enum
       (was_skipped, bytes_sent, listeners)            aired|skipped|
                                                       no_listeners|not_streamed
"""

from __future__ import annotations

# Metadata boolean flags that each independently mark a segment as fallback /
# rescue audio. Mirrors the historical inline check in on_stream_segment.
_FALLBACK_FLAG_KEYS = (
    "fallback",
    "queue_drain_recovery",
    "resume_bridge",
    "silence_fallback",
    "idle_bridge",
)

# audio_source values that are themselves fallback sources even when no boolean
# flag is set.
_FALLBACK_AUDIO_SOURCES = ("norm_cache", "emergency_tone")

# aired_status values for a Tier-3 stream_result row.
AIRED = "aired"  # streamed to completion with at least one listener
SKIPPED = "skipped"  # cut mid-stream (operator skip or skip_event)
NO_LISTENERS = "no_listeners"  # streamed but nobody was connected
NOT_STREAMED = "not_streamed"  # selected but zero bytes left the box (file error)
FALLBACK_RESCUE = "fallback_rescue"  # pure streamer-created fallback, no LLM provenance


def is_fallback_active(metadata: dict) -> bool:
    """True if this segment is fallback / rescue audio rather than the real thing.

    Checks every rescue flag, not just ``fallback`` — see module docstring.
    """
    if any(bool(metadata.get(key)) for key in _FALLBACK_FLAG_KEYS):
        return True
    raw_audio_source = str(metadata.get("audio_source") or "")
    if raw_audio_source.startswith("fallback"):
        return True
    return raw_audio_source in _FALLBACK_AUDIO_SOURCES


def classify_stream_outcome(
    *,
    was_skipped: bool,
    bytes_sent: int,
    listeners: int,
    fallback_active: bool = False,
) -> str:
    """Classify how a segment actually reached (or did not reach) listeners.

    Args:
        was_skipped: the send loop broke early on a skip event.
        bytes_sent: bytes actually broadcast during the send loop.
        listeners: connected listeners when the segment started streaming.
        fallback_active: this was rescue / fallback audio (``is_fallback_active``),
            not the intended segment — reported as ``fallback_rescue`` once it has
            actually reached a listener.

    Reach problems (a skip, nothing left the box, nobody connected) take priority
    over the source distinction, then a clean air of rescue audio is
    ``fallback_rescue``, otherwise ``aired``.
    """
    if was_skipped:
        return SKIPPED
    if bytes_sent <= 0:
        return NOT_STREAMED
    if listeners <= 0:
        return NO_LISTENERS
    if fallback_active:
        return FALLBACK_RESCUE
    return AIRED
