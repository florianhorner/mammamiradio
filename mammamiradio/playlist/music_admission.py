"""Admission policy for YouTube music candidates entering radio rotation."""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from mammamiradio.core.models import Track

AdmissionStatus = Literal["accept", "hold", "reject"]

# The station's built-in fallback track shape is roughly a modern single. This is
# a floor for sparse/test playlists, not a cap: real playlist durations can widen
# the envelope when the current station is intentionally running longer songs.
REFERENCE_SINGLE_TRACK_SEC = 210.0
DEFAULT_SONGS_BETWEEN_BANTER = 2
HIGH_TRACK_MEDIAN_MULTIPLE = 2.0

YOUTUBE_ADMISSION_SEARCH_DEPTH = 5

_HOLD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdj\s+set\b", re.I), "dj_set"),
    (re.compile(r"\bfull\s+album\b", re.I), "full_album"),
    (re.compile(r"\balbum\s+completo\b", re.I), "full_album"),
    (re.compile(r"\bcomplete\s+album\b", re.I), "full_album"),
    (re.compile(r"\bcompilation\b", re.I), "compilation"),
    (re.compile(r"\bmegamix\b", re.I), "mix"),
    (re.compile(r"\bcontinuous\s+mix\b", re.I), "mix"),
    (re.compile(r"\bessential\s+mix\b", re.I), "mix"),
    (re.compile(r"\bboiler\s+room\b", re.I), "dj_set"),
    (re.compile(r"\blive\s+stream\b", re.I), "live_stream"),
    (re.compile(r"\bfull\s+concert\b", re.I), "concert"),
    (re.compile(r"\bfull\s+show\b", re.I), "show"),
    (re.compile(r"\bcomplete\s+set\b", re.I), "dj_set"),
    (re.compile(r"\bconcerto\s+completo\b", re.I), "concert"),
    (re.compile(r"\bmix\s+completo\b", re.I), "mix"),
    (re.compile(r"\b\d+\s*(?:hour|hours|hr|hrs)\b", re.I), "hour_long"),
    (re.compile(r"\b\d+\s*(?:ore|ora)\b", re.I), "hour_long"),
    (re.compile(r"\b(?:60|90|120|180)\s*(?:min|minutes|minute)\b", re.I), "hour_long"),
)

_REJECT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpodcast\b", re.I), "podcast"),
    (re.compile(r"\bepisode\b", re.I), "episode"),
    (re.compile(r"\baudiobook\b", re.I), "audiobook"),
    (re.compile(r"\binterview\b", re.I), "interview"),
    (re.compile(r"\bdocumentary\b", re.I), "documentary"),
    (re.compile(r"\btutorial\b", re.I), "tutorial"),
    (re.compile(r"\blecture\b", re.I), "lecture"),
)


@dataclass(frozen=True)
class MusicAdmissionEnvelope:
    median_track_sec: float
    high_track_sec: float
    intended_music_run_sec: float
    longform_threshold_sec: float
    sample_size: int


@dataclass(frozen=True)
class MusicAdmissionVerdict:
    status: AdmissionStatus
    reason: str
    message: str
    duration_sec: float | None = None
    envelope: MusicAdmissionEnvelope | None = None
    marker: str = ""

    @property
    def accepted(self) -> bool:
        return self.status == "accept"

    @property
    def notice_reason(self) -> str:
        if self.accepted:
            return ""
        if self.status == "reject":
            return "non_music_audio"
        return "longform_audio"


def build_music_admission_envelope(
    playlist: Iterable[Track],
    pacing: Any,
) -> MusicAdmissionEnvelope:
    """Derive the station's current single-track envelope from rotation shape."""
    durations = sorted(
        duration
        for track in playlist
        if (duration := _duration_ms_to_sec(getattr(track, "duration_ms", None))) is not None
    )
    sample_size = len(durations)
    if durations:
        median_sec = max(float(statistics.median_low(durations)), REFERENCE_SINGLE_TRACK_SEC)
        # A stale/manual long-form track already in rotation must not redefine the
        # single-track window for every future YouTube candidate. Keep the p90
        # signal, but cap its contribution relative to the median; the pacing
        # window below still expands naturally for intentionally longer rotations.
        high_sec = max(
            min(_nearest_rank_percentile(durations, 0.90), median_sec * HIGH_TRACK_MEDIAN_MULTIPLE),
            REFERENCE_SINGLE_TRACK_SEC,
        )
    else:
        median_sec = REFERENCE_SINGLE_TRACK_SEC
        high_sec = REFERENCE_SINGLE_TRACK_SEC

    songs_between_banter = _coerce_songs_between_banter(pacing)
    intended_music_run_sec = median_sec * songs_between_banter
    threshold_sec = max(high_sec, intended_music_run_sec)
    return MusicAdmissionEnvelope(
        median_track_sec=median_sec,
        high_track_sec=high_sec,
        intended_music_run_sec=intended_music_run_sec,
        longform_threshold_sec=threshold_sec,
        sample_size=sample_size,
    )


