"""HTTP route + snapshot capture for the v1 now-playing integration contract.

The route's only job beyond serialization is to capture a single atomic
snapshot of mutable ``StationState`` at the top of the handler so the
serializer never reads through a segment transition. ETag and ``changed_at``
both derive from that same snapshot.

The endpoint is read-only, unauthenticated (matches /public-status), and
documented at ``docs/integrations/now-playing.md``.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

from mammamiradio.audio.stream_format import stream_audio_metadata
from mammamiradio.integrations.schema import HostEntry, StationBlock
from mammamiradio.integrations.serializer import (
    NowPlayingSnapshot,
    fingerprint,
    serialize_now_playing,
)
from mammamiradio.scheduling.scheduler import preview_upcoming

UP_NEXT_LIMIT = 8
CACHE_CONTROL = "public, max-age=2"

router = APIRouter(prefix="/api/integrations/v1", tags=["integrations"])


def _station_block(config) -> StationBlock:
    """Return the station identity slice of the v1 contract."""
    brand = getattr(config, "brand", None)
    hosts: list[HostEntry] = []
    if brand is not None:
        for host in getattr(brand, "hosts", []) or []:
            hosts.append(
                HostEntry(
                    engine_host=getattr(host, "engine_host", ""),
                    display_name=getattr(host, "display_name", ""),
                    description=getattr(host, "description", ""),
                )
            )
    frequency = getattr(brand, "frequency", "") if brand is not None else ""
    return StationBlock(
        name=config.station.name,
        frequency=frequency,
        theme=config.station.theme,
        hosts=hosts,
    )


def _resolve_stream_urls(request: Request) -> tuple[str, str | None]:
    """Return ``(relative_url, absolute_url_or_None)`` for the live MP3.

    ``relative_url`` is the canonical path consumers on the same instance
    should resolve against the addon URL. ``absolute_url`` is opt-in and only
    set when computable from the request URL AND the request is not behind
    HA Supervisor ingress (detected via ``X-Ingress-Path``).
    """
    relative = "/stream"
    ingress_path = request.headers.get("X-Ingress-Path") or request.headers.get("x-ingress-path")
    if ingress_path:
        return relative, None
    try:
        parts = urlsplit(str(request.url))
        scheme = parts.scheme or "http"
        netloc = parts.netloc
        if not netloc:
            return relative, None
        return relative, f"{scheme}://{netloc}{relative}"
    except (TypeError, ValueError):
        return relative, None


def _capture_snapshot(request: Request) -> NowPlayingSnapshot:
    """Atomically copy the mutable radio state needed to render the response."""
    state = request.app.state.station_state
    config = request.app.state.config
    now_streaming = copy.deepcopy(getattr(state, "now_streaming", {}) or {})
    queued_segments = tuple(copy.deepcopy(item) for item in (getattr(state, "queued_segments", []) or []))
    if queued_segments:
        upcoming_predicted: tuple[dict, ...] = ()
        upcoming_mode = "queued"
    else:
        try:
            predicted = preview_upcoming(state, config.pacing, state.playlist, count=UP_NEXT_LIMIT)
        except Exception:
            predicted = []
        upcoming_predicted = tuple(predicted)
        upcoming_mode = "building"
    session_stopped = bool(getattr(state, "session_stopped", False))
    playback_epoch = int(getattr(state, "playback_epoch", 0) or 0)
    last_change = float(getattr(state, "last_state_change_at", 0.0) or 0.0)
    started = now_streaming.get("started")
    if isinstance(started, int | float):
        last_change = max(last_change, float(started))
    relative_url, absolute_url = _resolve_stream_urls(request)
    return NowPlayingSnapshot(
        now_streaming=now_streaming,
        queued_segments=queued_segments,
        upcoming_predicted=upcoming_predicted,
        session_stopped=session_stopped,
        playback_epoch=playback_epoch,
        station=_station_block(config),
        audio_format=dict(stream_audio_metadata(config)),
        relative_stream_url=relative_url,
        absolute_stream_url=absolute_url,
        changed_at=last_change,
        up_next_limit=UP_NEXT_LIMIT,
        upcoming_mode=upcoming_mode,
    )


@router.get("/now-playing")
async def now_playing(request: Request) -> Response:
    """Return the v1 now-playing contract for external integrators.

    Returns 304 Not Modified when ``If-None-Match`` matches the current
    weak ETag derived from the snapshot fingerprint. Always returns 200 +
    a stable payload otherwise — degradations are expressed in the payload
    (``session_state`` / ``segment_class``), not in the HTTP status.
    """
    snapshot = _capture_snapshot(request)
    etag = f'W/"{fingerprint(snapshot)}"'
    headers = {
        "ETag": etag,
        "Cache-Control": CACHE_CONTROL,
    }
    if_none_match = request.headers.get("If-None-Match") or request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip() == etag:
        return Response(status_code=304, headers=headers)
    payload = serialize_now_playing(snapshot)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return Response(content=body, media_type="application/json", headers=headers)
