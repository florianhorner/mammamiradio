"""Contract tests for /api/integrations/v1/now-playing.

These tests pin the v1 response shape against accidental field removal,
rename, or type change. They mirror the MA_CONSUMED_* drift-guard pattern
in tests/web/test_public_status_contract.py — when these break, an
integration consumer breaks.
"""

from __future__ import annotations

import httpx
import pytest

from mammamiradio.core.models import SegmentType
from tests.integrations.conftest import make_integrations_app, play_music_segment, play_segment

# Drift-guard tuples — keep in sync with docs/integrations/now-playing.md
V1_TOP_LEVEL = (
    "schema_version",
    "station",
    "stream",
    "now_playing",
    "up_next",
    "session_state",
    "changed_at",
)
V1_STATION = ("name", "frequency", "theme", "hosts")
V1_STREAM = ("relative_url", "audio_format")
V1_AUDIO_FORMAT = ("codec", "mime_type", "bitrate_kbps", "sample_rate_hz", "channels")
V1_NOW_PLAYING = (
    "segment_class",
    "segment_type",
    "title",
    "started_at",
    "duration_estimate_sec",
    "artist",
    "artwork",
    "album",
    "year",
    "external_ids",
    "host",
    "context",
)
V1_UP_NEXT_ITEM = ("segment_class", "segment_type", "title", "predicted")


# ---------------------------------------------------------------------------
# T1 — top-level shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_endpoint_returns_v1_top_level_keys():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/integrations/v1/now-playing")
    assert resp.status_code == 200
    body = resp.json()
    for key in V1_TOP_LEVEL:
        assert key in body, f"v1 payload missing top-level key: {key}"
    assert body["schema_version"] == "1"


@pytest.mark.asyncio
async def test_station_block_has_required_fields():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    station = body["station"]
    for key in V1_STATION:
        assert key in station, f"station block missing key: {key}"


@pytest.mark.asyncio
async def test_station_block_uses_resolved_station_identity(monkeypatch):
    monkeypatch.setenv("STATION_NAME", "Radio Test")
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()

    assert body["station"]["name"] == "Radio Test"


@pytest.mark.asyncio
async def test_stream_block_has_relative_url_and_audio_format():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    stream = body["stream"]
    for key in V1_STREAM:
        assert key in stream, f"stream block missing key: {key}"
    assert stream["relative_url"] == "/stream"
    for key in V1_AUDIO_FORMAT:
        assert key in stream["audio_format"], f"audio_format missing key: {key}"


# ---------------------------------------------------------------------------
# T2-T8 — every SegmentType maps to a stable segment_class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_music_segment_payload():
    app = make_integrations_app()
    play_music_segment(app.state.station_state, album="Mr Volare", year=1958)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    now = body["now_playing"]
    assert now is not None
    for key in V1_NOW_PLAYING:
        assert key in now, f"now_playing missing key: {key}"
    assert now["segment_class"] == "music"
    assert now["segment_type"] == "music"
    assert now["title"] == "Volare"
    assert now["artist"] == "Domenico Modugno"
    assert now["artwork"] == "http://example.test/art.jpg"
    assert now["album"] == "Mr Volare"
    assert now["year"] == 1958
    assert now["duration_estimate_sec"] == pytest.approx(210.0)
    assert now["external_ids"] == {"spotify": "v01", "youtube": "y01"}
    assert now["host"] is None
    assert now["context"] == {}
    assert body["session_state"] == "live"


@pytest.mark.asyncio
async def test_banter_segment_payload_is_voice():
    app = make_integrations_app()
    play_segment(app.state.station_state, SegmentType.BANTER, title="Gianni e Marco", host="Gianni")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    now = body["now_playing"]
    assert now["segment_class"] == "voice"
    assert now["segment_type"] == "banter"
    assert now["host"] == "Gianni"
    assert now["artist"] is None
    assert now["artwork"] is None


@pytest.mark.asyncio
async def test_news_flash_segment_payload_is_voice():
    app = make_integrations_app()
    play_segment(app.state.station_state, SegmentType.NEWS_FLASH, title="News flash", host="Marco")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    now = body["now_playing"]
    assert now["segment_class"] == "voice"
    assert now["segment_type"] == "news_flash"
    assert now["host"] == "Marco"


@pytest.mark.asyncio
async def test_ad_segment_payload_is_interstitial():
    app = make_integrations_app()
    play_segment(app.state.station_state, SegmentType.AD, title="Ad break")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["now_playing"]["segment_class"] == "interstitial"
    assert body["now_playing"]["segment_type"] == "ad"


@pytest.mark.asyncio
async def test_station_id_segment_payload_is_interstitial():
    app = make_integrations_app()
    play_segment(app.state.station_state, SegmentType.STATION_ID, title="Station ID")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["now_playing"]["segment_class"] == "interstitial"
    assert body["now_playing"]["segment_type"] == "station_id"


