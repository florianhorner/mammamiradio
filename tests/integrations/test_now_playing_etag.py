"""ETag + Cache-Control + If-None-Match tests for the v1 contract."""

from __future__ import annotations

import httpx
import pytest

from tests.integrations.conftest import make_integrations_app, play_music_segment


@pytest.mark.asyncio
async def test_response_has_etag_and_cache_control():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/integrations/v1/now-playing")
    assert resp.status_code == 200
    etag = resp.headers.get("ETag")
    assert etag is not None
    assert etag.startswith('W/"')
    # Lock the exact policy — drifting to private or a different TTL is a contract change.
    cache_control = resp.headers.get("Cache-Control", "")
    directives = {d.strip() for d in cache_control.split(",")}
    assert "public" in directives, f"Cache-Control missing 'public': {cache_control!r}"
    assert "max-age=2" in directives, f"Cache-Control max-age must be 2: {cache_control!r}"


@pytest.mark.asyncio
async def test_if_none_match_unchanged_state_returns_304():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        etag = first.headers["ETag"]
        second = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"If-None-Match": etag},
        )
    assert second.status_code == 304
    assert second.headers.get("ETag") == etag
    # 304 should not have a JSON body
    assert second.content == b""


@pytest.mark.asyncio
async def test_if_none_match_state_change_returns_200_with_new_etag():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        old_etag = first.headers["ETag"]
        # Mutate state — a new segment starts
        play_music_segment(app.state.station_state)
        second = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"If-None-Match": old_etag},
        )
    assert second.status_code == 200
    new_etag = second.headers["ETag"]
    assert new_etag != old_etag
    body = second.json()
    assert body["now_playing"] is not None
    assert body["now_playing"]["segment_class"] == "music"


@pytest.mark.asyncio
async def test_etag_includes_session_stopped_in_fingerprint():
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        old_etag = first.headers["ETag"]
        # Flip session_stopped — should change fingerprint
        app.state.station_state.session_stopped = True
        app.state.station_state.last_state_change_at += 1.0
        second = await client.get("/api/integrations/v1/now-playing")
    new_etag = second.headers["ETag"]
    assert new_etag != old_etag


@pytest.mark.asyncio
async def test_etag_invalidates_when_force_next_changes_predicted_up_next():
    """force_next can flip the first predicted up_next item without changing
    queue length or any other snapshot field. The body-hash ETag must catch
    this — a length-only fingerprint would silently serve 304 with a stale
    representation.
    """
    from mammamiradio.core.models import SegmentType

    app = make_integrations_app()
    state = app.state.station_state
    state.force_next = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        first_etag = first.headers["ETag"]
        first_up_next = first.json()["up_next"]
        # Now flip force_next — predicted up_next first item changes.
        state.force_next = SegmentType.AD
        second = await client.get("/api/integrations/v1/now-playing")
    assert second.status_code == 200
    second_etag = second.headers["ETag"]
    second_body = second.json()
    # Guard the array access — if the scheduler stops emitting predictions for
    # some unrelated reason the test would crash before validating the ETag.
    assert first_up_next, "expected at least one predicted up_next item before force_next flip"
    assert second_body["up_next"], "expected at least one predicted up_next item after force_next flip"
    # First predicted item changed type — that change MUST show through to the ETag.
    assert first_up_next[0]["segment_type"] != second_body["up_next"][0]["segment_type"]
    assert first_etag != second_etag, "body changed but ETag did not — clients will see stale 304s"


@pytest.mark.asyncio
async def test_if_none_match_with_multiple_etags_returns_304_when_any_match():
    """RFC 7232: ``If-None-Match`` carries a comma-separated list of ETags."""
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        etag = first.headers["ETag"]
        multi = f'W/"stale-1", {etag}, W/"stale-2"'
        resp = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"If-None-Match": multi},
        )
    assert resp.status_code == 304


@pytest.mark.asyncio
async def test_if_none_match_star_returns_304():
    """``If-None-Match: *`` is a wildcard match per RFC 7232."""
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/api/integrations/v1/now-playing",
            headers={"If-None-Match": "*"},
        )
    assert resp.status_code == 304


@pytest.mark.asyncio
async def test_head_returns_etag_without_body():
    """HEAD must surface ETag + Cache-Control so the curl quickstart works."""
    app = make_integrations_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        get_resp = await client.get("/api/integrations/v1/now-playing")
        head_resp = await client.head("/api/integrations/v1/now-playing")
    assert head_resp.status_code == 200
    assert head_resp.headers.get("ETag") == get_resp.headers.get("ETag")
    assert "max-age" in head_resp.headers.get("Cache-Control", "")
    assert head_resp.content == b""


@pytest.mark.asyncio
async def test_etag_invalidates_when_now_streaming_swaps_to_skipping_sentinel():
    """A /api/skip transition writes a fresh ``started`` timestamp to ``now_streaming``.

    /api/skip does not call ``on_stream_segment`` (it writes the sentinel
    directly), so ``last_state_change_at`` is NOT bumped. The route still
    has to invalidate the ETag because the response body changed. The
    snapshot does this by max()-ing ``now_streaming["started"]`` into
    ``changed_at`` before fingerprinting.
    """
    app = make_integrations_app()
    state = app.state.station_state
    play_music_segment(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/api/integrations/v1/now-playing")
        old_etag = first.headers["ETag"]
        # Simulate /api/skip writing a new sentinel WITHOUT going through on_stream_segment.
        state.now_streaming = {
            "type": "skipping",
            "label": "Skipping...",
            "started": state.now_streaming["started"] + 5.0,
            "metadata": {},
        }
        second = await client.get("/api/integrations/v1/now-playing")
    new_etag = second.headers["ETag"]
    assert new_etag != old_etag, "skip sentinel must invalidate the ETag even without last_state_change_at bump"
    body = second.json()
    assert body["now_playing"]["segment_class"] == "unavailable"
