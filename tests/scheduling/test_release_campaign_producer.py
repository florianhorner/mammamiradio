from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import SegmentType, StationState, Track
from mammamiradio.hosts.memory_extractor import MemoryExtractionCommit
from mammamiradio.scheduling.producer import (
    _abandon_release_beat_commit,
    _memory_extraction_metadata_from_commit,
    _release_beat_metadata_from_commit,
    _release_campaign_should_force_first_banter,
    run_producer,
)

TOML_PATH = str(Path(__file__).resolve().parents[2] / "radio.toml")
PRODUCER_MODULE = "mammamiradio.scheduling.producer"
SCRIPTWRITER_MODULE = "mammamiradio.hosts.scriptwriter"


class _Campaign:
    def __init__(self, *, aired_count=0, due=True):
        self.ledger = SimpleNamespace(aired_count=aired_count)
        self.due = due
        self.abandoned = []
        self.in_flight_abandoned = 0

    def is_due(self):
        return self.due and not self.abandoned

    def abandon_attempt(self, *, attempt_id):
        self.abandoned.append(attempt_id)

    def abandon_in_flight(self):
        self.in_flight_abandoned += 1


class _ReleaseCommit:
    release_beat_used = True
    attempt_id = "attempt-1"

    def segment_metadata(self):
        return {"release_beat_id": "beat-1", "release_beat_attempt_id": self.attempt_id}

    def abandon(self, state):
        state.release_campaign.abandon_attempt(attempt_id=self.attempt_id)


def test_release_campaign_forces_only_first_due_banter():
    state = StationState(release_campaign=_Campaign(aired_count=0, due=True))
    assert _release_campaign_should_force_first_banter(state) is True

    state.release_campaign = _Campaign(aired_count=1, due=True)
    assert _release_campaign_should_force_first_banter(state) is False


def test_release_campaign_force_yields_to_home_directive():
    state = StationState(release_campaign=_Campaign(aired_count=0, due=True), ha_pending_directive="react now")
    assert _release_campaign_should_force_first_banter(state) is False


def test_release_campaign_force_swallows_is_due_exception():
    """is_due() is called inside a bare `except Exception` — a raising campaign
    must not force banter (or propagate) rather than fail safe."""

    class _BoomCampaign:
        ledger = SimpleNamespace(aired_count=0)

        def is_due(self):
            raise RuntimeError("disk error")

    state = StationState(release_campaign=_BoomCampaign())
    assert _release_campaign_should_force_first_banter(state) is False


def test_release_beat_metadata_and_abandon_helpers():
    state = StationState(release_campaign=_Campaign())
    commit = SimpleNamespace(release_beat=_ReleaseCommit())

    assert _release_beat_metadata_from_commit(commit) == {
        "release_beat_id": "beat-1",
        "release_beat_attempt_id": "attempt-1",
    }

    _abandon_release_beat_commit(state, commit)
    assert state.release_campaign.abandoned == ["attempt-1"]


def test_memory_extraction_metadata_helper_uses_final_aired_script():
    commit = SimpleNamespace(
        memory_extraction=MemoryExtractionCommit(
            script_lines=[{"host": "Marco", "text": "draft"}],
            persona_context="existing memory",
            interaction_context={"reactive_directive": "door opened"},
            youtube_id="yt-final",
            source_session=3,
        )
    )

    metadata = _memory_extraction_metadata_from_commit(
        commit,
        [
            {"host": "Sofia", "text": "Allora...", "type": "transition"},
            {"host": "Marco", "text": "final aired line"},
        ],
    )

    payload = metadata["memory_extraction"]
    assert payload["script_lines"] == [
        {"host": "Sofia", "text": "Allora...", "type": "transition"},
        {"host": "Marco", "text": "final aired line"},
    ]
    assert payload["youtube_id"] == "yt-final"
    assert payload["source_session"] == 3


@pytest.mark.asyncio
async def test_release_beat_attempt_restored_when_banter_tts_fails(tmp_path):
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    campaign = _Campaign()
    state = StationState(
        listeners_active=1,
        playlist=[Track(title="Canzone", artist="Artista", duration_ms=180_000, spotify_id="song1")],
        release_campaign=campaign,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    host = config.hosts[0]
    commit = SimpleNamespace(release_beat=_ReleaseCommit())

    def _concat(_inputs, output, *_args, **_kwargs) -> None:
        Path(output).write_bytes(b"audio")

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(f"{SCRIPTWRITER_MODULE}.write_transition", new_callable=AsyncMock, return_value=(host, "Allora...")),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            side_effect=[
                ([(host, "Novita!")], commit),
                ([(host, "Secondo tentativo.")], None),
            ],
        ),
        patch(f"{PRODUCER_MODULE}.synthesize", new_callable=AsyncMock, return_value=tmp_path / "transition.mp3"),
        patch(f"{PRODUCER_MODULE}._try_crossfade", new_callable=AsyncMock, return_value=tmp_path / "transition.mp3"),
        patch(
            f"{PRODUCER_MODULE}.synthesize_dialogue",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("tts down"), tmp_path / "banter.mp3"],
        ),
        patch(f"{PRODUCER_MODULE}.concat_files", side_effect=_concat),
        patch(f"{PRODUCER_MODULE}.validate_segment_audio", return_value=None),
        patch(f"{PRODUCER_MODULE}._probe_segment_duration", return_value=12.0),
        patch(
            f"{PRODUCER_MODULE}._apply_talk_bed", new_callable=AsyncMock, side_effect=lambda audio, *_a, **_kw: audio
        ),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while queue.empty():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not queue after release-beat TTS failure")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.CancelledError:
                pass

    assert campaign.abandoned == ["attempt-1"]
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_release_beat_attempt_restored_when_transition_raises_in_gather(tmp_path):
    """F5: write_banter succeeds (opening an attempt) but its sibling
    write_transition raises inside the same asyncio.gather. The tuple never
    unpacks, so no commit survives — the producer's commit-free
    abandon_in_flight() safety net must restore the campaign."""
    config = load_config(TOML_PATH)
    config.pacing.lookahead_segments = 1
    config.homeassistant.enabled = False
    config.tmp_dir = tmp_path
    config.cache_dir = tmp_path
    campaign = _Campaign()
    state = StationState(
        listeners_active=1,
        playlist=[Track(title="Canzone", artist="Artista", duration_ms=180_000, spotify_id="song1")],
        release_campaign=campaign,
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    host = config.hosts[0]

    with (
        patch(f"{PRODUCER_MODULE}.next_segment_type", return_value=SegmentType.BANTER),
        patch(f"{SCRIPTWRITER_MODULE}.has_script_llm", return_value=True),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_transition",
            new_callable=AsyncMock,
            side_effect=RuntimeError("transition writer down"),
        ),
        patch(
            f"{SCRIPTWRITER_MODULE}.write_banter",
            new_callable=AsyncMock,
            return_value=([(host, "Novita!")], SimpleNamespace(release_beat=_ReleaseCommit())),
        ),
        patch(f"{PRODUCER_MODULE}._prefetch_next", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.fetch_home_context", new_callable=AsyncMock),
        patch(f"{PRODUCER_MODULE}.check_reactive_triggers", return_value=None),
    ):
        task = asyncio.create_task(run_producer(queue, state, config))
        try:
            deadline = asyncio.get_event_loop().time() + 5.0
            while campaign.in_flight_abandoned == 0:
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Producer did not restore the stranded release-beat attempt")
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.CancelledError:
                pass

    assert campaign.in_flight_abandoned >= 1
    assert queue.qsize() == 0
