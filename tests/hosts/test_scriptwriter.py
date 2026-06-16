"""Tests for scriptwriter module: prompt building, banter, and ad generation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mammamiradio.hosts.scriptwriter as scriptwriter_module
from mammamiradio.core.config import load_config, resolve_model
from mammamiradio.core.models import (
    ChaosSubtype,
    HostPersonality,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.hosts.ad_creative import AD_FORMATS, SPEAKER_ROLES, AdBrand, AdFormat, AdScript, AdVoice
from mammamiradio.hosts.scriptwriter import (
    CHAOS_MODE_BLOCK,
    ListenerRequestCommit,
    _build_system_prompt,
    _chaos_prompt_block,
    _host_expression_block,
    _massage_transition_text,
    _personality_modifier,
    _plan_listener_request_block,
    write_ad,
    write_banter,
    write_news_flash,
    write_transition,
)


@pytest.fixture()
def config():
    cfg = load_config()
    cfg.anthropic_api_key = "test-key"
    cfg.openai_api_key = ""
    return cfg


@pytest.fixture()
def state():
    return StationState(playlist=[Track(title="Test", artist="Artist", duration_ms=1000, spotify_id="test1")])


@pytest.fixture(autouse=True)
def _reset_provider_backoff_state():
    scriptwriter_module.reset_provider_backoff()
    yield
    scriptwriter_module.reset_provider_backoff()


def _mock_anthropic_response(text: str):
    """Build a mock AsyncAnthropic whose messages.create returns the given text."""
    mock_content_block = MagicMock()
    mock_content_block.text = text

    mock_response = MagicMock()
    mock_response.content = [mock_content_block]

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


def _mock_openai_response(text: str):
    """Build a mock OpenAI client whose chat.completions.create returns the given text."""
    mock_message = MagicMock()
    mock_message.content = text

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_usage = MagicMock()
    mock_usage.prompt_tokens = 11
    mock_usage.completion_tokens = 7

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage

    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = MagicMock(return_value=mock_response)
    return mock_client


# --- _build_system_prompt tests ---


def test_system_prompt_includes_host_names(config):
    prompt = _build_system_prompt(config)
    for host in config.hosts:
        assert host.name in prompt


def test_system_prompt_includes_language(config):
    prompt = _build_system_prompt(config)
    assert config.station.language in prompt


def test_system_prompt_includes_theme(config):
    prompt = _build_system_prompt(config)
    assert config.station.theme in prompt


def test_system_prompt_includes_station_name(config):
    prompt = _build_system_prompt(config)
    assert config.station.name in prompt


def test_prompt_world_constants_byte_stable():
    """Pin the moved prompt-fiction constants byte-for-byte (env-independent).

    Guards the verbatim prompt_world extraction against whitespace / load-bearing-
    newline drift — the substring assertions above wouldn't catch a stray newline.
    Unlike the assembled system prompt (which varies with config/env, so it can't be
    pinned across machines and CI), these are pure module constants and hash stably
    everywhere. If the prompt-fiction data legitimately changes, re-capture the hash.
    """
    import hashlib

    from mammamiradio.hosts import prompt_world as pw

    blob = "\x00".join(
        [
            repr(pw._EXPRESSION_BANK),
            repr(pw._HOST_FINGERPRINTS),
            pw._ECHO_STYLE_INSTRUCTION,
            pw._REACT_STYLE_INSTRUCTION,
            pw._EXCLAIM_STYLE_INSTRUCTION,
            repr(pw._STYLE_INSTRUCTIONS),
            pw.CHAOS_MODE_BLOCK,
            pw.FESTIVAL_MODE_BLOCK,
            repr(pw.CHAOS_SUBTYPE_BLOCKS),
        ]
    )
    assert (
        hashlib.sha256(blob.encode("utf-8")).hexdigest()
        == "b4901714e3e4476dfd2da6645cdf5c9d79ed50354d0aac71832fdea5a209001f"
    ), "prompt-fiction constants changed — if intentional, re-capture the hash"


def test_hot_reload_resets_system_prompt_cache():
    """Reloading scriptwriter must clear the cached system prompt.

    /api/hot-reload reloads prompt_world then scriptwriter. importlib.reload re-runs
    scriptwriter's module body, which re-executes ``_cached_system_prompt = ""`` /
    ``_cached_prompt_key = ""``. Without that reset, an operator editing prompt_world.py
    and hot-reloading would keep serving the stale cached prompt until the config
    structure changed. This locks the propagation contract.
    """
    import importlib

    scriptwriter_module._cached_system_prompt = "stale-sentinel"
    scriptwriter_module._cached_prompt_key = "stale-key"
    importlib.reload(scriptwriter_module)
    assert scriptwriter_module._cached_system_prompt == ""
    assert scriptwriter_module._cached_prompt_key == ""


def test_transitions_fallbacks_extraction_structural_and_reexport():
    """Guard the H1b move of transition + fallback data to their own modules.

    Structural, not byte-locked: these are frequently-tuned host copy, so we assert the
    moved constants stay well-formed rather than pinning a hash (test_chaos_banter /
    test_ads pin the exact chaos + ad-break content; the transition map is covered here
    plus by test_massage_transition_text_*). Re-export identity: every moved symbol the
    facade re-exposes must resolve to the SAME object as its new home, so the facade
    re-import didn't fork a stale copy.
    """
    from mammamiradio.hosts import fallbacks, transitions

    # transitions: rewrite map covers each next-segment, openers/stems are non-empty str
    assert {"banter", "ad", "news_flash"} <= set(transitions._TRANSITION_REWRITE_MAP)
    for openers in transitions._TRANSITION_REWRITE_MAP.values():
        assert openers and all(isinstance(o, str) and o.strip() for o in openers)
    assert transitions._BORING_TRANSITION_STEMS and all(
        isinstance(s, str) and s for s in transitions._BORING_TRANSITION_STEMS
    )

    # fallbacks: every chaos subtype has stock lines; ad bumpers are non-empty str
    assert set(fallbacks.CHAOS_STOCK_LINES) == set(ChaosSubtype)
    for lines in fallbacks.CHAOS_STOCK_LINES.values():
        assert lines and all(isinstance(line, str) and line.strip() for line in lines)
    for bumpers in (fallbacks.AD_BREAK_INTROS, fallbacks.AD_BREAK_OUTROS):
        assert bumpers and all(isinstance(b, str) and b.strip() for b in bumpers)

    # facade re-export identity — same object, not a forked copy. Every moved symbol
    # the facade still exposes (producer/tests read these through scriptwriter) is checked
    # uniformly so the AD_BREAK_* re-export can't silently drop off the facade namespace.
    assert scriptwriter_module.CHAOS_STOCK_LINES is fallbacks.CHAOS_STOCK_LINES
    assert scriptwriter_module.AD_BREAK_INTROS is fallbacks.AD_BREAK_INTROS
    assert scriptwriter_module.AD_BREAK_OUTROS is fallbacks.AD_BREAK_OUTROS
    assert scriptwriter_module._massage_transition_text is transitions._massage_transition_text
    assert scriptwriter_module._transition_stem is transitions._transition_stem


def test_news_flash_category_prompts_do_not_seed_recycled_premises():
    """News flash category prompts must not hand the model tired concrete jokes.

    The categories should describe shape and tone, leaving the LLM to invent a
    fresh premise for each bulletin instead of copying hardcoded examples.
    """
    joined = " ".join(scriptwriter_module.NEWS_FLASH_CATEGORIES.values()).lower()

    for stale_fragment in (
        "buffalo",
        "autostrada",
        "pavarotti",
        "all restaurants",
        "vatican has released",
        "leaning tower",
        "raining espresso",
        "panna on carbonara",
    ):
        assert stale_fragment not in joined

    for copied_submission_fragment in (
        "almost a crime",
        "where to have lunch",
        "with this heat, better",
        "record highs",
        "still undefeated",
        "second medical opinion",
        "cannot be translated into words",
        "nobody wanted more lasagna",
        "not that way. listen to me",
        "broke spaghetti in half",
    ):
        assert copied_submission_fragment not in joined

    for category, prompt in scriptwriter_module.NEWS_FLASH_CATEGORIES.items():
        assert "invent" in prompt.lower(), f"{category} prompt should require fresh premises"


# --- _host_expression_block tests ---


def test_host_expression_block_known_hosts():
    result = _host_expression_block(["Giulia", "Marco"])
    assert "Giulia's preferred expressions:" in result
    assert "Marco's preferred expressions:" in result


def test_host_expression_block_custom_host():
    result = _host_expression_block(["CustomDJ"])
    assert "use the full expression bank below" in result


def test_system_prompt_contains_fingerprint(config):
    prompt = _build_system_prompt(config)
    assert "preferred expressions:" in prompt


def test_system_prompt_no_legacy_fillers(config):
    prompt = _build_system_prompt(config)
    # The old encouraged-filler list included "oddio" and "aspetta aspetta".
    # Confirm neither appears as an encouraged filler (the VARIETY RULE may still
    # mention oddio in a de-emphasis context, so we check the old encouragement phrase).
    assert "oddio, aspetta aspetta" not in prompt
    assert "basta, dai, ma va, figurati" not in prompt


def test_host_expression_block_distinct_sections():
    result = _host_expression_block(["Giulia", "Marco"])
    giulia_start = result.index("Giulia's preferred expressions:")
    marco_start = result.index("Marco's preferred expressions:")
    giulia_section = result[giulia_start:marco_start]
    marco_section = result[marco_start:]
    # Confirm the sections have different content — hosts are not interchangeable
    assert giulia_section != marco_section


def test_host_expression_block_case_sensitivity():
    # Host name matching is case-sensitive; lowercase falls back to full bank
    result_lower = _host_expression_block(["giulia"])
    assert "use the full expression bank below" in result_lower
    result_exact = _host_expression_block(["Giulia"])
    assert "Giulia's preferred expressions:" in result_exact


def test_system_prompt_contains_giulia_expression(config):
    # Verify a specific Giulia fingerprint expression actually lands in the prompt
    prompt = _build_system_prompt(config)
    assert "Ammazza!" in prompt


def test_abbreviated_bank_block_covers_all_registers():
    from mammamiradio.hosts.scriptwriter import _abbreviated_bank_block

    result = _abbreviated_bank_block()
    for category in ("surprise", "hesitation", "agreement", "disagreement", "transition", "reaction"):
        assert f"[{category}]" in result


def test_abbreviated_bank_block_reads_from_expression_bank():
    from mammamiradio.hosts.scriptwriter import _EXPRESSION_BANK, _abbreviated_bank_block

    result = _abbreviated_bank_block()
    # First expression in every category should appear in the output
    for exprs in _EXPRESSION_BANK.values():
        assert exprs[0] in result


def test_system_prompt_abbreviated_bank_in_sync(config):
    # The abbreviated bank in the built prompt must contain expressions
    # from _EXPRESSION_BANK, not a stale hard-coded list
    from mammamiradio.hosts.scriptwriter import _EXPRESSION_BANK

    prompt = _build_system_prompt(config)
    first_surprise = _EXPRESSION_BANK["surprise"][0]
    assert first_surprise in prompt


def test_chaos_mode_block_is_not_in_cached_system_prompt(config, state):
    state.chaos_mode_active = True

    assert CHAOS_MODE_BLOCK.strip() not in _build_system_prompt(config)
    assert "CHAOS_FOURTH_WALL" in _chaos_prompt_block(state, ChaosSubtype.FOURTH_WALL)


def test_massage_transition_text_rewrites_repeated_che_pezzo():
    text = _massage_transition_text(
        "Che pezzo, mamma mia.",
        "banter",
        ["Che pezzo assurdo.", "Che pezzo, davvero."],
    )

    assert "che pezzo" not in text.lower()


def test_massage_transition_text_keeps_fresh_opener():
    text = _massage_transition_text(
        "Aspetta un secondo, qui c'e da ridere.",
        "banter",
        ["Che pezzo, mamma mia."],
    )

    assert text == "Aspetta un secondo, qui c'e da ridere."


def test_massage_transition_text_all_rewrites_exhausted_returns_first():
    """When the opener is a repeated boring stem AND every rewrite candidate's stem is
    already in the recent set, fall through to the first canned opener (defensive path).
    """
    from mammamiradio.hosts.transitions import _TRANSITION_REWRITE_MAP

    recent = ["Allora"] + _TRANSITION_REWRITE_MAP["banter"]
    text = _massage_transition_text("Allora", "banter", recent)
    assert text == _TRANSITION_REWRITE_MAP["banter"][0]


# --- write_banter tests ---


@pytest.mark.asyncio
async def test_write_banter_parses_valid_json(config, state):
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [
                {"host": host_name, "text": "Ciao a tutti!"},
                {"host": host_name, "text": "Che bella giornata!"},
            ],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 2
    assert result[0][0].name == host_name
    assert result[0][1] == "Ciao a tutti!"
    assert result[1][1] == "Che bella giornata!"


@pytest.mark.asyncio
async def test_write_banter_strips_markdown_fences(config, state):
    host_name = config.hosts[0].name
    response_text = (
        "```json\n"
        + json.dumps(
            {
                "lines": [{"host": host_name, "text": "Eccoci!"}],
                "new_joke": None,
            }
        )
        + "\n```"
    )
    mock_cls = _mock_anthropic_response(response_text)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "Eccoci!"


@pytest.mark.asyncio
async def test_write_banter_adds_new_joke(config, state):
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Haha!"}],
            "new_joke": "The traffic joke",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    assert len(state.running_jokes) == 0
    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        await write_banter(state, config)

    assert "The traffic joke" in state.running_jokes


@pytest.mark.asyncio
async def test_write_banter_stashes_pending_verbal_gag(config, state):
    """new_joke {text, punch} is stashed on state.pending_verbal_gag for the producer
    to commit to the cross-domain ledger at queue time (Callback Director seed path)."""
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Haha!"}],
            "new_joke": {"text": "bathroom fans", "punch": 5},
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    assert state.pending_verbal_gag is None
    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        await write_banter(state, config)

    assert state.pending_verbal_gag == {"text": "bathroom fans", "punch": 5.0}
    assert "bathroom fans" in state.running_jokes  # running_jokes still seeded too


@pytest.mark.asyncio
async def test_write_banter_no_new_joke_leaves_pending_gag_none(config, state):
    """No new_joke -> nothing stashed; a canned/no-joke banter must not plant a gag."""
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Haha!"}],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        await write_banter(state, config)

    assert state.pending_verbal_gag is None


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_api_exception(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    # Fallback now returns a multi-line exchange (3 lines) so banter sounds complete
    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert isinstance(text, str)
        assert len(text) > 0


@pytest.mark.asyncio
async def test_write_banter_restores_pending_directive_on_fallback(config, state):
    # Quotes are rewritten by _sanitize_prompt_data before the directive reaches
    # the prompt; the restore must put back the RAW directive, not that copy.
    raw_directive = 'Mention the "kitchen" light.'
    state.ha_pending_directive = raw_directive
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) >= 2
    assert state.ha_pending_directive == raw_directive


@pytest.mark.asyncio
async def test_write_banter_releases_gag_key_on_fallback(config, state):
    state.ha_running_gag = "The hallway light keeps winking at us."
    state.ha_running_gag_key = "gag-bucket-1"
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) >= 2
    # The gag never aired, so its cooldown bucket must be released (not spent).
    assert state.ha_running_gag_key == ""


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_malformed_json(config, state):
    mock_cls = _mock_anthropic_response("this is not valid json {{{")

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    # Fallback now returns a multi-line exchange so banter sounds complete
    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert isinstance(text, str)
        assert len(text) > 0


@pytest.mark.asyncio
async def test_write_banter_handles_string_shaped_lines(config, state):
    # The OpenAI fallback (gpt-4o-mini) sometimes returns `lines` as a list of
    # plain strings instead of {"host","text"} dicts. This must air as banter,
    # not crash to stock copy (observed live: AttributeError at scriptwriter.py).
    response_json = json.dumps(
        {
            "lines": ["Ciao a tutti!", "Che ridere!", "Restate con noi!"],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 3
    assert [text for _, text in result] == ["Ciao a tutti!", "Che ridere!", "Restate con noi!"]
    for host, _ in result:
        assert host in config.hosts
    # With two or more hosts, string lines alternate so it reads as a real exchange.
    if len(config.hosts) >= 2:
        assert result[0][0] is not result[1][0]


@pytest.mark.asyncio
async def test_write_banter_handles_mixed_and_empty_lines(config, state):
    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [
                {"host": host_name, "text": "Eccoci!"},
                "una battuta veloce",
                {"host": host_name, "text": ""},  # empty dict line dropped
                {"host": host_name},  # dict line missing "text" key dropped
                {"host": host_name, "text": None},  # null text dropped (never aired as "None")
                {"host": host_name, "text": ["x"]},  # container text dropped (never aired as "['x']")
                "",  # empty string dropped
                123,  # non-str/dict dropped
            ],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert [text for _, text in result] == ["Eccoci!", "una battuta veloce"]
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert text.strip()


@pytest.mark.asyncio
async def test_write_banter_falls_back_when_no_usable_lines(config, state):
    # A response with only empty/unusable lines has nothing airable → stock copy.
    response_json = json.dumps({"lines": ["", "   ", None], "new_joke": None})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    # Falls back to the stock multi-line exchange, never empty/silence.
    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert text.strip()


@pytest.mark.asyncio
async def test_write_banter_falls_back_when_lines_key_missing(config, state):
    # Valid dict but no "lines" key at all → nothing airable → stock copy.
    response_json = json.dumps({"new_joke": None})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert text.strip()


@pytest.mark.asyncio
async def test_write_banter_falls_back_when_data_not_dict(config, state):
    # A confused model returns a top-level JSON array instead of an object.
    # The parser must degrade to stock copy, never raise into the audio path.
    response_json = json.dumps(["Ciao a tutti!", "Che ridere!"])
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert text.strip()


@pytest.mark.asyncio
async def test_write_banter_string_lines_single_host(config, state):
    # A single-host operator config: string lines all assign the only host
    # (alternation degenerates cleanly, no crash).
    config.hosts = config.hosts[:1]
    response_json = json.dumps({"lines": ["Ciao!", "Ancora noi!"], "new_joke": None})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 2
    assert all(host is config.hosts[0] for host, _ in result)


@pytest.mark.asyncio
async def test_write_banter_string_lines_alternate_around_blanks(config, state):
    # Interleaved blank strings must not collapse two aired lines onto one host:
    # alternation counts only emitted string lines, not raw positions.
    if len(config.hosts) < 2:
        pytest.skip("needs at least two hosts to assert alternation")
    response_json = json.dumps({"lines": ["uno", "", "due"], "new_joke": None})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert [text for _, text in result] == ["uno", "due"]
    assert result[0][0] is not result[1][0]


@pytest.mark.asyncio
async def test_write_banter_no_llm_returns_language_fallback(config, state):
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "E torniamo alla musica!"


@pytest.mark.asyncio
async def test_write_banter_falls_back_to_openai_when_anthropic_fails(config, state):
    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(
        json.dumps({"lines": [{"host": host_name, "text": "OpenAI salva la diretta."}], "new_joke": None})
    )
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic invalid"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][0].name == host_name
    assert result[0][1] == "OpenAI salva la diretta."


@pytest.mark.asyncio
async def test_openai_fallback_default_model_is_gpt_5_5(config, state):
    """Lock the production default: balanced creative fallback uses GPT-5.5."""
    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "hi"}], "new_joke": None}))
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic invalid"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)

    call_kwargs = openai_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_openai_fallback_uses_max_completion_tokens(config, state):
    """Regression: gpt-5.x models 400 on `max_tokens` and require
    `max_completion_tokens`. Sending the old name silently killed the entire
    OpenAI fallback whenever Anthropic was unavailable (observed live on the
    HA edge addon). Lock the token-limit kwarg name."""
    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "hi"}], "new_joke": None}))
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic invalid"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)

    call_kwargs = openai_client.chat.completions.create.call_args.kwargs
    assert "max_completion_tokens" in call_kwargs
    assert "max_tokens" not in call_kwargs
    # gpt-5.x counts hidden reasoning tokens against this cap, so the OpenAI cap
    # must reserve headroom above the caller's visible-output budget or a
    # reasoning model can return an empty message that drops to stock copy.
    from mammamiradio.hosts.scriptwriter import _OPENAI_REASONING_HEADROOM

    assert call_kwargs["max_completion_tokens"] > _OPENAI_REASONING_HEADROOM


@pytest.mark.asyncio
async def test_openai_fallback_uses_configured_model(config, state):
    """When the OpenAI catalog is overridden, OpenAI is called with that model."""
    config.openai_api_key = "openai-key"
    # banter → creative role → balanced openai creative = "large"
    config.models.catalog["openai"]["large"] = "gpt-5.5-test"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "hi"}], "new_joke": None}))
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic invalid"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)

    call_kwargs = openai_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-5.5-test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "expected_model"),
    [
        ("news_flash", "gpt-5.5"),
        ("ad", "gpt-5.5"),
        ("transition", "gpt-5.4-mini"),
    ],
)
async def test_openai_fallback_routes_by_caller_role(config, state, caller, expected_model):
    """Creative fallbacks use GPT-5.5; latency-sensitive transitions use GPT-5.4-mini."""
    config.openai_api_key = "openai-key"
    openai_client = _mock_openai_response(json.dumps({"ok": True}))
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic invalid"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await scriptwriter_module._generate_json_response(
            prompt="Return JSON.",
            config=config,
            state=state,
            model=resolve_model(config.models, caller, "anthropic"),
            max_tokens=100,
            caller=caller,
        )

    call_kwargs = openai_client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == expected_model


@pytest.mark.asyncio
async def test_openai_fallback_logs_structured_event(config, state, caplog):
    """OpenAI fallback emits a structured 'openai_script_fallback' log event with eval-ready fields."""
    import logging

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "hi"}], "new_joke": None}))
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic 500"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        caplog.at_level(logging.INFO, logger="mammamiradio.hosts.scriptwriter"),
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)

    fallback_records = [r for r in caplog.records if getattr(r, "event", None) == "openai_script_call"]
    assert fallback_records, "expected at least one openai_script_call log record"
    record = fallback_records[-1]
    assert record.model == "gpt-5.5"
    assert record.caller == "banter"
    assert record.fallback_reason == "anthropic_exception"
    assert record.json_ok is True
    assert isinstance(record.latency_ms, int)
    assert record.prompt_tokens == 11
    assert record.completion_tokens == 7
    switch_records = [r for r in caplog.records if getattr(r, "event", None) == "provider_switch_event"]
    assert switch_records, "expected provider switch telemetry when Anthropic falls back to OpenAI"
    switch = switch_records[-1]
    assert switch.provider_class == "script_provider"
    assert switch.from_provider == "anthropic"
    assert switch.to_provider == "openai"
    assert switch.reason == "anthropic_exception"
    assert state.runtime_events[-1].provider_class == "script_provider"


@pytest.mark.asyncio
async def test_anthropic_max_tokens_truncation_is_labelled_honestly(config, state, caplog):
    """A truncated Anthropic response (stop_reason=max_tokens + unterminated JSON)
    is reported as 'anthropic_max_tokens_truncated', not a generic JSONDecodeError,
    while still falling back to OpenAI so the listener gets banter."""
    import logging

    config.anthropic_api_key = "anthropic-key"
    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "hi"}], "new_joke": None}))

    # Anthropic returns JSON cut off mid-string with stop_reason="max_tokens".
    mock_usage = MagicMock()
    mock_usage.input_tokens = 50
    mock_usage.output_tokens = 300
    mock_content = MagicMock()
    mock_content.text = '{"lines": [{"host": "Marco", "text": "Ciao a tutti, oggi'
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_response.usage = mock_usage
    mock_response.stop_reason = "max_tokens"
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock(return_value=mock_client)

    with (
        caplog.at_level(logging.INFO, logger="mammamiradio.hosts.scriptwriter"),
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)

    fallback_records = [r for r in caplog.records if getattr(r, "event", None) == "openai_script_call"]
    assert fallback_records, "expected an openai_script_call after Anthropic truncation"
    assert fallback_records[-1].fallback_reason == "anthropic_max_tokens_truncated"

    switch_records = [r for r in caplog.records if getattr(r, "event", None) == "provider_switch_event"]
    assert switch_records, "expected provider switch telemetry on truncation fallback"
    assert switch_records[-1].reason == "anthropic_max_tokens_truncated"
    # Illusion preserved: listener still gets banter via the OpenAI fallback.
    assert state.runtime_provider_state["script_provider"]["current_provider"] == "openai"


@pytest.mark.asyncio
async def test_anthropic_max_tokens_empty_content_is_labelled_honestly(config, state, caplog):
    """A max_tokens cut that returns an *empty* content list (IndexError on
    resp.content[0], not a JSONDecodeError) is still recognized as truncation —
    stop_reason is read before content is indexed."""
    import logging

    config.anthropic_api_key = "anthropic-key"
    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "hi"}], "new_joke": None}))

    mock_usage = MagicMock()
    mock_usage.input_tokens = 50
    mock_usage.output_tokens = 300
    mock_response = MagicMock()
    mock_response.content = []  # empty: resp.content[0] raises IndexError
    mock_response.usage = mock_usage
    mock_response.stop_reason = "max_tokens"
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock(return_value=mock_client)

    with (
        caplog.at_level(logging.INFO, logger="mammamiradio.hosts.scriptwriter"),
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)

    fallback_records = [r for r in caplog.records if getattr(r, "event", None) == "openai_script_call"]
    assert fallback_records, "expected an openai_script_call after empty-content truncation"
    assert fallback_records[-1].fallback_reason == "anthropic_max_tokens_truncated"
    assert state.runtime_provider_state["script_provider"]["current_provider"] == "openai"


@pytest.mark.asyncio
async def test_write_banter_populates_api_tokens_by_model(config, state):
    """End-to-end: a successful Anthropic banter call records tokens under the
    resolved model id, so the model-aware cost counter prices the right model."""
    config.anthropic_api_key = "test-key"
    host_name = config.hosts[0].name
    mock_usage = MagicMock()
    mock_usage.input_tokens = 123
    mock_usage.output_tokens = 456
    mock_content = MagicMock()
    mock_content.text = json.dumps({"lines": [{"host": host_name, "text": "Ciao!"}], "new_joke": None})
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_response.usage = mock_usage
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock(return_value=mock_client)

    expected_model = resolve_model(config.models, "banter", "anthropic")
    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        await write_banter(state, config)

    assert expected_model in state.api_tokens_by_model
    assert state.api_tokens_by_model[expected_model]["input"] == 123
    assert state.api_tokens_by_model[expected_model]["output"] == 456


@pytest.mark.asyncio
async def test_malformed_anthropic_response_does_not_mark_anthropic_active(config, state):
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    config.openai_api_key = "openai-key"
    openai_client = _mock_openai_response(json.dumps({"ok": "fallback"}))
    mock_cls = _mock_anthropic_response("this is not valid json {{{")

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        result = await _generate_json_response(prompt="p", config=config, state=state, model="model", max_tokens=100)

    assert result == {"ok": "fallback"}
    assert state.runtime_provider_state["script_provider"]["current_provider"] == "openai"
    assert [event.to_provider for event in state.runtime_events] == ["openai"]


@pytest.mark.asyncio
async def test_openai_call_logs_json_parse_failure_and_reraises(config, state, caplog):
    """When OpenAI returns malformed JSON, log fires with json_ok=False and JSONDecodeError propagates."""
    import logging

    from mammamiradio.hosts.scriptwriter import _generate_json_response

    config.anthropic_api_key = ""
    config.openai_api_key = "openai-key"
    openai_client = _mock_openai_response("not valid json {{{")

    with (
        caplog.at_level(logging.INFO, logger="mammamiradio.hosts.scriptwriter"),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
        pytest.raises(json.JSONDecodeError),
    ):
        await _generate_json_response(
            prompt="test prompt",
            config=config,
            state=state,
            model="unused",
            max_tokens=100,
            caller="ad",
        )

    fail_records = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "openai_script_call" and getattr(r, "json_ok", None) is False
    ]
    assert fail_records, "expected an openai_script_call log record with json_ok=False"
    record = fail_records[-1]
    assert record.caller == "ad"
    assert record.fallback_reason == "anthropic_absent"
    assert record.raw_preview.startswith("not valid json")


@pytest.mark.asyncio
async def test_failed_openai_fallback_does_not_update_provider_state(config, state):
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    config.openai_api_key = "openai-key"
    openai_client = _mock_openai_response("not valid json {{{")
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("anthropic invalid"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
        pytest.raises(json.JSONDecodeError),
    ):
        await _generate_json_response(prompt="p", config=config, state=state, model="model", max_tokens=100)

    assert "script_provider" not in state.runtime_provider_state
    assert list(state.runtime_events) == []


@pytest.mark.asyncio
async def test_auth_failure_is_memoized_and_skips_repeated_anthropic_calls(config, state):
    class AuthenticationError(Exception):
        pass

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "Fallback."}]}))

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=AuthenticationError("invalid x-api-key"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)
        await write_banter(state, config)

    assert mock_client.messages.create.await_count == 1
    assert state.anthropic_disabled_until > 0
    assert state.anthropic_auth_failures == 1


@pytest.mark.asyncio
async def test_model_not_found_is_memoized_and_skips_repeated_anthropic_calls(config, state):
    class NotFoundError(Exception):
        pass

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "Fallback."}]}))

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=NotFoundError("404 model not found"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)
        await write_banter(state, config)

    assert mock_client.messages.create.await_count == 1
    assert state.anthropic_disabled_until > 0
    assert state.anthropic_auth_failures == 0
    assert "NotFoundError" in state.anthropic_last_error


@pytest.mark.asyncio
async def test_usage_limit_is_memoized_and_skips_repeated_anthropic_calls(config, state):
    class UsageLimitError(Exception):
        pass

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "Fallback."}]}))

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=UsageLimitError("usage_limit: usage limits exceeded"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)
        await write_banter(state, config)

    assert mock_client.messages.create.await_count == 1
    assert state.anthropic_disabled_until > 0
    assert state.anthropic_auth_failures == 0
    assert "UsageLimitError" in state.anthropic_last_error


def test_nonretryable_classifier_yields_to_auth_precedence():
    from mammamiradio.hosts.scriptwriter import (
        _is_anthropic_auth_error,
        _is_anthropic_nonretryable_provider_error,
    )

    class NotFoundError(Exception):
        pass

    exc = NotFoundError("invalid x-api-key")
    assert _is_anthropic_auth_error(exc) is True
    assert _is_anthropic_nonretryable_provider_error(exc) is False


def test_usage_limit_classifier_matches_quota_patterns_only():
    from mammamiradio.hosts.scriptwriter import _is_anthropic_usage_limit_error

    assert _is_anthropic_usage_limit_error(Exception("You have reached your specified API usage limits")) is True
    assert _is_anthropic_usage_limit_error(Exception("usage_limit: monthly cap exceeded")) is True
    assert _is_anthropic_usage_limit_error(Exception("insufficient_quota for this account")) is True
    assert _is_anthropic_usage_limit_error(Exception("Your credit balance is too low")) is True
    assert _is_anthropic_usage_limit_error(Exception("invalid x-api-key")) is False
    assert _is_anthropic_usage_limit_error(Exception("invalid x-api-key; usage limit reached")) is False
    assert _is_anthropic_usage_limit_error(Exception("404 model not found")) is False
    assert _is_anthropic_usage_limit_error(Exception("404 model not found; usage limit reached")) is False


@pytest.mark.asyncio
async def test_model_not_found_backoff_is_scoped_to_model(config, state):
    import mammamiradio.hosts.scriptwriter as sw
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    class NotFoundError(Exception):
        pass

    config.openai_api_key = "openai-key"
    openai_client = _mock_openai_response(json.dumps({"ok": "fallback"}))
    ok_response = MagicMock()
    ok_content = MagicMock()
    ok_content.text = json.dumps({"ok": "anthropic"})
    ok_response.content = [ok_content]

    async def _create(**kwargs):
        if kwargs["model"] == "bad-model":
            raise NotFoundError("404 model not found")
        return ok_response

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=_create)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        first = await _generate_json_response(prompt="p", config=config, state=state, model="bad-model", max_tokens=100)
        second = await _generate_json_response(
            prompt="p", config=config, state=state, model="good-model", max_tokens=100
        )
        third = await _generate_json_response(prompt="p", config=config, state=state, model="bad-model", max_tokens=100)

    assert first == {"ok": "fallback"}
    assert second == {"ok": "anthropic"}
    assert third == {"ok": "fallback"}
    assert mock_client.messages.create.await_count == 2
    assert sw._anthropic_blocked_model == "bad-model"


@pytest.mark.asyncio
async def test_usage_limit_backoff_is_account_wide_across_models(config, state):
    import mammamiradio.hosts.scriptwriter as sw
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    class UsageLimitError(Exception):
        pass

    config.openai_api_key = "openai-key"
    openai_client = _mock_openai_response(json.dumps({"ok": "fallback"}))

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=UsageLimitError("usage limit reached"))

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        first = await _generate_json_response(prompt="p", config=config, state=state, model="model-a", max_tokens=100)
        second = await _generate_json_response(prompt="p", config=config, state=state, model="model-b", max_tokens=100)

    assert first == {"ok": "fallback"}
    assert second == {"ok": "fallback"}
    assert mock_client.messages.create.await_count == 1
    assert sw._anthropic_blocked_model == ""
    assert state.anthropic_auth_failures == 0


@pytest.mark.asyncio
async def test_blocked_anthropic_no_openai_raises(config, state):
    """_generate_json_response raises when Anthropic is auth-blocked and no OpenAI key (line 229)."""
    import mammamiradio.hosts.scriptwriter as sw
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    config.openai_api_key = ""
    sw._anthropic_auth_blocked_key = config.anthropic_api_key
    sw._anthropic_auth_blocked_until = float("inf")

    with pytest.raises(RuntimeError, match="temporarily disabled"):
        await _generate_json_response(prompt="prompt", config=config, state=state, model="model", max_tokens=100)


@pytest.mark.asyncio
async def test_live_auth_error_no_openai_reraises(config, state):
    """_generate_json_response re-raises auth error when no OpenAI key (line 260)."""
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    class AuthenticationError(Exception):
        pass

    config.openai_api_key = ""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=AuthenticationError("invalid x-api-key"))

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        pytest.raises(AuthenticationError),
    ):
        await _generate_json_response(prompt="prompt", config=config, state=state, model="model", max_tokens=100)

    assert state.anthropic_auth_failures == 1


# --- persona integration tests ---


@pytest.mark.asyncio
async def test_write_banter_injects_persona_context(config, state, tmp_path):
    """When a PersonaStore is attached, persona context appears in the prompt."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    await store.update_persona({"new_theories": ["ama il jazz notturno"]})
    await store.increment_session()
    state.persona_store = store

    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Bentornato!"}],
            "new_joke": None,
            "persona_updates": {
                "new_theories": ["ascolta sempre di sera"],
                "new_jokes": [],
                "callbacks_used": [],
            },
        }
    )

    # Capture the prompt sent to the LLM
    captured_prompts = []
    mock_cls = _mock_anthropic_response(response_json)
    original_create = mock_cls.return_value.messages.create

    async def _capture_create(**kwargs):
        captured_prompts.append(kwargs.get("messages", []))
        return await original_create(**kwargs)

    mock_cls.return_value.messages.create = AsyncMock(side_effect=_capture_create)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "Bentornato!"

    # Verify persona context was in the prompt
    assert len(captured_prompts) == 1
    prompt_text = captured_prompts[0][0]["content"]
    assert "listener_memory" in prompt_text
    assert "jazz notturno" in prompt_text

    # Verify persona_updates were persisted
    persona = await store.get_persona()
    assert "ascolta sempre di sera" in persona.theories


