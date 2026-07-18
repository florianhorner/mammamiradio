from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import pytest

from mammamiradio.home.ha_playback import (
    MAX_AREA_LENGTH,
    MAX_CANDIDATES,
    MAX_FRIENDLY_NAME_LENGTH,
    MEDIA_CONTENT_TYPE,
    MEDIA_PLAYER_DOMAIN,
    MEDIA_PLAYER_SERVICE,
    MEDIA_SOURCE_LIVE,
    MEDIA_SOURCE_ROOT,
    PLAY_MEDIA_FEATURE,
    HADiscoveryResult,
    HAPlaybackError,
    HAPlaybackReason,
    HAPlaybackService,
    HAPlaybackTimeouts,
)


def _player_state(
    entity_id: str = "media_player.kitchen",
    *,
    state: str = "idle",
    name: object = "Kitchen speaker",
    features: object = PLAY_MEDIA_FEATURE,
    device_class: object = "speaker",
) -> dict[str, object]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {
            "friendly_name": name,
            "supported_features": features,
            "device_class": device_class,
        },
    }


def _default_results() -> dict[str, object]:
    return {
        "get_states": [_player_state()],
        "config/entity_registry/list": [
            {
                "entity_id": "media_player.kitchen",
                "platform": "cast",
                "config_entry_id": "cast-entry",
                "device_id": "kitchen-device",
            }
        ],
        "config/device_registry/list": [{"id": "kitchen-device", "area_id": "kitchen", "name": "Kitchen Nest"}],
        "config/area_registry/list": [{"area_id": "kitchen", "name": "Kitchen"}],
        "config_entries/get": [{"entry_id": "station-entry", "domain": "mammamiradio"}],
        "media_source/browse_media": {
            "media_content_id": MEDIA_SOURCE_ROOT,
            "can_play": False,
            "children": [
                {
                    "media_content_id": MEDIA_SOURCE_LIVE,
                    "can_play": True,
                    "title": "Mamma Mi Radio Live",
                }
            ],
        },
        "call_service": None,
    }


BeforeCommand = Callable[[dict[str, object]], Awaitable[None]]


class _FakeHAServer:
    def __init__(
        self,
        *,
        results: dict[str, object] | None = None,
        greeting: str | bytes | None = None,
        suppress_greeting: bool = False,
        auth_response: dict[str, object] | None = None,
        failures: set[str] | None = None,
        before_command: BeforeCommand | None = None,
    ) -> None:
        self.results = _default_results() if results is None else results
        self.greeting = None if suppress_greeting else greeting or json.dumps({"type": "auth_required"})
        self.auth_response = auth_response or {"type": "auth_ok"}
        self.failures = failures or set()
        self.before_command = before_command
        self.urls: list[str] = []
        self.connect_kwargs: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []
        self.connections = 0

    def connect(self, url: str, **kwargs: object) -> _FakeSocketContext:
        self.urls.append(url)
        self.connect_kwargs.append(dict(kwargs))
        self.connections += 1
        return _FakeSocketContext(_FakeWebSocket(self))


class _FakeSocketContext:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.websocket

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _FakeWebSocket:
    def __init__(self, server: _FakeHAServer) -> None:
        self.server = server
        self.messages: asyncio.Queue[str | bytes] = asyncio.Queue()
        if server.greeting is not None:
            self.messages.put_nowait(server.greeting)

    async def send(self, raw: str) -> None:
        message: dict[str, object] = json.loads(raw)
        self.server.sent.append(message)
        message_type = message.get("type")
        if message_type == "auth":
            self.messages.put_nowait(json.dumps(self.server.auth_response))
            return
        if self.server.before_command is not None:
            await self.server.before_command(message)
        if not isinstance(message_type, str):
            return
        response = {
            "id": message.get("id"),
            "type": "result",
            "success": message_type not in self.server.failures,
            "result": self.server.results.get(message_type),
        }
        if message_type in self.server.failures:
            response["error"] = {"message": "sensitive raw HA failure"}
        self.messages.put_nowait(json.dumps(response))

    async def recv(self) -> str | bytes:
        return await self.messages.get()


