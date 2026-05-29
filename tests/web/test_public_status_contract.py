"""Cross-page contract tests for /public-status vs /status.

The CRITICAL INVARIANT: any field present in BOTH endpoints must hold the
same value at the same time. This is what prevents the bug class Florian
flagged on 2026-04-26 (admin says 'Waiting for signal…' while listener
says 'IN ONDA' — different code paths producing different state).

Cathedral standard:
- Strict subset: every field in /public-status must also exist in /status
- Bytes-identical for shared fields: capabilities dict, brand dict, uptime,
  tracks_played, session_stopped, now_streaming, upcoming/upcoming_mode,
  runtime_health, playback_actions, ha_moments
- Shape snapshot: catches accidental field additions on the listener side
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mammamiradio.core.models import Segment, SegmentType
from tests.web.test_streamer_routes import _make_test_app


@pytest.mark.asyncio
async def test_public_status_returns_brand_block():
    """Public listener payload must include the brand-fiction layer."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    assert resp.status_code == 200
    body = resp.json()
    assert "brand" in body
    brand = body["brand"]
    # Required fields present
    for field in ("station_name", "hosts", "theme"):
        assert field in brand, f"brand missing field: {field}"
    # Theme has all six tokens
    theme = brand["theme"]
    for token in ("primary_color", "accent_color", "background_color", "display_font", "body_font", "mono_font"):
        assert token in theme, f"theme missing token: {token}"


@pytest.mark.asyncio
async def test_public_status_returns_capabilities():
    """Listener page reads capabilities every poll for client-side feature gating."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    assert "capabilities" in body
    caps = body["capabilities"]
    # Boolean flags present (values depend on test env, but keys must exist)
    for flag in ("llm", "anthropic_key", "openai", "ha", "anthropic_degraded"):
        assert flag in caps, f"capabilities missing flag: {flag}"
        assert isinstance(caps[flag], bool), f"capability {flag} must be bool"


@pytest.mark.asyncio
async def test_public_status_returns_uptime_and_tracks():
    """Cross-page invariant facts that must match admin /status."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    assert "uptime_sec" in body
    assert "tracks_played" in body
    assert isinstance(body["uptime_sec"], int)
    assert isinstance(body["tracks_played"], int)
    assert body["tracks_played"] >= 0


@pytest.mark.asyncio
async def test_admin_listener_facts_agree():
    """THE cross-page invariant: shared facts must hold the same value at the same time.

    This catches the bug class Florian flagged: admin says 'Waiting for signal…'
    while listener says 'IN ONDA'. Both endpoints must read from the SAME state
    object via the SAME helper (_public_status_payload), and never compute their
    own divergent values.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Admin /status on loopback bypasses auth (no admin password set in test app)
        admin_resp = await client.get("/status")
        public_resp = await client.get("/public-status")
    assert admin_resp.status_code == 200
    assert public_resp.status_code == 200
    admin = admin_resp.json()
    public = public_resp.json()

    # Bytes-identical shared fields
    assert admin["uptime_sec"] == public["uptime_sec"]
    assert admin["tracks_played"] == public["tracks_played"]
    assert admin["session_stopped"] == public["session_stopped"]
    assert admin.get("now_streaming") == public.get("now_streaming")
    assert admin["brand"] == public["brand"]
    assert admin["capabilities"] == public["capabilities"]
    assert admin["upcoming"] == public["upcoming"]
    assert admin["upcoming_mode"] == public["upcoming_mode"]
    assert admin["runtime_health"] == public["runtime_health"]
    assert admin["playback_actions"] == public["playback_actions"]
    assert admin.get("ha_moments") == public.get("ha_moments")


@pytest.mark.asyncio
async def test_public_status_strict_subset_of_admin():
    """Every field in /public-status must also exist in /status.

    The listener can never invent fields the admin doesn't see. Prevents
    drift where listener.html depends on a field that admin doesn't expose.
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        admin = (await client.get("/status")).json()
        public = (await client.get("/public-status")).json()
    public_keys = set(public.keys())
    admin_keys = set(admin.keys())
    listener_only_keys = public_keys - admin_keys
    assert not listener_only_keys, (
        f"Listener payload has keys admin doesn't expose: {listener_only_keys}. "
        "/public-status must be a strict subset of /status for shared fields."
    )


@pytest.mark.asyncio
async def test_public_status_no_admin_secrets_leak():
    """Listener payload must NOT contain admin-only fields (queue_depth, costs, etc)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    # Admin-only fields that must NOT leak to listener
    forbidden = {
        "queue_depth",
        "consumption",
        "produced_log",
        "ha_details",
        "last_banter_script",
        "last_ad_script",
        "playlist",
        "pacing",
    }
    leaked = forbidden & set(body.keys())
    assert not leaked, f"Listener payload leaks admin-only fields: {leaked}"


@pytest.mark.asyncio
async def test_public_status_exposes_audio_format():
    """Integrations read stream.audio_format before declaring /stream playback.

    Defaults must match the documented MP3 contract. The legacy bitrate field
    must equal audio_format.bitrate_kbps in the same response so they cannot
    drift (cross-page invariant inside one payload).
    """
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    stream = body["stream"]
    assert stream["audio_format"] == {
        "codec": "mp3",
        "mime_type": "audio/mpeg",
        "bitrate_kbps": 192,
        "sample_rate_hz": 48000,
        "channels": 2,
    }
    # Legacy field must read from the same source so it cannot diverge.
    assert stream["bitrate_kbps"] == stream["audio_format"]["bitrate_kbps"]


@pytest.mark.asyncio
async def test_public_status_audio_format_reflects_non_default_config():
    """A non-default bitrate must propagate to both audio_format and the legacy field.

    Uses a test-local app instance so the config mutation cannot leak to
    other tests (no shared/global state).
    """
    app = _make_test_app()
    app.state.config.audio.bitrate = 128
    app.state.config.audio.sample_rate = 44100
    app.state.config.audio.channels = 1
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-status")
    body = resp.json()
    stream = body["stream"]
    assert stream["audio_format"]["bitrate_kbps"] == 128
    assert stream["audio_format"]["sample_rate_hz"] == 44100
    assert stream["audio_format"]["channels"] == 1
    assert stream["bitrate_kbps"] == 128


@pytest.mark.asyncio
async def test_public_listener_requests_endpoint():
    """Listener-safe dediche feed (filtered version of /api/listener-requests)."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-listener-requests")
    assert resp.status_code == 200
    body = resp.json()
    assert "requests" in body
    assert isinstance(body["requests"], list)


