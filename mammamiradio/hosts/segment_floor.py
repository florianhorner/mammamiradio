"""Deterministic raw-output integrity checks for generated script segments.

This module deliberately evaluates the parsed provider response *before* the
live path repairs it. It is currently consumed by the OpenAI fallback eval
harness only; it performs no I/O, provider calls, or audio-path work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from mammamiradio.core.config import GUEST_HOST_NAME, StationConfig
from mammamiradio.hosts.scriptwriter import _is_local_guest_host_tag, _normalize_host_tag
from mammamiradio.hosts.station_name_guard import sanitize_spoken_station_name

GateStatus = Literal["PASS", "FAIL", "N/A"]

_SPOKEN_CALLERS = frozenset({"banter", "ad", "news_flash", "transition"})


@dataclass(frozen=True, slots=True)
class GateResult:
    """One legible gate outcome, including a stable machine-readable reason."""

    status: GateStatus
    reason: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"status": self.status, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class FloorResult:
    """The tri-state receipt for one parsed raw script response."""

    station_name: GateResult
    roster: GateResult
    spoken_text: GateResult

    @property
    def status(self) -> GateStatus:
        statuses = (self.station_name.status, self.roster.status, self.spoken_text.status)
        if "FAIL" in statuses:
            return "FAIL"
        if "PASS" in statuses:
            return "PASS"
        return "N/A"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "gates": {
                "station_name": self.station_name.to_dict(),
                "roster": self.roster.to_dict(),
                "spoken_text": self.spoken_text.to_dict(),
            },
        }


def _not_applicable() -> GateResult:
    return GateResult("N/A", "not_applicable")


def _banter_texts(raw_output: dict[str, Any]) -> list[str]:
    lines = raw_output.get("lines")
    if not isinstance(lines, list):
        return []

    texts: list[str] = []
    for line in lines:
        if isinstance(line, str):
            texts.append(line)
        elif isinstance(line, dict) and isinstance(line.get("text"), str):
            texts.append(line["text"])
    return texts


def _ad_texts(raw_output: dict[str, Any]) -> list[str]:
    # ``write_ad`` treats an absent ``parts`` key like an empty list, then
    # promotes root-level ``text`` to its required voice part. A present value
    # with any other shape makes that live parser fail, so do not certify it
    # through the root-text fallback here.
    parts = raw_output.get("parts", [])
    if not isinstance(parts, list):
        return []

    if any(not isinstance(part, dict) for part in parts):
        return []

    has_voice_part = False
    texts: list[str] = []
    for part in parts:
        if part.get("type", "voice") != "voice":
            continue
        has_voice_part = True
        if isinstance(part.get("text"), str):
            texts.append(part["text"])

    if has_voice_part:
        return texts

    fallback_text = raw_output.get("text")
    return [fallback_text] if isinstance(fallback_text, str) else []


def _spoken_texts(segment: str, raw_output: dict[str, Any]) -> list[str]:
    if segment == "banter":
        return _banter_texts(raw_output)
    if segment == "ad":
        return _ad_texts(raw_output)
    if segment in {"news_flash", "transition"}:
        text = raw_output.get("text")
        return [text] if isinstance(text, str) else []
    return []


def _station_name_gate(texts: list[str], config: StationConfig) -> GateResult:
    spoken = [text for text in texts if text.strip()]
    if not spoken:
        return _not_applicable()

    station_name = config.display_station_name
    for text in spoken:
        if sanitize_spoken_station_name(text, station_name) != text:
            return GateResult("FAIL", "foreign_station_name")
    return GateResult("PASS")


def _roster_gate(segment: str, raw_output: dict[str, Any], config: StationConfig) -> GateResult:
    if segment != "banter":
        return _not_applicable()

    lines = raw_output.get("lines")
    if not isinstance(lines, list):
        return _not_applicable()

    roster = {_normalize_host_tag(host.name) for host in config.hosts if isinstance(host.name, str)}
    guest_is_configured = _normalize_host_tag(GUEST_HOST_NAME) in roster
    has_named_line = False
    for line in lines:
        if not isinstance(line, dict):
            continue
        has_named_line = True
        raw_host = line.get("host")
        if not isinstance(raw_host, str) or not raw_host.strip():
            return GateResult("FAIL", "missing_host")
        if _normalize_host_tag(raw_host) not in roster and not (
            guest_is_configured and _is_local_guest_host_tag(raw_host)
        ):
            return GateResult("FAIL", "unknown_host")

    return GateResult("PASS") if has_named_line else _not_applicable()


def _spoken_text_gate(segment: str, texts: list[str]) -> GateResult:
    if segment not in _SPOKEN_CALLERS:
        return _not_applicable()
    if any(text.strip() for text in texts):
        return GateResult("PASS")
    return GateResult("FAIL", "no_spoken_text")


def check_floor(segment: str, raw_output: object, config: StationConfig) -> FloorResult:
    """Return a total tri-state integrity receipt for parsed raw model output.

    ``direction`` and ``memory_extract`` are intentionally all-N/A: the former is
    playlist target data, and the latter is post-air control-plane data rather
    than listener-facing spoken copy.
    """
    normalized_segment = segment if isinstance(segment, str) else ""
    payload = raw_output if isinstance(raw_output, dict) else {}
    texts = _spoken_texts(normalized_segment, payload)

    return FloorResult(
        station_name=(
            _station_name_gate(texts, config) if normalized_segment in _SPOKEN_CALLERS else _not_applicable()
        ),
        roster=_roster_gate(normalized_segment, payload, config),
        spoken_text=_spoken_text_gate(normalized_segment, texts),
    )
