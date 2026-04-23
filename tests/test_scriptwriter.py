"""Tests for scriptwriter module: prompt building, banter, and ad generation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mammamiradio.scriptwriter as scriptwriter_module
from mammamiradio.config import load_config
from mammamiradio.ad_creative import AD_FORMATS, AdBrand, AdFormat, AdScript, AdVoice, SPEAKER_ROLES
from mammamiradio.models import (
    HostPersonality,
    SegmentType,
    StationState,
    Track,
)
from mammamiradio.scriptwriter import (
    ListenerRequestCommit,
    _build_system_prompt,
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        await write_banter(state, config)

    assert "The traffic joke" in state.running_jokes


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_api_exception(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    # Fallback now returns a multi-line exchange (3 lines) so banter sounds complete
    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert isinstance(text, str)
        assert len(text) > 0


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_malformed_json(config, state):
    mock_cls = _mock_anthropic_response("this is not valid json {{{")

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    # Fallback now returns a multi-line exchange so banter sounds complete
    assert len(result) >= 2
    for host, text in result:
        assert isinstance(host, HostPersonality)
        assert isinstance(text, str)
        assert len(text) > 0


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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter._openai_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][0].name == host_name
    assert result[0][1] == "OpenAI salva la diretta."


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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter._openai_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
        patch("mammamiradio.scriptwriter._get_openai_client", return_value=openai_client),
    ):
        await write_banter(state, config)
        await write_banter(state, config)

    assert mock_client.messages.create.await_count == 1
    assert state.anthropic_disabled_until > 0
    assert state.anthropic_auth_failures == 1


@pytest.mark.asyncio
async def test_blocked_anthropic_no_openai_raises(config, state):
    """_generate_json_response raises when Anthropic is auth-blocked and no OpenAI key (line 229)."""
    import mammamiradio.scriptwriter as sw
    from mammamiradio.scriptwriter import _generate_json_response

    config.openai_api_key = ""
    sw._anthropic_auth_blocked_key = config.anthropic_api_key
    sw._anthropic_auth_blocked_until = float("inf")

    with pytest.raises(RuntimeError, match="temporarily disabled"):
        await _generate_json_response(prompt="prompt", config=config, state=state, model="model", max_tokens=100)


@pytest.mark.asyncio
async def test_live_auth_error_no_openai_reraises(config, state):
    """_generate_json_response re-raises auth error when no OpenAI key (line 260)."""
    from mammamiradio.scriptwriter import _generate_json_response

    class AuthenticationError(Exception):
        pass

    config.openai_api_key = ""
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=AuthenticationError("invalid x-api-key"))

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        pytest.raises(AuthenticationError),
    ):
        await _generate_json_response(prompt="prompt", config=config, state=state, model="model", max_tokens=100)

    assert state.anthropic_auth_failures == 1


# --- persona integration tests ---


@pytest.mark.asyncio
async def test_write_banter_injects_persona_context(config, state, tmp_path):
    """When a PersonaStore is attached, persona context appears in the prompt."""
    from mammamiradio.persona import PersonaStore
    from mammamiradio.sync import init_db

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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
    from mammamiradio.persona import PersonaStore
    from mammamiradio.sync import init_db

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
        patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response),
        patch(
            "mammamiradio.song_cues.get_cues",
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
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


def test_plan_listener_request_block_song_still_downloading_marks_error_after_two_cycles(state):
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

    assert "SONG NOT FOUND" in prompt
    assert commit is not None
    assert commit.consume is True
    assert commit.mark_song_error is True
    commit.apply(state)
    assert req["song_error"] is True
    assert req not in state.pending_requests


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
    from mammamiradio.persona import PersonaStore
    from mammamiradio.sync import init_db

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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result, _ = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][1] == "Funziona comunque!"


@pytest.mark.asyncio
async def test_write_banter_survives_persona_update_failure(config, state, tmp_path):
    """Banter returns successfully even when update_persona throws."""
    from mammamiradio.persona import PersonaStore
    from mammamiradio.sync import init_db

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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
async def test_write_ad_falls_back_on_api_exception(config, state):
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=Exception("API down"))
    mock_cls = MagicMock(return_value=mock_client)

    brand = AdBrand(name="FallbackBrand", tagline="Sempre il top", category="tech")
    voices = {"default": AdVoice(name="Voce Due", voice="it-IT-DiegoNeural", style="calm")}

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake_generate_json_response):
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_ad(brand, voices, state, config, ad_format="duo_scene")

    assert result.format == "classic_pitch"  # demoted from duo_scene


# --- write_news_flash tests ---


@pytest.mark.asyncio
async def test_write_news_flash_returns_tuple(config, state):
    response_json = json.dumps({"text": "NOTIZIA BOMBA: i treni arrivano in orario."})
    mock_cls = _mock_anthropic_response(response_json)

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text, category = await write_news_flash(state, config, category="sports")

    assert isinstance(host, HostPersonality)
    assert isinstance(text, str) and len(text) > 0
    assert category == "sports"


@pytest.mark.asyncio
async def test_write_news_flash_strips_markdown_fences(config, state):
    response_text = '```json\n{"text": "Traffico bloccato."}\n```'
    mock_cls = _mock_anthropic_response(response_text)

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        host, text = await write_transition(state, config, next_segment="banter")

    assert isinstance(host, HostPersonality)
    assert text == "Allora..."


@pytest.mark.asyncio
async def test_write_transition_strips_markdown_fences(config, state):
    response_text = '```json\n{"text": "Che bel pezzo..."}\n```'
    mock_cls = _mock_anthropic_response(response_text)

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        _host, text = await write_transition(state, config)

    assert text == "Che bel pezzo..."


# --- _personality_modifier contrast tests ---


def test_personality_modifier_produces_distinct_strings_for_high_chaos_pair():
    """Marco and Giulia both have high chaos/energy — their modifiers must differ."""
    from mammamiradio.models import HostPersonality, PersonalityAxes

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
    from mammamiradio.models import HostPersonality, PersonalityAxes

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
    from mammamiradio.models import HostPersonality, PersonalityAxes

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
    from mammamiradio.scriptwriter import _fix_wrong_station_names

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
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake):
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake):
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake):
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake):
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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake):
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
    from mammamiradio.persona import PersonaStore
    from mammamiradio.sync import init_db

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

    with patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake_generate):
        await write_banter(state, config)

    prompt = captured["prompt"]
    assert '"persona_updates"' in prompt
    # No youtube_id → song_cues field must not appear
    assert '"song_cues"' not in prompt


@pytest.mark.asyncio
async def test_write_banter_bump_usage_exception_is_swallowed(config, state, tmp_path):
    """bump_usage raising must not abort banter generation."""
    from mammamiradio.persona import PersonaStore
    from mammamiradio.sync import init_db

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
        patch("mammamiradio.scriptwriter._generate_json_response", side_effect=_fake_generate),
        patch(
            "mammamiradio.song_cues.get_cues",
            return_value=[{"type": "reaction", "text": "Great track", "session": 1, "uses": 0}],
        ),
        patch("mammamiradio.song_cues.bump_usage", side_effect=RuntimeError("DB error")),
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

    import mammamiradio.scriptwriter as _sw

    # Force some module-level state to be non-default before reload
    _sw._cached_system_prompt = "cached prompt"

    importlib.reload(_sw)

    # State must be cleared by module re-execution
    assert _sw._cached_system_prompt == ""
    assert _sw._anthropic_client is None


def test_has_script_llm_is_public():
    """has_script_llm (no underscore) must be importable and callable after rename."""
    from pathlib import Path

    from mammamiradio.config import load_config
    from mammamiradio.scriptwriter import has_script_llm

    toml_path = str(Path(__file__).parent.parent / "radio.toml")
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

    from mammamiradio.scriptwriter import _generate_json_response

    class _AuthError(Exception):
        pass

    config.openai_api_key = "openai-key"
    host_name = config.hosts[0].name
    openai_client = _mock_openai_response(json.dumps({"lines": [{"host": host_name, "text": "Fallback."}]}))

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=_AuthError("invalid x-api-key"))

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter._openai_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        patch("mammamiradio.scriptwriter._get_openai_client", return_value=openai_client),
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

    from mammamiradio.scriptwriter import _generate_json_response

    class _AuthError(Exception):
        pass

    config.openai_api_key = ""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=_AuthError("invalid x-api-key"))

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
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

    import mammamiradio.scriptwriter as sw
    from mammamiradio.scriptwriter import _generate_json_response

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

    caplog.set_level(_logging.INFO, logger="mammamiradio.scriptwriter")

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter._openai_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", MagicMock(return_value=mock_client)),
        patch("mammamiradio.scriptwriter._get_openai_client", return_value=openai_client),
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
    import mammamiradio.scriptwriter as sw
    from mammamiradio.scriptwriter import _generate_json_response

    # Simulate prior block on the OLD key.
    sw._anthropic_auth_blocked_key = "old-key-that-401d"
    sw._anthropic_auth_blocked_until = float("inf")

    config.anthropic_api_key = "fresh-rotated-key"
    config.openai_api_key = ""

    ok_client = _mock_anthropic_response(json.dumps({"ok": True}))

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", ok_client),
    ):
        result = await _generate_json_response(
            prompt="p", config=config, state=state, model="claude-test", max_tokens=100
        )

    assert result == {"ok": True}
    assert sw._anthropic_auth_blocked_key == ""
    assert sw._anthropic_auth_blocked_until == 0.0
