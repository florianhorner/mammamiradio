"""Unit tests for the pure serializer (no HTTP)."""

from __future__ import annotations

import pytest

from mammamiradio.integrations.schema import StationBlock
from mammamiradio.integrations.serializer import (
    SAFE_METADATA_KEYS,
    NowPlayingSnapshot,
    serialize_now_playing,
)

_AUDIO_FORMAT: dict = {
    "codec": "mp3",
    "mime_type": "audio/mpeg",
    "bitrate_kbps": 192,
    "sample_rate_hz": 44100,
    "channels": 2,
}
_STATION: StationBlock = StationBlock(name="Test FM", frequency="98.1", theme="warm", hosts=[])


def _make_snapshot(
    *,
    now_streaming: dict | None = None,
    queued_segments: tuple[dict, ...] = (),
    upcoming_predicted: tuple[dict, ...] = (),
    session_stopped: bool = False,
    playback_epoch: int = 0,
    absolute_stream_url: str | None = None,
    changed_at: float = 0.0,
) -> NowPlayingSnapshot:
    return NowPlayingSnapshot(
        now_streaming=now_streaming or {},
        queued_segments=queued_segments,
        upcoming_predicted=upcoming_predicted,
        session_stopped=session_stopped,
        playback_epoch=playback_epoch,
        station=_STATION,
        audio_format=_AUDIO_FORMAT,
        relative_stream_url="/stream",
        absolute_stream_url=absolute_stream_url,
        changed_at=changed_at,
    )


def test_safe_metadata_keys_locked():
    """If this list grows, doc the new key in docs/integrations/now-playing.md."""
    expected = {
        "title",
        "title_only",
        "artist",
        "album",
        "album_art",
        "spotify_id",
        "youtube_id",
        "musicbrainz_id",
        "host",
        "year",
        "source_kind",
        "music_attribution",
    }
    assert frozenset(expected) == SAFE_METADATA_KEYS


def test_serialize_empty_queue_state():
    snapshot = _make_snapshot()
    payload = serialize_now_playing(snapshot)
    assert payload["session_state"] == "empty_queue"
    assert payload["now_playing"] is None
    assert payload["up_next"] == []
    assert payload["schema_version"] == "1"


def test_serialize_stopped_state():
    snapshot = _make_snapshot(session_stopped=True)
    payload = serialize_now_playing(snapshot)
    assert payload["session_state"] == "stopped"
    assert payload["now_playing"] is None


def test_serialize_live_music_segment():
    snapshot = _make_snapshot(
        now_streaming={
            "type": "music",
            "label": "Volare — Domenico Modugno",
            "started": 1000.0,
            "duration_sec": 210.0,
            "metadata": {
                "title": "Volare",
                "title_only": "Volare",
                "artist": "Domenico Modugno",
                "album_art": "http://example/art.jpg",
                "spotify_id": "v01",
            },
        },
        playback_epoch=5,
        changed_at=1000.0,
    )
    payload = serialize_now_playing(snapshot)
    assert payload["session_state"] == "live"
    now = payload["now_playing"]
    assert now["segment_class"] == "music"
    assert now["artist"] == "Domenico Modugno"
    assert now["external_ids"] == {"spotify": "v01"}


def test_serialize_music_attribution_additively_without_schema_bump():
    attribution = {
        "provider": "incompetech",
        "license_id": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://incompetech.com/music/royalty-free/music.html",
        "credit": "Carefree by Kevin MacLeod, CC BY 4.0",
        "modified": True,
        "basis": "bundled_manifest",
    }
    payload = serialize_now_playing(
        _make_snapshot(
            now_streaming={
                "type": "music",
                "label": "Carefree",
                "started": 1.0,
                "duration_sec": 180.0,
                "metadata": {"title": "Carefree", "music_attribution": attribution},
            }
        )
    )

    assert payload["schema_version"] == "1"
    assert payload["now_playing"]["music_attribution"] == attribution