def _service(server: _FakeHAServer, **kwargs: Any) -> HAPlaybackService:
    return HAPlaybackService(
        "http://ha.local:8123",
        "secret-token",
        connect_factory=server.connect,
        **kwargs,
    )


async def _reason(coro: Awaitable[object]) -> HAPlaybackError:
    with pytest.raises(HAPlaybackError) as raised:
        await coro
    return raised.value


@pytest.mark.parametrize(
    ("url", "token"),
    [
        (None, "token"),
        ("http://ha.local:8123", None),
        ("not-a-url", "token"),
        ("http://[broken", "token"),
        ("http://user:pass@ha.local:8123", "token"),
    ],
)
async def test_discovery_rejects_missing_or_unsafe_access_without_connecting(
    url: str | None,
    token: str | None,
) -> None:
    server = _FakeHAServer()
    service = HAPlaybackService(url, token, connect_factory=server.connect)

    error = await _reason(service.discover())

    assert error.reason is HAPlaybackReason.HA_ACCESS_MISSING
    assert str(error) == "ha_access_missing"
    assert server.connections == 0


async def test_discovery_maps_auth_failure_without_leaking_ha_or_token_text() -> None:
    server = _FakeHAServer(auth_response={"type": "auth_invalid", "message": "secret-token raw HA explanation"})

    error = await _reason(_service(server).discover())

    assert error.reason is HAPlaybackReason.HA_AUTH_FAILED
    assert str(error) == "ha_auth_failed"
    assert "secret-token" not in str(error)
    assert "raw HA" not in str(error)


async def test_discovery_timeout_is_bounded_and_safe() -> None:
    server = _FakeHAServer(suppress_greeting=True)
    service = _service(
        server,
        timeouts=HAPlaybackTimeouts(
            connect=0.02,
            auth=0.02,
            command=0.02,
            discovery_total=0.04,
            dispatch_total=0.04,
            callback=0.02,
        ),
    )

    error = await _reason(service.discover())

    assert error.reason is HAPlaybackReason.HA_UNREACHABLE
    assert str(error) == "ha_unreachable"


async def test_discovery_commands_share_the_total_envelope_instead_of_the_single_command_timeout() -> None:
    """A large HA registry may answer slowly without exhausting a 6s per-command budget."""

    async def modest_command_delay(_message: dict[str, object]) -> None:
        await asyncio.sleep(0.02)

    server = _FakeHAServer(before_command=modest_command_delay)
    service = _service(
        server,
        timeouts=HAPlaybackTimeouts(
            connect=0.05,
            auth=0.05,
            command=0.01,
            discovery_total=0.25,
            dispatch_total=0.05,
            callback=0.05,
        ),
    )

    result = await service.discover()

    assert [candidate.entity_id for candidate in result.candidates] == ["media_player.kitchen"]
    assert len([message for message in server.sent if message.get("type") != "auth"]) == 6


async def test_discovery_rejects_malformed_results_with_safe_reason() -> None:
    results = _default_results()
    results["get_states"] = {"hostile": ["raw body"]}

    error = await _reason(_service(_FakeHAServer(results=results)).discover())

    assert error.reason is HAPlaybackReason.HA_UNREACHABLE
    assert str(error) == "ha_unreachable"


@pytest.mark.parametrize(
    "results",
    [
        {
            **_default_results(),
            "media_source/browse_media": {
                "media_content_id": MEDIA_SOURCE_ROOT,
                "children": [{"media_content_id": MEDIA_SOURCE_LIVE, "can_play": False}],
            },
        },
        {**_default_results(), "config_entries/get": []},
    ],
)
async def test_discovery_requires_one_browsable_live_station(results: dict[str, object]) -> None:
    error = await _reason(_service(_FakeHAServer(results=results)).discover())

    assert error.reason is HAPlaybackReason.MEDIA_SOURCE_MISSING