@pytest.mark.asyncio
async def test_public_listener_requests_filters_sensitive_fields():
    """Listener requests endpoint must drop internal IDs and error fields."""
    from mammamiradio.core.models import StationState

    app = _make_test_app()
    state: StationState = app.state.station_state
    # Inject a request with sensitive fields
    state.pending_requests.append(
        {
            "ts": 1700000000.0,
            "name": "Marco",
            "message": "Per Lucia",
            "type": "dedica",
            "song_found": True,
            "song_track": "Vasco Rossi - Albachiara",
            "song_error": "INTERNAL_ERROR_DETAILS",  # admin-visible only
            "request_id": "admin-mutation-id",
            "public_token": "listener-visible-token",
        }
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/public-listener-requests")
    body = resp.json()
    requests = body["requests"]
    if requests:
        req = requests[0]
        # Public-safe fields present
        assert req.get("name") == "Marco"
        assert req.get("message") == "Per Lucia"
        assert "song_track" in req
        assert "age_s" in req
        assert req.get("public_token") == "listener-visible-token"
        # Sensitive fields absent
        assert "id" not in req, "Internal ID must not leak to public"
        assert "request_id" not in req, "Admin mutation ID must not leak to public"
        assert "song_error" not in req, "Error details must not leak to public"
        assert "ts" not in req, "Raw timestamp must not leak (use age_s instead)"


# ---------------------------------------------------------------------------
# Music Assistant integration contract
#
# The mammamiradio Music Assistant provider (music-assistant/server:
# providers/mammamiradio/) polls /public-status every ~12s and maps
# now_streaming onto a StreamMetadata for MA's now-playing card. That makes
# /public-status a SECOND-CONSUMER contract beyond listener.js — renaming or
# dropping any field below silently degrades the merged MA provider with no
# other test catching it. These tests are the drift detector: keep the
# MA_CONSUMED_* tuples in sync with the MA provider's
# _segment_to_stream_metadata helper.
# ---------------------------------------------------------------------------

# Top-level keys the MA provider reads from /public-status.
MA_CONSUMED_TOP_LEVEL = ("now_streaming", "upcoming", "ha_moments", "brand")
# now_streaming keys the MA provider reads for every segment.
MA_CONSUMED_SEGMENT = ("type", "label", "started", "metadata")
# now_streaming.metadata sub-keys read for a music segment.
MA_CONSUMED_MUSIC_META = ("title", "title_only", "artist", "album_art")
# now_streaming.metadata sub-keys read for a news_flash segment.
MA_CONSUMED_NEWS_META = ("host",)


def _assert_ma_segment_contract(now: dict, metadata_keys: tuple[str, ...], *, prefix: str = "now_streaming") -> None:
    assert now is not None
    for key in MA_CONSUMED_SEGMENT:
        assert key in now, f"{prefix} missing MA-consumed key: {key}"
    for key in metadata_keys:
        assert key in now["metadata"], f"{prefix} metadata missing MA-consumed key: {key}"


@pytest.mark.asyncio
async def test_public_status_ma_top_level_keys_present():
    """The four top-level keys the MA provider polls must always be present."""
    app = _make_test_app()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()
    for key in MA_CONSUMED_TOP_LEVEL:
        assert key in body, f"/public-status missing MA-consumed key: {key}"
    # brand sub-fields the provider reads for banter artist / station name.
    assert "station_name" in body["brand"], "brand missing MA-consumed key: station_name"
    assert "hosts" in body["brand"], "brand missing MA-consumed key: hosts"


@pytest.mark.asyncio
async def test_public_status_ma_music_segment_contract():
    """A music now_streaming exposes every field the MA provider's music branch reads."""
    app = _make_test_app()
    app.state.station_state.on_stream_segment(
        Segment(
            type=SegmentType.MUSIC,
            path=Path("/tmp/volare.mp3"),
            duration_sec=210,
            metadata={
                "title": "Volare — Domenico Modugno",
                "title_only": "Volare",
                "artist": "Domenico Modugno",
                "album_art": "http://example/art.jpg",
            },
        )
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()
    now = body["now_streaming"]
    assert now["label"] == "Volare — Domenico Modugno"
    _assert_ma_segment_contract(now, MA_CONSUMED_MUSIC_META)


@pytest.mark.asyncio
async def test_public_status_ma_news_flash_segment_contract():
    """A news_flash now_streaming exposes metadata.host — the MA provider's artist source."""
    app = _make_test_app()
    app.state.station_state.on_stream_segment(
        Segment(
            type=SegmentType.NEWS_FLASH,
            path=Path("/tmp/news.mp3"),
            duration_sec=20,
            metadata={
                "host": "Gianni",
                "title": "News flash: sports",
            },
        )
    )

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = (await client.get("/public-status")).json()
    now = body["now_streaming"]
    assert now["label"] == "News flash: sports"
    _assert_ma_segment_contract(now, MA_CONSUMED_NEWS_META, prefix="news_flash now_streaming")
