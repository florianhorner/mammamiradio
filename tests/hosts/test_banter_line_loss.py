"""Per-line loss accounting and guards for generated host exchanges.

The language guard protects the whole exchange; these cover the paths that used
to shorten one *inside* an exchange that still aired.  Every fixture line sits
inside the Normal Mode target band so these tests exercise the drop paths rather
than the language policy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mammamiradio.core.config import GUEST_HOST_NAME, load_config
from mammamiradio.core.models import DialogueLine, HostPersonality, StationState, Track
from mammamiradio.hosts.scriptwriter import (
    _CLEAN_SPOKEN_TEXT_RULE,
    LineLossAccounting,
    _drop_caused_same_host_run,
    _regular_hosts,
    repair_banter_without_listener_context,
    write_banter,
)

# In-band Normal Mode copy (each ~0.73-0.77 English), long enough to clear the
# short-copy rule, so the language guard never decides these tests' outcome.
TEXT_A = "The music is back and we stay with the song, ciao amici grazie"
TEXT_B = "That is exactly right and the room is ready tonight, ciao bene allora"
TEXT_C = "We have more music for you and this next track, ciao amici grazie"
TEXT_D = "Stay here with us because the next song is ready, ciao bene allora"


@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    cfg.super_italian_mode = False
    return cfg


@pytest.fixture()
def state():
    return StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="t1")])


def _host(name: str) -> HostPersonality:
    return HostPersonality(name=name, voice="en-US-GuyNeural", style="")


async def _run_banter(state, config, raw_lines):
    """Drive write_banter with one canned model response."""
    response = {"lines": raw_lines, "new_joke": None}
    with (
        patch(
            "mammamiradio.hosts.scriptwriter._generate_json_response",
            new_callable=AsyncMock,
            return_value=response,
        ) as mock_generate,
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.99),
    ):
        result, _commit = await write_banter(state, config)
    return result, mock_generate


# --- unit: the same-host-run detector -------------------------------------


def test_same_host_run_ignores_a_clean_alternating_exchange():
    a, b = _host("Marco"), _host("Giulia")
    lines = [DialogueLine(a, TEXT_A), DialogueLine(b, TEXT_B), DialogueLine(a, TEXT_C)]

    tags = {0: "marco", 1: "giulia", 2: "marco"}

    assert _drop_caused_same_host_run(lines, [0, 1, 2], tags, multi_host=True) is False


def test_same_host_run_detects_a_hole_closed_by_a_drop():
    """Survivors 0 and 1 share a speaker because the line between them went."""
    a, b = _host("Marco"), _host("Giulia")
    lines = [DialogueLine(a, TEXT_A), DialogueLine(a, TEXT_B), DialogueLine(b, TEXT_C)]

    tags = {0: "marco", 1: "giulia", 2: "marco", 3: "giulia"}

    assert _drop_caused_same_host_run(lines, [0, 2, 3], tags, multi_host=True) is True


def test_same_host_run_forgives_a_run_the_model_wrote_itself():
    """Adjacent in the model's own list means the model wrote it that way.

    Rejecting those would trade a serviceable exchange for stock copy — the
    over-strict failure this guard must not reintroduce.
    """
    a, b = _host("Marco"), _host("Giulia")
    lines = [DialogueLine(a, TEXT_A), DialogueLine(a, TEXT_B), DialogueLine(b, TEXT_C)]

    tags = {0: "marco", 1: "marco", 2: "marco", 3: "giulia"}

    assert _drop_caused_same_host_run(lines, [0, 1, 3], tags, multi_host=True) is False


def test_same_host_run_allows_a_single_host_station():
    """A one-host station legitimately runs solo — every line shares a speaker."""
    solo = _host("Marco")
    lines = [DialogueLine(solo, TEXT_A), DialogueLine(solo, TEXT_B)]

    tags = {0: "marco", 1: "marco", 2: "marco"}

    assert _drop_caused_same_host_run(lines, [0, 2], tags, multi_host=False) is False


def test_same_host_run_forgives_a_gap_that_held_only_the_same_speaker():
    """Three lines by one host, middle one deduped: the run pre-dated the drop.

    Index adjacency alone would call this drop-caused and reject a serviceable
    exchange into stock copy.
    """
    solo = _host("Marco")
    lines = [DialogueLine(solo, TEXT_A), DialogueLine(solo, TEXT_B)]

    assert _drop_caused_same_host_run(lines, [0, 2], {0: "marco", 1: "marco", 2: "marco"}, multi_host=True) is False


def test_same_host_run_catches_survivors_that_all_share_one_speaker():
    """Keyed off the roster, not the survivors.

    Deriving multi-host from the surviving lines made the check disappear
    exactly when a drop left nothing but one speaker — the case it exists for.
    """
    solo = _host("Marco")
    lines = [DialogueLine(solo, TEXT_A), DialogueLine(solo, TEXT_B)]

    tags = {0: "marco", 1: "giulia", 2: "marco"}

    assert _drop_caused_same_host_run(lines, [0, 2], tags, multi_host=True) is True


def test_line_loss_accounting_totals_and_row():
    loss = LineLossAccounting(authored=6, aired=4, dropped_empty=1, dropped_duplicate=1)

    assert loss.dropped == 2
    assert loss.as_row() == {
        "authored": 6,
        "aired": 4,
        "dropped_empty": 1,
        "dropped_malformed": 0,
        "dropped_guest_host": 0,
        "dropped_duplicate": 1,
    }


def test_line_loss_accounting_reports_no_loss_for_a_full_exchange():
    assert LineLossAccounting(authored=4, aired=4).dropped == 0


# --- the prompt rule that keeps brackets out of the model's output ---------


@pytest.mark.asyncio
async def test_banter_prompt_always_asks_for_clean_spoken_text(config, state):
    """The bracket sanitizer runs unconditionally, so its instruction must too.

    It used to live only inside the V3 delivery contract, which is empty whenever
    no host has V3 cues — arming the sanitizer while disarming the rule that
    keeps its input clean.
    """
    regulars = _regular_hosts(config)
    _result, mock_generate = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
        ],
    )

    assert _CLEAN_SPOKEN_TEXT_RULE in mock_generate.await_args_list[0].kwargs["prompt"]


# --- write_banter: drops that still leave a real exchange -----------------


@pytest.mark.asyncio
async def test_bracket_only_line_is_dropped_and_recorded(config, state):
    """A sanitized-away line airs short but leaves a trace, not silence."""
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[0].name, "text": TEXT_C},
            {"host": regulars[1].name, "text": "[ride]"},
        ],
    )

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss == {
        "authored": 4,
        "aired": 3,
        "dropped_empty": 1,
        "dropped_malformed": 0,
        "dropped_guest_host": 0,
        "dropped_duplicate": 0,
    }


@pytest.mark.asyncio
async def test_full_exchange_records_no_line_loss(config, state):
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[0].name, "text": TEXT_C},
        ],
    )

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss is None


# --- write_banter: drops that damage the exchange fall back ---------------


@pytest.mark.asyncio
async def test_drop_that_welds_two_lines_onto_one_host_falls_back(config, state):
    """The audible artifact: the dropped line's neighbours share a speaker."""
    regulars = _regular_hosts(config)
    authored = [TEXT_A, TEXT_B, TEXT_C]
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": "[applausi]"},
            {"host": regulars[0].name, "text": TEXT_B},
            {"host": regulars[1].name, "text": TEXT_C},
        ],
    )

    assert all(line.text not in authored for line in result)
    assert state.last_banter_line_loss is None


