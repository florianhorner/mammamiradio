from __future__ import annotations

from typing import Literal

from mammamiradio.core.config import PacingSection
from mammamiradio.core.models import Track
from mammamiradio.playlist.music_admission import (
    build_music_admission_envelope,
    classify_youtube_candidate,
)


def _track(
    title: str,
    *,
    duration_ms: int,
    youtube_id: str = "vid12345678",
    source: Literal["youtube", "jamendo", "local", "demo", "classic"] = "youtube",
) -> Track:
    return Track(
        title=title,
        artist="Artist",
        duration_ms=duration_ms,
        youtube_id=youtube_id,
        source=source,
    )


def test_music_admission_holds_longform_marker():
    playlist = [_track("Single A", duration_ms=180_000, youtube_id="single00001")]
    candidate = _track("Sunday DJ Set", duration_ms=180_000, youtube_id="set00000001")

    verdict = classify_youtube_candidate(
        candidate,
        playlist,
        PacingSection(songs_between_banter=2),
        metadata={"title": "Sunday DJ Set - full album continuous mix"},
    )

    assert verdict.status == "hold"
    assert verdict.reason.startswith("longform_marker:")
    assert verdict.notice_reason == "longform_audio"


def test_music_admission_reject_notice_reason_is_non_music():
    playlist = [_track("Single A", duration_ms=180_000, youtube_id="single00001")]
    candidate = _track("Morning Podcast Episode", duration_ms=180_000, youtube_id="talk0000001")

    verdict = classify_youtube_candidate(candidate, playlist, PacingSection(songs_between_banter=2))

    assert verdict.status == "reject"
    assert verdict.reason.startswith("non_music_marker:")
    assert verdict.notice_reason == "non_music_audio"


def test_music_admission_holds_duration_outside_station_envelope():
    playlist = [
        _track("Single A", duration_ms=180_000, youtube_id="single00001"),
        _track("Single B", duration_ms=210_000, youtube_id="single00002"),
        _track("Single C", duration_ms=240_000, youtube_id="single00003"),
    ]
    candidate = _track("Two Hour Mix", duration_ms=7_200_000, youtube_id="mix00000001")

    verdict = classify_youtube_candidate(candidate, playlist, PacingSection(songs_between_banter=2))

    assert verdict.status == "hold"
    assert verdict.reason == "longform_duration"
    assert verdict.envelope is not None
    assert verdict.duration_sec and verdict.duration_sec > verdict.envelope.longform_threshold_sec


def test_music_admission_treats_source_youtube_without_id_as_ytdlp_candidate():
    playlist = [_track("Single A", duration_ms=180_000, youtube_id="single00001")]
    candidate = _track("Looks Short", duration_ms=180_000, youtube_id="", source="youtube")

    verdict = classify_youtube_candidate(
        candidate,
        playlist,
        PacingSection(songs_between_banter=2),
        actual_duration_sec=7_200.0,
    )

    assert verdict.status == "hold"
    assert verdict.reason == "longform_duration"


def test_music_admission_accepts_longer_song_when_rotation_envelope_fits():
    playlist = [
        _track("Longer Single A", duration_ms=460_000, youtube_id="single00001"),
        _track("Longer Single B", duration_ms=480_000, youtube_id="single00002"),
        _track("Longer Single C", duration_ms=500_000, youtube_id="single00003"),
    ]
    candidate = _track("Legitimate Long Song", duration_ms=520_000, youtube_id="single00004")

    verdict = classify_youtube_candidate(candidate, playlist, PacingSection(songs_between_banter=2))

    assert verdict.accepted is True
    assert verdict.reason == "single_track_music"


def test_music_admission_longform_outlier_does_not_widen_envelope():
    playlist = [
        _track("Single A", duration_ms=180_000, youtube_id="single00001"),
        _track("Single B", duration_ms=200_000, youtube_id="single00002"),
        _track("Stale Long Set", duration_ms=7_200_000, youtube_id="staleset001"),
    ]
    candidate = _track("Another Long Set", duration_ms=7_200_000, youtube_id="newset00001")

    verdict = classify_youtube_candidate(candidate, playlist, PacingSection(songs_between_banter=2))

    assert verdict.status == "hold"
    assert verdict.reason == "longform_duration"
    assert verdict.envelope is not None
    assert verdict.envelope.longform_threshold_sec < 7_200.0


def test_music_admission_even_playlist_outlier_does_not_average_median():
    playlist = [
        _track("Single A", duration_ms=180_000, youtube_id="single00001"),
        _track("Stale Long Set", duration_ms=7_200_000, youtube_id="staleset001"),
    ]

    envelope = build_music_admission_envelope(playlist, PacingSection(songs_between_banter=2))

    assert envelope.median_track_sec == 210.0
    assert envelope.longform_threshold_sec == 420.0


def test_music_admission_envelope_uses_station_pacing():
    playlist = [_track("Single A", duration_ms=180_000, youtube_id="single00001")]

    envelope = build_music_admission_envelope(playlist, PacingSection(songs_between_banter=3))

    assert envelope.median_track_sec == 210.0
    assert envelope.intended_music_run_sec == 630.0
    assert envelope.longform_threshold_sec == 630.0
