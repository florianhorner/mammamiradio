"""Security tests for the v1 contract.

The endpoint forwards ``Segment.metadata`` through an allowlist
(``SAFE_METADATA_KEYS``). Without the allowlist, internal fields like
``direct_url``, ``local_path``, signed URLs, or error strings would leak
to any consumer that polls /api/integrations/v1/now-playing. This test
lock prevents that regression.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.integrations.conftest import make_integrations_app, play_music_segment


@pytest.mark.asyncio
async def test_metadata_allowlist_rejects_internal_fields():
    """Poisoned metadata fields must never appear in the v1 response."""
    poisoned = {
        "direct_url": "https://signed.s3.example/secret?token=abc",
        "local_path": "/tmp/tracks/track-12345.mp3",
        "error": "download_failed: 401 from provider",
        "signed_url": "https://signed.example/secret",
        "ttl": 99,
        "internal_score": 0.92,
        "audio_source": "norm_cache_rescue",
    }
    app = make_integrations_app()
    play_music_segment(
        app.state.station_state,
        extra_metadata=poisoned,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/integrations/v1/now-playing")
    raw_body = resp.content.decode("utf-8")
    # Belt-and-suspenders: scan the whole response for the poisoned values.
    for forbidden in (
        "signed.s3.example",
        "/tmp/tracks/track-12345.mp3",
        "download_failed",
        "signed.example",
        "norm_cache_rescue",
    ):
        assert forbidden not in raw_body, f"leaked internal field value to response: {forbidden}"
    # The allowed identifier should still be present.
    body = json.loads(raw_body)
    assert body["now_playing"]["external_ids"]["spotify"] == "v01"


@pytest.mark.asyncio
async def test_non_dict_metadata_does_not_crash():
    """A malformed ``metadata`` value is handled defensively."""
    app = make_integrations_app()
    app.state.station_state.now_streaming = {
        "type": "music",
        "label": "Garbage",
        "started": 1.0,
        "duration_sec": 0.0,
        "metadata": ["not", "a", "dict"],
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/integrations/v1/now-playing")
    assert resp.status_code == 200
    body = resp.json()
    assert body["now_playing"]["segment_class"] == "music"
    assert body["now_playing"]["artist"] is None
    assert body["now_playing"]["external_ids"] == {}