async def test_discovery_fails_closed_for_multiple_station_entries() -> None:
    results = _default_results()
    results["config_entries/get"] = [
        {"entry_id": "station-one"},
        {"entry_id": "station-two"},
    ]

    error = await _reason(_service(_FakeHAServer(results=results)).discover())

    assert error.reason is HAPlaybackReason.AMBIGUOUS_STATION


async def test_discovery_sanitizes_labels_and_excludes_all_station_controllers() -> None:
    hostile_name = "\x00  <b>Kitchen</b>\n" + "x" * 300
    states = [
        _player_state(name=hostile_name),
        _player_state("media_player.mammamiradio"),
        _player_state("media_player.mammamiradio_2"),
        _player_state("media_player.renamed_station"),
        _player_state("media_player.station_entry_owned"),
        _player_state("media_player.offline", state="unavailable"),
        _player_state("media_player.no_play", features=0),
    ]
    registry = [
        {
            "entity_id": "media_player.kitchen",
            "platform": "cast",
            "device_id": "kitchen-device",
        },
        {
            "entity_id": "media_player.renamed_station",
            "platform": "mammamiradio",
            "config_entry_id": "other-entry",
        },
        {
            "entity_id": "media_player.station_entry_owned",
            "platform": "other",
            "config_entry_id": "station-entry",
        },
    ]
    results = _default_results()
    results["get_states"] = states
    results["config/entity_registry/list"] = registry
    results["config/area_registry/list"] = [{"area_id": "kitchen", "name": "K" * (MAX_AREA_LENGTH + 20)}]

    result = await _service(_FakeHAServer(results=results)).discover()

    assert [candidate.entity_id for candidate in result.candidates] == ["media_player.kitchen"]
    candidate = result.candidates[0]
    assert "\x00" not in candidate.friendly_name
    assert "\n" not in candidate.friendly_name
    assert len(candidate.friendly_name) == MAX_FRIENDLY_NAME_LENGTH
    assert candidate.area is not None and len(candidate.area) == MAX_AREA_LENGTH
    assert asdict(candidate).keys() == {
        "entity_id",
        "friendly_name",
        "state",
        "device_class",
        "area",
        "supports_play_media",
        "available",
    }
    assert candidate.supports_play_media is True
    assert candidate.available is True


async def test_discovery_caps_candidate_count() -> None:
    results = _default_results()
    results["get_states"] = [
        _player_state(f"media_player.speaker_{index:03d}", name=f"Speaker {index:03d}")
        for index in range(MAX_CANDIDATES + 25)
    ]
    results["config/entity_registry/list"] = []
    results["config/device_registry/list"] = []
    results["config/area_registry/list"] = []

    result = await _service(_FakeHAServer(results=results)).discover()

    assert len(result.candidates) == MAX_CANDIDATES


async def test_discovery_uses_one_cooldown_result_and_existing_websocket_convention() -> None:
    server = _FakeHAServer()
    service = _service(server, discovery_cooldown=60)

    first = await service.discover()
    second = await service.discover()

    assert first is second
    assert server.connections == 1
    assert server.urls == ["ws://ha.local:8123/api/websocket"]
    assert server.connect_kwargs[0]["max_size"] == 4 * 1024 * 1024


async def test_cancelling_one_discovery_waiter_does_not_cancel_shared_probe() -> None:
    command_started = asyncio.Event()
    release_command = asyncio.Event()

    async def block_first_command(message: dict[str, object]) -> None:
        if message.get("type") == "get_states":
            command_started.set()
            await release_command.wait()

    server = _FakeHAServer(before_command=block_first_command)
    service = _service(server)
    cancelled_waiter = asyncio.create_task(service.discover(force=True))
    await asyncio.wait_for(command_started.wait(), timeout=1)
    surviving_waiter = asyncio.create_task(service.discover(force=True))
    await asyncio.sleep(0)

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    release_command.set()
    result = await asyncio.wait_for(surviving_waiter, timeout=1)

    assert isinstance(result, HADiscoveryResult)
    assert server.connections == 1


