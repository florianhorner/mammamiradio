"""Tests for PersonaStore (async SQLite persona persistence)."""

from __future__ import annotations

import pytest

from mammamiradio.persona import ListenerPersona, PersonaStore


@pytest.fixture()
async def store(tmp_path):
    """PersonaStore backed by a temp SQLite DB, tables initialized."""
    from mammamiradio.sync import init_db

    db_path = tmp_path / "persona.db"
    init_db(db_path)
    return PersonaStore(db_path)


@pytest.mark.asyncio
async def test_get_persona_returns_empty_on_no_row(store):
    persona = await store.get_persona()
    assert isinstance(persona, ListenerPersona)
    assert persona.motifs == []
    assert persona.theories == []
    assert persona.session_count == 0


@pytest.mark.asyncio
async def test_update_persona_ignores_non_dict(store):
    """update_persona with non-dict input logs error and returns without raising."""
    await store.update_persona("not a dict")  # should not raise
    persona = await store.get_persona()
    assert persona.theories == []


@pytest.mark.asyncio
async def test_update_persona_adds_theories(store):
    await store.update_persona({"new_theories": ["ama il jazz", "ascolta di notte"]})
    persona = await store.get_persona()
    assert "ama il jazz" in persona.theories
    assert "ascolta di notte" in persona.theories


@pytest.mark.asyncio
async def test_update_persona_sanitizes_entries(store):
    """HTML-like content in persona updates is stripped."""
    await store.update_persona({"new_theories": ["<script>evil()</script>"]})
    persona = await store.get_persona()
    assert all("<" not in t for t in persona.theories)


@pytest.mark.asyncio
async def test_update_persona_caps_field_length(store):
    long_theory = "x" * 500
    await store.update_persona({"new_theories": [long_theory]})
    persona = await store.get_persona()
    from mammamiradio.persona import MAX_FIELD_ENTRY_LEN

    assert all(len(t) <= MAX_FIELD_ENTRY_LEN for t in persona.theories)


@pytest.mark.asyncio
async def test_record_play_stores_entry(store):
    """record_play inserts a play_history row and commits without error."""
    # Should not raise even for a youtube_id with no matching track row
    await store.record_play("yt_test_123", session_id="s1")


@pytest.mark.asyncio
async def test_get_recent_plays_empty_on_fresh_db(store):
    plays = await store.get_recent_plays(n=5)
    assert plays == []


@pytest.mark.asyncio
async def test_maybe_new_session_returns_true_on_gap(store):
    """maybe_new_session returns True when enough time has passed since the last session."""
    import time

    from mammamiradio.persona import SESSION_GAP_SECONDS

    # First call with no prior activity: should return True
    result = store.maybe_new_session()
    assert result is True

    # Simulate a gap by rolling back the timestamp
    store._last_listener_at = time.time() - SESSION_GAP_SECONDS - 1
    result = store.maybe_new_session()
    assert result is True


@pytest.mark.asyncio
async def test_increment_session_persists_to_db(store):
    """increment_session bumps session_count in the database.

    update_persona creates the row; increment_session then modifies it.
    """
    # Seed the row via update_persona (which does INSERT ... ON CONFLICT)
    await store.update_persona({})
    await store.increment_session()
    persona = await store.get_persona()
    assert persona.session_count == 1

    await store.increment_session()
    persona = await store.get_persona()
    assert persona.session_count == 2


@pytest.mark.asyncio
async def test_persona_to_prompt_context_includes_fields(store):
    await store.update_persona(
        {
            "new_theories": ["ama la musica lenta"],
            "new_jokes": ["la battuta del traffico"],
        }
    )
    persona = await store.get_persona()
    ctx = persona.to_prompt_context()
    assert "musica lenta" in ctx
    assert "battuta del traffico" in ctx


def test_listener_persona_json_size():
    p = ListenerPersona(motifs=["jazz", "bossa nova"], theories=["ascolta di notte"])
    size = p.json_size()
    assert size > 0
    assert isinstance(size, int)