@pytest.mark.asyncio
async def test_write_banter_prompt_includes_optional_context_blocks(config, state, tmp_path):
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    config.cache_dir = tmp_path
    init_db(config.cache_dir / "mammamiradio.db")
    store = PersonaStore(db_path)
    await store.update_persona({"new_theories": ["torna sempre dopo mezzanotte"]})
    await store.increment_session()
    state.persona_store = store
    state.ha_context = "La cucina e accesa."
    state.ha_events_summary = "- La macchina del caffè: spento/a -> acceso/a (1 min fa)"
    state.ha_weather_arc = "Meteo: soleggiato, 22°C."
    state.ha_home_mood = "Musica in casa"
    state.ha_pending_directive = "Florian è appena tornato a casa. Salutalo subito."
    state.played_tracks.append(
        Track(
            title="Test Track",
            artist="Rule Artist",
            duration_ms=210000,
            spotify_id="track-rule-1",
            youtube_id="yt123",
        )
    )
    if len(config.hosts) == 1:
        config.hosts.append(HostPersonality(name="Giulia", voice="it-IT-IsabellaNeural", style="sharp"))
    config.hosts[0].personality.chaos = 90
    for _ in range(5):
        state.listener.record_outcome(skipped=False, energy_hint="high", track_display="Test Track")

    captured = {}

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "lines": [{"host": config.hosts[0].name, "text": "Bentornati."}],
            "new_joke": None,
            "persona_updates": {"new_theories": [], "new_jokes": [], "callbacks_used": []},
        }

    with (
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response),
        patch(
            "mammamiradio.playlist.song_cues.get_cues",
            return_value=[
                {"type": "reaction", "text": "React to the chorus like it's a scandal", "session": 1, "uses": 0}
            ],
        ),
    ):
        result, _ = await write_banter(state, config, is_first_listener=True)

    assert len(result) == 1
    prompt = captured["prompt"]
    assert "<home_state_data>" in prompt
    assert "EVENTI RECENTI" in prompt
    assert "La macchina del caffè" in prompt
    assert "WEATHER ARC" in prompt
    assert "TRACK MEMORY for Rule Artist" in prompt
    assert "HOME MOOD: Musica in casa" in prompt
    assert "HIGH PRIORITY" in prompt
    assert "<listener_behavior>" in prompt
    assert "<arc_phase>" in prompt
    assert "<listener_memory>" in prompt
    assert "FIRST listener" in prompt
    assert '"persona_updates"' in prompt
    assert '"song_cues"' in prompt
    assert state.ha_pending_directive == ""