async def test_discovery_does_not_return_results_from_superseded_ha_access() -> None:
    command_started = asyncio.Event()
    release_command = asyncio.Event()

    async def block_first_command(message: dict[str, object]) -> None:
        if message.get("type") == "get_states":
            command_started.set()
            await release_command.wait()

    server = _FakeHAServer(before_command=block_first_command)
    service = _service(server)
    stale_discovery = asyncio.create_task(service.discover(force=True))
    await asyncio.wait_for(command_started.wait(), timeout=1)

    service.configure("http://replacement-ha.local:8123", "replacement-token")
    release_command.set()
    error = await _reason(stale_discovery)

    assert error.reason is HAPlaybackReason.HA_UNREACHABLE


async def test_play_revalidates_target_and_sends_only_the_fixed_service_payload() -> None:
    server = _FakeHAServer()
    resumed = 0
    persisted: list[str] = []

    async def resume() -> None:
        nonlocal resumed
        resumed += 1

    async def persist(entity_id: str) -> str:
        persisted.append(entity_id)
        return "attempt-123"

    result = await _service(
        server,
        resume_station=resume,
        persist_accepted_attempt=persist,
    ).play("media_player.kitchen")

    assert result.accepted is True
    assert result.station_resumed is True
    assert result.receipt_persisted is True
    assert result.attempt_id == "attempt-123"
    assert resumed == 1
    assert persisted == ["media_player.kitchen"]
    service_calls = [message for message in server.sent if message.get("type") == "call_service"]
    assert service_calls == [
        {
            "id": 1,
            "type": "call_service",
            "domain": MEDIA_PLAYER_DOMAIN,
            "service": MEDIA_PLAYER_SERVICE,
            "service_data": {
                "media_content_id": MEDIA_SOURCE_LIVE,
                "media_content_type": MEDIA_CONTENT_TYPE,
            },
            "target": {"entity_id": "media_player.kitchen"},
        }
    ]
    assert "volume" not in json.dumps(server.sent).lower()
    assert server.connections == 2


async def test_play_rejects_an_unavailable_selected_target_without_dispatch() -> None:
    results = _default_results()
    results["get_states"] = [
        _player_state("media_player.kitchen", state="unavailable"),
        _player_state("media_player.living_room", name="Living room"),
    ]
    server = _FakeHAServer(results=results)

    error = await _reason(_service(server).play("media_player.kitchen"))

    assert error.reason is HAPlaybackReason.PLAYER_UNAVAILABLE
    assert all(message.get("type") != "call_service" for message in server.sent)


async def test_dispatch_failure_reports_that_station_was_already_resumed_and_does_not_retry() -> None:
    server = _FakeHAServer(failures={"call_service"})
    resumes = 0

    async def resume() -> None:
        nonlocal resumes
        resumes += 1

    error = await _reason(_service(server, resume_station=resume).play("media_player.kitchen"))

    assert error.reason is HAPlaybackReason.SERVICE_REJECTED
    assert error.station_resumed is True
    assert resumes == 1
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_accepted_dispatch_returns_partial_truth_when_receipt_persistence_fails() -> None:
    server = _FakeHAServer()

    async def fail_persistence(_entity_id: str) -> str:
        raise OSError("private disk path")

    result = await _service(server, persist_accepted_attempt=fail_persistence).play("media_player.kitchen")

    assert result.accepted is True
    assert result.receipt_persisted is False
    assert result.reason is HAPlaybackReason.RECEIPT_UNAVAILABLE
    assert result.attempt_id is None
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_pending_receipt_retry_never_rediscovers_resumes_or_dispatches_again() -> None:
    server = _FakeHAServer()
    persistence_attempts = 0
    resumes = 0

    async def resume() -> None:
        nonlocal resumes
        resumes += 1

    async def persist(_entity_id: str) -> str:
        nonlocal persistence_attempts
        persistence_attempts += 1
        if persistence_attempts == 1:
            raise OSError("temporary receipt failure")
        return "attempt-after-storage-repair"

    service = _service(
        server,
        resume_station=resume,
        persist_accepted_attempt=persist,
    )
    partial = await service.play("media_player.kitchen")
    connections_after_play = server.connections
    assert service.pending_receipt_entity_id() == "media_player.kitchen"

    recovered = await service.persist_pending_receipt("media_player.kitchen")

    assert partial.accepted is True
    assert partial.receipt_persisted is False
    assert recovered.accepted is True
    assert recovered.receipt_persisted is True
    assert recovered.attempt_id == "attempt-after-storage-repair"
    assert persistence_attempts == 2
    assert resumes == 1
    assert server.connections == connections_after_play
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1
    assert service.pending_receipt_entity_id() is None

    error = await _reason(service.persist_pending_receipt("media_player.kitchen"))
    assert error.reason is HAPlaybackReason.RECEIPT_RECOVERY_MISSING


