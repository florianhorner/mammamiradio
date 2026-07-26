from __future__ import annotations

from unittest.mock import patch

from mammamiradio.audio.norm_cache import (
    RESCUE_COOLDOWN_SECONDS,
    is_recent_music,
    recent_music_identity_keys,
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
    assert select_norm_cache_rescue(tmp_path, StationState(), allow_recent_repeat=True) is None


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
        rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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
        assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True) == alternative


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
        assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True) == second

    choice.assert_called_once_with([first, second])


def test_select_norm_cache_rescue_allows_only_cache_file_when_recent(tmp_path):
    state = StationState(
        current_track=Track(title="Ordinary", artist="Alex Warren", duration_ms=180_000, spotify_id="ordinary")
    )
    only = _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True) == only


def test_select_norm_cache_rescue_skips_blocklisted_cache_file(tmp_path):
    """A banned song must never re-air through the rescue path. The blocklisted
    cache file is dropped even though it is not a recent identity."""
    state = StationState(blocklist={("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}})

    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")
    allowed = _write_norm(tmp_path, "norm_zzz_alternative.mp3", title="Musica Leggera", artist="Colapesce")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first) as choice:
        rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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
        rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

    assert rescue == first
    choice.assert_called_once_with([first, second])
    load_metadata.assert_not_called()


def test_select_norm_cache_rescue_returns_none_when_only_file_is_banned(tmp_path):
    """If every cache file is banned, the rescue degrades to None so the caller's
    next layer (canned clip / forced banter) keeps audio flowing — never a banned song."""
    state = StationState(blocklist={("alex warren", "ordinary"): {"display": "Alex Warren - Ordinary"}})
    _write_norm(tmp_path, "norm_aaa_ordinary.mp3", title="Ordinary", artist="Alex Warren")

    assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True) is None


def test_select_norm_cache_rescue_skips_rejected_cache_key_even_when_file_remains(tmp_path):
    cache_key = "youtube_rejected001"
    try:
        reject_cached_download(tmp_path, cache_key, "simulated failed purge")
        _write_norm(tmp_path, f"norm_{cache_key}_192k.mp3", title="Rejected Set", artist="Selector")
        allowed = _write_norm(tmp_path, "norm_youtube_allowed001_192k.mp3", title="Single", artist="Artist")

        with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=lambda items: items[0]) as choice:
            rescue = select_norm_cache_rescue(tmp_path, StationState(), allow_recent_repeat=True)

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
        assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True) == malformed


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
        rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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
            rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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
            rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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
            rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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
            rescue = select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True)

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


# ---------------------------------------------------------------------------
# The shared recent-music gate. Both the playback-gap rescue and the live-control
# continuity reservation ask the same question through these two helpers, so the
# two paths cannot disagree about "is this the song on air right now?".
# ---------------------------------------------------------------------------


def test_recent_music_identity_keys_covers_now_streaming_and_stream_log(tmp_path):
    state = StationState()
    assert recent_music_identity_keys(state) == set()

    state.now_streaming = {
        "type": "music",
        "label": "Fleece – Dont Lose Your Way",
        "metadata": {"title": "Fleece – Dont Lose Your Way", "title_only": "Dont Lose Your Way", "artist": "Fleece"},
    }
    state.stream_log.append(
        SegmentLogEntry(
            type=SegmentType.MUSIC.value,
            label="Nomadi – Io Vagabondo",
            timestamp=0.0,
            metadata={"title_only": "Io Vagabondo", "artist": "Nomadi"},
        )
    )
    keys = recent_music_identity_keys(state)

    assert any("lose your way" in key for key in keys)
    assert any("vagabondo" in key for key in keys)


def test_is_recent_music_matches_the_on_air_song_and_spares_others(tmp_path):
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Fleece – Dont Lose Your Way",
        "metadata": {"title": "Fleece – Dont Lose Your Way", "title_only": "Dont Lose Your Way", "artist": "Fleece"},
    }
    keys = recent_music_identity_keys(state)
    on_air = _write_norm(tmp_path, "norm_on_air_192k.mp3", title="Dont Lose Your Way", artist="Fleece")
    other = _write_norm(tmp_path, "norm_other_192k.mp3", title="Io Vagabondo", artist="Nomadi")

    assert is_recent_music(on_air, keys) is True
    assert is_recent_music(other, keys) is False
    # No recent keys at all (a cold boot) can never exclude anything.
    assert is_recent_music(on_air, set()) is False


def test_is_recent_music_reuses_a_preloaded_sidecar(tmp_path):
    """The live-control hot path already read the sidecar; it must not read twice."""
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Fleece – Dont Lose Your Way",
        "metadata": {"title_only": "Dont Lose Your Way", "artist": "Fleece"},
    }
    keys = recent_music_identity_keys(state)
    path = _write_norm(tmp_path, "norm_on_air_192k.mp3", title="Dont Lose Your Way", artist="Fleece")

    with patch("mammamiradio.audio.norm_cache.load_track_metadata") as load_metadata:
        assert is_recent_music(path, keys, sidecar={"title": "Dont Lose Your Way", "artist": "Fleece"}) is True

    load_metadata.assert_not_called()

    # An EMPTY dict is a loaded-but-useless sidecar, not "please reload".
    with patch("mammamiradio.audio.norm_cache.load_track_metadata") as load_metadata:
        is_recent_music(path, keys, sidecar={})

    load_metadata.assert_not_called()


# ---------------------------------------------------------------------------
# Ladder policy contract. `allow_recent_repeat` is a safety policy, and the way
# it went wrong was not a bad value — it was a DEFAULT that two ladders acquired
# by saying nothing. These tests hold the policy itself and the callers to it.
# ---------------------------------------------------------------------------