@pytest.mark.asyncio
async def test_write_banter_keeps_interrupt_directive_until_producer_queues(config, state):
    state.ha_pending_directive = "La pasta scotta. Interrompi tutto."
    state.chaos_pending = ChaosSubtype.URGENT_INTERRUPT

    captured = {}

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "lines": [{"host": config.hosts[0].name, "text": "Muoviti."}],
            "new_joke": None,
            "persona_updates": {"new_theories": [], "new_jokes": [], "callbacks_used": []},
        }

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
        result, _ = await write_banter(state, config)

    assert result == [(config.hosts[0], "Muoviti.")]
    assert "HIGH PRIORITY" in captured["prompt"]
    assert "La pasta scotta" in captured["prompt"]
    assert state.ha_pending_directive == "La pasta scotta. Interrompi tutto."


@pytest.mark.asyncio
async def test_write_banter_prompt_includes_new_listener_block_for_non_first_listener(config, state):
    state.ha_home_mood = "Mood sconosciuto"
    state.ha_weather_arc = "Pioggia in avvicinamento"
    captured = {}

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "lines": [{"host": config.hosts[0].name, "text": "Ci siete?"}],
            "new_joke": None,
        }

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
        result, _ = await write_banter(state, config, is_new_listener=True, is_first_listener=False)

    assert len(result) == 1
    prompt = captured["prompt"]
    assert "A new listener JUST tuned in right now!" in prompt
    assert "FIRST listener" not in prompt
    assert "HOME MOOD: Mood sconosciuto" in prompt


