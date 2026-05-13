"""Active, secret-safe AI provider connectivity checks."""

from __future__ import annotations

from typing import Any

import httpx

from mammamiradio.core.config import StationConfig


def _error_payload(body: str) -> dict[str, Any]:
    try:
        data = httpx.Response(200, content=body).json()
    except ValueError:
        return {"message": body[:240]}
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return error
        return data
    return {"message": str(data)[:240]}


def _classify_error(status_code: int | None, body: str) -> tuple[str, str]:
    payload = _error_payload(body)
    raw = " ".join(str(payload.get(key, "")) for key in ("type", "code", "message") if payload.get(key))
    text = raw.lower()

    if status_code == 401 or "authentication" in text or "invalid_api_key" in text or "invalid x-api-key" in text:
        return "authentication_error", raw[:240]
    if status_code == 403 or "permission_error" in text or "credit balance" in text:
        return "insufficient_quota", raw[:240]
    if "insufficient_quota" in text:
        return "insufficient_quota", raw[:240]
    if status_code == 429 or "rate_limit" in text or "too many requests" in text:
        return "rate_limit", raw[:240]
    if "usage limits" in text:
        return "usage_limit", raw[:240]
    if status_code == 404 or "model_not_found" in text or "not found" in text:
        return "model_not_found", raw[:240]
    if status_code is None:
        return "network_error", raw[:240]
    return "provider_error", raw[:240]


def _missing_result(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "configured": False,
        "ok": False,
        "status_code": None,
        "error_type": "not_configured",
        "detail": "",
    }


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[int | None, str]:
    try:
        response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        return None, str(exc)
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type or response.status_code >= 400:
        return response.status_code, response.text[:2000]
    return response.status_code, ""


def _scrub_secrets(value: str, secrets: tuple[str, ...]) -> str:
    scrubbed = value
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, "[redacted]")
    return scrubbed


def _result(provider: str, status_code: int | None, body: str, *, secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    if status_code is not None and 200 <= status_code < 300:
        return {
            "provider": provider,
            "configured": True,
            "ok": True,
            "status_code": status_code,
            "error_type": "",
            "detail": "",
        }
    error_type, detail = _classify_error(status_code, body)
    return {
        "provider": provider,
        "configured": True,
        "ok": False,
        "status_code": status_code,
        "error_type": error_type,
        "detail": _scrub_secrets(detail, secrets),
    }


async def check_provider_keys(config: StationConfig, *, timeout_s: float = 12.0) -> dict[str, Any]:
    """Probe configured AI keys without returning or logging secret values."""
    results: dict[str, Any] = {
        "anthropic": _missing_result("anthropic"),
        "openai_chat": _missing_result("openai_chat"),
        "openai_tts": _missing_result("openai_tts"),
    }

    if not config.anthropic_api_key and not config.openai_api_key:
        return {
            "ok": False,
            "providers": results,
        }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        if config.anthropic_api_key:
            status, body = await _post_json(
                client,
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": config.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
                payload={
                    "model": config.audio.claude_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "Reply with ok."}],
                },
            )
            results["anthropic"] = _result("anthropic", status, body, secrets=(config.anthropic_api_key,))

        if config.openai_api_key:
            openai_headers = {"Authorization": f"Bearer {config.openai_api_key}"}
            status, body = await _post_json(
                client,
                "https://api.openai.com/v1/chat/completions",
                headers=openai_headers,
                payload={
                    "model": config.audio.openai_script_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "Reply ok."}],
                },
            )
            results["openai_chat"] = _result("openai_chat", status, body, secrets=(config.openai_api_key,))

            status, body = await _post_json(
                client,
                "https://api.openai.com/v1/audio/speech",
                headers=openai_headers,
                payload={
                    "model": "gpt-4o-mini-tts",
                    "voice": "onyx",
                    "input": "ok",
                    "response_format": "mp3",
                },
            )
            results["openai_tts"] = _result("openai_tts", status, body, secrets=(config.openai_api_key,))

    return {
        "ok": any(item["ok"] for item in results.values()),
        "providers": results,
    }