def test_allow_recent_repeat_is_required_and_has_no_default():
    """A safety policy that can be acquired by forgetting is not a policy."""
    import inspect

    param = inspect.signature(select_norm_cache_rescue).parameters["allow_recent_repeat"]
    assert param.default is inspect.Parameter.empty, "allow_recent_repeat must not have a default"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "must be keyword-only so call sites read as policy"


def test_strict_mode_refuses_the_on_air_song_and_permissive_mode_serves_it(tmp_path):
    """The whole contract in one place: same cache, same state, opposite answers."""
    state = StationState()
    state.now_streaming = {
        "type": "music",
        "label": "Fleece – Dont Lose Your Way",
        "metadata": {"title_only": "Dont Lose Your Way", "artist": "Fleece"},
    }
    only = _write_norm(tmp_path, "norm_on_air_192k.mp3", title="Dont Lose Your Way", artist="Fleece")

    with patch("mammamiradio.audio.norm_cache.random.choice", side_effect=_choose_first):
        # A caller with real audio below it drops through rather than repeating.
        assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=False) is None
        # A near-last rung serves the repeat rather than falling silent.
        assert select_norm_cache_rescue(tmp_path, state, allow_recent_repeat=True) == only


def test_every_ladder_declares_its_repeat_policy_explicitly():
    """Each caller must state its policy, and match its real rung position.

    A caller may inherit the wrong policy in either direction: permissive while
    real audio sits beneath it (a needless repeat), or strict while nothing real
    does (a looping ident with a playable song in the cache). Both shipped on this
    branch. Grepping the call sites is the check that scales to the next ladder.
    """
    import inspect

    from mammamiradio.scheduling import producer
    from mammamiradio.web import streamer

    expectations = {
        # Music-first bridge: packaged clip + emergency tone sit below it.
        (producer._queue_continuity_bridge, "allow_recent_repeat=False"): True,
        # Post-clip retry: only 2s of emergency tone is left below.
        (producer._queue_continuity_bridge, "allow_recent_repeat=True"): True,
        # Error recovery: the next rung recycles last-known-good, a certain repeat.
        (producer._producer_error_recovery_segment, "allow_recent_repeat=True"): True,
    }
    for (func, expected), _ in expectations.items():
        source = inspect.getsource(func)
        assert expected in source, f"{func.__name__} must declare {expected}"

    # The playback gap asks PERMISSIVELY, and the honest reason is that the rungs
    # once claimed to sit below it do not. `assets/demo/music/` is not packaged,
    # and the packaged-clip branch sets `segment_ready`, which makes the 60s
    # forced-banter escape unreachable — so a strict ask there means the same
    # 4.4s ident looping while a playable song sits in the cache. Permissive is
    # not "repeat freely": the selector still prefers a non-recent candidate.
    # The packaging half of that reasoning is asserted in
    # tests/web/test_streamer_routes.py, so this fails loudly if demo music ships.
    playback = inspect.getsource(streamer.run_playback_loop)
    assert "allow_recent_repeat=True" in playback, "playback-gap rescue must ask permissively"

    # No caller anywhere may omit the policy.
    for module in (producer, streamer):
        module_source = inspect.getsource(module)
        for line in module_source.splitlines():
            if "norm_cache_rescue(" not in line or line.strip().startswith("#"):
                continue
            if "def " in line:
                continue
            assert "allow_recent_repeat" in line or line.rstrip().endswith("("), (
                f"call site omits the repeat policy: {line.strip()}"
            )


def test_operations_doc_repeat_policy_table_matches_the_code():
    """The operator doc's `allow_recent_repeat` table must not contradict the code.

    This table has now been wrong in BOTH directions on one branch: first it
    claimed the playback-gap rescue asked permissively while the code asked
    strictly, then the code changed and the doc kept the old answer. An operator
    reading it to decide whether a repeat is expected behaviour got the opposite
    of the truth each time, and neither slip was catchable by any existing check.

    Every `producer.py:NNN` / `streamer.py:NNN` reference inside that section is
    resolved against the real source line. A stale line number fails just as
    loudly as a wrong bucket, because a doc pointing at the wrong line is not
    documentation either.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    doc = (repo_root / "docs" / "operations.md").read_text(encoding="utf-8")

    section = doc.split("Whether a caller may re-serve a recent song")[1]
    section = section.split("That parameter is not cosmetic")[0]

    buckets: dict[str, list[tuple[str, int]]] = {"True": [], "False": []}
    current: str | None = None
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- `False`"):
            current = "False"
        elif stripped.startswith("- `True`"):
            current = "True"
        if current is None:
            continue
        for module_name, lineno in re.findall(r"\b(producer\.py|streamer\.py):(\d+)", line):
            buckets[current].append((module_name, int(lineno)))

    assert buckets["True"], "doc lists no permissive call sites — did the section move?"
    assert buckets["False"], "doc lists no strict call sites — did the section move?"

    paths = {
        "producer.py": repo_root / "mammamiradio" / "scheduling" / "producer.py",
        "streamer.py": repo_root / "mammamiradio" / "web" / "streamer.py",
    }
    for expected, sites in buckets.items():
        for module_name, lineno in sites:
            source_line = paths[module_name].read_text(encoding="utf-8").splitlines()[lineno - 1]
            assert "allow_recent_repeat" in source_line, (
                f"docs/operations.md points at {module_name}:{lineno}, which no longer "
                f"declares a repeat policy: {source_line.strip()!r}"
            )
            assert f"allow_recent_repeat={expected}" in source_line, (
                f"docs/operations.md lists {module_name}:{lineno} as {expected}, "
                f"but the code says: {source_line.strip()!r}"
            )
