from __future__ import annotations

import httpx
import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.provider_checks import check_provider_keys

TOML_PATH = "radio.toml"


@pytest.mark.asyncio
async def test_provider_check_reports_missing_keys_without_network(monkeypatch):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.azure_speech_key = ""
    config.azure_speech_region = ""
    config.elevenlabs_api_key = ""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("network client should not be created when no provider keys are configured")

    monkeypatch.setattr(httpx, "AsyncClient", fail_if_called)

    result = await check_provider_keys(config)

    assert result["ok"] is False
    assert result["providers"]["anthropic"]["error_type"] == "not_configured"
    assert result["providers"]["openai_chat"]["error_type"] == "not_configured"
    assert result["providers"]["openai_tts"]["error_type"] == "not_configured"
    assert result["providers"]["azure_speech"]["error_type"] == "not_configured"
    assert result["providers"]["elevenlabs_tts"]["error_type"] == "not_configured"


@pytest.mark.asyncio
async def test_provider_check_classifies_anthropic_auth_and_openai_success(monkeypatch):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = "anthropic-secret"
    config.openai_api_key = "openai-secret"
    config.azure_speech_key = ""
    config.azure_speech_region = ""
    config.elevenlabs_api_key = ""

    seen_auth_headers: list[str] = []
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            seen_auth_headers.append(request.headers["authorization"])
        if request.url.host == "api.anthropic.com":
            return httpx.Response(
                401,
                json={"error": {"type": "authentication_error", "message": "invalid x-api-key anthropic-secret"}},
            )
        if str(request.url).endswith("/v1/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        if str(request.url).endswith("/v1/audio/speech"):
            return httpx.Response(200, content=b"mp3", headers={"content-type": "audio/mpeg"})
        return httpx.Response(500, json={"error": {"message": "unexpected URL"}})

    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await check_provider_keys(config)

    assert result["ok"] is True
    assert result["providers"]["anthropic"]["ok"] is False
    assert result["providers"]["anthropic"]["status_code"] == 401
    assert result["providers"]["anthropic"]["error_type"] == "authentication_error"
    assert "[redacted]" in result["providers"]["anthropic"]["detail"]
    assert result["providers"]["openai_chat"]["ok"] is True
    assert result["providers"]["openai_tts"]["ok"] is True
    assert "anthropic-secret" not in str(result)
    assert "openai-secret" not in str(result)
    assert seen_auth_headers == ["Bearer openai-secret", "Bearer openai-secret"]


@pytest.mark.asyncio
async def test_provider_check_classifies_network_error(monkeypatch):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = "anthropic-secret"
    config.openai_api_key = ""
    config.azure_speech_key = ""
    config.azure_speech_region = ""
    config.elevenlabs_api_key = ""

    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS resolution failed")

    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await check_provider_keys(config)

    assert result["ok"] is False
    assert result["providers"]["anthropic"]["ok"] is False
    assert result["providers"]["anthropic"]["error_type"] == "network_error"
    assert "anthropic-secret" not in str(result)


@pytest.mark.asyncio
async def test_provider_check_classifies_anthropic_credit_exhausted(monkeypatch):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = "anthropic-secret"
    config.openai_api_key = ""
    config.azure_speech_key = ""
    config.azure_speech_region = ""
    config.elevenlabs_api_key = ""

    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "type": "error",
                "error": {"type": "permission_error", "message": "Your credit balance is too low to access this API"},
            },
        )

    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await check_provider_keys(config)

    assert result["ok"] is False
    assert result["providers"]["anthropic"]["ok"] is False
    assert result["providers"]["anthropic"]["error_type"] == "insufficient_quota"
    assert "anthropic-secret" not in str(result)


@pytest.mark.asyncio
async def test_provider_check_probes_azure_and_elevenlabs_tts(monkeypatch):
    config = load_config(TOML_PATH)
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.azure_speech_key = "azure-secret"
    config.azure_speech_region = "westeurope"
    config.elevenlabs_api_key = "eleven-secret"

    async_client = httpx.AsyncClient
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host or "")
        # Azure: token issuer endpoint (POST, no body) — lightweight key probe
        if request.url.host == "westeurope.api.cognitive.microsoft.com":
            assert request.headers["Ocp-Apim-Subscription-Key"] == "azure-secret"
            return httpx.Response(200, content=b"eyJhbGciOiJIUzI1NiJ9.fake.token")
        # ElevenLabs: /v1/user — lightweight key probe
        if request.url.host == "api.elevenlabs.io":
            assert request.headers["xi-api-key"] == "eleven-secret"
            return httpx.Response(200, json={"xi_api_key": "eleven-secret", "subscription": {}})
        return httpx.Response(500, json={"error": {"message": "unexpected URL"}})

    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await check_provider_keys(config)

    assert result["ok"] is True
    assert result["providers"]["azure_speech"]["ok"] is True
    assert result["providers"]["elevenlabs_tts"]["ok"] is True
    assert "azure-secret" not in str(result)
    assert "eleven-secret" not in str(result)
    assert seen_hosts == ["westeurope.api.cognitive.microsoft.com", "api.elevenlabs.io"]


@pytest.mark.asyncio
async def test_provider_check_classifies_azure_and_elevenlabs_auth_errors(monkeypatch):
    """A revoked Azure/ElevenLabs key yields ok=False, authentication_error, and the
    secret is scrubbed from the detail — the Engine Room shows 'replace key', not green."""
    config = load_config(TOML_PATH)
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    config.azure_speech_key = "azure-secret"
    config.azure_speech_region = "westeurope"
    config.elevenlabs_api_key = "eleven-secret"

    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "westeurope.api.cognitive.microsoft.com":
            return httpx.Response(401, text="Access denied due to invalid subscription key azure-secret")
        if request.url.host == "api.elevenlabs.io":
            return httpx.Response(
                401,
                json={"detail": {"status": "invalid_api_key", "message": "Invalid x-api-key eleven-secret"}},
            )
        return httpx.Response(500, json={"error": {"message": "unexpected URL"}})

    transport = httpx.MockTransport(handler)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = await check_provider_keys(config)

    assert result["ok"] is False
    assert result["providers"]["azure_speech"]["ok"] is False
    assert result["providers"]["azure_speech"]["status_code"] == 401
    assert result["providers"]["azure_speech"]["error_type"] == "authentication_error"
    assert result["providers"]["elevenlabs_tts"]["ok"] is False
    assert result["providers"]["elevenlabs_tts"]["status_code"] == 401
    assert result["providers"]["elevenlabs_tts"]["error_type"] == "authentication_error"
    assert "azure-secret" not in str(result)
    assert "eleven-secret" not in str(result)