@pytest.mark.asyncio
async def test_write_banter_works_without_persona_store(config, state):
    """Banter generation still works when no persona store is attached."""
    assert not hasattr(state, "persona_store") or state.persona_store is None

    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Ciao!"}],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "Ciao!"


@pytest.mark.asyncio
async def test_write_banter_defers_listener_request_mutation_until_commit(config, state):
    """Listener requests stay pending until the produced banter is actually committed."""
    host_name = config.hosts[0].name
    state.pending_requests.append(
        {
            "name": "Luca",
            "message": "Ciao radio",
            "type": "shoutout",
            "song_found": False,
            "song_error": False,
            "song_track": None,
            "banter_cycles_missed": 0,
            "ts": 0,
        }
    )

    async def _fake_generate_json_response(**kwargs):
        return {"lines": [{"host": host_name, "text": "Ciao Luca!"}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
        result, listener_request_commit = await write_banter(state, config)

    assert len(result) == 1
    assert state.pending_requests[0]["message"] == "Ciao radio"
    assert listener_request_commit is not None

    listener_request_commit.apply(state)

    assert state.pending_requests == []


def test_plan_listener_request_block_empty_queue(state):
    prompt, commit = _plan_listener_request_block(state)

    assert prompt == ""
    assert commit is None


def test_plan_listener_request_block_song_still_downloading_defers(state):
    req = {
        "name": "Luca",
        "message": "metti Eros Ramazzotti",
        "type": "song_request",
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert prompt == ""
    assert commit is not None
    assert commit.consume is False
    assert commit.mark_song_error is False
    commit.apply(state)
    assert req["banter_cycles_missed"] == 1
    assert req in state.pending_requests


def test_plan_listener_request_block_song_still_downloading_defers_at_cycle_two(state):
    """Cycle 2 (banter_cycles_missed=1→2) must still defer — timeout is at cycle 5."""
    req = {
        "name": "Luca",
        "message": "metti Eros Ramazzotti",
        "type": "song_request",
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 1,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert prompt == ""
    assert commit is not None
    assert commit.consume is False
    assert commit.mark_song_error is False
    commit.apply(state)
    assert req["banter_cycles_missed"] == 2
    assert req in state.pending_requests


def test_plan_listener_request_block_song_still_downloading_defers_at_cycle_three(state):
    """Cycle 3 (banter_cycles_missed=2→3) must still defer — timeout is at cycle 5."""
    req = {
        "name": "Luca",
        "message": "metti Eros Ramazzotti",
        "type": "song_request",
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 2,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert prompt == ""
    assert commit is not None
    assert commit.consume is False
    assert commit.mark_song_error is False
    commit.apply(state)
    assert req["banter_cycles_missed"] == 3
    assert req in state.pending_requests


def test_plan_listener_request_block_song_still_downloading_defers_at_cycle_four(state):
    """Cycle 4 (banter_cycles_missed=3→4) must still defer — timeout is at cycle 5."""
    req = {
        "name": "Luca",
        "message": "metti Eros Ramazzotti",
        "type": "song_request",
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 3,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert prompt == ""
    assert commit is not None
    assert commit.consume is False
    assert commit.mark_song_error is False
    commit.apply(state)
    assert req["banter_cycles_missed"] == 4
    assert req in state.pending_requests


def test_plan_listener_request_block_song_still_downloading_marks_error_after_five_cycles(state):
    req = {
        "name": "Luca",
        "message": "metti Eros Ramazzotti",
        "type": "song_request",
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 4,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert "SONG NOT FOUND" in prompt
    assert commit is not None
    assert commit.consume is True
    assert commit.mark_song_error is True
    commit.apply(state)
    assert req["song_error"] is True
    assert req not in state.pending_requests
    # Request moves to recently_consumed with song_not_found status
    assert len(state.recently_consumed_requests) == 1
    assert state.recently_consumed_requests[0]["status"] == "song_not_found"
    assert state.recently_consumed_requests[0]["name"] == "Luca"


def test_plan_listener_request_block_background_failure_consumes_song_not_found(state):
    # A song request whose background download already FAILED (song_error set
    # directly by _download_listener_song, before the 5-cycle timeout) must
    # consume as "song_not_found", not the default "sent_to_hosts".
    req = {
        "name": "Giulia",
        "message": "metti una canzone",
        "type": "song_request",
        "song_found": False,
        "song_error": True,
        "song_track": None,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(req)

    _, commit = _plan_listener_request_block(state)

    assert commit is not None
    assert commit.consume is True
    assert commit.mark_song_error is True
    commit.apply(state)
    assert req not in state.pending_requests
    assert len(state.recently_consumed_requests) == 1
    assert state.recently_consumed_requests[0]["status"] == "song_not_found"
    assert state.recently_consumed_requests[0]["name"] == "Giulia"


def test_listener_request_commit_populates_recently_consumed_on_acknowledge(state):
    req = {"name": "Sofia", "message": "ciao!", "type": "dedica", "ts": 1000.0}
    state.pending_requests.append(req)
    from mammamiradio.hosts.scriptwriter import ListenerRequestCommit

    commit = ListenerRequestCommit(request=req, consume=True)
    commit.apply(state)
    assert req not in state.pending_requests
    assert len(state.recently_consumed_requests) == 1
    consumed = state.recently_consumed_requests[0]
    assert consumed["status"] == "sent_to_hosts"
    assert consumed["name"] == "Sofia"


def test_plan_listener_request_block_song_found_announcement(state):
    requested_track = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=120000,
        youtube_id="yt123",
    )
    req = {
        "name": "Giulia",
        "message": "metti Albachiara",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_track": "Vasco Rossi - Albachiara",
        "song_track_obj": requested_track,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert "Vasco Rossi - Albachiara" in prompt
    assert commit is not None
    assert commit.consume is True
    assert state.pinned_track is requested_track
    assert state.force_next == SegmentType.MUSIC


def test_plan_listener_request_block_ignores_ready_second_song_until_it_reaches_head(state):
    first_req = {
        "name": "Luca",
        "message": "metti Eros Ramazzotti",
        "type": "song_request",
        "song_found": False,
        "song_error": False,
        "song_track": None,
        "banter_cycles_missed": 0,
    }
    second_track = Track(
        title="Albachiara",
        artist="Vasco Rossi",
        duration_ms=120000,
        youtube_id="yt123",
    )
    second_req = {
        "name": "Giulia",
        "message": "metti Albachiara",
        "type": "song_request",
        "song_found": True,
        "song_error": False,
        "song_track": "Vasco Rossi - Albachiara",
        "song_track_obj": second_track,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.extend([first_req, second_req])

    prompt, commit = _plan_listener_request_block(state)

    assert prompt == ""
    assert commit is not None
    assert commit.consume is False
    assert state.pinned_track is None
    assert state.force_next is None


def test_plan_listener_request_block_song_error_branch(state):
    req = {
        "name": "Giulia",
        "message": "metti Albachiara",
        "type": "song_request",
        "song_found": False,
        "song_error": True,
        "song_track": None,
        "banter_cycles_missed": 0,
    }
    state.pending_requests.append(req)

    prompt, commit = _plan_listener_request_block(state)

    assert "SONG NOT FOUND" in prompt
    assert commit is not None
    assert commit.consume is True


def test_listener_request_commit_apply_noops_when_request_missing(state):
    req = {
        "name": "Marta",
        "message": "ciao",
        "type": "song_request",
        "song_error": False,
        "banter_cycles_missed": 0,
    }
    commit = ListenerRequestCommit(request=req, banter_cycles_missed=3, mark_song_error=True, consume=True)

    commit.apply(state)

    assert req["song_error"] is False
    assert req["banter_cycles_missed"] == 0


@pytest.mark.asyncio
async def test_write_banter_survives_persona_get_failure(config, state, tmp_path):
    """Banter still generates when persona_store.get_persona() throws."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)

    # Make get_persona raise
    store.get_persona = AsyncMock(side_effect=RuntimeError("DB locked"))
    state.persona_store = store

    host_name = config.hosts[0].name
    response_json = json.dumps({"lines": [{"host": host_name, "text": "Funziona comunque!"}], "new_joke": None})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "Funziona comunque!"


@pytest.mark.asyncio
async def test_write_banter_survives_persona_update_failure(config, state, tmp_path):
    """Banter returns successfully even when update_persona throws."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    await store.update_persona({"new_theories": ["test theory"]})
    # Make update_persona raise
    store.update_persona = AsyncMock(side_effect=RuntimeError("DB locked"))
    state.persona_store = store

    host_name = config.hosts[0].name
    response_json = json.dumps(
        {
            "lines": [{"host": host_name, "text": "Banter ok!"}],
            "new_joke": None,
            "persona_updates": {"new_theories": ["will fail"], "new_jokes": [], "callbacks_used": []},
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    # Banter still returned despite update failure
    assert len(result) == 1
    assert result[0][1] == "Banter ok!"


# --- write_ad tests ---


@pytest.mark.asyncio
async def test_write_ad_returns_adscript(config, state):
    response_json = json.dumps(
        {
            "parts": [
                {"type": "sfx", "sfx": "chime"},
                {"type": "voice", "text": "Comprate ora!"},
            ],
            "mood": "upbeat",
            "summary": "A test ad for TestBrand",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="TestBrand", tagline="Il meglio del meglio", category="food")
    voices = {"default": AdVoice(name="Voce Uno", voice="it-IT-IsabellaNeural", style="enthusiastic")}

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config)

    assert isinstance(result, AdScript)
    assert result.brand == "TestBrand"
    assert result.mood == "upbeat"
    assert result.summary == "A test ad for TestBrand"
    assert len(result.parts) == 2
    assert result.parts[0].type == "sfx"
    assert result.parts[1].type == "voice"
    assert result.parts[1].text == "Comprate ora!"


@pytest.mark.asyncio
async def test_write_ad_strips_foreign_station_name_from_voice_parts(config, state):
    """Illusion guard wired into ads: an improvised competitor station name in an
    ad voice line is replaced with our station name before the spot airs."""
    brand = AdBrand(name="TestBrand", tagline="Il meglio", category="food")
    voices = {"default": AdVoice(name="Voce Uno", voice="it-IT-IsabellaNeural", style="enthusiastic")}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={
            "parts": [{"type": "voice", "text": "Solo su Radio Deejay Milano: TestBrand!"}],
            "summary": "ad",
        },
    ):
        result = await write_ad(brand, voices, state, config)

    joined = " ".join(p.text for p in result.parts if p.type == "voice")
    assert "Deejay" not in joined
    assert config.station.name in joined


@pytest.mark.asyncio
async def test_write_news_flash_strips_foreign_station_name(config, state):
    """Illusion guard wired into news flashes: an improvised competitor station
    name in the bulletin is replaced with our station name."""
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={"text": "Siamo su Radio Kiss Kiss e arriva una notizia bomba!"},
    ):
        _host, text, _category = await write_news_flash(state, config, category="breaking")

    assert "Kiss Kiss" not in text
    assert config.station.name in text


@pytest.mark.asyncio
async def test_write_ad_falls_back_on_api_exception(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    brand = AdBrand(name="FallbackBrand", tagline="Sempre il top", category="tech")
    voices = {"default": AdVoice(name="Voce Due", voice="it-IT-DiegoNeural", style="calm")}

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config)

    assert isinstance(result, AdScript)
    assert result.brand == "FallbackBrand"
    assert "Fallback" in result.summary
    assert len(result.parts) >= 1
    assert result.parts[0].type == "voice"


@pytest.mark.asyncio
async def test_write_ad_no_llm_returns_minimal_script(config, state):
    config.anthropic_api_key = ""
    config.openai_api_key = ""
    brand = AdBrand(name="FallbackBrand", tagline="Sempre il top", category="tech")
    voices = {"default": AdVoice(name="Voce Due", voice="it-IT-DiegoNeural", style="calm")}

    result = await write_ad(brand, voices, state, config)

    assert result.brand == "FallbackBrand"
    assert result.summary == "Sempre il top"
    assert result.parts[0].text == "FallbackBrand. Sempre il top"


@pytest.mark.asyncio
async def test_write_ad_ensures_voice_part_when_llm_returns_none(config, state):
    """If the LLM returns no voice parts, write_ad should add at least one."""
    response_json = json.dumps(
        {
            "parts": [
                {"type": "sfx", "sfx": "chime"},
                {"type": "pause", "duration": 0.5},
            ],
            "mood": "mysterious",
            "summary": "An ad with no voice",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="SilentBrand", tagline="Silenzio è oro", category="luxury")
    voices = {"default": AdVoice(name="Voce Tre", voice="it-IT-ElsaNeural", style="whispery")}

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config)

    assert isinstance(result, AdScript)
    voice_parts = [p for p in result.parts if p.type == "voice"]
    assert len(voice_parts) >= 1


@pytest.mark.asyncio
async def test_write_ad_prompt_includes_campaign_and_home_context(config, state):
    captured = {}
    state.ha_context = "Il balcone e aperto."
    state.record_ad_spot(brand="SagaBrand", summary="Il primo capitolo")
    state.record_ad_spot(brand="OtherBrand", summary="Una pubblicita concorrente")
    brand = AdBrand(
        name="SagaBrand",
        tagline="Sempre avanti",
        category="tech",
        campaign=MagicMock(
            premise="Vendono modem emotivi",
            escalation_rule="Ogni spot peggiora la situazione",
        ),
    )
    brand.campaign.sonic_signature = "startup_synth+whoosh"
    voices = {"hammer": AdVoice(name="Voce Uno", voice="it-IT-IsabellaNeural", style="enthusiastic")}

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "parts": [{"type": "voice", "text": "Compra adesso", "role": "hammer"}],
            "mood": "upbeat",
            "summary": "Campaign ad",
        }

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
        result = await write_ad(brand, voices, state, config)

    assert result.summary == "Campaign ad"
    prompt = captured["prompt"]
    assert "CAMPAIGN ARC" in prompt
    assert "CAMPAIGN SPINE" in prompt
    assert "<home_state_data>" in prompt


# --- Signature ad system tests ---


def test_ad_formats_constant_is_well_formed():
    """AD_FORMATS dict has an entry for every AdFormat enum value."""
    for fmt in AdFormat:
        assert fmt.value in AD_FORMATS
        assert isinstance(AD_FORMATS[fmt.value], str)
        assert len(AD_FORMATS[fmt.value]) > 20  # meaningful description


def test_speaker_roles_constant():
    """SPEAKER_ROLES has entries for the core roles."""
    for role in ("hammer", "seductress", "bureaucrat", "maniac", "witness", "disclaimer_goblin"):
        assert role in SPEAKER_ROLES


@pytest.mark.asyncio
async def test_write_ad_multi_role_json(config, state):
    """write_ad parses multi-role JSON from LLM."""
    response_json = json.dumps(
        {
            "parts": [
                {"type": "voice", "text": "Io sono il venditore!", "role": "hammer"},
                {"type": "sfx", "sfx": "chime"},
                {"type": "voice", "text": "Io sono il cliente!", "role": "witness"},
            ],
            "mood": "suspicious_jazz",
            "summary": "A duo ad",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="DuoBrand", tagline="Due voci", category="tech")
    voices = {
        "hammer": AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
        "witness": AdVoice(name="Testimonia", voice="it-IT-ElsaNeural", style="fake customer", role="witness"),
    }

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config, ad_format="duo_scene")

    assert isinstance(result, AdScript)
    roles = {p.role for p in result.parts if p.type == "voice" and p.role}
    assert "hammer" in roles
    assert "witness" in roles
    assert result.roles_used == sorted(roles)


@pytest.mark.asyncio
async def test_write_ad_legacy_json_compat(config, state):
    """write_ad handles old-format JSON (no role field on parts)."""
    response_json = json.dumps(
        {
            "parts": [
                {"type": "voice", "text": "Compra ora!"},
                {"type": "sfx", "sfx": "chime"},
            ],
            "mood": "lounge",
            "summary": "Simple ad",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="OldBrand", tagline="Tag", category="food")
    voices = {"default": AdVoice(name="Ann", voice="it-IT-DiegoNeural", style="warm")}

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config)

    assert isinstance(result, AdScript)
    assert result.parts[0].role == ""  # no role in legacy JSON
    assert result.format == "classic_pitch"  # default


@pytest.mark.asyncio
async def test_write_ad_demotes_duo_scene_with_single_role(config, state):
    """duo_scene with only 1 role in LLM output should be demoted to classic_pitch."""
    response_json = json.dumps(
        {
            "parts": [
                {"type": "voice", "text": "Solo io parlo!", "role": "hammer"},
                {"type": "sfx", "sfx": "sweep"},
                {"type": "voice", "text": "Ancora io!", "role": "hammer"},
            ],
            "mood": "upbeat",
            "summary": "Single-role duo attempt",
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    brand = AdBrand(name="DemoteBrand", tagline="Tag", category="tech")
    voices = {
        "hammer": AdVoice(name="Roberto", voice="it-IT-GianniNeural", style="booming", role="hammer"),
        "maniac": AdVoice(name="Fiamma", voice="it-IT-FiammaNeural", style="enthusiastic", role="maniac"),
    }

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config, ad_format="duo_scene")

    assert result.format == "classic_pitch"  # demoted from duo_scene


# --- write_news_flash tests ---


@pytest.mark.asyncio
async def test_write_news_flash_returns_tuple(config, state):
    response_json = json.dumps({"text": "NOTIZIA BOMBA: i treni arrivano in orario."})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_news_flash(state, config, category="breaking")

    host, text, category = result
    assert isinstance(host, HostPersonality)
    assert text == "NOTIZIA BOMBA: i treni arrivano in orario."
    assert category == "breaking"


@pytest.mark.asyncio
async def test_write_news_flash_no_key_returns_fallback(config, state):
    config.anthropic_api_key = ""
    host, text, category = await write_news_flash(state, config)
    assert isinstance(host, HostPersonality)
    assert "ultima ora" in text.lower() or len(text) > 0
    assert isinstance(category, str)


@pytest.mark.asyncio
async def test_write_news_flash_api_exception_returns_fallback(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("network error"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text, category = await write_news_flash(state, config, category="sports")

    assert isinstance(host, HostPersonality)
    assert isinstance(text, str) and len(text) > 0
    assert category == "sports"


@pytest.mark.asyncio
async def test_write_news_flash_sports_prompt_prioritizes_clarity(config, state):
    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        new_callable=AsyncMock,
        return_value={"text": "Il Borgo Sud pareggia al novantesimo con freddezza."},
    ) as mock_generate:
        _host, _text, category = await write_news_flash(state, config, category="sports")

    prompt = mock_generate.await_args.kwargs["prompt"]
    assert category == "sports"
    assert "measured and followable" in prompt
    assert "no all-caps hype" in prompt
    assert "no extended goal screams" in prompt
    assert "crescendo-meltdown" in prompt


@pytest.mark.asyncio
async def test_write_news_flash_strips_markdown_fences(config, state):
    response_text = '```json\n{"text": "Traffico bloccato."}\n```'
    mock_cls = _mock_anthropic_response(response_text)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        _host, text, _category = await write_news_flash(state, config)

    assert text == "Traffico bloccato."


# --- write_transition tests ---


@pytest.mark.asyncio
async def test_write_transition_returns_host_and_text(config, state):
    state.played_tracks = [Track(title="L'Estate", artist="Vivaldi", duration_ms=180000, spotify_id="v1")]
    response_json = json.dumps({"text": "Bellissima... e adesso una pausa."})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text = await write_transition(state, config, next_segment="ad")

    assert isinstance(host, HostPersonality)
    assert text == "Bellissima... e adesso una pausa."


@pytest.mark.asyncio
async def test_write_transition_no_key_returns_fallback(config, state):
    config.anthropic_api_key = ""
    for next_seg, expected in [("banter", "Allora..."), ("ad", "E adesso..."), ("news_flash", "Attenzione...")]:
        host, text = await write_transition(state, config, next_segment=next_seg)
        assert isinstance(host, HostPersonality)
        assert text == expected


@pytest.mark.asyncio
async def test_write_transition_api_exception_returns_fallback(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("timeout"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text = await write_transition(state, config, next_segment="banter")

    assert isinstance(host, HostPersonality)
    assert text == "Allora..."


@pytest.mark.asyncio
async def test_write_transition_strips_markdown_fences(config, state):
    response_text = '```json\n{"text": "Che bel pezzo..."}\n```'
    mock_cls = _mock_anthropic_response(response_text)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        _host, text = await write_transition(state, config)

    assert text == "Che bel pezzo..."


@pytest.mark.asyncio
async def test_write_transition_exclaim_style_selected_when_cues_present(config, state):
    """Exclaim style fires when r < 0.10 AND song_cues is non-empty."""
    state.played_tracks = [Track(title="Volare", artist="Modugno", duration_ms=180000, spotify_id="v1")]
    cues = [{"type": "anthem", "text": "starts slow then builds to a crescendo"}]
    captured_prompts = []

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "—e dai, basta così— e adesso parliamo."}

    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.05),
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        host, text = await write_transition(state, config, song_cues=cues)

    assert isinstance(host, HostPersonality)
    assert text == "—e dai, basta così— e adesso parliamo."
    assert captured_prompts and "Musical exclamation FIRST" in captured_prompts[0]


@pytest.mark.asyncio
async def test_write_transition_exclaim_suppressed_when_no_cues(config, state):
    """Empty list suppresses cue loading; exclaim style never fires without cues."""
    state.played_tracks = [
        Track(
            title="Volare",
            artist="Modugno",
            duration_ms=180000,
            spotify_id="v1",
            youtube_id="yt-volare",
        )
    ]
    captured_prompts = []

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "Bellissima, e adesso..."}

    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.05),
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        host, text = await write_transition(state, config, song_cues=[])

    assert isinstance(host, HostPersonality)
    assert text == "Bellissima, e adesso..."
    assert captured_prompts, "_generate_json_response was never called — patch path may be wrong"
    assert "Musical exclamation FIRST" not in captured_prompts[0]


@pytest.mark.asyncio
async def test_write_transition_loads_song_cues_from_current_track(config, state):
    """Default transition path should auto-load per-track cues for live callers."""
    state.played_tracks = [
        Track(
            title="Volare",
            artist="Modugno",
            duration_ms=180000,
            spotify_id="v1",
            youtube_id="yt-volare",
        )
    ]
    captured_prompts = []
    fake_cues = [{"type": "anthem", "text": "crowd favourite, all-hands sing-along"}]

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "—e dai, basta così— e adesso parliamo."}

    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.05),
        patch("mammamiradio.playlist.song_cues.get_cues", new=AsyncMock(return_value=fake_cues)) as mock_get_cues,
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        host, text = await write_transition(state, config)

    assert isinstance(host, HostPersonality)
    assert text == "—e dai, basta così— e adesso parliamo."
    mock_get_cues.assert_awaited_once()
    assert captured_prompts
    assert "SONG CHARACTER:" in captured_prompts[0]
    assert "crowd favourite" in captured_prompts[0]
    assert "Musical exclamation FIRST" in captured_prompts[0]


@pytest.mark.asyncio
async def test_write_transition_style_boundaries(config, state):
    """Deterministic style selection at RNG boundaries."""
    state.played_tracks = [Track(title="L'Estate", artist="Vivaldi", duration_ms=180000, spotify_id="v1")]
    cues = [{"type": "reaction", "text": "always energetic"}]
    captured_prompts = []

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "Allora..."}

    captured_prompts.clear()
    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.05),
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        await write_transition(state, config, song_cues=cues)
    assert captured_prompts and "Musical exclamation FIRST" in captured_prompts[0]

    captured_prompts.clear()
    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.15),
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        await write_transition(state, config, song_cues=cues)
    assert captured_prompts
    assert "Musical exclamation FIRST" not in captured_prompts[0]
    assert "still INSIDE the song's feeling" in captured_prompts[0]

    captured_prompts.clear()
    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.50),
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        await write_transition(state, config, song_cues=cues)
    assert captured_prompts
    assert "React to the song naturally" in captured_prompts[0]
    assert "Musical exclamation FIRST" not in captured_prompts[0]
    assert "still INSIDE the song's feeling" not in captured_prompts[0]


@pytest.mark.asyncio
async def test_write_transition_exclaim_echo_boundary(config, state):
    """At exactly r=0.10 with cues present, style must be echo (not exclaim)."""
    state.played_tracks = [Track(title="L'Estate", artist="Vivaldi", duration_ms=180000, spotify_id="v1")]
    cues = [{"type": "reaction", "text": "boundary test cue"}]
    captured_prompts = []

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "Allora..."}

    with (
        patch("mammamiradio.hosts.scriptwriter.random.random", return_value=0.10),
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt),
    ):
        await write_transition(state, config, song_cues=cues)

    assert captured_prompts, "_generate_json_response was never called — patch path may be wrong"
    # r=0.10 is NOT < 0.10, so exclaim must not fire; r < 0.30 → echo
    assert "Musical exclamation FIRST" not in captured_prompts[0]
    assert "still INSIDE the song's feeling" in captured_prompts[0]