@pytest.mark.asyncio
async def test_time_check_segment_payload_is_interstitial():
    app = make_integrations_app()
    play_segment(app.state.station_state, SegmentType.TIME_CHECK, title="Ora esatta")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["now_playing"]["segment_class"] == "interstitial"
    assert body["now_playing"]["segment_type"] == "time_check"


@pytest.mark.asyncio
async def test_sweeper_segment_payload_is_interstitial():
    app = make_integrations_app()
    play_segment(app.state.station_state, SegmentType.SWEEPER, title="Sweeper")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["now_playing"]["segment_class"] == "interstitial"
    assert body["now_playing"]["segment_type"] == "sweeper"


# ---------------------------------------------------------------------------
# T15 — Missing optional metadata returns null/empty with stable shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_optional_music_metadata_returns_null():
    app = make_integrations_app()
    play_music_segment(
        app.state.station_state,
        artist="",
        album_art="",
        spotify_id="",
        youtube_id="",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    now = body["now_playing"]
    assert now["artist"] is None
    assert now["artwork"] is None
    assert now["external_ids"] == {}
    # Shape stable: keys present, values null/empty
    for key in V1_NOW_PLAYING:
        assert key in now


# ---------------------------------------------------------------------------
# T16 — external_ids is a provider->id map (NOT spotify_id/youtube_id keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_ids_is_provider_keyed_map():
    app = make_integrations_app()
    play_music_segment(app.state.station_state, spotify_id="abc123", youtube_id="xyz789")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    ext = body["now_playing"]["external_ids"]
    assert ext == {"spotify": "abc123", "youtube": "xyz789"}
    assert "spotify_id" not in ext
    assert "youtube_id" not in ext


# ---------------------------------------------------------------------------
# T22 — Drift-guard tuples cover the full shape after every segment class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_station_theme_uses_brand_tagline_not_internal_prompt():
    """``station.theme`` must surface ``brand.tagline`` (listener-safe), NOT
    ``config.station.theme`` (the internal scriptwriter prompt).

    The internal prompt contains production-side direction for the AI hosts
    and must never leak into the unauthenticated public endpoint.
    """
    app = make_integrations_app()
    config = app.state.config
    # Force a known divergence between the two fields so a regression that
    # reads station.theme by mistake shows up immediately.
    config.station.theme = "INTERNAL: scriptwriter directive, never expose"
    config.brand.tagline = "Public tagline"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    assert body["station"]["theme"] == "Public tagline"
    assert "INTERNAL" not in body["station"]["theme"]


@pytest.mark.asyncio
async def test_up_next_combines_queued_prefix_with_predicted_tail():
    """When the queue is partially filled, ``up_next`` must extend with
    predicted items so consumers see the full lookahead, not a truncated list.
    """

    app = make_integrations_app()
    state = app.state.station_state
    # Real queued segment (will appear with predicted=false).
    state.queued_segments = [
        {"type": "music", "label": "Queued track", "metadata": {"title": "Queued track"}},
    ]
    state.now_streaming = {
        "type": "music",
        "label": "Now playing",
        "started": 1.0,
        "duration_sec": 1.0,
        "metadata": {"title": "Now playing"},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    items = body["up_next"]
    assert len(items) > 1, f"expected queued + predicted tail, got {items}"
    assert items[0]["predicted"] is False, "queued segment must lead the list"
    assert items[0]["title"] == "Queued track"
    assert any(item["predicted"] is True for item in items[1:]), (
        "expected predicted entries to fill remaining slots after the queued prefix"
    )


@pytest.mark.asyncio
async def test_up_next_items_have_drift_guard_keys():
    app = make_integrations_app()
    # Force at least one predicted item by leaving queue empty
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/api/integrations/v1/now-playing")).json()
    up_next = body["up_next"]
    if up_next:
        for item in up_next:
            for key in V1_UP_NEXT_ITEM:
                assert key in item, f"up_next item missing key: {key}"


# ---------------------------------------------------------------------------
# T24 — SegmentType.segment_class is exhaustive
# ---------------------------------------------------------------------------


def test_segment_class_property_is_exhaustive():
    """Every SegmentType maps to one of the stable buckets."""
    valid_classes = {"music", "voice", "interstitial"}
    for seg in SegmentType:
        assert seg.segment_class in valid_classes, f"{seg.name} missing segment_class"


def test_segment_class_mapping_is_correct():
    """Lock the specific mapping; renaming a bucket breaks consumers."""
    assert SegmentType.MUSIC.segment_class == "music"
    assert SegmentType.BANTER.segment_class == "voice"
    assert SegmentType.NEWS_FLASH.segment_class == "voice"
    assert SegmentType.AD.segment_class == "interstitial"
    assert SegmentType.STATION_ID.segment_class == "interstitial"
    assert SegmentType.TIME_CHECK.segment_class == "interstitial"
    assert SegmentType.SWEEPER.segment_class == "interstitial"