async def test_pending_receipt_retry_requires_matching_current_ha_acceptance() -> None:
    server = _FakeHAServer()
    persistence_attempts = 0

    async def persist(_entity_id: str) -> str:
        nonlocal persistence_attempts
        persistence_attempts += 1
        raise OSError("receipt unavailable")

    service = _service(server, persist_accepted_attempt=persist)

    no_pending = await _reason(service.persist_pending_receipt("media_player.kitchen"))
    assert no_pending.reason is HAPlaybackReason.RECEIPT_RECOVERY_MISSING

    partial = await service.play("media_player.kitchen")
    assert partial.receipt_persisted is False
    assert service.pending_receipt_entity_id() == "media_player.kitchen"
    service.configure("http://replacement-ha.local:8123", "replacement-token")
    assert service.pending_receipt_entity_id() is None

    superseded = await _reason(service.persist_pending_receipt("media_player.kitchen"))
    assert superseded.reason is HAPlaybackReason.RECEIPT_RECOVERY_MISSING
    assert persistence_attempts == 1
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_pending_receipt_retry_rejects_a_different_entity_without_persisting() -> None:
    server = _FakeHAServer()
    persistence_attempts = 0

    async def persist(_entity_id: str) -> str:
        nonlocal persistence_attempts
        persistence_attempts += 1
        raise OSError("receipt unavailable")

    service = _service(server, persist_accepted_attempt=persist)
    partial = await service.play("media_player.kitchen")

    wrong_entity = await _reason(service.persist_pending_receipt("media_player.living_room"))

    assert partial.receipt_persisted is False
    assert wrong_entity.reason is HAPlaybackReason.RECEIPT_RECOVERY_MISSING
    assert persistence_attempts == 1
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_accepted_dispatch_waits_past_callback_timeout_for_durable_receipt() -> None:
    server = _FakeHAServer()
    persisted: list[str] = []

    async def slow_persistence(entity_id: str) -> str:
        await asyncio.sleep(0.03)
        persisted.append(entity_id)
        return "attempt-after-slow-fsync"

    result = await _service(
        server,
        persist_accepted_attempt=slow_persistence,
        timeouts=HAPlaybackTimeouts(callback=0.01),
    ).play("media_player.kitchen")

    assert result.accepted is True
    assert result.receipt_persisted is True
    assert result.attempt_id == "attempt-after-slow-fsync"
    assert persisted == ["media_player.kitchen"]
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_duplicate_play_returns_request_in_flight_without_queueing_or_dispatching_twice() -> None:
    resume_started = asyncio.Event()
    release_resume = asyncio.Event()

    async def blocking_resume() -> None:
        resume_started.set()
        await release_resume.wait()

    server = _FakeHAServer()
    service = _service(server, resume_station=blocking_resume)
    first = asyncio.create_task(service.play("media_player.kitchen"))
    await asyncio.wait_for(resume_started.wait(), timeout=1)

    duplicate_error = await _reason(service.play("media_player.kitchen"))
    release_resume.set()
    first_result = await asyncio.wait_for(first, timeout=1)

    assert duplicate_error.reason is HAPlaybackReason.REQUEST_IN_FLIGHT
    assert first_result.accepted is True
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_cancel_after_dispatch_send_drains_ack_and_persists_before_unlocking() -> None:
    dispatch_sent = asyncio.Event()
    release_dispatch_ack = asyncio.Event()
    persisted: list[str] = []

    async def block_dispatch_ack(message: dict[str, object]) -> None:
        if message.get("type") == "call_service":
            dispatch_sent.set()
            await release_dispatch_ack.wait()

    async def persist(entity_id: str) -> str:
        persisted.append(entity_id)
        return "attempt-after-cancel"

    server = _FakeHAServer(before_command=block_dispatch_ack)
    service = _service(server, persist_accepted_attempt=persist)
    first = asyncio.create_task(service.play("media_player.kitchen"))
    await asyncio.wait_for(dispatch_sent.wait(), timeout=1)

    first.cancel()
    await asyncio.sleep(0)
    duplicate_error = await _reason(service.play("media_player.kitchen"))
    assert not first.done()

    release_dispatch_ack.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=1)

    assert duplicate_error.reason is HAPlaybackReason.REQUEST_IN_FLIGHT
    assert persisted == ["media_player.kitchen"]
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_repeated_cancel_after_dispatch_keeps_play_mutex_until_transaction_settles() -> None:
    dispatch_sent = asyncio.Event()
    release_dispatch_ack = asyncio.Event()
    persisted: list[str] = []

    async def block_dispatch_ack(message: dict[str, object]) -> None:
        if message.get("type") == "call_service":
            dispatch_sent.set()
            await release_dispatch_ack.wait()

    async def persist(entity_id: str) -> str:
        persisted.append(entity_id)
        return "attempt-after-double-cancel"

    server = _FakeHAServer(before_command=block_dispatch_ack)
    service = _service(server, persist_accepted_attempt=persist)
    first = asyncio.create_task(service.play("media_player.kitchen"))
    await asyncio.wait_for(dispatch_sent.wait(), timeout=1)

    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)

    assert not first.done()
    duplicate_error = await _reason(service.play("media_player.kitchen"))
    assert duplicate_error.reason is HAPlaybackReason.REQUEST_IN_FLIGHT

    release_dispatch_ack.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, timeout=1)

    assert persisted == ["media_player.kitchen"]
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1


