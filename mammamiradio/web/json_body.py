"""Shared JSON-body parsing for web write routes."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

DEFAULT_JSON_BODY_ERROR = "Send the details and try again."


async def read_json_object(
    request: Request,
    *,
    error_message: str = DEFAULT_JSON_BODY_ERROR,
) -> tuple[dict[str, Any], JSONResponse | None]:
    """Return a JSON object body or a graceful 422 response.

    Route handlers keep semantic validation local. This helper owns only the
    parse-layer invariant: empty, malformed, or top-level non-object bodies must
    never leak as raw server errors.
    """
    try:
        body = await request.json()
    except ValueError:
        return {}, JSONResponse({"ok": False, "error": error_message}, status_code=422)
    if not isinstance(body, dict):
        return {}, JSONResponse({"ok": False, "error": error_message}, status_code=422)
    return body, None
