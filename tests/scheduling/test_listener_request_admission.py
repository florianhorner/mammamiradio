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
    _abandon_release_beat_commit,
    _enqueue_with_egress,
    _listener_request_plan_stale_reason,
)
from mammamiradio.web.streamer import _apply_ban

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
    state = StationState(playlist=[requested], listeners_active=1)
    request = _matched_request(requested)
    state.pending_requests.append(request)
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
    assert state.recently_consumed_requests[-1]["status"] == "sent_to_hosts"
    assert state.recently_consumed_requests[-1]["song_found"] is True


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
    _abandon_release_beat_commit(state, commit)
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
    _abandon_release_beat_commit(state, timeout_commit)
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
    _abandon_release_beat_commit(state, commit)
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