def classify_youtube_candidate(
    track: Track,
    playlist: Iterable[Track],
    pacing: Any,
    *,
    metadata: dict[str, Any] | None = None,
    actual_duration_sec: float | None = None,
) -> MusicAdmissionVerdict:
    """Classify a YouTube candidate before it enters automatic rotation."""
    if not is_youtube_music_candidate(track, metadata):
        return _accept()

    envelope = build_music_admission_envelope(playlist, pacing)
    text = _candidate_text(track, metadata)
    marker = _find_marker(text, _REJECT_PATTERNS)
    if marker:
        return MusicAdmissionVerdict(
            status="reject",
            reason=f"non_music_marker:{marker}",
            marker=marker,
            envelope=envelope,
            duration_sec=_effective_duration_sec(track, metadata, actual_duration_sec),
            message=(
                "That result looks like talk or non-music audio, so it stayed out of rotation. "
                "Pick a single-track music result instead."
            ),
        )

    marker = _find_marker(text, _HOLD_PATTERNS)
    if marker:
        return MusicAdmissionVerdict(
            status="hold",
            reason=f"longform_marker:{marker}",
            marker=marker,
            envelope=envelope,
            duration_sec=_effective_duration_sec(track, metadata, actual_duration_sec),
            message=(
                "That looks like a set, album, or long-form music item, so it stayed out of rotation. "
                "Pick a single-track result instead."
            ),
        )

    duration_sec = _effective_duration_sec(track, metadata, actual_duration_sec)
    if duration_sec is None:
        return MusicAdmissionVerdict(
            status="hold",
            reason="unknown_duration",
            envelope=envelope,
            message=(
                "That result did not publish a clear track length, so it stayed out of rotation. "
                "Pick a single-track result with a visible duration."
            ),
        )

    if duration_sec > envelope.longform_threshold_sec:
        return MusicAdmissionVerdict(
            status="hold",
            reason="longform_duration",
            duration_sec=duration_sec,
            envelope=envelope,
            message=(
                "That runs longer than this station's current music window, so it stayed out of rotation. "
                "Pick a single-track result instead."
            ),
        )

    return _accept(duration_sec=duration_sec, envelope=envelope)


def _accept(
    *,
    duration_sec: float | None = None,
    envelope: MusicAdmissionEnvelope | None = None,
) -> MusicAdmissionVerdict:
    return MusicAdmissionVerdict(
        status="accept",
        reason="single_track_music",
        message="Accepted for rotation.",
        duration_sec=duration_sec,
        envelope=envelope,
    )


def _duration_ms_to_sec(value: Any) -> float | None:
    try:
        duration_ms = int(value)
    except (TypeError, ValueError):
        return None
    if duration_ms <= 0:
        return None
    return duration_ms / 1000.0


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    if not values:
        return REFERENCE_SINGLE_TRACK_SEC
    index = max(0, min(len(values) - 1, math.ceil(percentile * len(values)) - 1))
    return float(values[index])


def _coerce_songs_between_banter(pacing: Any) -> int:
    try:
        value = int(getattr(pacing, "songs_between_banter", DEFAULT_SONGS_BETWEEN_BANTER))
    except (TypeError, ValueError):
        value = DEFAULT_SONGS_BETWEEN_BANTER
    return max(1, value)


def is_youtube_music_candidate(track: Track, metadata: dict[str, Any] | None = None) -> bool:
    """Return true for tracks whose audio will be sourced through yt-dlp/YouTube."""
    if getattr(track, "youtube_id", ""):
        return True
    if str(getattr(track, "source", "") or "").strip().casefold() == "youtube":
        return True
    return bool(metadata and metadata.get("youtube_id"))


def _effective_duration_sec(
    track: Track,
    metadata: dict[str, Any] | None,
    actual_duration_sec: float | None,
) -> float | None:
    if actual_duration_sec is not None:
        try:
            actual = float(actual_duration_sec)
        except (TypeError, ValueError):
            actual = 0.0
        if actual > 0:
            return actual
    if metadata is not None:
        meta_duration = _duration_ms_to_sec(metadata.get("duration_ms"))
        if meta_duration is not None:
            return meta_duration
    return _duration_ms_to_sec(getattr(track, "duration_ms", None))


def _candidate_text(track: Track, metadata: dict[str, Any] | None) -> str:
    fields: list[str] = [
        getattr(track, "title", ""),
        getattr(track, "artist", ""),
        getattr(track, "display", ""),
    ]
    if metadata:
        for key in ("title", "artist", "uploader", "channel", "display", "webpage_url", "description"):
            value = metadata.get(key)
            if isinstance(value, str):
                fields.append(value)
    return " ".join(field for field in fields if field)


def _find_marker(text: str, patterns: tuple[tuple[re.Pattern[str], str], ...]) -> str:
    for pattern, marker in patterns:
        if pattern.search(text):
            return marker
    return ""
