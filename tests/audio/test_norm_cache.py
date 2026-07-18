from __future__ import annotations

from unittest.mock import patch

from mammamiradio.audio.norm_cache import (
    RESCUE_COOLDOWN_SECONDS,
    record_rescue_airplay,
    rescue_on_cooldown,
    rescue_rotation_status,
    select_norm_cache_rescue,
)
from mammamiradio.audio.normalizer import save_track_metadata
from mammamiradio.core.models import Segment, SegmentLogEntry, SegmentType, StationState, Track
from mammamiradio.playlist.downloader import clear_rejected_cache_keys, reject_cached_download


def _write_norm(tmp_path, name: str, *, title: str | None = None, artist: str | None = None):
    path = tmp_path / name
    path.write_bytes(b"audio")
    if title is not None and artist is not None:
        save_track_metadata(path, title=title, artist=artist)
    return path


def _choose_first(items, **_kwargs):
    return items[0]


def _choose_last(items, **_kwargs):
    return items[-1]


def test_select_norm_cache_rescue_returns_none_without_cache(tmp_path):
    assert select_norm_cache_rescue(tmp_path, StationState()) is None


def test_select_norm_cache_rescue_avoids_current_song(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }

    current = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    alternative = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="A far l amore", artist="Raffaella Carra")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == alternative
    choice.assert_called_once_with([alternative])
    assert rescue != current


def test_select_norm_cache_rescue_avoids_recent_stream_log_music(tmp_path):
    state = StationState()
    state.stream_log.append(
        SegmentLogEntry(
            type=SegmentType.MUSIC.value,
            label="Alex Warren - Ordinary",
            metadata={"title": "Ordinary", "artist": "Alex Warren"},
        )
    )
    state.stream_log.append(
        SegmentLogEntry(type=SegmentType.BANTER.value, label="Hosts", metadata={"title": "Ordinary"})
    )

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    alternative = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="Musica Leggera", artist="Colapesce")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        assert select_norm_cache_rescue(tmp_path, state) == alternative


def test_select_norm_cache_rescue_falls_back_when_every_cache_file_is_recent(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }
    state.stream_log.append(
        SegmentLogEntry(
            type=SegmentType.MUSIC.value,
            label="Raffaella Carra - A far l amore",
            metadata={"title": "A far l amore", "artist": "Raffaella Carra"},
        )
    )

    first = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    second = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="A far l amore", artist="Raffaella Carra")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_last) as choice:
        assert select_norm_cache_rescue(tmp_path, state) == second

    choice.assert_called_once_with([first, second])


def test_select_norm_cache_rescue_allows_only_cache_file_when_recent(tmp_path):
    state = StationState(
        current_track=Track(title="Ordinary", artist="Alex Warren", duration_ms=180_000, spotify_id="ordinary")
    )
    only = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        assert select_norm_cache_rescue(tmp_path, state) == only


