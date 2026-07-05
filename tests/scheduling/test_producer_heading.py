"""Selection-driven heading announcement guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import Heading, Segment, SegmentType, StationState, Track
from mammamiradio.hosts.scriptwriter import write_banter
from mammamiradio.playlist.playlist import read_persisted_heading
from mammamiradio.scheduling.producer import (
    _arm_accepted_heading_announcement,
    _enqueue_with_egress,
    _select_accepted_music_track,
)


def _track(title: str, *, heading_id: str = "") -> Track:
    return Track(title=title, artist="Artist", duration_ms=180_000, spotify_id=title, heading_id=heading_id)


def _heading() -> Heading:
    return Heading(
        id="heading-80s",
        seed="classic://italian/80s",
        label="Anni '80",
        set_at=1.0,
        set_by="operator",
    )


def _producer_config():
    return SimpleNamespace(playlist=SimpleNamespace(repeat_cooldown=8, artist_cooldown=3))


def test_accepted_tagged_selection_arms_heading_once():
    heading = _heading()
    state = StationState(playlist=[_track("Vibe", heading_id=heading.id)], heading=heading)

    with patch("mammamiradio.scheduling.producer.is_rejected_cache_key", return_value=False):
        selected = _select_accepted_music_track(state, _producer_config())

    assert selected is not None
    _arm_accepted_heading_announcement(state, selected)
    assert selected.heading_id == heading.id
    assert state.heading_pending_announcement == "Anni '80"
    state.heading_announced_id = heading.id
    state.heading_pending_announcement = ""

    with patch("mammamiradio.scheduling.producer.is_rejected_cache_key", return_value=False):
        selected = _select_accepted_music_track(state, _producer_config())

    assert selected is not None
    _arm_accepted_heading_announcement(state, selected)
    assert state.heading_pending_announcement == ""


def test_rejected_tagged_candidate_then_auto_track_does_not_arm_heading():
    heading = _heading()
    tagged = _track("Vibe", heading_id=heading.id)
    auto = _track("Auto")
    state = StationState(playlist=[tagged, auto], heading=heading)

    with (
        patch.object(state, "select_next_track", side_effect=[tagged, auto]) as select_next,
        patch(
            "mammamiradio.scheduling.producer.is_rejected_cache_key",
            side_effect=lambda key: key == tagged.cache_key,
        ),
    ):
        selected = _select_accepted_music_track(state, _producer_config())

    assert selected is auto
    _arm_accepted_heading_announcement(state, selected)
    assert select_next.call_count == 2
    assert state.heading_pending_announcement == ""
    assert state.heading_announced_id == ""


def test_non_tagged_accepted_selection_does_not_arm_heading():
    state = StationState(playlist=[_track("Auto")], heading=_heading())

    with patch("mammamiradio.scheduling.producer.is_rejected_cache_key", return_value=False):
        selected = _select_accepted_music_track(state, _producer_config())

    assert selected is not None
    _arm_accepted_heading_announcement(state, selected)
    assert state.heading_pending_announcement == ""


def test_cleared_heading_does_not_arm():
    state = StationState(playlist=[_track("Old Vibe", heading_id="heading-80s")], heading=None)

    with patch("mammamiradio.scheduling.producer.is_rejected_cache_key", return_value=False):
        selected = _select_accepted_music_track(state, _producer_config())

    assert selected is not None
    _arm_accepted_heading_announcement(state, selected)
    assert state.heading_pending_announcement == ""


@pytest.mark.asyncio
async def test_write_banter_prompt_frames_heading_as_request_mood(tmp_path):
    config = load_config()
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    config.cache_dir = tmp_path
    config.party_mode = None
    heading = _heading()
    state = StationState(heading=heading, heading_pending_announcement=heading.label)
    captured: dict[str, str] = {}

    async def _capture_prompt(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Qualcuno aveva voglia di anni ottanta."}]}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_capture_prompt):
        _, commit = await write_banter(state, config)

    course_block = captured["prompt"].split("RECORD HUNT:", 1)[1].split("Return JSON:", 1)[0].lower()
    assert "digging through lp/cd crates" in course_block
    assert "steering" in course_block
    assert "exact next song" in course_block
    assert "now playing" not in course_block
    assert "tornando ora" not in course_block
    assert "has just turned" not in course_block
    assert "going back" not in course_block
    assert state.heading_pending_announcement == ""
    assert state.heading_pending_narration_kind == ""
    assert state.heading_announced_id == ""
    assert state.heading is not None
    assert state.heading.announced is False
    assert read_persisted_heading(tmp_path) is None
    assert commit is not None


@pytest.mark.asyncio
async def test_hunt_start_notice_does_not_mark_first_record_announced(tmp_path):
    config = load_config()
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    config.cache_dir = tmp_path
    config.party_mode = None
    heading = _heading()
    state = StationState(
        heading=heading,
        heading_pending_announcement=heading.label,
        heading_pending_narration_kind="hunt_start",
    )

    async def _capture_prompt(**kwargs):
        return {"lines": [{"host": config.hosts[0].name, "text": "Stiamo scavando nelle casse."}]}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_capture_prompt):
        _, commit = await write_banter(state, config)

    assert commit is not None
    commit.apply(state, config)
    assert state.heading is not None
    assert state.heading.hunt_started_announced is True
    assert state.heading.announced is False
    assert state.heading_announced_id == ""
    assert read_persisted_heading(tmp_path) == state.heading


@pytest.mark.asyncio
async def test_banter_notice_still_queues_after_heading_clear(tmp_path):
    config = load_config()
    config.cache_dir = tmp_path
    config.tmp_dir = tmp_path
    heading = _heading()
    state = StationState(heading=heading)
    audio_path = tmp_path / "banter.mp3"
    audio_path.write_bytes(b"audio")
    segment = Segment(
        type=SegmentType.BANTER,
        path=audio_path,
        metadata={"type": "banter"},
        ephemeral=True,
    )
    state.heading = None
    queue: asyncio.Queue[Segment] = asyncio.Queue()

    with patch("mammamiradio.scheduling.producer._apply_egress", return_value=segment):
        queued = await _enqueue_with_egress(queue, state, config, segment)

    assert queued is True
    assert await queue.get() is segment
    assert audio_path.exists()


@pytest.mark.asyncio
async def test_discarded_heading_notice_rearms_but_queued_notice_spends_once(tmp_path):
    config = load_config()
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    config.cache_dir = tmp_path
    config.party_mode = None
    heading = _heading()
    tagged = _track("Vibe", heading_id=heading.id)
    state = StationState(playlist=[tagged], heading=heading, heading_pending_announcement=heading.label)

    async def _capture_prompt(**kwargs):
        return {"lines": [{"host": config.hosts[0].name, "text": "Qualcuno aveva voglia di anni ottanta."}]}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_capture_prompt):
        _, stale_commit = await write_banter(state, config)

    assert state.heading_pending_announcement == ""
    assert state.heading_pending_narration_kind == ""
    assert state.heading_announced_id == ""
    assert stale_commit is not None

    state.playlist_revision += 1
    _arm_accepted_heading_announcement(state, tagged)

    assert state.heading_pending_announcement == heading.label
    assert state.heading_pending_narration_kind == "first_found"
    assert state.heading_announced_id == ""
    assert state.heading is not None
    assert state.heading.announced is False
    assert read_persisted_heading(tmp_path) is None

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_capture_prompt):
        _, queued_commit = await write_banter(state, config)

    assert queued_commit is not None
    queued_commit.apply(state, config)

    assert state.heading_pending_announcement == ""
    assert state.heading_pending_narration_kind == ""
    assert state.heading_announced_id == heading.id
    assert state.heading is not None
    assert state.heading.announced is True
    assert read_persisted_heading(tmp_path) == state.heading

    _arm_accepted_heading_announcement(state, tagged)

    assert state.heading_pending_announcement == ""
