"""Regression coverage for transition-copy safety guards."""

from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import load_config
from mammamiradio.core.models import StationState, Track
from mammamiradio.hosts.scriptwriter import write_transition
from mammamiradio.hosts.transitions import _transition_text_usable


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stay with us, amici — the next record is ready.", True),
        ("Stay with us - the next record is ready.", True),
        ("Hold that thought -", False),
        ("Hold that thought --", False),
        ('Hold that thought -")]', False),
        ("Hold that thought --»", False),
        ('Hold that thought - " )', False),
    ],
)
def test_transition_text_usable_rejects_ascii_terminal_cutoffs(text: str, expected: bool) -> None:
    assert _transition_text_usable(text) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", ["-", '--")]'])
async def test_write_transition_ascii_terminal_cutoff_uses_stock_fallback(cutoff: str) -> None:
    config = load_config()
    config.anthropic_api_key = "test-key"
    config.openai_api_key = ""
    state = StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="test1")])
    state.played_tracks.append(Track(title="L'Estate", artist="Vivaldi", duration_ms=180000, spotify_id="v1"))

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response_with_language_guard",
        new_callable=AsyncMock,
        return_value={"text": f"Hold that thought {cutoff}"},
    ):
        _host, text, played_track_ref = await write_transition(state, config, next_segment="ad", song_cues=[])

    assert text == "Stay close, amici — a quick word from our sponsors."
    assert played_track_ref is None