def test_select_norm_cache_rescue_skips_blocklisted_cache_file(tmp_path):
    """A banned song must never re-air through the rescue path. The blocklisted
    cache file is dropped even though it is not a recent identity."""
    state = StationState(blocklist={("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}})

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    allowed = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="Musica Leggera", artist="Colapesce")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == allowed
    choice.assert_called_once_with([allowed])


def test_select_norm_cache_rescue_ignores_preferences_on_hot_path(tmp_path):
    state = StationState(
        song_preferences={
            ("raffaella carra", "a far l amore"): {"score": 1},
            ("alex warren", "ordinary"): {"score": -1},
        }
    )

    first = _write_norm(tmp_path, "norm_aaa_liked.mp3")
    second = _write_norm(tmp_path, "norm_zzz_disliked.mp3")

    with (
        patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice,
        patch("mammamiradio.audio.norm_cache.load_track_metadata") as load_metadata,
    ):
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == first
    choice.assert_called_once_with([first, second])
    load_metadata.assert_not_called()


def test_select_norm_cache_rescue_returns_none_when_only_file_is_banned(tmp_path):
    """If every cache file is banned, the rescue degrades to None so the caller's
    next layer (canned clip / forced banter) keeps audio flowing — never a banned song."""
    state = StationState(blocklist={("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}})
    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    assert select_norm_cache_rescue(tmp_path, state) is None


def test_select_norm_cache_rescue_skips_rejected_cache_key_even_when_file_remains(tmp_path):
    cache_key = "youtube_rejected001"
    try:
        reject_cached_download(tmp_path, cache_key, "simulated failed purge")
        _write_norm(tmp_path, f"norm_{cache_key}_192k.mp3", title="Rejected Set", artist="Selector")
        allowed = _write_norm(tmp_path, "norm_youtube_allowed001_192k.mp3", title="Single", artist="Artist")

        with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]) as choice:
            rescue = select_norm_cache_rescue(tmp_path, StationState())

        assert rescue == allowed
        choice.assert_called_once_with([allowed])
    finally:
        clear_rejected_cache_keys()


def test_select_norm_cache_rescue_ignores_malformed_sidecar(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Alex Warren - Ordinary",
        "metadata": {"title": "Ordinary", "artist": "Alex Warren"},
    }

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    malformed = tmp_path / "norm_broken_sidecar.mp3"
    malformed.write_bytes(b"audio")
    (tmp_path / "norm_broken_sidecar.mp3.json").write_text("{not valid json")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        assert select_norm_cache_rescue(tmp_path, state) == malformed


# --- Rescue rotation cooldown (the same-song-three-times-in-21-minutes fix) ---


def test_select_rescue_with_empty_airplay_behaves_like_no_rotation(tmp_path):
    """Scenario 3 (post-restart): a fresh process has an empty airplay map even with
    a persisted ``session_stopped`` flag still set, so no cached song is falsely on
    cooldown and selection matches pre-rotation behavior without reading a sidecar."""
    state = StationState()
    state.session_stopped = True  # flag persisted from a prior run / watchdog restart
    first = _write_norm(tmp_path, "norm_aaa_first.mp3")
    second = _write_norm(tmp_path, "norm_zzz_second.mp3")

    with (
        patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice,
        patch("mammamiradio.audio.norm_cache.load_track_metadata") as load_metadata,
    ):
        rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == first
    choice.assert_called_once_with([first, second])
    load_metadata.assert_not_called()


def test_select_rescue_skips_song_still_inside_cooldown(tmp_path):
    """A song that aired as a rescue a minute ago is skipped for a fresher one —
    this is what stops the three-in-a-row replay."""
    state = StationState()
    cooling = _write_norm(tmp_path, "norm_aaa_cooling.mp3", title="Cooling", artist="A")
    fresh = _write_norm(tmp_path, "norm_zzz_fresh.mp3", title="Fresh", artist="B")

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[cooling] = 10_000.0 - 60.0  # heard 60s ago
        with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
            rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == fresh
    choice.assert_called_once_with([fresh])


def test_select_rescue_shares_cooldown_across_bitrate_variants(tmp_path):
    """The same cache key at two bitrates is one song for rescue rotation."""
    state = StationState()
    cooling = _write_norm(tmp_path, "norm_youtube_same_track_192k.mp3")
    bitrate_variant = _write_norm(tmp_path, "norm_youtube_same_track_128k.mp3")
    fresh = _write_norm(tmp_path, "norm_youtube_fresh_track_192k.mp3")

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[cooling] = 10_000.0 - 60.0
        with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
            rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == fresh
    assert bitrate_variant != rescue
    choice.assert_called_once_with([fresh])


def test_select_rescue_when_all_cooling_picks_least_recently_heard(tmp_path):
    """Never dead air and never an immediate repeat: with every candidate cooling,
    round-robin to the one heard longest ago instead of returning None or shuffling."""
    state = StationState()
    older = _write_norm(tmp_path, "norm_aaa_older.mp3", title="Older", artist="A")
    newer = _write_norm(tmp_path, "norm_zzz_newer.mp3", title="Newer", artist="B")

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[older] = 10_000.0 - 100.0  # heard 100s ago
        state.rescue_airplay[newer] = 10_000.0 - 10.0  # heard 10s ago
        with patch("mammamiradio.audio.norm_cache.random.choice") as choice:
            rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == older
    choice.assert_not_called()  # deterministic least-recent, not a shuffle


def test_select_rescue_exactly_at_cooldown_boundary_is_eligible(tmp_path):
    """A candidate exactly RESCUE_COOLDOWN_SECONDS old has left the window; one a
    second short of it has not."""
    state = StationState()
    boundary = _write_norm(tmp_path, "norm_aaa_boundary.mp3", title="Boundary", artist="A")
    cooling = _write_norm(tmp_path, "norm_zzz_cooling.mp3", title="Cooling", artist="B")

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[boundary] = 10_000.0 - RESCUE_COOLDOWN_SECONDS
        state.rescue_airplay[cooling] = 10_000.0 - (RESCUE_COOLDOWN_SECONDS - 1.0)
        with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
            rescue = select_norm_cache_rescue(tmp_path, state)

    assert rescue == boundary
    choice.assert_called_once_with([boundary])


def test_rescue_on_cooldown_never_heard_is_false(tmp_path):
    state = StationState()
    path = tmp_path / "norm_never.mp3"
    assert rescue_on_cooldown(state, path) is False


def _rescue_segment(path, *, audio_source: str) -> Segment:
    return Segment(type=SegmentType.MUSIC, path=path, metadata={"audio_source": audio_source})


def test_record_rescue_airplay_only_stamps_norm_cache_sources(tmp_path):
    state = StationState()
    path = tmp_path / "norm_aaa.mp3"

    record_rescue_airplay(state, _rescue_segment(path, audio_source="youtube"))
    assert path not in state.rescue_airplay  # a normally-aired song is not a rescue

    record_rescue_airplay(state, _rescue_segment(path, audio_source="norm_cache"))
    assert path in state.rescue_airplay

    other = tmp_path / "norm_bbb.mp3"
    record_rescue_airplay(state, _rescue_segment(other, audio_source="fallback_norm_cache"))
    assert other in state.rescue_airplay


def test_record_rescue_airplay_prunes_entries_two_cooldowns_old(tmp_path):
    state = StationState()
    stale = tmp_path / "norm_stale.mp3"
    fresh = tmp_path / "norm_fresh.mp3"

    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=100_000.0):
        state.rescue_airplay[stale] = 100_000.0 - (2 * RESCUE_COOLDOWN_SECONDS) - 1.0
        record_rescue_airplay(state, _rescue_segment(fresh, audio_source="norm_cache"))

    assert fresh in state.rescue_airplay
    assert stale not in state.rescue_airplay  # evicted-file bookkeeping never accumulates


def test_rescue_rotation_status_reports_cooling_without_filesystem_paths(tmp_path):
    state = StationState()
    with patch("mammamiradio.audio.norm_cache.time.monotonic", return_value=10_000.0):
        state.rescue_airplay[tmp_path / "norm_youtube_track_192k.mp3"] = 10_000.0 - 60.0
        state.rescue_airplay[tmp_path / "norm_youtube_track_128k.mp3"] = 10_000.0 - 30.0
        status = rescue_rotation_status(state)

    assert status["cooldown_seconds"] == RESCUE_COOLDOWN_SECONDS
    assert status["tracked"] == 1
    assert status["cooling"] == 1
    assert status["most_recent"]
    assert "/" not in status["most_recent"]
    assert ".mp3" not in status["most_recent"]


def test_rescue_rotation_status_empty_is_quiet(tmp_path):
    status = rescue_rotation_status(StationState())
    assert status["tracked"] == 0
    assert status["cooling"] == 0
    assert status["most_recent"] == ""
