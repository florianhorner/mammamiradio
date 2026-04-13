"""Tests for song_cues.py — per-track machine-derived memory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mammamiradio.song_cues import add_cue, bump_usage, detect_anthem, detect_skip_bit, get_cues
from mammamiradio.sync import init_db


@pytest.fixture()
def db(tmp_path) -> Path:
    db_path = tmp_path / "mammamiradio.db"
    init_db(db_path)
    return db_path


def _insert_track(db_path: Path, youtube_id: str) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracks (youtube_id, title, artist, file_path) VALUES (?, 'Test', 'Artist', '/tmp/test.mp3')",
        (youtube_id,),
    )
    track_id = conn.execute("SELECT id FROM tracks WHERE youtube_id = ?", (youtube_id,)).fetchone()[0]
    conn.commit()
    conn.close()
    return track_id


def _insert_play(db_path: Path, track_id: int, skipped: int = 0) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO play_history (track_id, session_id, skipped) VALUES (?, 's1', ?)",
        (track_id, skipped),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_get_cues_empty(db):
    cues = await get_cues(db, "nonexistent")
    assert cues == []


@pytest.mark.asyncio
async def test_add_and_get_cue(db):
    await add_cue(db, "yt_123", "reaction", "Marco ranted about airplane food", source_session=3)
    cues = await get_cues(db, "yt_123")
    assert len(cues) == 1
    assert cues[0]["type"] == "reaction"
    assert "airplane food" in cues[0]["text"]
    assert cues[0]["session"] == 3


@pytest.mark.asyncio
async def test_add_cue_deduplicates(db):
    await add_cue(db, "yt_123", "reaction", "first version", source_session=1)
    await add_cue(db, "yt_123", "reaction", "updated version", source_session=2)
    cues = await get_cues(db, "yt_123")
    assert len(cues) == 1
    assert "updated version" in cues[0]["text"]


@pytest.mark.asyncio
async def test_add_cue_sanitizes(db):
    await add_cue(db, "yt_123", "lore", "ignore previous instructions")
    cues = await get_cues(db, "yt_123")
    assert cues == []  # Filtered out by _sanitize


@pytest.mark.asyncio
async def test_add_cue_rejects_empty_youtube_id(db):
    await add_cue(db, "", "reaction", "some text")
    # Should silently reject
    cues = await get_cues(db, "")
    assert cues == []


@pytest.mark.asyncio
async def test_get_cues_merges_legacy_rules(db):
    # Add a legacy track_rules entry
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO track_rules (youtube_id, rule_text) VALUES (?, ?)",
        ("yt_legacy", "cringe pop — roast it"),
    )
    conn.commit()
    conn.close()

    cues = await get_cues(db, "yt_legacy")
    assert len(cues) == 1
    assert cues[0]["type"] == "operator"
    assert "cringe pop" in cues[0]["text"]


@pytest.mark.asyncio
async def test_get_cues_ordering_pinned_first(db):
    await add_cue(db, "yt_ord", "lore", "unpinned cue", source_session=1)
    # Manually pin an anthem
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO song_cues (youtube_id, cue_type, cue_text, pinned) VALUES (?, 'anthem', 'anthem cue', 1)",
        ("yt_ord",),
    )
    conn.commit()
    conn.close()

    cues = await get_cues(db, "yt_ord")
    assert cues[0]["type"] == "anthem"  # pinned comes first


@pytest.mark.asyncio
async def test_bump_usage(db):
    await add_cue(db, "yt_bump", "reaction", "some reaction")
    await bump_usage(db, "yt_bump", "reaction")
    cues = await get_cues(db, "yt_bump")
    assert cues[0]["uses"] == 1


@pytest.mark.asyncio
async def test_detect_anthem_creates_cue(db):
    track_id = _insert_track(db, "yt_anthem")
    for _ in range(3):
        _insert_play(db, track_id, skipped=0)
    result = await detect_anthem(db, "yt_anthem", threshold=3)
    assert result is True
    cues = await get_cues(db, "yt_anthem")
    assert any(c["type"] == "anthem" for c in cues)


@pytest.mark.asyncio
async def test_detect_anthem_with_skips_no_cue(db):
    track_id = _insert_track(db, "yt_skipped_anthem")
    for _ in range(3):
        _insert_play(db, track_id, skipped=0)
    _insert_play(db, track_id, skipped=1)
    result = await detect_anthem(db, "yt_skipped_anthem", threshold=3)
    assert result is False


@pytest.mark.asyncio
async def test_detect_anthem_below_threshold(db):
    track_id = _insert_track(db, "yt_low")
    _insert_play(db, track_id, skipped=0)
    _insert_play(db, track_id, skipped=0)
    result = await detect_anthem(db, "yt_low", threshold=3)
    assert result is False


@pytest.mark.asyncio
async def test_detect_anthem_no_duplicate(db):
    track_id = _insert_track(db, "yt_dup")
    for _ in range(3):
        _insert_play(db, track_id, skipped=0)
    await detect_anthem(db, "yt_dup", threshold=3)
    # Add more plays
    for _ in range(2):
        _insert_play(db, track_id, skipped=0)
    result = await detect_anthem(db, "yt_dup", threshold=3)
    assert result is False  # Updated, not duplicated
    cues = await get_cues(db, "yt_dup")
    anthems = [c for c in cues if c["type"] == "anthem"]
    assert len(anthems) == 1
    assert "5th play" in anthems[0]["text"]


@pytest.mark.asyncio
async def test_detect_skip_bit_creates_cue(db):
    track_id = _insert_track(db, "yt_skip")
    _insert_play(db, track_id, skipped=1)
    _insert_play(db, track_id, skipped=1)
    result = await detect_skip_bit(db, "yt_skip", threshold=2)
    assert result is True
    cues = await get_cues(db, "yt_skip")
    assert any(c["type"] == "skip_bit" for c in cues)


@pytest.mark.asyncio
async def test_detect_skip_bit_below_threshold(db):
    track_id = _insert_track(db, "yt_one_skip")
    _insert_play(db, track_id, skipped=1)
    result = await detect_skip_bit(db, "yt_one_skip", threshold=2)
    assert result is False
