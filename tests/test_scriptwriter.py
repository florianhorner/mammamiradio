"""Tests for scriptwriter module: prompt building, banter, and ad generation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mammamiradio.config import load_config
from mammamiradio.models import (
    AdBrand,
    AdFormat,
    AdScript,
    AdVoice,
    HostPersonality,
    StationState,
    Track,
)
from mammamiradio.scriptwriter import (
    AD_FORMATS,
    SPEAKER_ROLES,
    _build_system_prompt,
    _massage_transition_text,
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
        result = await write_banter(state, config)

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
        result = await write_banter(state, config)

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
        result = await write_banter(state, config)

    assert len(result) == 1
    # Fallback text for Italian
    assert result[0][1] in ("E torniamo alla musica!", "And back to the music!")


@pytest.mark.asyncio
async def test_write_banter_falls_back_on_malformed_json(config, state):
    mock_cls = _mock_anthropic_response("this is not valid json {{{")

    with (
        patch("mammamiradio.scriptwriter._anthropic_client", None),
        patch("mammamiradio.scriptwriter.anthropic.AsyncAnthropic", mock_cls),
    ):
        result = await write_banter(state, config)

    assert len(result) == 1
    # Should be fallback copy
    assert isinstance(result[0][0], HostPersonality)
    assert isinstance(result[0][1], str)


@pytest.mark.asyncio
async def test_write_banter_no_llm_returns_language_fallback(config, state):
    config.anthropic_api_key = ""
    config.openai_api_key = ""

    result = await write_banter(state, config)

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
        result = await write_banter(state, config)

    assert len(result) == 1
    assert result[0][0].name == host_name
    assert result[0][1] == "OpenAI salva la diretta."


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
        result = await write_banter(state, config)

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
        patch("mammamiradio.track_rules.get_rules", return_value=["React to the chorus like it's a scandal"]),
    ):
        result = await write_banter(state, config, is_first_listener=True)

    assert len(result) == 1
    prompt = captured["prompt"]
    assert "<home_state_data>" in prompt
    assert "EVENTI RECENTI" in prompt
    assert "La macchina del caffè" in prompt
    assert "WEATHER ARC" in prompt
    assert "TRACK RULES for Rule Artist" in prompt
    assert "HOME MOOD: Musica in casa" in prompt
    assert "HIGH PRIORITY" in prompt
    assert "<listener_behavior>" in prompt
    assert "<listener_memory>" in prompt
    assert "FIRST listener" in prompt
    assert '"persona_updates"' in prompt
    assert state.ha_pending_directive == ""


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
        result = await write_banter(state, config)

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
        result, listener_request_commit = await write_banter(state, config, return_listener_request_commit=True)

    assert len(result) == 1
    assert state.pending_requests[0]["message"] == "Ciao radio"
    assert listener_request_commit is not None

    listener_request_commit.apply(state)

    assert state.pending_requests == []


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
        result = await write_banter(state, config)

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
        result = await write_banter(state, config)

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