@pytest.mark.asyncio
async def test_write_transition_cues_in_prompt(config, state):
    """SONG CHARACTER block appears in LLM prompt when song_cues is non-empty."""
    state.played_tracks = [Track(title="Azzurro", artist="Celentano", duration_ms=200000, spotify_id="a1")]
    cues = [{"type": "reaction", "text": "crowd favourite, sing-along moment"}]
    captured_prompts = []

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "—e dai— e adesso!"}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt):
        await write_transition(state, config, song_cues=cues)

    assert captured_prompts
    assert "SONG CHARACTER:" in captured_prompts[0]
    assert "crowd favourite" in captured_prompts[0]


@pytest.mark.asyncio
async def test_write_transition_cues_sanitized(config, state):
    """Cue text is passed through _sanitize_prompt_data (max_len=80) before prompt injection."""
    state.played_tracks = [Track(title="Test", artist="Artist", duration_ms=120000, spotify_id="t1")]
    # Cue text > 80 chars should be truncated; <>{} chars should be stripped
    long_cue = [{"type": "opera<tor>", "text": "x" * 100 + "{injected}"}]
    captured_prompts = []

    async def capture_prompt(*args, **kwargs):
        captured_prompts.append(kwargs.get("prompt", args[0] if args else ""))
        return {"text": "Allora..."}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", capture_prompt):
        await write_transition(state, config, song_cues=long_cue)

    assert captured_prompts
    assert "SONG CHARACTER:" in captured_prompts[0]
    # _sanitize_prompt_data strips <>{} and truncates to 80 chars + "..."
    assert "{injected}" not in captured_prompts[0]  # {} stripped
    assert "x" * 101 not in captured_prompts[0]  # truncated to ≤ 83 chars (80 + "...")
    assert "x" * 80 in captured_prompts[0]  # truncated form still present


