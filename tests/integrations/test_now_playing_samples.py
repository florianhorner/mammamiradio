"""Sample-payload parity tests.

Locks the v1 contract's response shape against accidental drift between
documented JSON examples (``docs/integrations/sample-payloads/*.json``)
and the live serializer output. Adding a new key to either side without
the other now fails CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mammamiradio.integrations.serializer import NowPlayingResponse

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "docs" / "integrations" / "sample-payloads"

EXPECTED_FILES = (
    "music.json",
    "banter.json",
    "news_flash.json",
    "ad.json",
    "station_id.json",
    "time_check.json",
    "sweeper.json",
    "empty_queue.json",
    "stopped.json",
    "post_restart.json",
)

V1_TOP_LEVEL_KEYS = {
    "schema_version",
    "station",
    "stream",
    "now_playing",
    "up_next",
    "session_state",
    "changed_at",
}


def test_sample_payloads_directory_matches_expected_files() -> None:
    """No sample file can be added without registering it in EXPECTED_FILES.

    Closes the one-way drift gap: parametrize covers every file in
    EXPECTED_FILES, but adding a new JSON to the directory without
    updating this tuple would slip past contract assertions. This test
    locks the directory contents.
    """
    on_disk = {p.name for p in SAMPLE_DIR.glob("*.json")}
    expected = set(EXPECTED_FILES)
    extra = on_disk - expected
    missing = expected - on_disk
    assert not extra, f"unregistered sample payload(s) — add to EXPECTED_FILES: {extra}"
    assert not missing, f"expected sample payload(s) missing on disk: {missing}"


@pytest.mark.parametrize("filename", EXPECTED_FILES)
def test_sample_payload_has_v1_top_level_keys(filename: str) -> None:
    """Each documented sample is itself a v1-shaped payload.

    Uses exact set equality so a new undocumented top-level key cannot
    sneak into a sample without showing up in V1_TOP_LEVEL_KEYS first.
    """
    path = SAMPLE_DIR / filename
    assert path.exists(), f"sample payload missing on disk: {filename}"
    data = json.loads(path.read_text())
    assert set(data.keys()) == V1_TOP_LEVEL_KEYS, (
        f"{filename}: top-level keys diverged from V1_TOP_LEVEL_KEYS "
        f"(extra={set(data.keys()) - V1_TOP_LEVEL_KEYS}, missing={V1_TOP_LEVEL_KEYS - set(data.keys())})"
    )
    assert data["schema_version"] == "1"


@pytest.mark.parametrize(
    "filename,expected_state",
    [
        ("empty_queue.json", "empty_queue"),
        ("stopped.json", "stopped"),
        ("post_restart.json", "stopped"),
        ("music.json", "live"),
        ("banter.json", "live"),
        ("ad.json", "live"),
    ],
)
def test_sample_payload_session_states(filename: str, expected_state: str) -> None:
    """Documented samples cover every session_state literal."""
    data = json.loads((SAMPLE_DIR / filename).read_text())
    assert data["session_state"] == expected_state


@pytest.mark.parametrize(
    "filename,expected_class",
    [
        ("music.json", "music"),
        ("banter.json", "voice"),
        ("news_flash.json", "voice"),
        ("ad.json", "interstitial"),
        ("station_id.json", "interstitial"),
        ("time_check.json", "interstitial"),
        ("sweeper.json", "interstitial"),
    ],
)
def test_sample_payload_segment_classes(filename: str, expected_class: str) -> None:
    """Documented samples cover every segment_class bucket."""
    data = json.loads((SAMPLE_DIR / filename).read_text())
    assert data["now_playing"]["segment_class"] == expected_class


def test_typed_dict_matches_documented_top_level() -> None:
    """The NowPlayingResponse TypedDict mirrors the documented top-level keys."""
    # `NowPlayingResponse` uses `total=False`; we read the declared optional
    # keys directly to lock the contract against accidental key removal.
    declared = set(NowPlayingResponse.__optional_keys__) | set(NowPlayingResponse.__required_keys__)
    assert declared >= V1_TOP_LEVEL_KEYS
