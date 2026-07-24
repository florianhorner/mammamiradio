"""
Shared golden-scenario builder for the frozen v1 now-playing contract.

Single source of truth for ``tests/integrations/golden/v1_now_playing.json``:
the contract-drift CI workflow runs this file in ``--check`` mode on every
pull request, and maintainers re-run it in write mode during a contract
window to regenerate the fixture. Both consumers share this one code path so
CI and regeneration cannot drift apart.

Usage::

    python tests/integrations/golden/generate_fixture.py          # (re)write the fixture
    python tests/integrations/golden/generate_fixture.py --check  # compare, exit 1 on drift
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from mammamiradio.integrations.schema import HostEntry, StationBlock
from mammamiradio.integrations.serializer import NowPlayingSnapshot, serialize_now_playing

FIXTURE_PATH = Path(__file__).resolve().parent / "v1_now_playing.json"

# Volatile-field pins. The serializer is pure, so with these pinned inputs the
# rendered payload is fully deterministic. When a live-captured payload
# replaces this provisional fixture, normalize_volatile() maps its real
# timestamps onto the same pins before any comparison.
PINNED_CHANGED_AT = 1746500000.0
PINNED_STARTED_AT = 1746500000.0


def build_golden_snapshot() -> NowPlayingSnapshot:
    """
    Return the pinned representative music-segment snapshot.

    Mirrors the documented ``docs/integrations/sample-payloads/music.json``
    scenario: a live music segment with full metadata, one queued and one
    predicted up-next item, and an absolute stream URL.
    """
    return NowPlayingSnapshot(
        now_streaming={
            "type": "music",
            "label": "Volare — Domenico Modugno",
            "started": PINNED_STARTED_AT,
            "duration_sec": 210.0,
            "metadata": {
                "title": "Volare — Domenico Modugno",
                "title_only": "Volare",
                "artist": "Domenico Modugno",
                "album": "Mr Volare",
                "album_art": "https://example.test/art.jpg",
                "year": 1958,
                "spotify_id": "v01",
                "youtube_id": "y01",
            },
        },
        queued_segments=({"type": "music", "label": "Sapore di Sale — Gino Paoli"},),
        upcoming_predicted=({"type": "banter", "label": "Host banter"},),
        session_stopped=False,
        playback_epoch=7,
        station=StationBlock(
            name="Mamma Mi Radio",
            frequency="94.7",
            theme="Italia da bere",
            hosts=[
                HostEntry(engine_host="gianni", display_name="Gianni", description="Anchor host."),
                HostEntry(engine_host="marco", display_name="Marco", description="Co-host."),
            ],
        ),
        audio_format={
            "codec": "mp3",
            "mime_type": "audio/mpeg",
            "bitrate_kbps": 192,
            "sample_rate_hz": 44100,
            "channels": 2,
        },
        relative_stream_url="/stream",
        absolute_stream_url="http://homeassistant.local:8000/stream",
        changed_at=PINNED_CHANGED_AT,
    )


def render_golden_payload() -> dict[str, Any]:
    """Render the golden scenario through the real serializer."""
    return dict(serialize_now_playing(build_golden_snapshot()))


def normalize_volatile(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Return a copy of ``payload`` with volatile fields set to their pins.

    Volatile fields are ``changed_at`` and ``now_playing.started_at`` — wall
    clock values in a live capture. The ETag is an HTTP header derived from
    the body and never appears in the payload itself.

    :raises ValueError: if a volatile field is not a plain number — a type
        change on these fields is contract drift, not volatility, and must
        not be pinned into invisibility.
    """
    normalized = copy.deepcopy(payload)
    if "changed_at" in normalized:
        _require_number("changed_at", normalized["changed_at"])
        normalized["changed_at"] = PINNED_CHANGED_AT
    now_playing = normalized.get("now_playing")
    if isinstance(now_playing, dict) and "started_at" in now_playing:
        _require_number("now_playing.started_at", now_playing["started_at"])
        now_playing["started_at"] = PINNED_STARTED_AT
    return normalized


def fixture_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical on-disk encoding used for both writing and byte-comparison."""
    return (json.dumps(normalize_volatile(payload), indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """
    Write the golden fixture, or verify it with ``--check``.

    :param argv: Optional argument list (defaults to ``sys.argv[1:]``).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the serializer's rendered output against the committed fixture instead of writing it",
    )
    args = parser.parse_args(argv)

    try:
        rendered = fixture_bytes(render_golden_payload())
    except ValueError as exc:
        print(f"DRIFT: rendered payload failed volatile-field normalization: {exc}")
        return 1

    if not args.check:
        FIXTURE_PATH.write_bytes(rendered)
        print(f"wrote {FIXTURE_PATH}")
        return 0

    if not FIXTURE_PATH.exists():
        print(f"DRIFT: fixture missing at {FIXTURE_PATH} — run this script without --check to create it")
        return 1
    raw = FIXTURE_PATH.read_bytes()
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            print(f"DRIFT: fixture at {FIXTURE_PATH} root is not a JSON object")
            return 1
        on_disk = fixture_bytes(parsed)
    except ValueError as exc:
        # Covers invalid JSON/UTF-8 and volatile-field type drift alike.
        print(f"DRIFT: fixture at {FIXTURE_PATH} failed validation: {exc}")
        return 1
    if on_disk != rendered:
        print("DRIFT: serializer output no longer matches tests/integrations/golden/v1_now_playing.json")
        print("The v1 wire contract is frozen — see CONTRACT.md for the change process.")
        print("If this change went through a contract window, regenerate the fixture with:")
        print("  python tests/integrations/golden/generate_fixture.py")
        return 1
    if raw != on_disk:
        # Keep the committed bytes exactly canonical so this check and the
        # cross-repo sha256 comparison always watch the same bytes.
        print("DRIFT: fixture bytes are not in the canonical encoding")
        print("Rewrite it canonically with:")
        print("  python tests/integrations/golden/generate_fixture.py")
        return 1
    print("golden fixture matches serializer output")
    return 0


def _require_number(field: str, value: Any) -> None:
    """Reject a volatile-field value whose type drifted away from a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"volatile field {field} must be a number, got {type(value).__name__}")


if __name__ == "__main__":
    sys.exit(main())