async def test_repeated_cancel_keeps_receipt_retry_mutex_until_persistence_settles() -> None:
    server = _FakeHAServer()
    persistence_attempts = 0
    retry_started = asyncio.Event()
    release_retry = asyncio.Event()

    async def persist(_entity_id: str) -> str:
        nonlocal persistence_attempts
        persistence_attempts += 1
        if persistence_attempts == 1:
            raise OSError("temporary receipt failure")
        retry_started.set()
        await release_retry.wait()
        return "attempt-after-retry-cancel"

    service = _service(server, persist_accepted_attempt=persist)
    partial = await service.play("media_player.kitchen")
    assert partial.receipt_persisted is False

    retry = asyncio.create_task(service.persist_pending_receipt("media_player.kitchen"))
    await asyncio.wait_for(retry_started.wait(), timeout=1)
    retry.cancel()
    await asyncio.sleep(0)
    retry.cancel()
    await asyncio.sleep(0)

    assert not retry.done()
    duplicate_error = await _reason(service.persist_pending_receipt("media_player.kitchen"))
    assert duplicate_error.reason is HAPlaybackReason.REQUEST_IN_FLIGHT

    release_retry.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(retry, timeout=1)

    assert persistence_attempts == 2
    assert sum(message.get("type") == "call_service" for message in server.sent) == 1
    settled = await _reason(service.persist_pending_receipt("media_player.kitchen"))
    assert settled.reason is HAPlaybackReason.RECEIPT_RECOVERY_MISSING