@pytest.mark.asyncio
async def test_write_transition_default_song_cues_is_none(config, state):
    """write_transition() called without song_cues kwarg works without error."""
    state.played_tracks = [Track(title="Sole", artist="Artist", duration_ms=120000, spotify_id="s1")]
    response_json = json.dumps({"text": "E adesso..."})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text = await write_transition(state, config)

    assert isinstance(host, HostPersonality)
    assert isinstance(text, str)


# --- _personality_modifier contrast tests ---


def test_personality_modifier_produces_distinct_strings_for_high_chaos_pair():
    """Marco and Giulia both have high chaos/energy — their modifiers must differ."""
    from mammamiradio.core.models import HostPersonality, PersonalityAxes

    marco_axes = PersonalityAxes(energy=100, chaos=100, warmth=55, verbosity=68, nostalgia=75)
    giulia_axes = PersonalityAxes(energy=72, chaos=92, warmth=20, verbosity=66, nostalgia=30)

    marco_host = HostPersonality(name="Marco", voice="onyx", style="manic", personality=marco_axes)
    giulia_host = HostPersonality(name="Giulia", voice="test", style="sharp", personality=giulia_axes)

    marco_modifier = _personality_modifier("Marco", marco_axes, other_host=giulia_host)
    giulia_modifier = _personality_modifier("Giulia", giulia_axes, other_host=marco_host)

    # Both should produce non-empty modifiers
    assert marco_modifier, "Marco should get a non-empty modifier"
    assert giulia_modifier, "Giulia should get a non-empty modifier"

    # The modifiers must not be identical — the contrast is the whole point
    assert marco_modifier != giulia_modifier, (
        "Marco and Giulia received identical personality modifiers; relative contrast logic is not working."
    )

    # Marco (higher energy) should contain runaway/lead framing
    assert "runaway" in marco_modifier.lower() or "lead" in marco_modifier.lower(), (
        f"Marco (higher energy) should contain 'runaway' or 'lead' framing, got: {marco_modifier!r}"
    )

    # Giulia (lower energy) should contain surgical/controlled framing
    assert "surgical" in giulia_modifier.lower() or "controlled" in giulia_modifier.lower(), (
        f"Giulia (lower energy) should contain 'surgical' or 'controlled' framing, got: {giulia_modifier!r}"
    )