def test_serialize_rejects_unsafe_music_attribution_urls():
    payload = serialize_now_playing(
        _make_snapshot(
            now_streaming={
                "type": "music",
                "label": "Unsafe",
                "started": 1.0,
                "duration_sec": 180.0,
                "metadata": {
                    "music_attribution": {
                        "provider": "jamendo",
                        "license_id": "CC-BY-4.0",
                        "license_url": "https://creativecommons.org/licenses/by/4.0/",
                        "source_url": "https://user:secret@www.jamendo.com/track/1",
                        "credit": "Artist - Track",
                        "modified": True,
                        "basis": "provider_reported",
                    }
                },
            }
        )
    )

    assert "music_attribution" not in payload["now_playing"]


@pytest.mark.parametrize(
    "attribution",
    [
        None,
        {"basis": "legal_clearance"},
        {
            "provider": 123,
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://www.jamendo.com/track/1",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
        {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "",
            "source_url": "https://www.jamendo.com/track/1",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
        {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org:bad/licenses/by/4.0/",
            "source_url": "https://www.jamendo.com/track/1",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
        {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://www.jamendo.com/track/1#fragment",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
        {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://www.jamendo.com/track/../private",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
    ],
)
def test_serialize_rejects_malformed_attribution_shapes(attribution):
    payload = serialize_now_playing(
        _make_snapshot(
            now_streaming={
                "type": "music",
                "label": "Unsafe",
                "started": 1.0,
                "duration_sec": 180.0,
                "metadata": {"music_attribution": attribution},
            }
        )
    )

    assert "music_attribution" not in payload["now_playing"]


@pytest.mark.parametrize(
    "attribution",
    [
        {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://storage.jamendo.com/download/track.mp3?token=private",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
        {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://example.com/track/1",
            "credit": "Artist - Track",
            "modified": True,
            "basis": "provider_reported",
        },
        {
            "provider": "incompetech",
            "license_id": "CC-BY-3.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://incompetech.com/music/royalty-free/music.html",
            "credit": "Carefree by Kevin MacLeod",
            "modified": True,
            "basis": "bundled_manifest",
        },
    ],
)
def test_serialize_rejects_noncanonical_attribution_links_and_license_pairs(attribution):
    payload = serialize_now_playing(
        _make_snapshot(
            now_streaming={
                "type": "music",
                "label": "Unsafe",
                "started": 1.0,
                "duration_sec": 180.0,
                "metadata": {"music_attribution": attribution},
            }
        )
    )

    assert "music_attribution" not in payload["now_playing"]


def test_serialize_up_next_queued_items_marked_predicted_false():
    snapshot = _make_snapshot(
        queued_segments=(
            {"type": "music", "label": "Next song"},
            {"type": "banter", "label": "Banter coming"},
        )
    )
    payload = serialize_now_playing(snapshot)
    items = payload["up_next"]
    assert len(items) == 2
    assert all(item["predicted"] is False for item in items)
    assert items[0]["segment_class"] == "music"
    assert items[1]["segment_class"] == "voice"


def test_serialize_up_next_predicted_items_marked_predicted_true():
    snapshot = _make_snapshot(
        queued_segments=(),
        upcoming_predicted=({"type": "music", "label": "Future"},),
        now_streaming={"type": "music", "label": "Now", "started": 1.0, "metadata": {}, "duration_sec": 1.0},
    )
    payload = serialize_now_playing(snapshot)
    items = payload["up_next"]
    assert items[0]["predicted"] is True
    assert items[0]["segment_class"] == "music"


def test_serialize_absolute_url_optional():
    snapshot_with_url = _make_snapshot(absolute_stream_url="https://radio.example/stream")
    payload_with_absolute_url = serialize_now_playing(snapshot_with_url)
    assert payload_with_absolute_url["stream"]["absolute_url"] == "https://radio.example/stream"
    payload_without_absolute_url = serialize_now_playing(_make_snapshot(absolute_stream_url=None))
    assert "absolute_url" not in payload_without_absolute_url["stream"]
