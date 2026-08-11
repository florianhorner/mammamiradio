"""Admission races for listener-request banter promises."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import GenerationWasteReason, Segment, SegmentType, StationState, Track
from mammamiradio.hosts.scriptwriter import _plan_listener_request_block
from mammamiradio.scheduling.producer import (
    _abandon_banter_commit,
    _enqueue_with_egress,
    _listener_request_plan_stale_reason,
    _select_accepted_music_track,
)
from mammamiradio.web.streamer import _apply_ban, _segment_is_listener_reserved

PRODUCER_MODULE = "mammamiradio.scheduling.producer"
TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")


def _config(tmp_path: Path):
    config = load_config(TOML_PATH)
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    return config


def _matched_request(track: Track) -> dict:
    return {
        "request_id": "listener-request",
        "public_token": "listener-token",
        "name": "Luca",
        "message": f"Play {track.title} by {track.artist}",
        "type": "song_request",
        "status": "queued",
        "song_found": True,
        "song_error": False,
        "song_error_reason": "",
        "song_pinned": False,
        "song_track": track.display,
        "song_track_obj": track,
        "banter_cycles_missed": 0,
    }


@pytest.mark.asyncio
async def test_admitted_banter_commit_survives_following_music_pin_selection(tmp_path):
    requested = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=120_000,
        youtube_id="admitted-listener-pick",
    )
    request = _matched_request(requested)
    later_request = _matched_request(
        Track(
            title="Albachiara",
            artist="Vasco Rossi",
            duration_ms=120_000,
            youtube_id="later-listener-pick",
        )
    )
    later_request["request_id"] = "later-listener-request"
    state = StationState(
        playlist=[requested],
        pending_requests=[request, later_request],
        listeners_active=1,
    )
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)

    prompt, commit = _plan_listener_request_block(state)
    assert "La canzone che stai per suonare" in prompt
    assert commit is not None

    rendered = tmp_path / "admitted-dedication.mp3"
    rendered.write_bytes(b"dedication")
    banter = Segment(type=SegmentType.BANTER, path=rendered, metadata={"title": "Listener dedication"})
    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=banter):
        admitted = await _enqueue_with_egress(
            queue,
            state,
            config,
            banter,
            stale_check=lambda: _listener_request_plan_stale_reason(state, commit),
        )

    assert admitted is True
    assert queue.get_nowait() is banter
    queue.task_done()

    # After admission, lookahead is allowed to spend the exact pin on the
    # following MUSIC before the accepted banter's deferred callback runs.
    assert state.select_next_track() is requested
    assert state.pinned_track is None
    assert commit.is_plan_current(state) is False  # an unadmitted promise would now be stale

    commit.apply(state)

    assert request not in state.pending_requests
    assert later_request in state.pending_requests
    assert state.listener_request_handoff is not None
    assert state.recently_consumed_requests[-1]["status"] == "sent_to_hosts"
    assert state.recently_consumed_requests[-1]["song_found"] is True

    music_path = tmp_path / "preselected-promised-song.mp3"
    music_path.write_bytes(b"music")
    promised = Segment(
        type=SegmentType.MUSIC,
        path=music_path,
        metadata={
            "artist": requested.artist,
            "title_only": requested.title,
            **state.listener_request_handoff_metadata(requested),
        },
    )
    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=promised):
        music_admitted = await _enqueue_with_egress(
            queue,
            state,
            config,
            promised,
            admission_callback=state.admit_listener_request_handoff,
        )

    assert music_admitted is True
    assert state.listener_request_handoff is None
    assert _segment_is_listener_reserved(state, promised) is False


@pytest.mark.asyncio
async def test_later_same_recording_reservation_does_not_steal_admitted_handoff(tmp_path):
    first = Track(
        title="L'Italiano",
        artist="Toto Cutugno",
        duration_ms=180_000,
        youtube_id="shared-listener-recording",
    )
    later_alias = Track(
        title="LItaliano",
        artist="TotoCutugno",
        duration_ms=180_000,
        youtube_id="shared-listener-recording",
    )
    ordinary = Track(title="Ordinary", artist="Rotation", duration_ms=180_000, youtube_id="ordinary")
    first_request = _matched_request(first)
    later_request = _matched_request(later_alias)
    later_request.update(
        {
            "request_id": "later-listener-request",
            "public_token": "later-listener-token",
            "name": "Giulia",
        }
    )
    state = StationState(
        playlist=[first, ordinary],
        pending_requests=[first_request, later_request],
        listeners_active=1,
    )
    config = _config(tmp_path)

    prompt, commit = _plan_listener_request_block(state)
    assert "La canzone che stai per suonare" in prompt
    assert commit is not None
    commit.apply(state)

    assert first_request not in state.pending_requests
    assert later_request in state.pending_requests
    assert state.listener_request_handoff is not None
    assert state.listener_request_handoff.request_id == first_request["request_id"]
    assert _plan_listener_request_block(state) == ("", None)

    selected = _select_accepted_music_track(state, config)
    assert selected is first
    assert state.pinned_track is None

    rendered = tmp_path / "promised-song.mp3"
    rendered.write_bytes(b"music")
    promised = Segment(
        type=SegmentType.MUSIC,
        path=rendered,
        duration_sec=180.0,
        metadata={
            "artist": first.artist,
            "title_only": first.title,
            **state.listener_request_handoff_metadata(first),
        },
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=promised):
        admitted = await _enqueue_with_egress(
            queue,
            state,
            config,
            promised,
            admission_callback=state.admit_listener_request_handoff,
        )

    assert admitted is True
    assert state.listener_request_handoff is None
    assert _segment_is_listener_reserved(state, promised) is False
    unmarked_alias = Segment(
        type=SegmentType.MUSIC,
        path=tmp_path / "unmarked-alias.mp3",
        metadata={"artist": later_alias.artist, "title_only": later_alias.title},
    )
    assert _segment_is_listener_reserved(state, unmarked_alias) is True
    assert _select_accepted_music_track(state, config) is ordinary


@pytest.mark.asyncio
async def test_failed_handoff_admission_retains_exact_track_for_retry(tmp_path):
    requested = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=180_000,
        youtube_id="retry-listener-recording",
    )
    later = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=180_000,
        youtube_id="later-listener-recording",
    )
    first_request = _matched_request(requested)
    later_request = _matched_request(later)
    later_request["request_id"] = "later-request"
    state = StationState(
        playlist=[requested],
        pending_requests=[first_request, later_request],
        listeners_active=1,
    )
    config = _config(tmp_path)

    _, commit = _plan_listener_request_block(state)
    assert commit is not None
    commit.apply(state)
    assert _select_accepted_music_track(state, config) is requested

    rendered = tmp_path / "rejected-promised-song.mp3"
    rendered.write_bytes(b"music")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=rendered,
        metadata={
            "artist": requested.artist,
            "title_only": requested.title,
            **state.listener_request_handoff_metadata(requested),
        },
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    admitted = await _enqueue_with_egress(
        queue,
        state,
        config,
        segment,
        stale_check=lambda: GenerationWasteReason.STALE_SOURCE,
        admission_callback=state.admit_listener_request_handoff,
    )

    assert admitted is False
    assert queue.empty()
    assert state.listener_request_handoff is not None
    assert _select_accepted_music_track(state, config) is requested


def test_session_rejected_handoff_releases_later_listener_request(tmp_path):
    from mammamiradio.playlist.downloader import clear_rejected_cache_keys, reject_cached_download

    requested = Track(
        title="Unplayable Promise",
        artist="First Artist",
        duration_ms=180_000,
        youtube_id="unplayable-listener-recording",
    )
    later = Track(
        title="Unplayable Promise",
        artist="First Artist",
        duration_ms=180_000,
        youtube_id="unplayable-listener-recording",
    )
    ordinary = Track(title="Ordinary", artist="Rotation", duration_ms=180_000, youtube_id="ordinary")
    first_request = _matched_request(requested)
    later_request = _matched_request(later)
    later_request["request_id"] = "later-request"
    state = StationState(
        playlist=[requested, ordinary],
        pending_requests=[first_request, later_request],
        listeners_active=1,
    )
    config = _config(tmp_path)

    _, commit = _plan_listener_request_block(state)
    assert commit is not None
    commit.apply(state)
    assert state.listener_request_handoff is not None

    clear_rejected_cache_keys()
    try:
        reject_cached_download(tmp_path, requested.cache_key, "source unavailable")
        assert _select_accepted_music_track(state, config) is ordinary
    finally:
        clear_rejected_cache_keys()

    assert state.listener_request_handoff is None
    later_prompt, later_commit = _plan_listener_request_block(state)
    assert "LISTENER REQUEST (SONG UNAVAILABLE):" in later_prompt
    assert later_commit is not None
    assert later_request["song_error_reason"] == "download_failed"


def test_same_title_different_media_cannot_consume_listener_handoff(tmp_path):
    album_version = Track(
        title="High",
        artist="Lighthouse Family",
        duration_ms=180_000,
        youtube_id="album-version",
    )
    live_version = Track(
        title="High",
        artist="Lighthouse Family",
        duration_ms=240_000,
        youtube_id="live-version",
    )
    request = _matched_request(album_version)
    state = StationState(
        playlist=[album_version, live_version],
        pending_requests=[request],
        listeners_active=1,
    )
    config = _config(tmp_path)

    _, commit = _plan_listener_request_block(state)
    assert commit is not None
    commit.apply(state)
    state.pinned_track = live_version
    # Model the producer cycle after it consumes the promised MUSIC force.
    state.force_next = None

    assert _select_accepted_music_track(state, config) is live_version
    assert state.listener_request_handoff_metadata(live_version) == {}
    assert state.listener_request_handoff is not None
    assert state.listener_request_handoff.track is album_version


@pytest.mark.asyncio
async def test_later_reserved_alias_waits_behind_exact_active_handoff(tmp_path):
    album_version = Track(
        title="High",
        artist="Lighthouse Family",
        duration_ms=180_000,
        youtube_id="album-version",
    )
    live_version = Track(
        title="High",
        artist="Lighthouse Family",
        duration_ms=240_000,
        youtube_id="live-version",
    )
    ordinary = Track(title="Ordinary", artist="Rotation", duration_ms=180_000, youtube_id="ordinary")
    first_request = _matched_request(album_version)
    later_request = _matched_request(live_version)
    later_request["request_id"] = "later-live-request"
    state = StationState(
        playlist=[album_version, live_version, ordinary],
        pending_requests=[first_request, later_request],
        listeners_active=1,
    )
    config = _config(tmp_path)

    _, commit = _plan_listener_request_block(state)
    assert commit is not None
    commit.apply(state)
    state.pinned_track = live_version
    state.force_next = None

    with patch("mammamiradio.core.models.random.choices", return_value=[ordinary]):
        assert _select_accepted_music_track(state, config) is album_version
    assert state.pinned_track is live_version
    assert state.force_next is None
    assert state.listener_request_handoff_metadata(album_version)
    assert state.listener_request_handoff_metadata(live_version) == {}

    rendered = tmp_path / "rejected-album-version.mp3"
    rendered.write_bytes(b"music")
    segment = Segment(
        type=SegmentType.MUSIC,
        path=rendered,
        metadata={
            "artist": album_version.artist,
            "title_only": album_version.title,
            **state.listener_request_handoff_metadata(album_version),
        },
    )
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)
    admitted = await _enqueue_with_egress(
        queue,
        state,
        config,
        segment,
        stale_check=lambda: GenerationWasteReason.STALE_SOURCE,
        admission_callback=state.admit_listener_request_handoff,
    )

    assert admitted is False
    assert state.listener_request_handoff is not None
    assert state.pinned_track is live_version
    assert state.force_next is None
    with patch("mammamiradio.core.models.random.choices", return_value=[ordinary]):
        assert _select_accepted_music_track(state, config) is album_version


def test_banning_textual_alias_revokes_exact_source_handoff(tmp_path):
    album_version = Track(
        title="High",
        artist="Lighthouse Family",
        duration_ms=180_000,
        youtube_id="album-version",
    )
    live_version = Track(
        title="High",
        artist="Lighthouse Family",
        duration_ms=240_000,
        youtube_id="live-version",
    )
    ordinary = Track(title="Ordinary", artist="Rotation", duration_ms=180_000, youtube_id="ordinary")
    request = _matched_request(album_version)
    state = StationState(
        playlist=[album_version, live_version, ordinary],
        pending_requests=[request],
        listeners_active=1,
    )
    config = _config(tmp_path)

    _, commit = _plan_listener_request_block(state)
    assert commit is not None
    commit.apply(state)
    assert state.listener_request_handoff is not None

    result = _apply_ban(state, config, [live_version])

    assert result["removed"] == 2
    assert state.listener_request_handoff is None
    assert _select_accepted_music_track(state, config) is ordinary


@pytest.mark.parametrize("front_insert", [False, True])
@pytest.mark.asyncio
async def test_ban_during_matched_promise_rejects_normal_and_front_insert_admission(tmp_path, front_insert):
    requested = Track(
        title="Più bella cosa",
        artist="Eros Ramazzotti",
        duration_ms=240_000,
        youtube_id="matched-request",
    )
    ordinary = Track(title="Ordinary", artist="Rotation", duration_ms=180_000, youtube_id="ordinary")
    state = StationState(playlist=[requested, ordinary], listeners_active=1)
    request = _matched_request(requested)
    state.pending_requests.append(request)
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)

    prompt, commit = _plan_listener_request_block(state)
    assert "La canzone che stai per suonare" in prompt
    assert commit is not None
    assert state.pinned_track is requested
    assert request["song_pinned"] is True

    rendered = tmp_path / "matched-promise.mp3"
    rendered.write_bytes(b"rendered")
    segment = Segment(type=SegmentType.BANTER, path=rendered, metadata={"title": "Listener dedication"})

    async def _ban_during_egress(candidate: Segment, _config) -> Segment:
        _apply_ban(state, config, [requested], queue=queue)
        return candidate

    with patch(f"{PRODUCER_MODULE}._apply_egress", side_effect=_ban_during_egress):
        admitted = await _enqueue_with_egress(
            queue,
            state,
            config,
            segment,
            front_insert=front_insert,
            shadow_entry={"id": "dedication", "type": "banter", "label": "Listener dedication"},
            stale_check=lambda: _listener_request_plan_stale_reason(state, commit),
        )

    assert admitted is False
    assert queue.empty()
    assert state.discard_by_reason == {GenerationWasteReason.EGRESS_STALE: 1}
    _abandon_banter_commit(state, commit)
    assert request in state.pending_requests
    assert request["song_found"] is False
    assert request["song_error"] is True
    assert request["song_error_reason"] == "banned"
    assert request["song_pinned"] is False
    assert state.pinned_track is None
    assert state.force_next is None

    truthful_prompt, truthful_commit = _plan_listener_request_block(state)
    assert "LISTENER REQUEST (SONG UNAVAILABLE):" in truthful_prompt
    assert truthful_commit is not None


@pytest.mark.asyncio
async def test_fifth_timeout_late_match_then_ban_stays_pending_for_truthful_ack(tmp_path):
    requested = Track(
        title="Più bella cosa",
        artist="Eros Ramazzotti",
        duration_ms=240_000,
        youtube_id="late-match",
    )
    ordinary = Track(title="Ordinary", artist="Rotation", duration_ms=180_000, youtube_id="ordinary")
    state = StationState(playlist=[ordinary], listeners_active=1)
    request = {
        "request_id": "late-listener-request",
        "public_token": "late-listener-token",
        "name": "Luca",
        "message": "Play Più bella cosa by Eros Ramazzotti",
        "type": "song_request",
        "status": "queued",
        "song_found": False,
        "song_error": False,
        "song_error_reason": "",
        "song_pinned": False,
        "song_track": None,
        "song_track_obj": None,
        "banter_cycles_missed": 4,
    }
    state.pending_requests.append(request)
    config = _config(tmp_path)
    queue: asyncio.Queue[Segment] = asyncio.Queue(maxsize=4)

    timeout_prompt, timeout_commit = _plan_listener_request_block(state)
    assert "LOOKUP STILL PENDING" in timeout_prompt
    assert timeout_commit is not None

    # The lookup finishes during rendering, then the operator bans that exact
    # canonical song before admission. The timeout greeting may not archive the
    # now-known ban, and the matched song may not regain its pin.
    state.playlist.append(requested)
    state.pinned_track = requested
    state.force_next = SegmentType.MUSIC
    request.update(
        {
            "song_found": True,
            "song_track": requested.display,
            "song_track_obj": requested,
            "song_pinned": True,
        }
    )
    _apply_ban(state, config, [requested], queue=queue)

    rendered = tmp_path / "pending-lookup.mp3"
    rendered.write_bytes(b"rendered")
    segment = Segment(type=SegmentType.BANTER, path=rendered, metadata={"title": "Pending request greeting"})
    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=segment) as egress:
        admitted = await _enqueue_with_egress(
            queue,
            state,
            config,
            segment,
            stale_check=lambda: _listener_request_plan_stale_reason(state, timeout_commit),
        )

    assert admitted is False
    egress.assert_not_awaited()  # rejected at the final pre-egress admission gate
    _abandon_banter_commit(state, timeout_commit)
    assert request in state.pending_requests
    assert state.recently_consumed_requests == []
    assert request["song_error_reason"] == "banned"

    ack_prompt, ack_commit = _plan_listener_request_block(state)
    assert "LISTENER REQUEST (SONG UNAVAILABLE):" in ack_prompt
    assert ack_commit is not None
    ack_commit.apply(state)
    assert request not in state.pending_requests
    assert state.recently_consumed_requests[-1]["status"] == "song_not_found"
    assert state.recently_consumed_requests[-1]["song_error_reason"] == "banned"


@pytest.mark.asyncio
async def test_post_capacity_pin_change_retracts_copy_and_preserves_newer_operator_pin(tmp_path):
    requested = Track(
        title="Somewhere I Belong",
        artist="Linkin Park",
        duration_ms=200_000,
        youtube_id="listener-pick",
    )
    operator_pick = Track(
        title="Operator Pick",
        artist="Operator",
        duration_ms=180_000,
        youtube_id="operator-pick",
    )
    state = StationState(playlist=[requested, operator_pick], listeners_active=1)
    request = _matched_request(requested)
    state.pending_requests.append(request)
    config = _config(tmp_path)

    _, commit = _plan_listener_request_block(state)
    assert commit is not None
    assert request["song_pinned"] is True
    assert state.pinned_track is requested

    blocker_path = tmp_path / "blocker.mp3"
    blocker_path.write_bytes(b"blocker")
    blocker = Segment(type=SegmentType.MUSIC, path=blocker_path, metadata={"title": "Blocker"})
    rendered = tmp_path / "dedication.mp3"
    rendered.write_bytes(b"dedication")
    candidate = Segment(type=SegmentType.BANTER, path=rendered, metadata={"title": "Dedication"})
    put_started = asyncio.Event()

    class ObservedQueue(asyncio.Queue[Segment]):
        async def put(self, item: Segment) -> None:
            if item is candidate:
                put_started.set()
            await super().put(item)

    queue = ObservedQueue(maxsize=1)
    queue.put_nowait(blocker)

    with patch(f"{PRODUCER_MODULE}._apply_egress", new_callable=AsyncMock, return_value=candidate):
        enqueue_task = asyncio.create_task(
            _enqueue_with_egress(
                queue,
                state,
                config,
                candidate,
                shadow_entry={"id": "dedication", "type": "banter", "label": "Dedication"},
                stale_check=lambda: _listener_request_plan_stale_reason(state, commit),
            )
        )
        await asyncio.wait_for(put_started.wait(), timeout=1.0)
        # A newer operator action replaces the listener pin while queue.put is
        # waiting for capacity. The post-capacity gate must retract the already
        # inserted dedication before publishing its shadow or applying commits.
        state.pinned_track = operator_pick
        state.force_next = SegmentType.MUSIC
        assert queue.get_nowait() is blocker
        queue.task_done()
        admitted = await asyncio.wait_for(enqueue_task, timeout=1.0)

    assert admitted is False
    assert queue.empty()
    assert state.queued_segments == []
    assert state.discard_by_reason == {GenerationWasteReason.EGRESS_STALE: 1}
    _abandon_banter_commit(state, commit)
    assert request in state.pending_requests
    assert request["song_pinned"] is False
    assert state.pinned_track is operator_pick
    assert state.force_next is SegmentType.MUSIC

    waiting_prompt, waiting_commit = _plan_listener_request_block(state)
    assert waiting_prompt == ""
    assert waiting_commit is None
    state.pinned_track = None
    state.force_next = None
    retry_prompt, retry_commit = _plan_listener_request_block(state)
    assert "La canzone che stai per suonare" in retry_prompt
    assert retry_commit is not None
    assert request["song_pinned"] is True
    assert state.pinned_track is requested