def test_personality_modifier_tie_energy_picks_single_deterministic_leader():
    """Equal-energy high-chaos pairs must still produce one leader and one contrast host."""
    from mammamiradio.core.models import HostPersonality, PersonalityAxes

    marco_axes = PersonalityAxes(energy=90, chaos=95, warmth=50, verbosity=55, nostalgia=40)
    giulia_axes = PersonalityAxes(energy=90, chaos=92, warmth=45, verbosity=60, nostalgia=35)

    marco_host = HostPersonality(name="Marco", voice="onyx", style="manic", personality=marco_axes)
    giulia_host = HostPersonality(name="Giulia", voice="test", style="sharp", personality=giulia_axes)

    marco_modifier = _personality_modifier("Marco", marco_axes, other_host=giulia_host)
    giulia_modifier = _personality_modifier("Giulia", giulia_axes, other_host=marco_host)
    marco_modifier_again = _personality_modifier("Marco", marco_axes, other_host=giulia_host)
    giulia_modifier_again = _personality_modifier("Giulia", giulia_axes, other_host=marco_host)

    assert marco_modifier and giulia_modifier
    assert marco_modifier == marco_modifier_again
    assert giulia_modifier == giulia_modifier_again

    runaway_count = sum("runaway" in m.lower() or "lead" in m.lower() for m in (marco_modifier, giulia_modifier))
    surgical_count = sum(
        "surgical" in m.lower() or "controlled" in m.lower() for m in (marco_modifier, giulia_modifier)
    )
    assert runaway_count == 1
    assert surgical_count == 1


def test_personality_modifier_energy_controls_runaway_when_axes_conflict():
    """If hosts split energy/chaos leadership, energy decides the runaway role."""
    from mammamiradio.core.models import HostPersonality, PersonalityAxes

    host_a_axes = PersonalityAxes(energy=95, chaos=80, warmth=45, verbosity=55, nostalgia=40)
    host_b_axes = PersonalityAxes(energy=80, chaos=100, warmth=45, verbosity=55, nostalgia=40)

    host_a = HostPersonality(name="HostA", voice="onyx", style="manic", personality=host_a_axes)
    host_b = HostPersonality(name="HostB", voice="test", style="sharp", personality=host_b_axes)

    host_a_modifier = _personality_modifier("HostA", host_a_axes, other_host=host_b)
    host_b_modifier = _personality_modifier("HostB", host_b_axes, other_host=host_a)

    assert host_a_modifier
    assert host_b_modifier
    assert host_a_modifier != host_b_modifier
    assert "runaway" in host_a_modifier.lower() or "lead" in host_a_modifier.lower()
    assert "surgical" in host_b_modifier.lower() or "controlled" in host_b_modifier.lower()


def test_fix_wrong_station_names_replaces_competitor():
    """Sanitizer swaps competitor station names with the correct station name."""
    from mammamiradio.hosts.scriptwriter import _fix_wrong_station_names

    station = "Mamma Mi Radio"

    # "siamo su <wrong>" → "siamo su <ours>"
    result = _fix_wrong_station_names("Siamo su Radio Kiss Kiss Moosach e la musica!", station)
    assert "Kiss Kiss" not in result
    assert station in result

    # standalone "Radio <wrong>" → station name
    result2 = _fix_wrong_station_names("Radio Kiss Kiss vi dà il benvenuto.", station)
    assert "Kiss Kiss" not in result2
    assert station in result2

    # correct station name left unchanged
    result3 = _fix_wrong_station_names(f"Siamo su {station} sempre!", station)
    assert station in result3
    assert result3 == f"Siamo su {station} sempre!"

    # no station mention — text passes through unchanged
    result4 = _fix_wrong_station_names("E adesso la musica.", station)
    assert result4 == "E adesso la musica."


@pytest.mark.asyncio
async def test_write_banter_dedup_drops_identical_consecutive_lines(config, state):
    """Banter dedup guard removes consecutive lines with identical text."""
    host_name = config.hosts[0].name
    # LLM returns two consecutive identical lines — a real copy-paste error
    response_json = json.dumps(
        {
            "lines": [
                {"host": host_name, "text": "Eccoci a voi!"},
                {"host": host_name, "text": "Eccoci a voi!"},  # duplicate
                {"host": host_name, "text": "E adesso la musica."},
            ],
            "new_joke": None,
        }
    )
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    texts = [text for _host, text in result]
    assert texts == ["Eccoci a voi!", "E adesso la musica."], f"Expected duplicate line dropped, got: {texts}"


