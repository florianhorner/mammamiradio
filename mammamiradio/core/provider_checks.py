"""Active, secret-safe AI provider connectivity checks."""

from __future__ import annotations

from typing import Any

import httpx

from mammamiradio.core.config import StationConfig, resolve_model


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


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
) -> tuple[int | None, str]:
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return None, str(exc)
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type or response.status_code >= 400:
        return response.status_code, response.text[:2000]
    return response.status_code, ""


async def _post_empty(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
) -> tuple[int | None, str]:
    """POST with no body — for token/auth probe endpoints that expect Content-Length: 0."""
    try:
        response = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        return None, str(exc)
    if response.status_code >= 400:
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
        "azure_speech": _missing_result("azure_speech"),
        "elevenlabs_tts": _missing_result("elevenlabs_tts"),
    }

    if not any(
        (
            config.anthropic_api_key,
            config.openai_api_key,
            config.azure_speech_key and config.azure_speech_region,
            config.elevenlabs_api_key,
        )
    ):
        return {
            "ok": False,
            "providers": results,
        }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        if config.anthropic_api_key:
            # Probe every distinct Anthropic model the active profile will use
            # (creative for banter/ads/news, fast for transitions) so a stale
            # fast-role model id is surfaced here, not only when a live
            # transition 404s. Report the first failing model.
            anth_models: list[str] = []
            for _caller in ("banter", "transition"):
                _m = resolve_model(config.models, _caller, "anthropic")
                if _m not in anth_models:
                    anth_models.append(_m)
            anth_result = results["anthropic"]
            for _m in anth_models:
                status, body = await _post_json(
                    client,
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": config.anthropic_api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    payload={
                        "model": _m,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "Reply with ok."}],
                    },
                )
                anth_result = _result("anthropic", status, body, secrets=(config.anthropic_api_key,))
                if not anth_result["ok"]:
                    break  # surface the first failing model
            results["anthropic"] = anth_result

        if config.openai_api_key:
            openai_headers = {"Authorization": f"Bearer {config.openai_api_key}"}
            # Mirror the Anthropic check: dynamic routing can use different
            # OpenAI chat models for creative copy and fast transitions.
            openai_models: list[str] = []
            for _caller in ("banter", "transition"):
                _m = resolve_model(config.models, _caller, "openai")
                if _m not in openai_models:
                    openai_models.append(_m)
            openai_result = results["openai_chat"]
            for _m in openai_models:
                status, body = await _post_json(
                    client,
                    "https://api.openai.com/v1/chat/completions",
                    headers=openai_headers,
                    payload={
                        "model": _m,
                        # gpt-5.x rejects `max_tokens` (use `max_completion_tokens`);
                        # the old name made this probe 400 and falsely report a
                        # valid OpenAI key as down.
                        "max_completion_tokens": 1,
                        "messages": [{"role": "user", "content": "Reply ok."}],
                    },
                )
                openai_result = _result("openai_chat", status, body, secrets=(config.openai_api_key,))
                if not openai_result["ok"]:
                    break
            results["openai_chat"] = openai_result

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

        if config.azure_speech_key and config.azure_speech_region:
            # Token-issuer endpoint: POST with no body, ~400-byte response vs
            # ~150KB from the voices/list endpoint. Same 401/403 on bad key.
            status, body = await _post_empty(
                client,
                f"https://{config.azure_speech_region}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
                headers={"Ocp-Apim-Subscription-Key": config.azure_speech_key},
            )
            results["azure_speech"] = _result("azure_speech", status, body, secrets=(config.azure_speech_key,))

        if config.elevenlabs_api_key:
            # /v1/user returns account info (~300 bytes) vs /v1/voices which
            # returns all voice metadata (20-80KB). Same 401 on bad key.
            status, body = await _get(
                client,
                "https://api.elevenlabs.io/v1/user",
                headers={"xi-api-key": config.elevenlabs_api_key},
            )
            results["elevenlabs_tts"] = _result(
                "elevenlabs_tts",
                status,
                body,
                secrets=(config.elevenlabs_api_key,),
            )

    return {
        "ok": any(item["ok"] for item in results.values()),
        "providers": results,
    }
