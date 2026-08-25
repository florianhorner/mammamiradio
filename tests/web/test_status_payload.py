"""Status payload helper extraction guards."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from mammamiradio.core.models import (
    LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY,
    LISTENER_REQUEST_HANDOFF_ADMITTED_KEY,
    LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY,
    LISTENER_REQUEST_HANDOFF_TOKEN_KEY,
    SEGMENT_PLAYLIST_SOURCE_KIND_KEY,
    URGENT_INTERRUPT_PRIORITY_KEY,
    Heading,
    MediaAttribution,
    PlaylistSource,
    Segment,
    SegmentLogEntry,
    SegmentType,
    SourceReadinessEvidence,
    StationState,
    Track,
)
from mammamiradio.web import status_payload, streamer

_MOVED_HELPERS = (
    "_admin_track_id",
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
                "id": "id-1",
                "title": "Song 1",
                "artist": "Artist",
                "display": "Artist – Song 1",
                "spotify_id": "id-1",
                "album_art": "",
                "source": "youtube",
                "year": 2001,
                "youtube_id": "",
                "duration_ms": 180_000,
                "heading_id": "",
            }
        ],
        "total": 3,
        "offset": 1,
        "limit": 1,
        "has_more": True,
        "revision": 7,
    }


def test_serialize_track_includes_heading_id_for_admin_hunt_rows():
    track = Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="id-1", heading_id="hunt-1")

    payload = status_payload._serialize_track(track)

    assert payload["heading_id"] == "hunt-1"


def test_serialize_track_includes_validated_music_attribution():
    track = Track(
        title="Carefree",
        artist="Kevin MacLeod",
        duration_ms=180_000,
        attribution=MediaAttribution(
            provider="incompetech",
            license_id="CC-BY-4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            source_url="https://incompetech.com/music/royalty-free/music.html",
            credit="Carefree by Kevin MacLeod, CC BY 4.0",
            modified=True,
            basis="bundled_manifest",
        ),
    )

    payload = status_payload._serialize_track(track)

    assert payload["music_attribution"]["provider"] == "incompetech"


def test_paginated_tracks_omits_revision_when_not_supplied():
    payload = status_payload._paginated_tracks([Track(title="Song", artist="Artist", duration_ms=180_000)], 0, 1)

    assert "revision" not in payload


def test_public_starter_catalog_serializes_only_listener_credit_facts(monkeypatch):
    entry = SimpleNamespace(
        title="Carefree",
        artist="Kevin MacLeod",
        isrc="USUAN1400037",
        license_id="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        official_piece_url="https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037",
        modification_notice="Pre-normalized for direct playback.",
        private_receipt="must not serialize",
    )
    monkeypatch.setattr(
        "mammamiradio.media.starter.load_starter_catalog",
        lambda *, require_complete: SimpleNamespace(entries=(entry,)) if require_complete else None,
    )

    assert status_payload._public_starter_catalog() == [
        {
            "title": "Carefree",
            "artist": "Kevin MacLeod",
            "isrc": "USUAN1400037",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "official_piece_url": "https://incompetech.com/music/royalty-free/index.html?isrc=USUAN1400037",
            "modification_notice": "Pre-normalized for direct playback.",
        }
    ]


def test_admin_track_id_prefers_trimmed_spotify_id_and_falls_back_to_cache_key():
    spotify_track = Track(title="Song", artist="Artist", duration_ms=180_000, spotify_id="  id-1  ")
    numeric_track = Track(title="Numeric", artist="Artist", duration_ms=180_000, spotify_id=123)  # type: ignore[arg-type]
    cache_track = Track(title="Fallback", artist="Artist", duration_ms=180_000, spotify_id="   ")

    assert status_payload._admin_track_id(spotify_track) == "id-1"
    assert status_payload._admin_track_id(numeric_track) == "123"
    assert status_payload._admin_track_id(cache_track) == cache_track.cache_key
    assert status_payload._serialize_track(cache_track)["id"] == cache_track.cache_key


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


def test_status_now_playback_exposes_only_safe_provider_attribution():
    attribution = {
        "provider": "jamendo",
        "license_id": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://www.jamendo.com/track/12345/example",
        "credit": "Artist - Track, provided by Jamendo, CC-BY-4.0",
        "modified": True,
        "basis": "provider_reported",
    }
    now_streaming = {
        "type": "music",
        "metadata": {
            "title": "Track",
            "source_kind": "jamendo",
            "provider_track_id": "12345",
            "lease_id": "private-lease",
            "operation_id": "private-operation",
            "artifact_path": "/tmp/jamendo/private/normalized.mp3",
            "music_attribution": attribution,
        },
    }

    payload = status_payload._status_now_playback(now_streaming, 25.3)
    metadata = payload["now_streaming"]["metadata"]

    assert metadata == {
        "title": "Track",
        "source_kind": "jamendo",
        "music_attribution": attribution,
    }


def test_public_segment_metadata_drops_malformed_attribution_and_copies_tuples():
    metadata = {
        "music_attribution": {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://storage.jamendo.com/download.mp3?token=private",
            "credit": "Private stream URL must not render",
            "modified": True,
            "basis": "provider_reported",
        },
        "public_tuple": ({"operation_id": "private", "label": "safe"},),
    }

    payload = status_payload._public_segment_metadata(metadata)

    assert payload == {"public_tuple": ({"label": "safe"},)}


def test_public_segment_metadata_rejects_non_mapping_input():
    assert status_payload._public_segment_metadata("not metadata") == {}


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


@pytest.mark.parametrize("invalid_duration", [math.inf, -math.inf, math.nan, True])
def test_duration_sec_from_payload_rejects_non_finite_or_boolean_values(invalid_duration):
    payload = {
        "duration_sec": invalid_duration,
        "metadata": {"duration_ms": invalid_duration, "duration_s": invalid_duration},
    }

    assert status_payload._duration_sec_from_payload(payload) is None


def test_duration_sec_from_payload_treats_oversized_integer_as_unavailable():
    assert status_payload._duration_sec_from_payload({"duration_sec": 10**1000}) is None


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


def test_public_segment_metadata_redacts_transition_track_ref():
    """transition_track_ref is internal claim-tracking bookkeeping (a track
    cache_key used to detect a stale "just finished playing" claim after an
    operator air-next) — it must never reach the public status payload."""
    metadata = {
        "source": "banter",
        "transition_track_ref": "youtube|abc123",
    }

    payload = status_payload._public_segment_metadata(metadata)

    assert payload == {"source": "banter"}


def test_public_segment_metadata_redacts_listener_request_handoff():
    metadata = {
        "title": "Song",
        LISTENER_REQUEST_HANDOFF_TOKEN_KEY: "private-request-token",
        LISTENER_REQUEST_HANDOFF_ADMITTED_KEY: True,
        LISTENER_REQUEST_DEDICATION_QUEUE_ID_KEY: "private-queue-id",
        LISTENER_REQUEST_HANDOFF_EXCLUSIVE_KEY: True,
        URGENT_INTERRUPT_PRIORITY_KEY: True,
    }

    assert status_payload._public_segment_metadata(metadata) == {"title": "Song"}


def test_public_segment_metadata_redacts_render_bound_playlist_source():
    metadata = {
        "source": "youtube",
        SEGMENT_PLAYLIST_SOURCE_KIND_KEY: "charts",
    }

    payload = status_payload._public_segment_metadata(metadata)

    assert payload == {"source": "youtube"}


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


def test_golden_path_status_does_not_treat_legacy_env_as_available_music(monkeypatch):
    class Config:
        anthropic_api_key = ""
        openai_api_key = ""
        allow_ytdlp = True
        playlist = SimpleNamespace(jamendo_client_id="")

    monkeypatch.setattr(status_payload, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload, "_golden_path_cache_ts", 0.0)
    payload = status_payload._golden_path_status(Config(), StationState())

    assert payload["stage"] == "needs_music_source"
    assert "yt-dlp downloads" not in payload["fallback_sources"]
    assert payload["source_readiness"]["sources"]["charts"]["configured"] is True
    assert payload["source_readiness"]["sources"]["charts"]["status"] == "configured_unchecked"


def _source_config(*, allow_ytdlp: bool = False, jamendo_client_id: str = ""):
    return SimpleNamespace(
        allow_ytdlp=allow_ytdlp,
        playlist=SimpleNamespace(jamendo_client_id=jamendo_client_id),
        anthropic_api_key="",
        openai_api_key="",
    )


def test_source_readiness_moves_candidates_to_playable_to_on_air_without_scanning(monkeypatch):
    track = Track(title="Song", artist="Artist", duration_ms=180_000, source="youtube")
    state = StationState(
        playlist=[track],
        playlist_source=PlaylistSource(kind="charts", label="Current Italian charts", track_count=1),
    )
    monkeypatch.setattr(status_payload, "_has_any_mp3", lambda _path: (_ for _ in ()).throw(AssertionError))

    candidates = status_payload._source_readiness_status(_source_config(allow_ytdlp=True), state)
    assert candidates["sources"]["charts"]["status"] == "candidates_only"
    assert candidates["programming_ready"] is False

    state.source_readiness.mark_playable("youtube")
    playable = status_payload._source_readiness_status(_source_config(allow_ytdlp=True), state)
    assert playable["sources"]["charts"]["status"] == "playable"
    assert playable["programming_ready"] is True

    state.on_stream_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=Path("song.mp3"),
            duration_sec=180,
            metadata={"title": "Song", "source_kind": "youtube", "duration_ms": 180_000},
        )
    )
    on_air = status_payload._source_readiness_status(_source_config(allow_ytdlp=True), state)
    assert on_air["sources"]["charts"]["status"] == "on_air"


def test_source_readiness_reports_only_terminal_candidate_exhaustion_as_unavailable(monkeypatch):
    evidence = SourceReadinessEvidence()
    evidence.mark_candidates("charts", 2)
    evidence.mark_failure("charts", "One candidate could not be prepared")
    state = StationState(source_readiness=evidence)

    partial = status_payload._source_readiness_status(_source_config(allow_ytdlp=True), state)
    assert partial["sources"]["charts"]["status"] == "candidates_only"
    assert partial["sources"]["charts"]["exhausted"] is False

    state.source_readiness.mark_exhausted(
        "charts",
        "No found track could be prepared as playable audio.",
    )
    state.source_readiness.configure("recovery", True, bundled=True)
    state.source_readiness.mark_on_air("recovery", recovery=True)
    exhausted = status_payload._source_readiness_status(_source_config(allow_ytdlp=True), state)

    charts = exhausted["sources"]["charts"]
    assert charts["status"] == "unavailable"
    assert charts["exhausted"] is True
    assert charts["candidates"] == 2
    assert charts["detail"] == "No found track could be prepared as playable audio."
    assert exhausted["sources"]["recovery"]["status"] == "on_air"
    assert exhausted["programming_ready"] is False
    assert exhausted["continuity_available"] is True
    assert exhausted["transport_only"] is True

    monkeypatch.setattr(status_payload, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload, "_golden_path_cache_key", None)
    monkeypatch.setattr(status_payload, "_golden_path_cache_ts", 0.0)
    golden_path = status_payload._golden_path_status(_source_config(allow_ytdlp=True), state)
    assert golden_path["stage"] == "needs_music_source"
    assert "Backup audio is ready" in golden_path["detail"]


def test_recovery_on_air_proves_transport_but_not_source_health():
    evidence = SourceReadinessEvidence()
    evidence.configure("recovery", True, bundled=True)
    state = StationState(source_readiness=evidence)
    state.on_stream_segment(
        Segment(
            type=SegmentType.BANTER,
            path=Path("continuity.mp3"),
            metadata={"title": "Station continuity", "rescue": True, "canned": True},
        )
    )

    payload = status_payload._source_readiness_status(_source_config(), state)

    assert payload["sources"]["recovery"]["status"] == "on_air"
    assert payload["programming_ready"] is False
    assert payload["transport_only"] is True


def test_recovery_cover_only_without_on_air_still_reports_backup_audio_ready(monkeypatch):
    """Regression: golden-path detail must widen to recovery_cover_available.

    Before this fix, the "Backup audio is ready" detail only fired when
    recovery was proven on_air. A bundled/configured recovery cover that has
    never actually aired (cover_only, not on_air) must still report
    continuity — otherwise First Listen stays stuck even though a usable
    fallback exists.
    """
    evidence = SourceReadinessEvidence()
    evidence.configure("recovery", True, bundled=True)
    state = StationState(source_readiness=evidence)

    readiness = status_payload._source_readiness_status(_source_config(), state)
    assert readiness["sources"]["recovery"]["status"] == "cover_only"
    assert readiness["recovery_on_air"] is False
    assert readiness["recovery_cover_available"] is True
    assert readiness["continuity_available"] is True
    assert readiness["programming_ready"] is False

    monkeypatch.setattr(status_payload, "_golden_path_cache", None)
    monkeypatch.setattr(status_payload, "_golden_path_cache_key", None)
    monkeypatch.setattr(status_payload, "_golden_path_cache_ts", 0.0)
    golden_path = status_payload._golden_path_status(_source_config(), state)

    assert golden_path["stage"] == "needs_music_source"
    assert "Backup audio is ready" in golden_path["detail"]


def test_configured_source_not_reached_does_not_claim_it_was_checked():
    payload = status_payload._source_readiness_status(
        _source_config(jamendo_client_id="configured"),
        StationState(),
    )

    jamendo = payload["sources"]["jamendo"]
    assert jamendo["status"] == "configured_unchecked"
    assert jamendo["attempted"] is False
    assert jamendo["detail"] == "Configured, but not checked because another source was selected."


def test_source_switch_resets_stale_playable_and_on_air_evidence():
    chart = Track(title="Chart", artist="Artist", duration_ms=180_000, source="youtube")
    state = StationState(
        playlist=[chart],
        playlist_source=PlaylistSource(kind="charts", label="Charts", track_count=1),
    )
    state.source_readiness.mark_playable("charts")
    state.source_readiness.mark_on_air("charts")
    state.source_readiness.mark_exhausted("charts", "stale terminal evidence")

    jamendo = Track(title="CC", artist="Artist", duration_ms=180_000, source="jamendo")
    state.switch_playlist(
        [jamendo],
        PlaylistSource(kind="jamendo", label="Jamendo", track_count=1),
    )

    assert state.source_readiness.source_revision == 1
    assert state.source_readiness.entries["charts"].playable == 0
    assert state.source_readiness.entries["charts"].on_air is False
    assert state.source_readiness.entries["charts"].exhausted is False
    assert state.source_readiness.entries["jamendo"].candidates == 1


def test_demo_without_assets_and_classic_rotation_stay_truthful():
    evidence = SourceReadinessEvidence()
    evidence.configure("demo", True, bundled=False)
    evidence.mark_candidates("demo", 10)
    evidence.mark_exhausted("demo", "Demo candidates could not be prepared")
    demo_state = StationState(source_readiness=evidence)

    demo = status_payload._source_readiness_status(_source_config(), demo_state)
    assert demo["sources"]["demo"]["status"] == "not_bundled"
    assert demo["sources"]["demo"]["exhausted"] is True
    assert demo["programming_ready"] is False

    classic = Track(title="Classic", artist="Artist", duration_ms=180_000, source="classic")
    classic_state = StationState(
        playlist=[classic],
        playlist_source=PlaylistSource(kind="classic", label="Classici anni '80", track_count=1),
    )
    classic_state.source_readiness.mark_playable("classic")
    advanced = status_payload._source_readiness_status(_source_config(), classic_state)
    assert advanced["advanced"]["kind"] == "classic"
    assert advanced["advanced"]["status"] == "playable"


@pytest.mark.parametrize(
    ("track_kind", "rotation_kind", "projected_kind"),
    [
        ("jamendo", "jamendo", "jamendo"),
        ("local", "local", "local"),
        ("demo", "demo", "demo"),
        ("classic", "classic", "advanced"),
    ],
)
def test_music_stream_start_maps_each_runtime_source_to_on_air(
    track_kind: Literal["jamendo", "local", "demo", "classic"],
    rotation_kind: str,
    projected_kind: str,
):
    track = Track(title="Song", artist="Artist", duration_ms=180_000, source=track_kind)
    state = StationState(
        playlist=[track],
        playlist_source=PlaylistSource(kind=rotation_kind, label=rotation_kind.title(), track_count=1),
    )
    state.on_stream_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=Path("song.mp3"),
            duration_sec=180,
            metadata={"title": "Song", "source_kind": track_kind, "duration_ms": 180_000},
        )
    )

    payload = status_payload._source_readiness_status(_source_config(), state)

    if projected_kind == "advanced":
        assert payload["advanced"]["status"] == "on_air"
    else:
        assert payload["sources"][projected_kind]["status"] == "on_air"


def test_public_status_etag_ignores_every_listener_advanced_clock():
    payload = {
        "now_streaming": {"type": "music", "label": "A — B"},
        "current_progress_sec": 12.3,
        "uptime_sec": 100,
        "runtime_health": {"queue_empty_elapsed_s": 3.2, "queue_empty": True},
        "ha_moments": {
            "last_event_label": "Morning launch",
            "last_event_ago_min": 1,
            "recent": [{"label": "Morning launch", "ago_min": 1, "status": "aired"}],
        },
        "session_stopped": False,
    }
    original = copy.deepcopy(payload)
    other = copy.deepcopy(payload)
    other["current_progress_sec"] = 45.6
    other["uptime_sec"] = 500
    other["runtime_health"]["queue_empty_elapsed_s"] = 90.1
    other["ha_moments"]["last_event_ago_min"] = 2
    other["ha_moments"]["recent"][0]["ago_min"] = 2

    assert status_payload.public_status_etag(payload) == status_payload.public_status_etag(other)
    assert payload == original, "ETag normalization mutated the response payload"

    semantic_change = copy.deepcopy(payload)
    semantic_change["ha_moments"]["recent"][0]["status"] = "airing"
    assert status_payload.public_status_etag(payload) != status_payload.public_status_etag(semantic_change)


@pytest.mark.parametrize("recent", [None, "invalid", [None, "invalid", {"ago_min": 3}]])
def test_public_status_etag_tolerates_nonstandard_recent_shapes(recent):
    payload = {"ha_moments": {"recent": recent}}
    original = copy.deepcopy(payload)

    assert status_payload.public_status_etag(payload).startswith('W/"')
    assert payload == original


@pytest.mark.parametrize(
    ("header", "etag", "expected"),
    [
        ('W/"abc123"', 'W/"abc123"', True),
        ('"abc123"', 'W/"abc123"', True),
        ('W/"stale", "abc123"', 'W/"abc123"', True),
        ('W/"a,b"', 'W/"a,b"', True),
        ('W/"\x80"', 'W/"\x80"', True),
        ("*", 'W/"abc123"', True),
        ('W/"other"', 'W/"abc123"', False),
        ('W/"ABC123"', 'W/"abc123"', False),
        ('W/"unterminated', 'W/"abc123"', False),
        ('W/"€"', 'W/"€"', False),
        ('*, W/"abc123"', 'W/"abc123"', False),
        ("", 'W/"abc123"', False),
    ],
)
def test_public_status_not_modified_honors_if_none_match(header, etag, expected):
    assert status_payload.public_status_not_modified({"If-None-Match": header}, etag) is expected


def test_public_status_not_modified_combines_repeated_header_lines():
    class RepeatedHeaders:
        @staticmethod
        def get(_name):
            return None

        @staticmethod
        def getlist(_name):
            return ['W/"stale"', '"abc123"']

    assert status_payload.public_status_not_modified(RepeatedHeaders(), 'W/"abc123"')
