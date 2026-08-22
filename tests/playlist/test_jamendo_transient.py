"""Contract and race tests for the single-use Jamendo provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import httpx
import pytest

from mammamiradio.playlist import jamendo_transient as jt

_CLIENT_ID = "client_id_12345678"


def _item(
    track_id: str = "12345",
    *,
    license_url: str = "https://creativecommons.org/licenses/by/4.0/",
    audio_url: str = "https://storage.jamendo.com/download/track.mp3?token=private",
    source_url: str = "https://www.jamendo.com/track/12345/example",
    duration: object = 180,
) -> dict[str, object]:
    return {
        "id": track_id,
        "name": "Una Canzone",
        "artist_name": "Artista Aperto",
        "duration": duration,
        "shareurl": source_url,
        "audio": audio_url,
        # A deliberately hostile download URL proves the provider never reads it.
        "audiodownload": "https://evil.example/private-download.mp3",
        "license_ccurl": license_url,
    }


def _payload(results: list[object], *, status: str = "success", code: object = 0) -> dict[str, object]:
    return {"headers": {"status": status, "code": code}, "results": results}


def _client_for_items(
    items: dict[str, dict[str, object]] | None = None,
    *,
    discovery_ids: list[str] | None = None,
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    exact_items = items or {"12345": _item()}
    discovered = discovery_ids or list(exact_items)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        exact_id = request.url.params.get("id")
        results: list[object]
        if exact_id is None:
            results = [exact_items.get(track_id, {"id": track_id}) for track_id in discovered]
        else:
            item = exact_items.get(exact_id)
            results = [item] if item is not None else []
        return httpx.Response(200, json=_payload(results), request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requests


def _successful_worker(
    candidates: list[dict[str, object]] | None = None,
    calls: list[Path] | None = None,
):
    def worker(candidate, operation_dir: Path, on_normalizing):
        if candidates is not None:
            candidates.append(dict(candidate))
        if calls is not None:
            calls.append(operation_dir)
        assert list(operation_dir.iterdir()) == []
        on_normalizing()
        final = operation_dir / "normalized.mp3"
        final.write_bytes(b"normalized-jamendo-audio")
        digest = hashlib.sha256(final.read_bytes()).hexdigest()
        return jt._PreparedArtifact(final, digest, 180.0, 4096)

    return worker


async def _wait_for_state(provider: jt.JamendoStreamProvider, expected: str, *, attempts: int = 300) -> None:
    for _ in range(attempts):
        if provider.status()["state"] == expected:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"provider did not reach {expected}: {provider.status()}")


@pytest.mark.asyncio
async def test_default_start_is_inert_and_redacted(tmp_path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    try:
        await provider.start()
        assert provider.status() == {
            "enabled": False,
            "state": "disabled",
            "client_id_configured": False,
            "noncommercial_acknowledged": False,
            "terms_scope": "noncommercial_api_use",
            "provider_confirmation": "pending",
            "ready": False,
            "in_flight": False,
            "last_success_age_sec": None,
            "last_failure_code": None,
            "rejected_count": 0,
        }
        assert requests == []
        assert provider.take_ready_segment() is None
        assert provider.retry() is False
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_enabled_provider_prepares_single_use_segment_from_audio_field(tmp_path, caplog):
    client, requests = _client_for_items()
    candidates: list[dict[str, object]] = []
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: 7,
        http_client=client,
        stream_worker=_successful_worker(candidates),
        clock=lambda: 1000.0,
    )
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")

        assert len(requests) == 2
        assert requests[0].url.params["audioformat"] == "mp32"
        assert requests[0].url.params["include"] == "licenses"
        assert requests[0].url.params["ccnc"] == "false"
        assert requests[0].url.params["ccsa"] == "false"
        assert requests[0].url.params["ccnd"] == "false"
        assert "audiodownload" not in requests[0].url.params
        assert requests[1].url.params["id"] == "12345"
        assert requests[1].url.params["include"] == "licenses"
        assert candidates[0]["audio_url"] == _item()["audio"]
        assert "evil.example" not in str(candidates[0])

        lifecycle_records = [
            record
            for record in caplog.records
            if record.getMessage() in {"Jamendo provider preparation started", "Jamendo provider ready"}
        ]
        assert [(record.levelno, record.getMessage()) for record in lifecycle_records] == [
            (logging.INFO, "Jamendo provider preparation started"),
            (logging.INFO, "Jamendo provider ready"),
        ]
        assert _CLIENT_ID not in caplog.text
        assert "token=private" not in caplog.text
        assert str(tmp_path) not in caplog.text

        safe_status = str(provider.status())
        assert _CLIENT_ID not in safe_status
        assert "token=private" not in safe_status
        assert str(tmp_path) not in safe_status

        segment = provider.take_ready_segment()
        assert segment is not None
        assert provider.status()["state"] == "queued"
        assert segment.metadata["source_kind"] == "jamendo"
        assert segment.metadata["provider_track_id"] == "12345"
        assert segment.metadata["music_attribution"] == {
            "provider": "jamendo",
            "license_id": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "source_url": "https://www.jamendo.com/track/12345/example",
            "credit": "Artista Aperto - Una Canzone, provided by Jamendo, CC-BY-4.0",
            "modified": True,
            "basis": "provider_reported",
        }
        assert "jamendo_lease_id" not in segment.metadata
        assert segment.mark_playback_started() is True
        assert segment.mark_playback_started() is True
        assert provider.status()["state"] == "playing"
        artifact = segment.path
        segment.release()
        segment.release()
        assert not artifact.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_incomplete_configuration_never_starts_network(tmp_path):
    client, requests = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=False)
        assert provider.status()["state"] == "needs_config"
        assert provider.status()["client_id_configured"] is True
        await provider.apply_config(enabled=True, client_id="short", noncommercial_acknowledged=True)
        assert provider.status()["state"] == "needs_config"
        assert requests == []
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_apply_failure_marker_blocks_stale_work_until_config_is_reapplied(tmp_path):
    client, requests = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: 0,
        http_client=client,
        stream_worker=_successful_worker(),
    )
    try:
        await provider.start()
        await provider.apply_config(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        provider.mark_apply_failure()

        await asyncio.sleep(0)
        assert provider.status()["state"] == "blocked"
        assert provider.status()["last_failure_code"] == "api_failed"
        assert provider.retry() is False
        assert requests == []
        await provider.apply_config(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        assert provider.status()["last_failure_code"] is None
        assert len(requests) == 2
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.parametrize(
    ("license_url", "license_id"),
    [
        ("https://creativecommons.org/licenses/by/3.0/", "CC-BY-3.0"),
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        ("http://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
    ],
)
def test_candidate_accepts_only_canonical_cc_by_versions(license_url, license_id):
    candidate = jt._candidate_from_exact_result(_item(license_url=license_url), "12345")
    assert candidate["license_id"] == license_id


@pytest.mark.parametrize(
    "license_url",
    [
        "https://creativecommons.org/licenses/by-nc/4.0/",
        "https://creativecommons.org/licenses/by-nd/4.0/",
        "https://creativecommons.org/licenses/by-sa/4.0/",
        "https://creativecommons.org/licenses/by/2.0/",
        "https://creativecommons.org/licenses/by/4.0/?downgrade=1",
    ],
)
def test_candidate_rejects_non_by_or_noncanonical_license(license_url):
    with pytest.raises(jt._CandidateRejectedError, match=r"license_rejected|invalid_url"):
        jt._candidate_from_exact_result(_item(license_url=license_url), "12345")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("audio_url", "http://storage.jamendo.com/track.mp3"),
        ("audio_url", "https://jamendo.com.evil.example/track.mp3"),
        ("audio_url", "https://user@storage.jamendo.com/track.mp3"),
        ("audio_url", "https://storage.jamendo.com:444/track.mp3"),
        ("audio_url", "https://storage.jamendo.com/%2e%2e/private"),
        ("source_url", "https://eviljamendo.com/track/12345"),
        ("source_url", "https://storage.jamendo.com/track/12345"),
        ("source_url", "https://artists.jamendo.com/track/12345"),
        ("source_url", "https://www.jamendo.com/album/12345"),
        ("source_url", "https://www.jamendo.com/track/12345?token=private"),
        ("source_url", "https://user@www.jamendo.com/track/12345"),
        ("source_url", "https://www.jamendo.com/track/12345#private"),
    ],
)
def test_candidate_rejects_hostile_audio_and_share_urls(field, value):
    kwargs = {field: value}
    with pytest.raises(jt._CandidateRejectedError, match="invalid_url"):
        jt._candidate_from_exact_result(_item(**kwargs), "12345")


@pytest.mark.parametrize(
    "source_url",
    [
        "https://jamendo.com/track/12345",
        "https://www.jamendo.com/track/12345/example",
    ],
)
def test_candidate_accepts_only_public_jamendo_track_hosts(source_url):
    candidate = jt._candidate_from_exact_result(_item(source_url=source_url), "12345")
    assert candidate["source_url"] == source_url


@pytest.mark.parametrize("duration", [True, "bad", 59.99, 720.01, float("inf")])
def test_candidate_rejects_invalid_duration(duration):
    with pytest.raises(jt._CandidateRejectedError, match="duration_rejected"):
        jt._candidate_from_exact_result(_item(duration=duration), "12345")


@pytest.mark.asyncio
async def test_rejected_candidate_falls_through_to_next_exact_id(tmp_path):
    items = {
        "111": _item("111", license_url="https://creativecommons.org/licenses/by-nc/4.0/"),
        "222": _item("222", source_url="https://www.jamendo.com/track/222/good"),
    }
    client, _ = _client_for_items(items, discovery_ids=["111", "111", "bad", "222"])
    candidates: list[dict[str, object]] = []
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(candidates)
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        assert candidates[0]["provider_track_id"] == "222"
        assert provider.status()["rejected_count"] == 3
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.parametrize(
    ("status_code", "body", "expected_state", "failure"),
    [
        (401, {}, "blocked", "api_auth_failed"),
        (500, {}, "degraded", "api_failed"),
        (200, {"headers": {"status": "fail", "code": 1}, "results": []}, "degraded", "api_failed"),
        (200, {"headers": [], "results": []}, "degraded", "api_failed"),
        (200, {"headers": {"status": "success", "code": 0}, "results": {}}, "degraded", "api_malformed"),
    ],
)
@pytest.mark.asyncio
async def test_api_contract_failures_are_bounded_and_coarse(tmp_path, status_code, body, expected_state, failure):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, expected_state)
        assert provider.status()["last_failure_code"] == failure
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_code_three_failure_log_omits_credentials_and_provider_error_message(tmp_path, caplog):
    provider_error = f"invalid include {_CLIENT_ID} token=private https://storage.jamendo.com/private.mp3 {tmp_path}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "headers": {
                    "status": "failed",
                    "code": "3",
                    "error_message": provider_error,
                },
                "results": [],
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "blocked")

        assert provider.status()["last_failure_code"] == "api_failed"
        failure_logs = [
            record for record in caplog.records if record.getMessage().startswith("Jamendo provider blocked")
        ]
        assert len(failure_logs) == 1
        assert failure_logs[0].levelno == logging.WARNING
        assert failure_logs[0].getMessage() == ("Jamendo provider blocked failure_code=api_failed provider_code=3")
        assert provider._pending_delay is None
        assert _CLIENT_ID not in caplog.text
        assert "token=private" not in caplog.text
        assert "storage.jamendo.com" not in caplog.text
        assert provider_error not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.parametrize(
    ("provider_code", "expected_state", "failure_code", "expected_log", "expected_sleeps"),
    [
        (
            1,
            "degraded",
            "api_failed",
            "Jamendo provider attempt failed failure_code=api_failed provider_code=1 retry_in_seconds=60",
            [60.0],
        ),
        (
            "5",
            "blocked",
            "api_auth_failed",
            "Jamendo provider blocked failure_code=api_auth_failed provider_code=5",
            [],
        ),
        (
            6,
            "degraded",
            "api_failed",
            "Jamendo provider attempt failed failure_code=api_failed provider_code=6 retry_in_seconds=60",
            [60.0],
        ),
        (
            999,
            "degraded",
            "api_failed",
            "Jamendo provider attempt failed failure_code=api_failed provider_code=999 retry_in_seconds=60",
            [60.0],
        ),
        (
            f"3 {_CLIENT_ID}",
            "degraded",
            "api_failed",
            "Jamendo provider attempt failed failure_code=api_failed provider_code=none retry_in_seconds=60",
            [60.0],
        ),
    ],
)
@pytest.mark.asyncio
async def test_provider_error_codes_distinguish_blocked_from_retryable_failures(
    tmp_path,
    caplog,
    provider_code,
    expected_state,
    failure_code,
    expected_log,
    expected_sleeps,
):
    sleeps: list[float] = []
    sleep_gate = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"headers": {"status": "failed", "code": provider_code}, "results": []},
            request=request,
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await sleep_gate.wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, sleep=fake_sleep)
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, expected_state)

        assert provider.status()["last_failure_code"] == failure_code
        for _ in range(100):
            if sleeps == expected_sleeps:
                break
            await asyncio.sleep(0)
        assert sleeps == expected_sleeps
        matching = [record for record in caplog.records if record.getMessage() == expected_log]
        assert len(matching) == 1
        assert matching[0].levelno == logging.WARNING
        assert _CLIENT_ID not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_oversized_metadata_body_is_stopped_before_json_parsing(tmp_path, caplog):
    requests: list[httpx.Request] = []

    class OversizedMetadataStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 40
            yield b"y" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=OversizedMetadataStream(), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    calls: list[Path] = []
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(calls=calls)
    )
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        with patch.object(jt, "_MAX_METADATA_RESPONSE_BYTES", 64):
            await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
            await _wait_for_state(provider, "degraded")

        assert provider.status()["last_failure_code"] == "metadata_oversize"
        assert len(requests) == 1
        assert calls == []
        assert _CLIENT_ID not in caplog.text
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_cumulative_metadata_deadline_stops_serial_exact_lookup(tmp_path):
    items = {
        "111": _item("111", source_url="https://www.jamendo.com/track/111/first"),
        "222": _item("222", source_url="https://www.jamendo.com/track/222/second"),
    }
    requested: list[str | None] = []
    completed: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        track_id = request.url.params.get("id")
        requested.append(track_id)
        if track_id is None:
            await asyncio.sleep(0.025)
            results = list(items.values())
        elif track_id == "111":
            await asyncio.sleep(0.025)
            results = [{**items[track_id], "audio": "https://storage.jamendo.com/download/changed.mp3"}]
        else:
            # This request is individually below the total budget, but the
            # discovery and prior exact-ID lookup have already consumed most
            # of the shared deadline.
            await asyncio.sleep(0.05)
            results = [items[track_id]]
        completed.append(track_id)
        return httpx.Response(200, json=_payload(results), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    calls: list[Path] = []
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(calls=calls)
    )
    try:
        with patch.object(jt, "_NETWORK_DEADLINE_SEC", 0.08):
            await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
            await _wait_for_state(provider, "degraded")

        assert provider.status()["last_failure_code"] == "network_timeout"
        assert requested == [None, "111", "222"]
        assert completed == [None, "111"]
        assert calls == []
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_exact_identity_mismatch_never_reaches_worker(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        results: list[object] = [_item()] if request.url.params.get("id") is None else [_item("99999")]
        return httpx.Response(200, json=_payload(results), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    calls: list[Path] = []
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(calls=calls)
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "degraded")
        assert provider.status()["last_failure_code"] == "identity_mismatch"
        assert provider.status()["rejected_count"] == 1
        assert calls == []
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_license_or_stream_url_change_during_exact_revalidation_is_rejected(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        item = (
            _item()
            if request.url.params.get("id") is None
            else _item(audio_url="https://storage.jamendo.com/download/changed.mp3")
        )
        return httpx.Response(200, json=_payload([item]), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    calls: list[Path] = []
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(calls=calls)
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "degraded")
        assert provider.status()["last_failure_code"] == "metadata_changed"
        assert provider.status()["rejected_count"] == 1
        assert calls == []
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_source_change_cancels_discovery_before_artifact(tmp_path):
    exact_started = asyncio.Event()
    release_exact = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("id") is None:
            return httpx.Response(200, json=_payload([_item()]), request=request)
        exact_started.set()
        await release_exact.wait()
        return httpx.Response(200, json=_payload([_item()]), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    calls: list[Path] = []
    revision = [1]
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: revision[0], http_client=client, stream_worker=_successful_worker(calls=calls)
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await exact_started.wait()
        revision[0] = 2
        await provider.invalidate_source()
        await provider.apply_config(enabled=False, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        release_exact.set()
        await asyncio.sleep(0.02)
        assert calls == []
        assert provider.take_ready_segment() is None
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_disable_during_worker_discards_late_result_before_replacement(tmp_path):
    client, _ = _client_for_items()
    worker_started = threading.Event()
    allow_worker = threading.Event()
    calls: list[Path] = []

    def worker(candidate, operation_dir: Path, on_normalizing):
        calls.append(operation_dir)
        worker_started.set()
        assert allow_worker.wait(timeout=3)
        on_normalizing()
        final = operation_dir / "normalized.mp3"
        final.write_bytes(b"late-result")
        return jt._PreparedArtifact(final, hashlib.sha256(b"late-result").hexdigest(), 180.0, 11)

    provider = jt.JamendoStreamProvider(tmp_path, lambda: 1, http_client=client, stream_worker=worker)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        assert await asyncio.to_thread(worker_started.wait, 3)
        await provider.apply_config(enabled=False, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        assert provider.retry() is False
        allow_worker.set()
        for _ in range(300):
            if not provider.status()["in_flight"]:
                break
            await asyncio.sleep(0.005)
        assert provider.status()["state"] == "disabled"
        assert provider.take_ready_segment() is None
        assert len(calls) == 1
        assert not calls[0].exists()
    finally:
        allow_worker.set()
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_worker_exposes_normalizing_state_and_coalesces_retry(tmp_path):
    client, _ = _client_for_items()
    normalizing = threading.Event()
    finish = threading.Event()

    def worker(candidate, operation_dir: Path, on_normalizing):
        on_normalizing()
        normalizing.set()
        assert finish.wait(timeout=3)
        final = operation_dir / "normalized.mp3"
        final.write_bytes(b"one-artifact")
        return jt._PreparedArtifact(final, hashlib.sha256(b"one-artifact").hexdigest(), 180.0, 12)

    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, stream_worker=worker)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        assert await asyncio.to_thread(normalizing.wait, 3)
        await _wait_for_state(provider, "normalizing")
        assert provider.retry() is False
        operation_files = list((tmp_path / "jamendo").glob("*/*/*"))
        assert operation_files == [], "the fake worker has not published its sole artifact yet"
        finish.set()
        await _wait_for_state(provider, "ready")
        assert len(list((tmp_path / "jamendo").glob("*/*/*"))) == 1
    finally:
        finish.set()
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_interrupts_backoff_but_coalesces_active_work(tmp_path):
    sleeps: list[float] = []
    sleep_gate = asyncio.Event()
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500, request=request)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await sleep_gate.wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, sleep=fake_sleep)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "degraded")
        for _ in range(100):
            if sleeps:
                break
            await asyncio.sleep(0)
        assert sleeps == [60.0]
        assert provider.retry() is True
        assert provider.retry() is False
        for _ in range(100):
            if request_count >= 2:
                break
            await asyncio.sleep(0.001)
        assert request_count == 2
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_schedule_caps_at_fifteen_minutes(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    delays = []
    for _ in range(6):
        provider._record_transient_failure_unlocked("api_failed")
        delays.append(provider._pending_delay)
    assert delays == [60.0, 120.0, 300.0, 900.0, 900.0, 900.0]
    await client.aclose()


@pytest.mark.asyncio
async def test_expired_ready_lease_is_deleted_instead_of_queued(tmp_path):
    now = [100.0]
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: 0,
        http_client=client,
        stream_worker=_successful_worker(),
        clock=lambda: now[0],
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        lease = provider._lease
        assert lease is not None
        now[0] += 1801
        assert provider.take_ready_segment() is None
        assert not lease.artifact_path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_ready_lease_has_a_hard_expiry_cleanup_callback(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker())
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        lease = provider._lease
        assert lease is not None
        assert provider._lease_expiry_handle is not None
        provider._expire_ready_lease(lease.lease_id)
        assert not lease.artifact_path.exists()
        assert provider._lease is None
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_tampered_ready_artifact_is_released_not_queued(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(), clock=lambda: 100.0
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        lease = provider._lease
        assert lease is not None
        lease.artifact_path.write_bytes(b"tampered")
        assert provider.take_ready_segment() is None
        assert not lease.artifact_path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_replaced_queued_artifact_is_denied_and_deleted_at_playback_admission(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(), clock=lambda: 100.0
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        segment = provider.take_ready_segment()
        assert segment is not None

        segment.path.write_bytes(b"queued-artifact-replaced")

        assert segment.mark_playback_started() is False
        assert provider._lease is None
        assert not segment.path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_symlinked_queued_artifact_is_denied_without_deleting_target(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker(), clock=lambda: 100.0
    )
    target = tmp_path / "operator-owned.mp3"
    target.write_bytes(b"operator-owned")
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        segment = provider.take_ready_segment()
        assert segment is not None
        segment.path.unlink()
        segment.path.symlink_to(target)

        assert segment.mark_playback_started() is False
        assert provider._lease is None
        assert not segment.path.exists()
        assert target.read_bytes() == b"operator-owned"
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_queued_lease_expiry_is_rechecked_at_playback_admission(tmp_path):
    now = [100.0]
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: 0,
        http_client=client,
        stream_worker=_successful_worker(),
        clock=lambda: now[0],
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        segment = provider.take_ready_segment()
        assert segment is not None
        now[0] += 1801.0

        assert segment.mark_playback_started() is False
        assert provider._lease is None
        assert not segment.path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_queued_lease_source_revision_is_rechecked_at_playback_admission(tmp_path):
    revision = [7]
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: revision[0],
        http_client=client,
        stream_worker=_successful_worker(),
        clock=lambda: 100.0,
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        segment = provider.take_ready_segment()
        assert segment is not None
        revision[0] += 1

        assert segment.mark_playback_started() is False
        assert provider._lease is None
        assert not segment.path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_release_info_log_redacts_lease_internals_and_sanitizes_reason(tmp_path, caplog):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker())
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        lease = provider._lease
        assert lease is not None

        assert provider.release(lease.lease_id, reason=f"{lease.operation_id}:{lease.provider_track_id}") is True

        assert "jamendo_lease_released code=released" in caplog.text
        assert lease.operation_id not in caplog.text
        assert lease.provider_track_id not in caplog.text
        assert lease.lease_id not in caplog.text
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_disable_does_not_delete_playing_lease_until_segment_releases(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, stream_worker=_successful_worker())
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        segment = provider.take_ready_segment()
        assert segment is not None
        assert segment.mark_playback_started() is True
        await provider.apply_config(enabled=False, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        assert provider.status()["enabled"] is False
        assert provider.status()["state"] == "playing"
        assert segment.path.exists()
        segment.release()
        assert provider.status()["state"] == "disabled"
        assert not segment.path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_startup_prune_deletes_only_recognized_operation_directories(tmp_path):
    boot = "a" * 32
    operation = "b" * 32
    recognized = tmp_path / "jamendo" / boot / operation
    recognized.mkdir(parents=True)
    (recognized / "normalized.mp3").write_bytes(b"old")
    unknown = tmp_path / "jamendo" / "operator-files"
    unknown.mkdir()
    (unknown / "keep.mp3").write_bytes(b"keep")

    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    try:
        await provider.start()
        assert not recognized.exists()
        assert (unknown / "keep.mp3").read_bytes() == b"keep"
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_symlinked_temp_root_blocks_without_traversal(tmp_path, caplog):
    target = tmp_path / "real"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    client, requests = _client_for_items()
    provider = jt.JamendoStreamProvider(linked, lambda: 0, http_client=client)
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        assert provider.status()["state"] == "blocked"
        assert provider.status()["last_failure_code"] == "temp_root_invalid"
        assert requests == []
        blocked_logs = [
            record for record in caplog.records if record.getMessage().startswith("Jamendo provider blocked")
        ]
        assert [(record.levelno, record.getMessage()) for record in blocked_logs] == [
            (logging.WARNING, "Jamendo provider blocked failure_code=temp_root_invalid provider_code=none")
        ]
        assert _CLIENT_ID not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        await provider.stop()
        await client.aclose()


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> int:
        self.data.extend(chunk)
        return len(chunk)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self.stdin = _Sink()
        self._returncode: int | None = None

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        Path(self.command[-1]).write_bytes(b"normalized-output")
        self._returncode = 0
        return 0

    def poll(self) -> int | None:
        return self._returncode

    def kill(self) -> None:
        self._returncode = -9


class _StreamResponse:
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"content-length": "12"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def iter_bytes(self, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield b"audio-"
        yield b"bytes"

    def close(self) -> None:
        pass


class _SyncClient:
    def __init__(self) -> None:
        self.requested_url = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def stream(self, method: str, url: str):
        assert method == "GET"
        self.requested_url = url
        return _StreamResponse()


def test_default_worker_streams_to_single_partial_then_atomic_final(tmp_path):
    operation_dir = tmp_path / "jamendo" / ("a" * 32) / ("b" * 32)
    operation_dir.mkdir(parents=True)
    candidate = jt._candidate_from_exact_result(_item(), "12345")
    client = _SyncClient()
    processes: list[_FakeProcess] = []

    def popen(command, **kwargs):
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        process = _FakeProcess(command)
        processes.append(process)
        return process

    normalized = []
    with (
        patch.object(jt.httpx, "Client", return_value=client),
        patch.object(jt.subprocess, "Popen", side_effect=popen),
        patch.object(
            jt.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="180.0\n"),
        ),
    ):
        artifact = jt._stream_and_normalize(candidate, operation_dir, lambda: normalized.append(True))

    assert client.requested_url == candidate["audio_url"]
    assert bytes(processes[0].stdin.data) == b"audio-bytes"
    assert normalized == [True]
    assert artifact.path == operation_dir / "normalized.mp3"
    assert artifact.duration_sec == 180.0
    assert [path.name for path in operation_dir.iterdir()] == ["normalized.mp3"]
    assert not (operation_dir / "normalized.part").exists()


def test_default_worker_rejects_content_length_over_actual_byte_cap(tmp_path):
    operation_dir = tmp_path / "jamendo" / ("a" * 32) / ("b" * 32)
    operation_dir.mkdir(parents=True)
    candidate = jt._candidate_from_exact_result(_item(), "12345")

    class OversizeResponse(_StreamResponse):
        headers: ClassVar[dict[str, str]] = {"content-length": str(jt._MAX_AUDIO_BYTES + 1)}

    client = _SyncClient()
    client.stream = lambda method, url: OversizeResponse()  # type: ignore[method-assign]
    with (
        patch.object(jt.httpx, "Client", return_value=client),
        patch.object(jt.subprocess, "Popen") as popen,
        pytest.raises(jt._CandidateRejectedError, match="audio_oversize"),
    ):
        jt._stream_and_normalize(candidate, operation_dir, lambda: None)
    popen.assert_not_called()
    assert not operation_dir.exists()


def test_default_worker_enforces_total_network_deadline(tmp_path):
    operation_dir = tmp_path / "jamendo" / ("a" * 32) / ("b" * 32)
    operation_dir.mkdir(parents=True)
    candidate = jt._candidate_from_exact_result(_item(), "12345")

    class SlowResponse(_StreamResponse):
        def iter_bytes(self, chunk_size: int):
            time.sleep(0.03)
            yield b"late"

    client = _SyncClient()
    client.stream = lambda method, url: SlowResponse()  # type: ignore[method-assign]
    with (
        patch.object(jt.httpx, "Client", return_value=client),
        patch.object(jt, "_NETWORK_DEADLINE_SEC", 0.01),
        patch.object(jt.subprocess, "Popen", side_effect=lambda command, **kwargs: _FakeProcess(command)),
        pytest.raises(jt._TransientError, match="network_timeout"),
    ):
        jt._stream_and_normalize(candidate, operation_dir, lambda: None)
    assert not operation_dir.exists()


@pytest.mark.requires_ffmpeg
def test_default_worker_real_ffmpeg_stream_normalize_and_probe(tmp_path):
    source = tmp_path / "source.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=61",
            "-b:a",
            "96k",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    source_bytes = source.read_bytes()
    operation_dir = tmp_path / "jamendo" / ("a" * 32) / ("b" * 32)
    operation_dir.mkdir(parents=True)
    candidate = jt._candidate_from_exact_result(_item(duration=61), "12345")

    class AudioResponse(_StreamResponse):
        headers: ClassVar[dict[str, str]] = {"content-length": str(len(source_bytes))}

        def iter_bytes(self, chunk_size: int):
            for offset in range(0, len(source_bytes), chunk_size):
                yield source_bytes[offset : offset + chunk_size]

    client = _SyncClient()
    client.stream = lambda method, url: AudioResponse()  # type: ignore[method-assign]
    normalized = []
    with patch.object(jt.httpx, "Client", return_value=client):
        artifact = jt._stream_and_normalize(candidate, operation_dir, lambda: normalized.append(True))

    assert normalized == [True]
    assert 60.0 <= artifact.duration_sec <= 62.0
    assert artifact.sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    assert [path.name for path in operation_dir.iterdir()] == ["normalized.mp3"]


@pytest.mark.asyncio
async def test_stop_returns_while_late_worker_drains_then_cleans(tmp_path):
    client, _ = _client_for_items()
    worker_started = threading.Event()
    allow_worker = threading.Event()
    operation_dirs: list[Path] = []

    def worker(candidate, operation_dir: Path, on_normalizing):
        operation_dirs.append(operation_dir)
        worker_started.set()
        assert allow_worker.wait(timeout=3)
        final = operation_dir / "normalized.mp3"
        final.write_bytes(b"shutdown-late")
        return jt._PreparedArtifact(final, hashlib.sha256(b"shutdown-late").hexdigest(), 180.0, 13)

    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client, stream_worker=worker)
    await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
    assert await asyncio.to_thread(worker_started.wait, 3)
    await asyncio.wait_for(provider.stop(), timeout=0.5)
    assert operation_dirs[0].is_dir(), "late worker keeps its isolated directory until it exits"
    allow_worker.set()
    for _ in range(300):
        if operation_dirs and not operation_dirs[0].exists():
            break
        await asyncio.sleep(0.005)
    assert operation_dirs and not operation_dirs[0].exists()
    await client.aclose()


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "x\x00y", "x" * 201],
)
def test_bounded_text_rejects_invalid_metadata(value):
    with pytest.raises(jt._CandidateRejectedError, match="metadata_invalid"):
        jt._bounded_text(value)


@pytest.mark.parametrize(
    "value",
    [None, "", "https:\\storage.jamendo.com/track.mp3", "https://storage.jamendo.com:bad/track.mp3"],
)
def test_safe_url_rejects_invalid_shapes(value):
    with pytest.raises(jt._CandidateRejectedError, match="invalid_url"):
        jt._safe_https_url(value, host_suffix="jamendo.com")


def test_candidate_rejects_non_mapping_metadata():
    with pytest.raises(jt._CandidateRejectedError, match="metadata_invalid"):
        jt._candidate_from_exact_result([_item()], "12345")


@pytest.mark.parametrize(
    ("status", "body", "error", "code"),
    [
        (401, b"{}", jt._BlockedError, "api_auth_failed"),
        (503, b"{}", jt._TransientError, "api_failed"),
        (200, b"not-json", jt._TransientError, "api_malformed"),
        (200, b"[]", jt._TransientError, "api_malformed"),
    ],
)
def test_validated_api_results_rejects_transport_and_shape_failures(status, body, error, code):
    with pytest.raises(error, match=code):
        jt._validated_api_results(status, body)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3, 3),
        ("3", 3),
        (True, None),
        (-1, None),
        (10_000, None),
        (f"3 {_CLIENT_ID}", None),
    ],
)
def test_provider_code_logging_accepts_only_bounded_numeric_values(value, expected):
    assert jt._coarse_provider_code(value) == expected


@pytest.mark.parametrize(
    ("provider_code", "failure_code"),
    [
        (2, "api_failed"),
        (3, "api_failed"),
        (4, "api_failed"),
        (7, "api_failed"),
        (8, "api_failed"),
        (9, "api_failed"),
        (10, "api_failed"),
        (12, "api_failed"),
        (13, "api_failed"),
        (5, "api_auth_failed"),
        (11, "api_auth_failed"),
    ],
)
def test_known_nonretryable_provider_codes_raise_blocked_errors(provider_code, failure_code):
    body = json.dumps({"headers": {"status": "failed", "code": provider_code}, "results": []}).encode()

    with pytest.raises(jt._BlockedError) as caught:
        jt._validated_api_results(200, body)

    assert caught.value.code == failure_code
    assert caught.value.provider_code == provider_code


@pytest.mark.parametrize(
    ("declared_length", "code"),
    [("invalid", "api_malformed"), ("-1", "api_malformed"), ("65", "metadata_oversize")],
)
@pytest.mark.asyncio
async def test_bounded_api_results_rejects_invalid_declared_lengths(declared_length, code):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": declared_length},
            content=b"{}",
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with (
            patch.object(jt, "_MAX_METADATA_RESPONSE_BYTES", 64),
            pytest.raises(jt._TransientError, match=code),
        ):
            await jt._bounded_api_results(client, {})
    finally:
        await client.aclose()


def test_cleanup_operation_rejects_unowned_paths_and_unlinks_owned_symlink(tmp_path):
    root = tmp_path / "jamendo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    jt._cleanup_operation(outside, root)
    assert outside.is_dir()

    invalid = root / "not-an-operation"
    invalid.mkdir()
    jt._cleanup_operation(invalid, root)
    assert invalid.is_dir()

    target = tmp_path / "operator-owned"
    target.mkdir()
    operation = root / ("a" * 32) / ("b" * 32)
    operation.parent.mkdir()
    operation.symlink_to(target, target_is_directory=True)
    jt._cleanup_operation(operation, root)
    assert not operation.exists()
    assert target.is_dir()


def test_cleanup_operation_swallows_filesystem_failure(tmp_path, caplog):
    root = tmp_path / "jamendo"
    operation = root / ("a" * 32) / ("b" * 32)
    operation.mkdir(parents=True)
    caplog.set_level(logging.DEBUG, logger=jt.__name__)
    with patch.object(Path, "is_symlink", side_effect=OSError("denied")):
        jt._cleanup_operation(operation, root)
    assert "Could not fully remove Jamendo operation" in caplog.text


def test_startup_prune_handles_owned_boot_symlink_and_unknown_child(tmp_path):
    root = tmp_path / "jamendo"
    root.mkdir()
    target = tmp_path / "operator-owned"
    target.mkdir()
    boot_link = root / ("a" * 32)
    boot_link.symlink_to(target, target_is_directory=True)

    boot = root / ("b" * 32)
    boot.mkdir()
    unknown = boot / "keep"
    unknown.write_bytes(b"operator")

    assert jt._prune_startup_operations(tmp_path) == 1
    assert not boot_link.exists()
    assert target.is_dir()
    assert unknown.read_bytes() == b"operator"


def test_startup_prune_rejects_symlinked_provider_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "jamendo").symlink_to(target, target_is_directory=True)
    with pytest.raises(jt._BlockedError, match="temp_root_invalid"):
        jt._prune_startup_operations(tmp_path)


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent_and_owned_client_closes(tmp_path):
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0)
    await provider.start()
    await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
    assert provider.status()["enabled"] is False
    await provider.stop()
    assert provider._http_client.is_closed
    await provider.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["config", "apply_failure", "invalidate"])
async def test_ready_lease_is_released_by_authority_changes(tmp_path, action):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: 0,
        http_client=client,
        stream_worker=_successful_worker(),
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        lease = provider._lease
        assert lease is not None
        # The test owns scheduling; authority invalidation is the behavior under test.
        provider._started = False
        if action == "config":
            await provider.apply_config(
                enabled=True,
                client_id=_CLIENT_ID,
                noncommercial_acknowledged=True,
                tags="jazz",
            )
            assert provider.status()["state"] == "idle"
        elif action == "apply_failure":
            provider.mark_apply_failure()
            assert provider.status()["state"] == "blocked"
        else:
            await provider.invalidate_source()
            assert provider.status()["state"] == "idle"
        assert provider._lease is None
        assert not lease.artifact_path.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_retry_without_existing_task_requests_immediate_work(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    provider._enabled = True
    provider._acknowledged = True
    provider._client_id = _CLIENT_ID
    provider._state = jt.JamendoProviderState.DEGRADED
    provider._last_failure_code = "api_failed"
    assert provider.retry() is True
    assert provider.status()["state"] == "idle"
    assert provider.status()["last_failure_code"] is None
    assert provider.mark_playing("missing") is False
    assert provider.release("missing") is False
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_source_revision_fails_closed_and_stale_backoff_returns(tmp_path):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: True, http_client=client)
    with pytest.raises(jt._BlockedError, match="source_revision_invalid"):
        provider._read_source_revision()
    assert provider._still_current_unlocked(0, jt._client_id_fingerprint(""), 0) is False

    async def stale_sleep(_delay: float) -> None:
        provider._epoch += 1

    provider._sleep = stale_sleep
    await provider._run_after(1.0)
    await provider._prepare_once()
    await client.aclose()


@pytest.mark.asyncio
async def test_task_done_callback_handles_unexpected_and_cancelled_errors(tmp_path, caplog):
    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    caplog.set_level(logging.DEBUG, logger=jt.__name__)

    class FailedTask:
        @staticmethod
        def cancelled() -> bool:
            return False

        @staticmethod
        def exception():
            return RuntimeError("boom")

    class RacyCancelledTask:
        @staticmethod
        def cancelled() -> bool:
            return False

        @staticmethod
        def exception():
            raise asyncio.CancelledError

    provider._on_provider_task_done(FailedTask())  # type: ignore[arg-type]
    provider._on_provider_task_done(RacyCancelledTask())  # type: ignore[arg-type]
    assert caplog.text.count("Jamendo provider task ended unexpectedly") == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_unexpected_current_task_failure_retries_without_logging_exception_details(tmp_path, caplog):
    private_failure = f"{_CLIENT_ID} token=private https://api.jamendo.com/private {tmp_path}"
    sleeps: list[float] = []
    sleep_gate = asyncio.Event()

    def invalid_revision() -> int:
        raise RuntimeError(private_failure)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await sleep_gate.wait()

    client, requests = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, invalid_revision, http_client=client, sleep=fake_sleep)
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "degraded")
        for _ in range(100):
            if sleeps:
                break
            await asyncio.sleep(0)

        assert provider.status()["last_failure_code"] == "internal_error"
        assert sleeps == [60.0]
        assert requests == []
        failure_logs = [
            record for record in caplog.records if record.getMessage().startswith("Jamendo provider attempt failed")
        ]
        assert [(record.levelno, record.getMessage()) for record in failure_logs] == [
            (
                logging.WARNING,
                "Jamendo provider attempt failed failure_code=internal_error provider_code=none retry_in_seconds=60",
            )
        ]
        assert _CLIENT_ID not in caplog.text
        assert "token=private" not in caplog.text
        assert "api.jamendo.com" not in caplog.text
        assert private_failure not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_unexpected_task_callback_after_stop_does_not_revive_provider(tmp_path, caplog):
    private_failure = f"{_CLIENT_ID} token=private https://api.jamendo.com/private {tmp_path}"

    async def fail() -> None:
        raise RuntimeError(private_failure)

    client, _ = _client_for_items()
    provider = jt.JamendoStreamProvider(tmp_path, lambda: 0, http_client=client)
    caplog.set_level(logging.INFO, logger=jt.__name__)
    await provider.start()
    provider._enabled = True
    provider._client_id = _CLIENT_ID
    provider._acknowledged = True
    assert provider._configuration_valid_unlocked() is True
    failed_task = asyncio.create_task(fail())
    await asyncio.sleep(0)
    provider._task = failed_task
    try:
        await provider.stop()
        assert provider._configuration_valid_unlocked() is True
        provider._on_provider_task_done(failed_task)

        assert provider.status()["state"] == "disabled"
        assert provider.status()["last_failure_code"] is None
        assert provider._pending_delay is None
        assert "Jamendo provider attempt failed" not in caplog.text
        assert _CLIENT_ID not in caplog.text
        assert "token=private" not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_prepare_rejects_stale_authority_temp_symlink_and_bad_artifact(tmp_path, caplog):
    candidate = jt._candidate_from_exact_result(_item(), "12345")
    client, _ = _client_for_items()

    stale = jt.JamendoStreamProvider(tmp_path / "stale", lambda: 0, http_client=client)
    stale._enabled = True
    stale._acknowledged = True
    stale._client_id = _CLIENT_ID

    async def stale_discovery(*_args):
        stale._epoch += 1
        return candidate

    with patch.object(stale, "_discover_candidate", side_effect=stale_discovery):
        await stale._prepare_once()
    assert stale.status()["state"] == "idle"

    linked_target = tmp_path / "linked-target"
    linked_target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(linked_target, target_is_directory=True)
    blocked = jt.JamendoStreamProvider(tmp_path / "blocked", lambda: 0, http_client=client)
    blocked._enabled = True
    blocked._acknowledged = True
    blocked._client_id = _CLIENT_ID
    blocked._temp_dir = linked
    blocked._jamendo_root.mkdir(parents=True)

    async def candidate_discovery(*_args):
        return candidate

    with patch.object(blocked, "_discover_candidate", side_effect=candidate_discovery):
        await blocked._prepare_once()
    assert blocked.status()["state"] == "blocked"
    assert blocked.status()["last_failure_code"] == "temp_root_invalid"

    private_hash = f"{_CLIENT_ID} token=private {tmp_path}"

    def invalid_worker(_candidate, operation_dir: Path, _on_normalizing):
        final = operation_dir / "normalized.mp3"
        final.write_bytes(b"bytes")
        return jt._PreparedArtifact(final, private_hash, 180.0, 5)

    bad = jt.JamendoStreamProvider(
        tmp_path / "bad",
        lambda: 0,
        http_client=client,
        stream_worker=invalid_worker,
        retry_delays_sec=(999.0,),
    )
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await bad.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(bad, "degraded")
        assert bad.status()["last_failure_code"] == "lease_invalid"
        assert bad.status()["rejected_count"] == 1
        failure_logs = [
            record for record in caplog.records if record.getMessage().startswith("Jamendo provider attempt failed")
        ]
        assert [(record.levelno, record.getMessage()) for record in failure_logs] == [
            (
                logging.WARNING,
                "Jamendo provider attempt failed failure_code=lease_invalid provider_code=none retry_in_seconds=999",
            )
        ]
        assert _CLIENT_ID not in caplog.text
        assert "token=private" not in caplog.text
        assert private_hash not in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        await stale.stop()
        await blocked.stop()
        await bad.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_discovery_rejects_empty_nonmapping_and_missing_exact_results(tmp_path):
    cases = [
        (_payload([]), "empty_results", 0),
        (_payload(["invalid"]), "metadata_invalid", 1),
    ]
    for index, (body, code, rejected) in enumerate(cases):

        def handler(request: httpx.Request, *, response_body=body) -> httpx.Response:
            return httpx.Response(200, json=response_body, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = jt.JamendoStreamProvider(tmp_path / str(index), lambda: 0, http_client=client)
        try:
            await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
            await _wait_for_state(provider, "degraded")
            assert provider.status()["last_failure_code"] == code
            assert provider.status()["rejected_count"] == rejected
        finally:
            await provider.stop()
            await client.aclose()

    def no_exact_handler(request: httpx.Request) -> httpx.Response:
        results: list[object] = [_item()] if request.url.params.get("id") is None else []
        return httpx.Response(200, json=_payload(results), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(no_exact_handler))
    provider = jt.JamendoStreamProvider(tmp_path / "no-exact", lambda: 0, http_client=client)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "degraded")
        assert provider.status()["last_failure_code"] == "identity_mismatch"
        assert provider.status()["rejected_count"] == 1
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_discovery_applies_country_and_detects_revision_races(tmp_path):
    requests: list[httpx.Request] = []
    revision = [0]

    def stale_after_discovery(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        revision[0] = 1
        return httpx.Response(200, json=_payload([_item()]), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(stale_after_discovery))
    provider = jt.JamendoStreamProvider(tmp_path / "discovery", lambda: revision[0], http_client=client)
    try:
        await provider.start(
            enabled=True,
            client_id=_CLIENT_ID,
            noncommercial_acknowledged=True,
            country="ita",
            order="",
        )
        for _ in range(300):
            if requests and not provider.status()["in_flight"]:
                break
            await asyncio.sleep(0)
        assert requests[0].url.params["country"] == "ITA"
        assert "order" not in requests[0].url.params
    finally:
        await provider.stop()
        await client.aclose()

    revision = [0]

    def stale_after_exact(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("id") is not None:
            revision[0] = 1
        return httpx.Response(200, json=_payload([_item()]), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(stale_after_exact))
    provider = jt.JamendoStreamProvider(tmp_path / "exact", lambda: revision[0], http_client=client)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        for _ in range(300):
            if provider.status()["state"] == "idle" and not provider.status()["in_flight"]:
                break
            await asyncio.sleep(0)
        assert provider.take_ready_segment() is None
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_worker_oserror_maps_to_coarse_failure_and_abandoned_callback_cleans(tmp_path, caplog):
    client, _ = _client_for_items()
    private_failure = f"{_CLIENT_ID} token=private https://storage.jamendo.com/private.mp3 {tmp_path}"

    def failed_worker(*_args):
        raise OSError(private_failure)

    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: 0,
        http_client=client,
        stream_worker=failed_worker,
        retry_delays_sec=(999.0,),
    )
    caplog.set_level(logging.INFO, logger=jt.__name__)
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "degraded")
        assert provider.status()["last_failure_code"] == "api_failed"
        failure_logs = [
            record for record in caplog.records if record.getMessage().startswith("Jamendo provider attempt failed")
        ]
        assert [(record.levelno, record.getMessage()) for record in failure_logs] == [
            (
                logging.WARNING,
                "Jamendo provider attempt failed failure_code=api_failed provider_code=none retry_in_seconds=999",
            )
        ]
        assert _CLIENT_ID not in caplog.text
        assert "token=private" not in caplog.text
        assert "storage.jamendo.com" not in caplog.text
        assert str(tmp_path) not in caplog.text

        operation = provider._jamendo_root / ("a" * 32) / ("b" * 32)
        operation.mkdir(parents=True)
        provider._abandoned_operations.add(operation.name)

        class FailedWorkerTask:
            @staticmethod
            def result():
                raise RuntimeError("late failure")

        provider._on_worker_done(FailedWorkerTask(), operation)  # type: ignore[arg-type]
        assert not operation.exists()
    finally:
        await provider.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_lease_revalidation_and_release_state_edge_cases(tmp_path):
    client, _ = _client_for_items()
    revision: list[object] = [0]
    provider = jt.JamendoStreamProvider(
        tmp_path,
        lambda: revision[0],  # type: ignore[return-value]
        http_client=client,
        stream_worker=_successful_worker(),
    )
    try:
        await provider.start(enabled=True, client_id=_CLIENT_ID, noncommercial_acknowledged=True)
        await _wait_for_state(provider, "ready")
        lease = provider._lease
        assert lease is not None
        provider._expire_ready_lease("wrong-id")
        assert provider._lease is lease

        revision[0] = True
        assert provider.take_ready_segment() is None
        assert provider._lease is None
        provider._release_current_unlocked("consumed")

        revision[0] = 0
        provider._lease = lease
        provider._configuration_apply_blocked = True
        provider._state = jt.JamendoProviderState.READY
        provider._started = False
        provider._release_current_unlocked("config_apply_failed")
        assert provider.status()["state"] == "blocked"

        provider._lease = lease
        provider._configuration_apply_blocked = False
        provider._client_id = ""
        provider._state = jt.JamendoProviderState.READY
        provider._release_current_unlocked("config_changed")
        assert provider.status()["state"] == "needs_config"
    finally:
        await provider.stop()
        await client.aclose()
