"""Status payload helper extraction guards."""

from __future__ import annotations

from mammamiradio.core.models import Heading, SegmentLogEntry, StationState, Track
from mammamiradio.web import status_payload, streamer

_MOVED_HELPERS = (
    "_page_bounds",
    "_has_any_mp3",
    "_cached_cache_size_mb",
    "_golden_path_status",
    "_ha_details_payload",
    "_serialize_source",
    "_heading_playlist_track_count",
    "_serialize_heading",
    "_serialize_brand",
    "_serialize_track",
    "_paginated_tracks",
    "_duration_sec_from_payload",
    "_public_segment_metadata",
    "_public_now_streaming_payload",
    "_status_now_playback",
    "_serialize_stream_log_entry",
)


def test_streamer_facade_reexports_status_payload_helpers():
    for name in _MOVED_HELPERS:
        assert getattr(streamer, name) is getattr(status_payload, name)


def test_status_payload_does_not_own_runtime_live_clock_helpers():
    for name in (
        "_public_status_payload",
        "_runtime_monotonic",
        "_queue_empty_elapsed",
        "_silence_with_listeners",
        "LiveStreamHub",
        "run_playback_loop",
    ):
        assert not hasattr(status_payload, name)


def test_page_bounds_clamps_offset_and_limit():
    assert status_payload._page_bounds("-4", "999", default_limit=20, max_limit=50) == (0, 50)
    assert status_payload._page_bounds("bad", "bad", default_limit=20, max_limit=50) == (0, 20)


def test_paginated_tracks_serializes_page_and_revision():
    tracks = [
        Track(title=f"Song {i}", artist="Artist", duration_ms=180_000, spotify_id=f"id-{i}", year=2000 + i)
        for i in range(3)
    ]

    payload = status_payload._paginated_tracks(tracks, 1, 1, revision=7)

    assert payload == {
        "tracks": [
            {
                "title": "Song 1",
                "artist": "Artist",
                "display": "Artist – Song 1",
                "spotify_id": "id-1",
                "album_art": "",
                "source": "youtube",
                "year": 2001,
                "youtube_id": "",
                "duration_ms": 180_000,
            }
        ],
        "total": 3,
        "offset": 1,
        "limit": 1,
        "has_more": True,
        "revision": 7,
    }


def test_status_now_playback_redacts_internal_metadata_and_reports_progress():
    now_streaming = {
        "title": "Song",
        "started": 10.0,
        "metadata": {
            "duration_ms": 90_000,
            "memory_extraction": {"private": True},
            "public": {"nested": ["ok"]},
        },
    }

    payload = status_payload._status_now_playback(now_streaming, 25.3)

    assert payload["current_progress_sec"] == 15.3
    assert payload["current_duration_sec"] == 90.0
    assert payload["now_streaming"]["metadata"] == {
        "duration_ms": 90_000,
        "public": {"nested": ["ok"]},
    }
    assert payload["now_streaming"]["metadata"] is not now_streaming["metadata"]


def test_serialize_stream_log_entry_uses_metadata_duration_fallback():
    entry = SegmentLogEntry(
        type="music",
        label="Artist - Song",
        timestamp=123.0,
        metadata={"duration_s": 12.5, "memory_extraction": {"private": True}, "source": "youtube"},
    )

    payload = status_payload._serialize_stream_log_entry(entry)

    assert payload == {
        "type": "music",
        "label": "Artist - Song",
        "timestamp": 123.0,
        "metadata": {"duration_s": 12.5, "source": "youtube"},
        "duration_sec": 12.5,
        "duration_ms": 12500,
    }


def test_public_segment_metadata_redacts_private_ritual_internals():
    metadata = {
        "source": "banter",
        "ritual_families": ["Kitchen ritual"],
        "ritual_recipe_matches": [{"entity_id": "binary_sensor.kitchen_fridge_door"}],
        "ritual_directive": "Mention the exact fridge door.",
    }

    payload = status_payload._public_segment_metadata(metadata)

    assert payload == {
        "source": "banter",
        "ritual_families": ["Kitchen ritual"],
    }


def test_ha_details_payload_absent_without_ha_observability():
    assert status_payload._ha_details_payload(StationState()) is None


def test_ha_details_payload_serializes_present_observability():
    state = StationState()
    state.ha_context = "Kitchen light on"
    state.ha_home_mood = "cooking"
    state.ha_weather_arc = "rainy"
    state.ha_events_summary = "Kitchen changed"
    state.ha_pending_directive = "mention kitchen"
    state.ha_recent_event_count = 3
    state.ha_last_event_label = "Kitchen"
    state.ha_scored_entities = [{"entity_id": f"sensor.{i}"} for i in range(20)]
    state.ha_denylist_hits = {"sensor.hidden": 2}
    state.ha_ritual_public_families = ["Kitchen ritual"]
    state.ha_ritual_matches = [{"recipe_id": "fridge_freezer_raid", "entity_id": "binary_sensor.fridge"}]
    state.ha_ritual_recipe_audit = [{"recipe_id": "chores_reminders", "status": "opportunity"}]

    payload = status_payload._ha_details_payload(state)

    assert payload is not None
    assert payload["mood"] == "cooking"
    assert payload["weather_arc"] == "rainy"
    assert payload["events_summary"] == "Kitchen changed"
    assert payload["pending_directive"] == "mention kitchen"
    assert payload["recent_event_count"] == 3
    assert payload["last_event_label"] == "Kitchen"
    assert len(payload["scored_entities"]) == 12
    assert payload["denylist_hits"] == {"sensor.hidden": 2}
    assert payload["rituals"]["public_families"] == ["Kitchen ritual"]
    assert payload["rituals"]["matches"][0]["recipe_id"] == "fridge_freezer_raid"
    assert payload["rituals"]["audit"][0]["status"] == "opportunity"


def test_serialize_heading_reports_resolving_until_track_tagged():
    state = StationState(playlist=[Track(title="Base", artist="Artist", duration_ms=1)])
    heading = Heading(
        "h1",
        "direction://x",
        "X",
        1.0,
        "operator",
        selection_budget=2,
        targets=[{"artist": "A", "title": "B"}],
    )

    payload = status_payload._serialize_heading(heading, state)

    assert payload["phase"] == "hunting"
    assert payload["tagged_count"] == 0
    assert payload["resolving"] is True

    state.playlist[0].heading_id = "h1"
    payload = status_payload._serialize_heading(heading, state)

    assert payload["phase"] == "steering"
    assert payload["tagged_count"] == 1
    assert payload["resolving"] is False


def test_golden_path_status_uses_new_module_globals(monkeypatch):
    class Config:
        anthropic_api_key = ""
        openai_api_key = ""

    monkeypatch.setattr(status_payload, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload, "_golden_path_cache_ts", 0.0)
    monkeypatch.setattr(status_payload, "_has_any_mp3", lambda _path: False)
    monkeypatch.setenv("MAMMAMIRADIO_ALLOW_YTDLP", "true")

    payload = status_payload._golden_path_status(Config(), StationState())

    assert payload["stage"] == "music_available"
    assert "yt-dlp downloads" in payload["fallback_sources"]