@pytest.mark.asyncio
async def test_dedup_collapsing_to_one_line_falls_back(config, state):
    """Dedup had no length guard of its own, so a solo line could reach air.

    Both duration floors return None below two lines, so this also switched the
    audio-quality length check off exactly when the exchange was most damaged.
    """
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[0].name, "text": TEXT_A},
        ],
    )

    assert len(result) >= 2
    assert [line.text for line in result] != [TEXT_A]
    assert state.last_banter_line_loss is None


@pytest.mark.asyncio
async def test_single_usable_line_without_drops_falls_back(config, state):
    """A one-line model response is not an exchange even when nothing was dropped."""
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [{"host": regulars[0].name, "text": TEXT_A}],
    )

    assert len(result) >= 2
    assert [line.text for line in result] != [TEXT_A]
    assert state.last_banter_line_loss is None


@pytest.mark.asyncio
async def test_model_authored_same_host_run_still_airs(config, state):
    """The model writing two lines for one host is a taste problem, not a hole.

    A drop elsewhere in the same response must not turn that into stock copy.
    """
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[0].name, "text": TEXT_B},
            {"host": regulars[1].name, "text": TEXT_C},
            {"host": regulars[0].name, "text": "[applausi]"},
        ],
    )

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss is not None
    assert state.last_banter_line_loss["dropped_empty"] == 1


@pytest.mark.asyncio
async def test_duplicate_drop_is_counted_when_the_exchange_survives(config, state):
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[0].name, "text": TEXT_C},
        ],
    )

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss is not None
    assert state.last_banter_line_loss["dropped_duplicate"] == 1


@pytest.mark.asyncio
async def test_uninvited_guest_host_drop_is_counted(config, state):
    """The guest is gated out unless invited; the drop must still be legible."""
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[0].name, "text": TEXT_C},
            {"host": GUEST_HOST_NAME, "text": TEXT_D},
        ],
    )

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss is not None
    assert state.last_banter_line_loss["dropped_guest_host"] == 1


