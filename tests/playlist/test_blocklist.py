"""Unit tests for the persistent operator song blocklist.

Covers the three mandatory audio-delivery scenarios at the data layer:
  * Normal      — a banned (artist, title) is filtered out of a pool.
  * Empty/edge  — missing / corrupt / non-dict blocklist.json yields {} (never raises).
  * Post-restart — save -> cold load -> filter still drops the banned song (the
                   reported "deleted songs come back after restart" bug).
Plus key normalization and cross-source identity (same song, different source id).
"""

from __future__ import annotations

import json

from mammamiradio.core.models import Track
from mammamiradio.playlist.blocklist import (
    block_meta,
    blocklist_path,
    load_blocklist,
    save_blocklist,
)
from mammamiradio.playlist.playlist import filter_blocklisted, normalized_track_key


def _track(title: str, artist: str, spotify_id: str = "") -> Track:
    return Track(title=title, artist=artist, duration_ms=180_000, spotify_id=spotify_id)


def test_normalized_key_strips_and_lowercases():
    a = _track("  Volare  ", "  Domenico Modugno ")
    b = _track("volare", "domenico modugno")
    assert normalized_track_key(a) == normalized_track_key(b) == ("domenico modugno", "volare")


def test_filter_blocklisted_drops_banned_keeps_rest():
    pool = [_track("Volare", "Modugno"), _track("Felicità", "Al Bano"), _track("Sarà perché ti amo", "Ricchi")]
    blocklist = {("modugno", "volare"): block_meta("Modugno - Volare")}
    out = filter_blocklisted(pool, blocklist)
    titles = [t.title for t in out]
    assert "Volare" not in titles
    assert "Felicità" in titles and "Sarà perché ti amo" in titles


def test_filter_blocklisted_empty_is_passthrough():
    pool = [_track("Volare", "Modugno")]
    assert filter_blocklisted(pool, {}) == pool
    assert filter_blocklisted(pool, None) == pool


def test_save_then_load_roundtrip(tmp_path):
    blocklist = {
        ("modugno", "volare"): block_meta("Modugno - Volare", banned_by="operator", banned_at=123.0),
        ("al bano", "felicità"): block_meta("Al Bano - Felicità", banned_at=456.0),
    }
    assert save_blocklist(tmp_path, blocklist) is True
    loaded = load_blocklist(tmp_path)
    assert set(loaded) == set(blocklist)
    assert loaded[("modugno", "volare")]["display"] == "Modugno - Volare"
    assert loaded[("al bano", "felicità")]["banned_at"] == 456.0


def test_ban_survives_restart_regression(tmp_path):
    """The headline bug: a banned song must not return on the cold re-fetch."""
    # Operator bans Volare; it's persisted.
    save_blocklist(tmp_path, {("modugno", "volare"): block_meta("Modugno - Volare")})
    # "Restart": the pool is re-fetched fresh from the source and the blocklist is
    # reloaded cold from disk, then filtered before reaching the producer.
    fresh_pool = [_track("Volare", "Modugno", spotify_id="NEW_ID"), _track("Felicità", "Al Bano")]
    blocklist = load_blocklist(tmp_path)
    survivors = filter_blocklisted(fresh_pool, blocklist)
    assert [t.title for t in survivors] == ["Felicità"]


def test_cross_source_same_song_different_id_still_filtered(tmp_path):
    """Ban via charts (one id) -> re-fetch from Jamendo (different id) -> still gone."""
    save_blocklist(tmp_path, {("modugno", "volare"): block_meta()})
    blocklist = load_blocklist(tmp_path)
    jamendo_copy = Track(
        title="Volare", artist="Modugno", duration_ms=180_000, spotify_id="JAMENDO_999", source="jamendo"
    )
    assert filter_blocklisted([jamendo_copy], blocklist) == []


def test_missing_file_returns_empty(tmp_path):
    assert load_blocklist(tmp_path) == {}


def test_corrupt_file_tolerated(tmp_path):
    blocklist_path(tmp_path).write_text("{not valid json", encoding="utf-8")
    # A parse failure must NOT raise and must NOT silently un-ban everything by
    # crashing the caller — it degrades to an empty blocklist.
    assert load_blocklist(tmp_path) == {}


def test_non_dict_json_tolerated(tmp_path):
    blocklist_path(tmp_path).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_blocklist(tmp_path) == {}


def test_save_is_atomic_no_partial_file(tmp_path):
    save_blocklist(tmp_path, {("a", "b"): block_meta("A - B")})
    # No leftover temp files from the tmp+os.replace publish.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".blocklist-")]
    assert leftovers == []
