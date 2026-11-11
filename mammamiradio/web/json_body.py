"""Shared JSON-body parsing for web write routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

DEFAULT_JSON_BODY_ERROR = "Send the details and try again."
DEFAULT_JSON_BODY_TOO_LARGE = "That is more than the station can take in one go. Shorten it and send it again."


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes | None:
    """Return the request body, or None as soon as it is known to exceed ``max_bytes``.

    A declared Content-Length over the cap is refused without reading a byte.
    Otherwise the body is pulled chunk by chunk and abandoned the moment the
    running total passes the cap, which also covers chunked uploads and a
    missing or understated Content-Length. Nothing past the cap is buffered.
    """
    declared = request.headers.get("content-length", "").strip()
    if declared.isascii() and declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def read_json_object(
    request: Request,
    *,
    error_message: str = DEFAULT_JSON_BODY_ERROR,
    max_bytes: int | None = None,
    too_large_message: str = DEFAULT_JSON_BODY_TOO_LARGE,
) -> tuple[dict[str, Any], JSONResponse | None]:
    """Return a JSON object body or a graceful 422 response.

    Route handlers keep semantic validation local. This helper owns only the
    parse-layer invariant: empty, malformed, or top-level non-object bodies must
    never leak as raw server errors.

    With ``max_bytes`` set, the body is size-checked BEFORE it is buffered or
    decoded and an oversized one gets a 413 in the same response shape. Public
    routes must pass it: parsing runs on the event loop that feeds the stream.
    """
    try:
        if max_bytes is None:
            body = await request.json()
        else:
            raw = await _read_bounded_body(request, max_bytes)
            if raw is None:
                return {}, JSONResponse({"ok": False, "error": too_large_message}, status_code=413)
            body = json.loads(raw)
    except ValueError:
        return {}, JSONResponse({"ok": False, "error": error_message}, status_code=422)
    if not isinstance(body, dict):
        return {}, JSONResponse({"ok": False, "error": error_message}, status_code=422)
    return body, None