# ---------------------------------------------------------------------------
# Tiered HA reference depth + weather-mood fusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banter_ha_tiered_no_mood(config, state):
    """When no mood is active, prompt says 'ONE item'."""
    state.ha_context = "Luci accese."

    captured = {}

    async def _fake(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Ciao."}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake):
        await write_banter(state, config)

    assert "ONE item" in captured["prompt"]
    assert "UP TO TWO" not in captured["prompt"]


@pytest.mark.asyncio
async def test_banter_ha_tiered_with_mood(config, state):
    """When mood is active, prompt says 'UP TO TWO'."""
    state.ha_context = "Luci accese."
    state.ha_home_mood = "Serata cinema"

    captured = {}

    async def _fake(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Ciao."}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake):
        await write_banter(state, config)

    assert "UP TO TWO" in captured["prompt"]
    assert "mood counts toward this cap" in captured["prompt"]


@pytest.mark.asyncio
async def test_banter_weather_mood_fusion(config, state):
    """When both weather and mood are set, fusion instruction appears."""
    state.ha_context = "Luci accese."
    state.ha_home_mood = "Serata cinema"
    state.ha_weather_arc = "Meteo: pioggia, 12°C."

    captured = {}

    async def _fake(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Ciao."}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake):
        await write_banter(state, config)

    assert "Weather and home mood are aligned" in captured["prompt"]


@pytest.mark.asyncio
async def test_banter_weather_only_no_fusion(config, state):
    """When only weather is set (no mood), no fusion instruction."""
    state.ha_context = "Luci accese."
    state.ha_weather_arc = "Meteo: soleggiato, 22°C."

    captured = {}

    async def _fake(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Ciao."}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake):
        await write_banter(state, config)

    assert "Weather and home mood are aligned" not in captured["prompt"]


@pytest.mark.asyncio
async def test_banter_security_boundary_preserved(config, state):
    """HA instructions must be OUTSIDE <home_state_data> tags."""
    state.ha_context = "Test data."
    state.ha_home_mood = "Serata cinema"

    captured = {}

    async def _fake(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Ciao."}], "new_joke": None}

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake):
        await write_banter(state, config)

    prompt = captured["prompt"]
    # Security boundary: instructions reference data tags but are NOT inside them
    data_start = prompt.index("<home_state_data>")
    data_end = prompt.index("</home_state_data>")
    inside_tags = prompt[data_start:data_end]
    # The instruction text ("READ-ONLY", "UP TO TWO") appears before or on the tag line,
    # but NOT inside the data content
    assert "READ-ONLY sensor data" in prompt
    assert "UP TO TWO" in prompt
    # The actual HA data is inside the tags
    assert "Test data." in inside_tags


@pytest.mark.asyncio
async def test_write_banter_song_cues_schema_omitted_when_no_yt_id(config, state, tmp_path):
    """When a track has no youtube_id, song_cues is omitted from the persona_update schema."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    await store.update_persona({"new_theories": ["notturno"]})
    await store.increment_session()
    state.persona_store = store
    # Track with empty youtube_id — song_cues schema should be omitted
    state.played_tracks.append(Track(title="No ID Track", artist="Artist", duration_ms=180000, youtube_id=""))

    captured = {}

    async def _fake_generate(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {
            "lines": [{"host": config.hosts[0].name, "text": "Ciao."}],
            "new_joke": None,
            "persona_updates": {"new_theories": [], "new_jokes": [], "callbacks_used": []},
        }

    with patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate):
        await write_banter(state, config)

    prompt = captured["prompt"]
    assert '"persona_updates"' in prompt
    # No youtube_id → song_cues field must not appear
    assert '"song_cues"' not in prompt


@pytest.mark.asyncio
async def test_write_banter_bump_usage_exception_is_swallowed(config, state, tmp_path):
    """bump_usage raising must not abort banter generation."""
    from mammamiradio.core.sync import init_db
    from mammamiradio.hosts.persona import PersonaStore

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    store = PersonaStore(db_path)
    await store.update_persona({"new_theories": ["notturno"]})
    await store.increment_session()
    state.persona_store = store
    state.played_tracks.append(Track(title="Test", artist="Artist", duration_ms=180000, youtube_id="yt_bump_err"))

    async def _fake_generate(**kwargs):
        return {
            "lines": [{"host": config.hosts[0].name, "text": "Ok."}],
            "new_joke": None,
            "persona_updates": {"new_theories": [], "new_jokes": [], "callbacks_used": []},
        }

    with (
        patch("mammamiradio.hosts.scriptwriter._generate_json_response", side_effect=_fake_generate),
        patch(
            "mammamiradio.playlist.song_cues.get_cues",
            return_value=[{"type": "reaction", "text": "Great track", "session": 1, "uses": 0}],
        ),
        patch("mammamiradio.playlist.song_cues.bump_usage", side_effect=RuntimeError("DB error")),
    ):
        result, _ = await write_banter(state, config)

    # Banter completed despite bump_usage raising
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Module state after importlib.reload
# ---------------------------------------------------------------------------


def test_module_state_reset_after_reload():
    """After importlib.reload(), module-level lazy-init state is cleared.

    _anthropic_client and _cached_system_prompt are reset to their initial
    values so the next LLM call reinitializes them cleanly.
    """
    import importlib

    import mammamiradio.hosts.scriptwriter as _sw

    # Force some module-level state to be non-default before reload
    _sw._cached_system_prompt = "cached prompt"

    importlib.reload(_sw)

    # State must be cleared by module re-execution
    assert _sw._cached_system_prompt == ""
    assert _sw._anthropic_client is None


def test_has_script_llm_is_public():
    """has_script_llm (no underscore) must be importable and callable after rename."""
    from pathlib import Path

    from mammamiradio.core.config import load_config
    from mammamiradio.hosts.scriptwriter import has_script_llm

    toml_path = str(Path(__file__).resolve().parents[2] / "radio.toml")
    config = load_config(toml_path)
    # Result is bool — function is accessible and callable
    assert isinstance(has_script_llm(config), bool)


# --- WS3-A concurrent auth-flood prevention ---


@pytest.mark.asyncio
async def test_concurrent_401s_trigger_exactly_one_anthropic_attempt(config, state):
    """N tasks racing _generate_json_response against a bad key must serialize.

    Reproduces the 2026-04-13 flood: concurrent banter/ad/transition tasks all
    raced past the block check before any could set _anthropic_auth_blocked_until.
    After the fix, the attempt lock serializes the critical section so only the
    first concurrent task hits Anthropic; the rest see the block and fall back.
    """
    import asyncio as _asyncio

    from mammamiradio.hosts.scriptwriter import _generate_json_response

    class _AuthError(Exception):
        pass

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "Fallback."}]}))

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=_AuthError("invalid x-api-key"))

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        results = await _asyncio.gather(
            *(
                _generate_json_response(prompt="p", config=config, state=state, model="claude-test", max_tokens=100)
                for _ in range(8)
            ),
            return_exceptions=True,
        )

    assert mock_client.messages.create.await_count == 1, (
        f"expected 1 Anthropic attempt across 8 concurrent calls, got {mock_client.messages.create.await_count}"
    )
    assert state.anthropic_auth_failures == 1
    assert all(not isinstance(r, Exception) for r in results), f"unexpected exceptions: {results}"


@pytest.mark.asyncio
async def test_concurrent_400s_no_openai_all_reraise(config, state):
    """Concurrent auth failures with no OpenAI fallback: first raises, rest see block and raise cleanly."""
    import asyncio as _asyncio

    from mammamiradio.hosts.scriptwriter import _generate_json_response

    class _AuthError(Exception):
        pass

    config.openai_api_key = ""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=_AuthError("invalid x-api-key"))

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
    ):
        results = await _asyncio.gather(
            *(
                _generate_json_response(prompt="p", config=config, state=state, model="claude-test", max_tokens=100)
                for _ in range(5)
            ),
            return_exceptions=True,
        )

    # Exactly one real HTTP attempt; the rest see the block and RuntimeError out.
    assert mock_client.messages.create.await_count == 1
    assert state.anthropic_auth_failures == 1
    assert sum(isinstance(r, _AuthError) for r in results) == 1
    assert sum(isinstance(r, RuntimeError) for r in results) == 4


@pytest.mark.asyncio
async def test_backoff_expiry_allows_exactly_one_retry_and_logs_once(config, state, caplog):
    """After backoff expires, next call retries Anthropic once (not a flood); log fires once."""
    import logging as _logging

    import mammamiradio.hosts.scriptwriter as sw
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "Fallback."}]}))

    class _AuthError(Exception):
        pass

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=_AuthError("invalid x-api-key"))

    # Pre-set an expired block for the current key.
    sw._anthropic_auth_blocked_key = config.anthropic_api_key
    sw._anthropic_auth_blocked_until = 1.0  # in the past
    sw._anthropic_block_expired_logged = False

    caplog.set_level(_logging.INFO, logger="mammamiradio.hosts.scriptwriter")

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter._openai_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        patch("mammamiradio.hosts.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        # First call after expiry: should retry Anthropic once.
        await _generate_json_response(prompt="p", config=config, state=state, model="claude-test", max_tokens=100)
        # Second call within the new backoff: no Anthropic attempt.
        await _generate_json_response(prompt="p", config=config, state=state, model="claude-test", max_tokens=100)

    assert mock_client.messages.create.await_count == 1
    expiry_logs = [r for r in caplog.records if "backoff expired" in r.getMessage()]
    assert len(expiry_logs) == 1, (
        f"expected 1 expiry log, got {len(expiry_logs)}: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_key_rotation_clears_block(config, state):
    """Loading a different anthropic_api_key resets the block so the new key is tried."""
    import mammamiradio.hosts.scriptwriter as sw
    from mammamiradio.hosts.scriptwriter import _generate_json_response

    # Simulate prior block on the OLD key.
    sw._anthropic_auth_blocked_key = "old-key-that-401d"
    sw._anthropic_auth_blocked_until = float("inf")

    config.anthropic_api_key = "fresh-rotated-key"
    config.openai_api_key = ""

    ok_client = _mock_anthropic_response(json.dumps({"ok": True}))

    with (
        patch("mammamiradio.hosts.scriptwriter._anthropic_client", None),
        patch("mammamiradio.hosts.scriptwriter.anthropic.AsyncAnthropic", ok_client),
    ):
        result = await _generate_json_response(
            prompt="p", config=config, state=state, model="claude-test", max_tokens=100
        )

    assert result == {"ok": True}
    assert sw._anthropic_auth_blocked_key == ""
    assert sw._anthropic_auth_blocked_until == 0.0


@pytest.mark.asyncio
async def test_write_banter_injects_running_gag_with_instruction_outside_fence(config, state):
    """Gag DATA goes inside <home_state_data>; the use/no-use INSTRUCTION outside it."""
    state.ha_running_gag = "La macchina del caffè: spento/a → acceso/a, di nuovo stasera."
    captured = {}

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Ancora caffè?"}], "new_joke": None}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        side_effect=_fake_generate_json_response,
    ):
        await write_banter(state, config)

    prompt = captured["prompt"]
    assert "STASERA:" in prompt
    assert "di nuovo stasera" in prompt
    assert "RUNNING GAG:" in prompt
    # The instruction must sit OUTSIDE the fence (before the opening tag); the
    # gag data must sit INSIDE it. The opening fence is the tag-on-its-own-line
    # ("<home_state_data>\n"); the bare "<home_state_data>" also appears in the
    # IMPORTANT boundary instruction, so anchor on the newline-delimited tags.
    fence_open = prompt.index("<home_state_data>\n")
    fence_close = prompt.index("</home_state_data>")
    assert prompt.index("RUNNING GAG:") < fence_open
    assert fence_open < prompt.index("STASERA:") < fence_close
    # Consumed after one use.
    assert state.ha_running_gag == ""


@pytest.mark.asyncio
async def test_write_banter_omits_running_gag_block_when_empty(config, state):
    """S2 empty-fallback: no gag → no STASERA block, no instruction, no crash."""
    state.ha_running_gag = ""
    captured = {}

    async def _fake_generate_json_response(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return {"lines": [{"host": config.hosts[0].name, "text": "Si parte."}], "new_joke": None}

    with patch(
        "mammamiradio.hosts.scriptwriter._generate_json_response",
        side_effect=_fake_generate_json_response,
    ):
        result, _ = await write_banter(state, config)

    prompt = captured["prompt"]
    assert "STASERA:" not in prompt
    assert "RUNNING GAG:" not in prompt
    assert len(result) == 1
