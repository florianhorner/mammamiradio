"""Bounded, on-demand Home Assistant speaker discovery and playback.

This module deliberately owns no startup hook, station state, or receipt storage.
Callers inject the station-resume and accepted-attempt persistence seams when
they need them.  Home Assistant data is reduced to the small, safe projection
defined by :class:`HAPlayerCandidate` before it leaves this adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar, cast
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect as websocket_connect

MEDIA_SOURCE_ROOT = "media-source://mammamiradio"
MEDIA_SOURCE_LIVE = f"{MEDIA_SOURCE_ROOT}/live"
MEDIA_CONTENT_TYPE = "music"
MEDIA_PLAYER_DOMAIN = "media_player"
MEDIA_PLAYER_SERVICE = "play_media"

# Home Assistant's MediaPlayerEntityFeature.PLAY_MEDIA bit.  Keeping the value
# local avoids importing the very large optional Home Assistant package.
PLAY_MEDIA_FEATURE = 512

MAX_CANDIDATES = 100
MAX_ENTITY_SCAN = 4_096
MAX_REGISTRY_ITEMS = 8_192
MAX_BROWSE_NODES = 256
MAX_RESULT_MESSAGES = 64
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_ENTITY_ID_LENGTH = 255
MAX_FRIENDLY_NAME_LENGTH = 120
MAX_STATE_LENGTH = 40
MAX_DEVICE_CLASS_LENGTH = 64
MAX_AREA_LENGTH = 80
MAX_ATTEMPT_ID_LENGTH = 128

_ENTITY_ID_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
_STATION_ENTITY_RE = re.compile(r"^media_player\.mammamiradio(?:_\d+)?$")
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class HAPlaybackReason(StrEnum):
    """Stable, safe reasons suitable for mapping at an API boundary."""

    HA_ACCESS_MISSING = "ha_access_missing"
    HA_AUTH_FAILED = "ha_auth_failed"
    HA_UNREACHABLE = "ha_unreachable"
    MEDIA_SOURCE_MISSING = "media_source_missing"
    AMBIGUOUS_STATION = "ambiguous_station"
    NO_PLAYERS = "no_players"
    PLAYER_UNAVAILABLE = "player_unavailable"
    SERVICE_REJECTED = "service_rejected"
    REQUEST_IN_FLIGHT = "request_in_flight"
    RECEIPT_UNAVAILABLE = "receipt_unavailable"
    RECEIPT_RECOVERY_MISSING = "receipt_recovery_missing"


class HAPlaybackError(RuntimeError):
    """A playback failure containing only a stable reason and partial-state bit."""

    def __init__(
        self,
        reason: HAPlaybackReason,
        *,
        station_resumed: bool = False,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.station_resumed = station_resumed


@dataclass(frozen=True, slots=True)
class HAPlaybackTimeouts:
    """Hard wall-clock bounds for every external operation."""

    connect: float = 5.0
    auth: float = 5.0
    command: float = 6.0
    discovery_total: float = 15.0
    dispatch_total: float = 12.0
    callback: float = 5.0

    def __post_init__(self) -> None:
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                self.connect,
                self.auth,
                self.command,
                self.discovery_total,
                self.dispatch_total,
                self.callback,
            )
        ):
            raise ValueError("HA playback timeouts must be positive")


@dataclass(frozen=True, slots=True)
class HAPlayerCandidate:
    """The complete public projection of one compatible-looking HA player."""

    entity_id: str
    friendly_name: str
    state: str
    device_class: str | None
    area: str | None
    supports_play_media: bool
    available: bool


@dataclass(frozen=True, slots=True)
class HADiscoveryResult:
    """Sanitized results of a successful HA/HACS preflight."""

    candidates: tuple[HAPlayerCandidate, ...]
    media_source_ready: bool = True

    @property
    def players(self) -> tuple[HAPlayerCandidate, ...]:
        """Compatibility-friendly alias for route code that speaks in players."""
        return self.candidates


@dataclass(frozen=True, slots=True)
class HAPlayResult:
    """Truthful result after Home Assistant accepts a play request."""

    entity_id: str
    accepted: bool
    station_resumed: bool
    receipt_persisted: bool
    attempt_id: str | None = None
    reason: HAPlaybackReason | None = None


ResumeStation = Callable[[], Awaitable[None]]
PersistAcceptedAttempt = Callable[[str], Awaitable[str]]
_TransactionResult = TypeVar("_TransactionResult")


async def _shield_and_drain_owned_task(task: asyncio.Task[_TransactionResult]) -> _TransactionResult:
    """Keep an external-side-effect transaction owned through every cancellation.

    One HTTP task can receive more than one ``cancel()`` while disconnecting.
    Each cancellation is remembered, but the independently-owned transaction
    remains shielded and fully drained before its mutex may be released.
    """
    cancellation_seen = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation_seen = True
        except BaseException:
            break

    if cancellation_seen:
        try:
            task.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    return task.result()


class _WebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class _SocketContext(Protocol):
    async def __aenter__(self) -> _WebSocket: ...

    async def __aexit__(self, _exc_type: object, exc: object, _traceback: object) -> object: ...


ConnectFactory = Callable[..., _SocketContext]


@dataclass(frozen=True, slots=True)
class _AccessConfig:
    ha_url: str
    ha_token: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _CachedDiscovery:
    expires_at: float
    result: HADiscoveryResult | None = None
    reason: HAPlaybackReason | None = None


@dataclass(frozen=True, slots=True)
class _PendingAcceptedAttempt:
    """One HA-accepted dispatch whose local receipt still needs persistence."""

    access_fingerprint: str
    entity_id: str
    station_resumed: bool


@dataclass(frozen=True, slots=True)
class _RegistryEntity:
    platform: str | None
    config_entry_id: str | None
    device_id: str | None
    area_id: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class _RegistryDevice:
    area_id: str | None
    name: str | None


class HAPlaybackService:
    """Discover HA players and dispatch the fixed Mamma Mi Radio media source."""

    def __init__(
        self,
        ha_url: str | None,
        ha_token: str | None,
        *,
        resume_station: ResumeStation | None = None,
        persist_accepted_attempt: PersistAcceptedAttempt | None = None,
        timeouts: HAPlaybackTimeouts | None = None,
        discovery_cooldown: float = 2.0,
        connect_factory: ConnectFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(discovery_cooldown) or discovery_cooldown < 0:
            raise ValueError("HA playback discovery cooldown cannot be negative")
        self._access = _make_access_config(ha_url, ha_token)
        self._resume_station = resume_station
        self._persist_accepted_attempt = persist_accepted_attempt
        self._timeouts = timeouts or HAPlaybackTimeouts()
        self._discovery_cooldown = discovery_cooldown
        self._connect_factory = connect_factory or cast(ConnectFactory, websocket_connect)
        self._clock = clock

        self._discovery_guard = asyncio.Lock()
        self._discovery_flights: dict[str, asyncio.Task[HADiscoveryResult]] = {}
        self._discovery_cache: dict[str, _CachedDiscovery] = {}

        self._play_guard = asyncio.Lock()
        self._play_active = False
        self._pending_accepted_attempt: _PendingAcceptedAttempt | None = None

    def configure(self, ha_url: str | None, ha_token: str | None) -> None:
        """Atomically replace the HA access snapshot used by future requests."""
        self._access = _make_access_config(ha_url, ha_token)
        self._discovery_cache.clear()
        self._pending_accepted_attempt = None

    def pending_receipt_entity_id(self) -> str | None:
        """Project the current server-owned recovery proof without HA I/O."""
        pending = self._pending_accepted_attempt
        if (
            pending is None
            or pending.access_fingerprint != self._access.fingerprint
            or self._persist_accepted_attempt is None
        ):
            return None
        return pending.entity_id

    async def discover(self, *, force: bool = False) -> HADiscoveryResult:
        """Run or join one shielded discovery for the current HA configuration."""
        return await self._discover_for_access(self._access, force=force)

    async def play(self, entity_id: str) -> HAPlayResult:
        """Revalidate and dispatch the fixed live source to exactly one target."""
        target = _valid_entity_id(entity_id)
        if target is None:
            raise HAPlaybackError(HAPlaybackReason.PLAYER_UNAVAILABLE)

        access = self._access
        _require_access(access)

        async with self._play_guard:
            if self._play_active:
                raise HAPlaybackError(HAPlaybackReason.REQUEST_IN_FLIGHT)
            self._play_active = True

        try:
            return await self._play_once(access, target)
        finally:
            async with self._play_guard:
                self._play_active = False

    async def persist_pending_receipt(self, entity_id: str) -> HAPlayResult:
        """Retry only local receipt persistence for one proven HA acceptance.

        This deliberately performs no discovery, station resume, or HA service
        call.  The pending fact is owned by this service and survives only in
        this process, so a client cannot manufacture audible proof by posting an
        entity id that was never accepted by Home Assistant.
        """
        target = _valid_entity_id(entity_id)
        if target is None:
            raise HAPlaybackError(HAPlaybackReason.RECEIPT_RECOVERY_MISSING)

        access = self._access
        _require_access(access)
        async with self._play_guard:
            pending = self._pending_accepted_attempt
            if self._play_active:
                raise HAPlaybackError(HAPlaybackReason.REQUEST_IN_FLIGHT)
            if (
                pending is None
                or pending.access_fingerprint != access.fingerprint
                or pending.entity_id != target
                or self._persist_accepted_attempt is None
            ):
                raise HAPlaybackError(HAPlaybackReason.RECEIPT_RECOVERY_MISSING)
            self._play_active = True

        transaction = asyncio.create_task(self._complete_pending_receipt(pending))
        try:
            return await _shield_and_drain_owned_task(transaction)
        finally:
            async with self._play_guard:
                self._play_active = False

    async def _discover_for_access(
        self,
        access: _AccessConfig,
        *,
        force: bool,
    ) -> HADiscoveryResult:
        _require_access(access)
        fingerprint = access.fingerprint

        async with self._discovery_guard:
            now = self._clock()
            cached = self._discovery_cache.get(fingerprint)
            if not force and cached is not None and cached.expires_at > now:
                if cached.reason is not None:
                    raise HAPlaybackError(cached.reason)
                if cached.result is not None:
                    return cached.result

            task = self._discovery_flights.get(fingerprint)
            if task is None or task.done():
                task = asyncio.create_task(self._run_discovery(access))
                self._discovery_flights[fingerprint] = task

        # A disconnected/cancelled HTTP waiter must not cancel the HA probe that
        # other waiters have joined.
        result = await asyncio.shield(task)
        if access.fingerprint != self._access.fingerprint:
            # Configuration changed while the shared request was running. Do
            # not return labels or readiness from the superseded HA instance.
            raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE)
        return result

    async def _run_discovery(self, access: _AccessConfig) -> HADiscoveryResult:
        current_task = asyncio.current_task()
        try:
            result = await self._discover_from_ha(access)
        except HAPlaybackError as exc:
            await self._cache_discovery(access, reason=exc.reason)
            raise
        except Exception:
            reason = HAPlaybackReason.HA_UNREACHABLE
            await self._cache_discovery(access, reason=reason)
            raise HAPlaybackError(reason) from None
        else:
            await self._cache_discovery(access, result=result)
            return result
        finally:
            async with self._discovery_guard:
                if self._discovery_flights.get(access.fingerprint) is current_task:
                    self._discovery_flights.pop(access.fingerprint, None)

    async def _cache_discovery(
        self,
        access: _AccessConfig,
        *,
        result: HADiscoveryResult | None = None,
        reason: HAPlaybackReason | None = None,
    ) -> None:
        async with self._discovery_guard:
            if access.fingerprint != self._access.fingerprint:
                return
            self._discovery_cache[access.fingerprint] = _CachedDiscovery(
                expires_at=self._clock() + self._discovery_cooldown,
                result=result,
                reason=reason,
            )

    async def _discover_from_ha(self, access: _AccessConfig) -> HADiscoveryResult:
        commands = (
            (1, {"id": 1, "type": "get_states"}, HAPlaybackReason.HA_UNREACHABLE),
            (
                2,
                {"id": 2, "type": "config/entity_registry/list"},
                HAPlaybackReason.HA_UNREACHABLE,
            ),
            (
                3,
                {"id": 3, "type": "config/device_registry/list"},
                HAPlaybackReason.HA_UNREACHABLE,
            ),
            (
                4,
                {"id": 4, "type": "config/area_registry/list"},
                HAPlaybackReason.HA_UNREACHABLE,
            ),
            (
                5,
                {"id": 5, "type": "config_entries/get", "domain": "mammamiradio"},
                HAPlaybackReason.MEDIA_SOURCE_MISSING,
            ),
            (
                6,
                {
                    "id": 6,
                    "type": "media_source/browse_media",
                    "media_content_id": MEDIA_SOURCE_ROOT,
                },
                HAPlaybackReason.MEDIA_SOURCE_MISSING,
            ),
        )

        try:
            async with asyncio.timeout(self._timeouts.discovery_total):
                async with self._connect(access) as ws:
                    await self._authenticate(ws, access.ha_token)
                    results = await self._run_commands(
                        ws,
                        commands,
                        timeout_budget=self._timeouts.discovery_total,
                    )
        except HAPlaybackError:
            raise
        except TimeoutError:
            raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE) from None
        except Exception:
            raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE) from None

        states = _list_result(results, 1)
        registry_entities = _list_result(results, 2)
        registry_devices = _list_result(results, 3)
        registry_areas = _list_result(results, 4)
        config_entries = _list_result(results, 5, reason=HAPlaybackReason.MEDIA_SOURCE_MISSING)
        browse = results.get(6)
        if not isinstance(browse, Mapping):
            raise HAPlaybackError(HAPlaybackReason.MEDIA_SOURCE_MISSING)

        station_entries = _station_config_entry_ids(config_entries)
        if not station_entries:
            raise HAPlaybackError(HAPlaybackReason.MEDIA_SOURCE_MISSING)
        if len(station_entries) > 1:
            raise HAPlaybackError(HAPlaybackReason.AMBIGUOUS_STATION)
        if not _has_live_media_source(browse):
            raise HAPlaybackError(HAPlaybackReason.MEDIA_SOURCE_MISSING)

        candidates = _build_candidates(
            states,
            registry_entities,
            registry_devices,
            registry_areas,
            station_entries,
        )
        if not candidates:
            raise HAPlaybackError(HAPlaybackReason.NO_PLAYERS)
        return HADiscoveryResult(candidates=tuple(candidates))

    async def _play_once(self, access: _AccessConfig, entity_id: str) -> HAPlayResult:
        discovery = await self._discover_for_access(access, force=True)
        if not any(candidate.entity_id == entity_id for candidate in discovery.candidates):
            raise HAPlaybackError(HAPlaybackReason.PLAYER_UNAVAILABLE)

        station_resumed = False
        if self._resume_station is not None:
            # The resume callback flips a durable stop marker and then live
            # memory; a cancellation delivered between those steps would tear
            # them apart. Own it like the dispatch transaction: a timeout or
            # client disconnect fails the play attempt but the transition
            # always drains, overshooting the deadline by at most one write.
            resume_callback = self._resume_station

            async def _run_resume() -> None:
                await resume_callback()

            resume_task: asyncio.Task[None] = asyncio.create_task(_run_resume())
            try:
                await asyncio.wait_for(
                    _shield_and_drain_owned_task(resume_task),
                    timeout=self._timeouts.callback,
                )
            except TimeoutError:
                raise HAPlaybackError(
                    HAPlaybackReason.SERVICE_REJECTED,
                    station_resumed=(
                        resume_task.done() and not resume_task.cancelled() and resume_task.exception() is None
                    ),
                ) from None
            except Exception:
                raise HAPlaybackError(HAPlaybackReason.SERVICE_REJECTED) from None
            station_resumed = True

        # Once dispatch starts it may have sent an external side effect before
        # the caller disconnects. Own and drain dispatch plus receipt persistence
        # as one transaction so cancellation cannot release the play mutex and
        # invite a second play while the first HA acknowledgement is still due.
        transaction_task = asyncio.create_task(self._complete_play_transaction(access, entity_id, station_resumed))
        return await _shield_and_drain_owned_task(transaction_task)

    async def _complete_play_transaction(
        self,
        access: _AccessConfig,
        entity_id: str,
        station_resumed: bool,
    ) -> HAPlayResult:
        """Dispatch once and settle accepted-attempt truth before releasing play."""
        try:
            await self._dispatch_play(access, entity_id)
        except HAPlaybackError as exc:
            raise HAPlaybackError(exc.reason, station_resumed=station_resumed) from None
        self._remember_pending_acceptance(access, entity_id, station_resumed)

        if self._persist_accepted_attempt is None:
            # An accepted HA side effect without a persistence callback is
            # explicitly unsaved.  Do not expose a third "unknown" state that
            # a First Listen caller could mistake for durable verification.
            return _receipt_failure_result(entity_id, station_resumed)

        try:
            # HA has already accepted an external side effect.  Receipt storage
            # is the local half of that transaction, and the caller owns and
            # shields this whole coroutine until it settles.  Do not impose the
            # generic callback timeout here: FirstListenReceiptStore drains its
            # filesystem worker on cancellation, so a slow successful fsync
            # would otherwise be reported as a failure after it had committed,
            # forcing the operator to dispatch the same playback a second time.
            attempt_id = await self._persist_accepted_attempt(entity_id)
        except TimeoutError:
            return _receipt_failure_result(entity_id, station_resumed)
        except Exception:
            return _receipt_failure_result(entity_id, station_resumed)

        if not _valid_attempt_id(attempt_id):
            return _receipt_failure_result(entity_id, station_resumed)
        self._pending_accepted_attempt = None
        return HAPlayResult(
            entity_id=entity_id,
            accepted=True,
            station_resumed=station_resumed,
            receipt_persisted=True,
            attempt_id=attempt_id,
        )

    def _remember_pending_acceptance(
        self,
        access: _AccessConfig,
        entity_id: str,
        station_resumed: bool,
    ) -> None:
        if access.fingerprint != self._access.fingerprint:
            return
        self._pending_accepted_attempt = _PendingAcceptedAttempt(
            access_fingerprint=access.fingerprint,
            entity_id=entity_id,
            station_resumed=station_resumed,
        )

    async def _complete_pending_receipt(self, pending: _PendingAcceptedAttempt) -> HAPlayResult:
        persist = self._persist_accepted_attempt
        if persist is None:
            raise HAPlaybackError(
                HAPlaybackReason.RECEIPT_UNAVAILABLE,
                station_resumed=pending.station_resumed,
            )
        try:
            attempt_id = await persist(pending.entity_id)
        except Exception:
            raise HAPlaybackError(
                HAPlaybackReason.RECEIPT_UNAVAILABLE,
                station_resumed=pending.station_resumed,
            ) from None
        if not _valid_attempt_id(attempt_id):
            raise HAPlaybackError(
                HAPlaybackReason.RECEIPT_UNAVAILABLE,
                station_resumed=pending.station_resumed,
            )
        if self._pending_accepted_attempt == pending:
            self._pending_accepted_attempt = None
        return HAPlayResult(
            entity_id=pending.entity_id,
            accepted=True,
            station_resumed=pending.station_resumed,
            receipt_persisted=True,
            attempt_id=attempt_id,
        )

    async def _dispatch_play(self, access: _AccessConfig, entity_id: str) -> None:
        command = (
            (
                1,
                {
                    "id": 1,
                    "type": "call_service",
                    "domain": MEDIA_PLAYER_DOMAIN,
                    "service": MEDIA_PLAYER_SERVICE,
                    "service_data": {
                        "media_content_id": MEDIA_SOURCE_LIVE,
                        "media_content_type": MEDIA_CONTENT_TYPE,
                    },
                    "target": {"entity_id": entity_id},
                },
                HAPlaybackReason.SERVICE_REJECTED,
            ),
        )
        accepted = False
        try:
            async with asyncio.timeout(self._timeouts.dispatch_total):
                try:
                    async with self._connect(access) as ws:
                        await self._authenticate(ws, access.ha_token)
                        await self._run_commands(
                            ws,
                            command,
                            timeout_budget=self._timeouts.command,
                        )
                        accepted = True
                except HAPlaybackError:
                    if accepted:
                        return
                    raise
                except Exception:
                    if accepted:
                        return
                    raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE) from None
        except HAPlaybackError:
            raise
        except TimeoutError:
            if accepted:
                return
            raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE) from None

    def _connect(self, access: _AccessConfig) -> _SocketContext:
        return self._connect_factory(
            _ha_websocket_url(access.ha_url),
            open_timeout=self._timeouts.connect,
            close_timeout=1,
            ping_timeout=self._timeouts.command,
            max_size=MAX_FRAME_BYTES,
            max_queue=16,
        )

    async def _authenticate(self, ws: _WebSocket, token: str) -> None:
        greeting = await _receive_message(ws, timeout=self._timeouts.auth)
        if greeting.get("type") != "auth_required":
            raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE)
        await asyncio.wait_for(
            ws.send(json.dumps({"type": "auth", "access_token": token}, separators=(",", ":"))),
            timeout=self._timeouts.auth,
        )
        auth = await _receive_message(ws, timeout=self._timeouts.auth)
        if auth.get("type") != "auth_ok":
            raise HAPlaybackError(HAPlaybackReason.HA_AUTH_FAILED)

    async def _run_commands(
        self,
        ws: _WebSocket,
        commands: Sequence[tuple[int, Mapping[str, object], HAPlaybackReason]],
        *,
        timeout_budget: float,
    ) -> dict[int, object]:
        """Run one command batch inside one explicit shared timeout budget."""
        expected = {message_id: failure_reason for message_id, _, failure_reason in commands}
        results: dict[int, object] = {}
        seen_messages = 0

        async with asyncio.timeout(timeout_budget):
            for _, payload, _ in commands:
                await ws.send(json.dumps(payload, separators=(",", ":")))

            while len(results) < len(expected):
                message = await _receive_message(ws)
                seen_messages += 1
                if seen_messages > MAX_RESULT_MESSAGES:
                    raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE)
                message_id = message.get("id")
                if isinstance(message_id, bool) or not isinstance(message_id, int):
                    continue
                if message_id not in expected or message.get("type") != "result":
                    continue
                if message.get("success") is not True:
                    raise HAPlaybackError(expected[message_id])
                results[message_id] = message.get("result")
        return results


def _make_access_config(ha_url: str | None, ha_token: str | None) -> _AccessConfig:
    url = ha_url.strip().rstrip("/") if isinstance(ha_url, str) else ""
    token = ha_token.strip() if isinstance(ha_token, str) else ""
    if len(url) > 2_048 or len(token) > 8_192:
        url = ""
        token = ""
    digest = hashlib.sha256(f"{url}\0{token}".encode(errors="replace")).hexdigest()
    return _AccessConfig(ha_url=url, ha_token=token, fingerprint=digest)


def _require_access(access: _AccessConfig) -> None:
    try:
        parsed = urlsplit(access.ha_url)
    except ValueError:
        raise HAPlaybackError(HAPlaybackReason.HA_ACCESS_MISSING) from None
    if (
        not access.ha_url
        or not access.ha_token
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise HAPlaybackError(HAPlaybackReason.HA_ACCESS_MISSING)


def _ha_websocket_url(ha_url: str) -> str:
    parsed = urlsplit(ha_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    if parsed.netloc == "supervisor":
        websocket_path = f"{base_path}/websocket" if base_path else "/core/websocket"
    else:
        websocket_path = f"{base_path}/api/websocket"
    return urlunsplit((scheme, parsed.netloc, websocket_path, "", ""))


async def _receive_message(ws: _WebSocket, *, timeout: float | None = None) -> dict[str, object]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout) if timeout is not None else await ws.recv()
    if not isinstance(raw, str | bytes) or len(raw) > MAX_FRAME_BYTES:
        raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE)
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE) from None
    if not isinstance(decoded, dict):
        raise HAPlaybackError(HAPlaybackReason.HA_UNREACHABLE)
    return decoded


def _list_result(
    results: Mapping[int, object],
    message_id: int,
    *,
    reason: HAPlaybackReason = HAPlaybackReason.HA_UNREACHABLE,
) -> list[object]:
    value = results.get(message_id)
    if not isinstance(value, list):
        raise HAPlaybackError(reason)
    return value


def _station_config_entry_ids(entries: Sequence[object]) -> set[str]:
    ids: set[str] = set()
    for entry in entries[:MAX_REGISTRY_ITEMS]:
        if not isinstance(entry, Mapping):
            continue
        domain = entry.get("domain")
        if domain is not None and domain != "mammamiradio":
            continue
        entry_id = entry.get("entry_id")
        if isinstance(entry_id, str) and 0 < len(entry_id) <= MAX_ENTITY_ID_LENGTH:
            ids.add(entry_id)
            if len(ids) > 1:
                break
    return ids


def _has_live_media_source(root: Mapping[object, object]) -> bool:
    stack: list[Mapping[object, object]] = [root]
    visited = 0
    while stack and visited < MAX_BROWSE_NODES:
        node = stack.pop()
        visited += 1
        if node.get("media_content_id") == MEDIA_SOURCE_LIVE and node.get("can_play") is True:
            return True
        children = node.get("children")
        if not isinstance(children, list):
            continue
        remaining = MAX_BROWSE_NODES - visited - len(stack)
        for child in reversed(children[: max(0, remaining)]):
            if isinstance(child, Mapping):
                stack.append(child)
    return False


def _build_candidates(
    states: Sequence[object],
    registry_entities: Sequence[object],
    registry_devices: Sequence[object],
    registry_areas: Sequence[object],
    station_config_entries: set[str],
) -> list[HAPlayerCandidate]:
    areas = _area_map(registry_areas)
    devices = _device_map(registry_devices)
    entities = _entity_map(registry_entities)
    candidates: list[HAPlayerCandidate] = []

    for raw_state in states[:MAX_ENTITY_SCAN]:
        if not isinstance(raw_state, Mapping):
            continue
        entity_id = _valid_entity_id(raw_state.get("entity_id"))
        if entity_id is None:
            continue
        identity = entities.get(entity_id)
        if _is_station_controller(entity_id, identity, station_config_entries):
            continue

        raw_status = raw_state.get("state")
        if not isinstance(raw_status, str):
            continue
        status_key = (_safe_label(raw_status, MAX_STATE_LENGTH) or "").casefold()
        if not status_key or status_key in {"unavailable", "unknown"}:
            continue

        attributes = raw_state.get("attributes")
        attrs = attributes if isinstance(attributes, Mapping) else {}
        supported_features = attrs.get("supported_features")
        if (
            isinstance(supported_features, bool)
            or not isinstance(supported_features, int)
            or supported_features < 0
            or supported_features > (1 << 63) - 1
        ):
            continue
        if supported_features & PLAY_MEDIA_FEATURE == 0:
            continue

        device = devices.get(identity.device_id) if identity is not None and identity.device_id else None
        area_id = identity.area_id if identity is not None else None
        if area_id is None and device is not None:
            area_id = device.area_id

        friendly_name = _first_label(
            attrs.get("friendly_name"),
            identity.name if identity is not None else None,
            device.name if device is not None else None,
            entity_id,
            limit=MAX_FRIENDLY_NAME_LENGTH,
        )
        state = _safe_label(raw_status, MAX_STATE_LENGTH) or "available"
        device_class = _safe_label(attrs.get("device_class"), MAX_DEVICE_CLASS_LENGTH)
        area = _safe_label(areas.get(area_id), MAX_AREA_LENGTH) if area_id is not None else None
        candidates.append(
            HAPlayerCandidate(
                entity_id=entity_id,
                friendly_name=friendly_name,
                state=state,
                device_class=device_class,
                area=area,
                supports_play_media=True,
                available=True,
            )
        )
        if len(candidates) >= MAX_CANDIDATES:
            break

    candidates.sort(key=lambda candidate: (candidate.friendly_name.casefold(), candidate.entity_id))
    return candidates


def _area_map(items: Sequence[object]) -> dict[str, str]:
    areas: dict[str, str] = {}
    for item in items[:MAX_REGISTRY_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        area_id = _internal_id(item.get("area_id"))
        name = _safe_label(item.get("name"), MAX_AREA_LENGTH)
        if area_id is not None and name is not None:
            areas[area_id] = name
    return areas


def _device_map(items: Sequence[object]) -> dict[str, _RegistryDevice]:
    devices: dict[str, _RegistryDevice] = {}
    for item in items[:MAX_REGISTRY_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        device_id = _internal_id(item.get("id"))
        if device_id is None:
            continue
        devices[device_id] = _RegistryDevice(
            area_id=_internal_id(item.get("area_id")),
            name=_first_optional_label(
                item.get("name_by_user"),
                item.get("name"),
                limit=MAX_FRIENDLY_NAME_LENGTH,
            ),
        )
    return devices


def _entity_map(items: Sequence[object]) -> dict[str, _RegistryEntity]:
    entities: dict[str, _RegistryEntity] = {}
    for item in items[:MAX_REGISTRY_ITEMS]:
        if not isinstance(item, Mapping):
            continue
        entity_id = _valid_entity_id(item.get("entity_id"))
        if entity_id is None:
            continue
        platform = item.get("platform")
        entities[entity_id] = _RegistryEntity(
            platform=platform.strip().lower() if isinstance(platform, str) else None,
            config_entry_id=_internal_id(item.get("config_entry_id")),
            device_id=_internal_id(item.get("device_id")),
            area_id=_internal_id(item.get("area_id")),
            name=_safe_label(item.get("name") or item.get("original_name"), MAX_FRIENDLY_NAME_LENGTH),
        )
    return entities


def _is_station_controller(
    entity_id: str,
    identity: _RegistryEntity | None,
    station_config_entries: set[str],
) -> bool:
    if _STATION_ENTITY_RE.fullmatch(entity_id):
        return True
    if identity is None:
        return False
    return identity.platform == "mammamiradio" or identity.config_entry_id in station_config_entries


def _valid_entity_id(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ENTITY_ID_LENGTH:
        return None
    if _ENTITY_ID_RE.fullmatch(value) is None:
        return None
    return value


def _internal_id(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ENTITY_ID_LENGTH:
        return None
    return value


def _safe_label(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    bounded = value[: limit * 8]
    printable = "".join(character for character in bounded if not unicodedata.category(character).startswith("C"))
    collapsed = " ".join(printable.split())
    if not collapsed:
        return None
    return collapsed[:limit]


def _first_label(*values: object, limit: int) -> str:
    for value in values:
        label = _safe_label(value, limit)
        if label is not None:
            return label
    return "Media player"


def _first_optional_label(*values: object, limit: int) -> str | None:
    for value in values:
        label = _safe_label(value, limit)
        if label is not None:
            return label
    return None


def _valid_attempt_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_ATTEMPT_ID_LENGTH
        and _ATTEMPT_ID_RE.fullmatch(value) is not None
    )


def _receipt_failure_result(entity_id: str, station_resumed: bool) -> HAPlayResult:
    return HAPlayResult(
        entity_id=entity_id,
        accepted=True,
        station_resumed=station_resumed,
        receipt_persisted=False,
        reason=HAPlaybackReason.RECEIPT_UNAVAILABLE,
    )