@pytest.mark.asyncio
async def test_line_loss_is_cleared_when_the_exchange_falls_back(config, state):
    """Stale accounting must not ride along to an unrelated segment's ledger row."""
    regulars = _regular_hosts(config)
    state.last_banter_line_loss = {"authored": 9, "aired": 1}

    await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": "[applausi]"},
            {"host": regulars[0].name, "text": TEXT_B},
            {"host": regulars[1].name, "text": TEXT_C},
        ],
    )

    assert state.last_banter_line_loss is None


@pytest.mark.asyncio
async def test_listener_truth_repair_refuses_a_damaged_exchange(config, state):
    """The repair is already the fallback, so a short repair airs as the product."""
    regulars = _regular_hosts(config)
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": "[ride]"},
            {"host": regulars[0].name, "text": TEXT_B},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        assert await repair_banter_without_listener_context(state, config) is None


@pytest.mark.asyncio
async def test_listener_truth_repair_publishes_its_own_accounting(config, state):
    """The repair replaces the exchange, so it must replace the accounting too.

    Otherwise the rejected banter's numbers ride onto the repair's ledger row and
    describe copy that never aired.
    """
    regulars = _regular_hosts(config)
    state.last_banter_line_loss = {"authored": 9, "aired": 1}
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[1].name, "text": "[ride]"},
            {"host": regulars[0].name, "text": TEXT_C},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        result = await repair_banter_without_listener_context(state, config)

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss is not None
    assert state.last_banter_line_loss["authored"] == 4
    assert state.last_banter_line_loss["aired"] == 3
    assert state.last_banter_line_loss["dropped_empty"] == 1


@pytest.mark.asyncio
async def test_listener_truth_repair_clears_accounting_for_a_clean_exchange(config, state):
    regulars = _regular_hosts(config)
    state.last_banter_line_loss = {"authored": 9, "aired": 1}
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        result = await repair_banter_without_listener_context(state, config)

    assert [line.text for line in result] == [TEXT_A, TEXT_B]
    assert state.last_banter_line_loss is None


@pytest.mark.asyncio
async def test_listener_truth_repair_ignores_a_hostless_entry_in_the_gap(config, state):
    """An entry naming no host never had a speaker, so it cannot weld anything.

    Guessing a fallback speaker for `{}` would invent a vanished turn and reject
    an exchange the model wrote as two lines by the same host.
    """
    regulars = _regular_hosts(config)
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {},
            {"host": regulars[0].name, "text": TEXT_B},
            {"host": regulars[1].name, "text": TEXT_C},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        result = await repair_banter_without_listener_context(state, config)

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss["dropped_malformed"] == 1


@pytest.mark.asyncio
async def test_listener_truth_repair_counts_a_gated_guest_line(config, state):
    regulars = _regular_hosts(config)
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": GUEST_HOST_NAME, "text": TEXT_D},
            {"host": regulars[0].name, "text": TEXT_C},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        result = await repair_banter_without_listener_context(state, config)

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss["dropped_guest_host"] == 1


@pytest.mark.asyncio
async def test_listener_truth_repair_refuses_a_drop_down_to_one_line(config, state):
    regulars = _regular_hosts(config)
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": "[ride]"},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        assert await repair_banter_without_listener_context(state, config) is None


@pytest.mark.asyncio
async def test_listener_truth_repair_refuses_one_usable_line_without_drops(config, state):
    regulars = _regular_hosts(config)
    response = {"lines": [{"host": regulars[0].name, "text": TEXT_A}]}
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        assert await repair_banter_without_listener_context(state, config) is None


@pytest.mark.asyncio
async def test_listener_truth_repair_keeps_an_intact_exchange(config, state):
    regulars = _regular_hosts(config)
    response = {
        "lines": [
            {"host": regulars[0].name, "text": TEXT_A},
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[0].name, "text": TEXT_C},
        ]
    }
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value=response,
    ):
        result = await repair_banter_without_listener_context(state, config)

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]


@pytest.mark.asyncio
async def test_malformed_entries_are_counted_not_skipped_silently(config, state):
    regulars = _regular_hosts(config)
    result, _ = await _run_banter(
        state,
        config,
        [
            {"host": regulars[0].name, "text": TEXT_A},
            None,
            {"host": regulars[1].name, "text": TEXT_B},
            {"host": regulars[0].name, "text": TEXT_C},
        ],
    )

    assert [line.text for line in result] == [TEXT_A, TEXT_B, TEXT_C]
    assert state.last_banter_line_loss is not None
    assert state.last_banter_line_loss["dropped_malformed"] == 1
    assert state.last_banter_line_loss["authored"] == 4
    assert state.last_banter_line_loss["aired"] == 3
