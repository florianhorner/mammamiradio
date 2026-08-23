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

import pytest

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


def test_filter_blocklisted_drops_exact_equivalent_spelling():
    """A source refresh cannot reintroduce a ban via punctuation or compact spacing."""
    refreshed = _track("LItaliano", "TotoCutugno", spotify_id="fresh-source-id")
    blocklist = {("toto cutugno", "l'italiano"): block_meta("Toto Cutugno - L'Italiano")}

    assert filter_blocklisted([refreshed], blocklist) == []


@pytest.mark.parametrize("title", ["LItaliano", "L_Italiano", "L/Italiano"])
def test_filter_blocklisted_joins_intra_word_identity_punctuation(title):
    refreshed = _track(title, "TotoCutugno", spotify_id="fresh-punctuation-id")
    blocklist = {("toto cutugno", "l'italiano"): block_meta("Toto Cutugno - L'Italiano")}

    assert filter_blocklisted([refreshed], blocklist) == []


@pytest.mark.parametrize(
    "artist",
    [
        "LucioBattistiVEVO",
        "Lucio Battisti VEVO",
        "Lucio Battisti - Topic",
        "Lucio Battisti Official Channel",
        "Lucio Battisti Official",
    ],
)
def test_filter_blocklisted_drops_explicit_platform_artist_wrappers(artist):
    refreshed = _track("Emozioni", artist, spotify_id="fresh-platform-id")
    blocklist = {("lucio battisti", "emozioni"): block_meta("Lucio Battisti - Emozioni")}

    assert filter_blocklisted([refreshed], blocklist) == []


@pytest.mark.parametrize("artist", ["AdeleVEVO", "SiaVEVO", "PinkVEVO", "NeYoVEVO", "U2VEVO", "LPVEVO"])
def test_filter_blocklisted_drops_short_artist_vevo_wrappers(artist):
    base_artist = artist.removesuffix("VEVO")
    refreshed = _track("Song", artist, spotify_id="fresh-short-platform-id")
    blocklist = {(base_artist.casefold(), "song"): block_meta(f"{base_artist} - Song")}

    assert filter_blocklisted([refreshed], blocklist) == []


def test_filter_blocklisted_cleans_platform_wrapper_inside_feature_credit():
    refreshed = _track("Shallow", "LadyGagaVEVO feat. Bradley Cooper")
    blocklist = {("lady gaga", "shallow"): block_meta("Lady Gaga - Shallow")}

    assert filter_blocklisted([refreshed], blocklist) == []


@pytest.mark.parametrize("artist", ["Devo", "Avevo", "Official Secrets Act", "The Official"])
def test_filter_blocklisted_preserves_legitimate_artist_suffixes(artist):
    refreshed = _track("Song", artist)
    blocklist = {("unrelated", "song"): block_meta("Unrelated - Song")}

    assert filter_blocklisted([refreshed], blocklist) == [refreshed]


@pytest.mark.parametrize(
    "title",
    [
        "Shallow (feat. Bradley Cooper)",
        "Shallow [ft Bradley Cooper]",
        "Shallow - feat Bradley Cooper",
        "Shallow | featuring Bradley Cooper",
        "Shallow ft. Bradley Cooper",
        "Shallow feat. Sia",
    ],
)
def test_filter_blocklisted_drops_title_side_feature_credit_layout(title):
    refreshed = _track(title, "Lady Gaga", spotify_id="fresh-feature-layout")
    blocklist = {("lady gaga", "shallow"): block_meta("Lady Gaga - Shallow")}

    assert filter_blocklisted([refreshed], blocklist) == []


def test_filter_blocklisted_does_not_treat_title_words_as_feature_credits():
    pool = [
        _track("A Feat of Strength", "Artist"),
        _track("A Feat Beyond Measure", "Artist"),
        _track("Never Ft Lauderdale", "Artist"),
        _track("Never Ft. Lauderdale", "Artist"),
        _track("The Song Featuring Tomorrow", "Artist"),
    ]
    blocklist = {
        ("artist", "a"): block_meta("Artist - A"),
        ("artist", "never"): block_meta("Artist - Never"),
        ("artist", "the song"): block_meta("Artist - The Song"),
    }

    assert filter_blocklisted(pool, blocklist) == pool


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


def test_corrupt_banned_at_does_not_raise(tmp_path):
    """A valid-JSON file with a non-numeric banned_at must NOT crash load_blocklist
    — it runs at startup, so a raise here would take the whole boot down (dead air).
    The bad value degrades to 0.0 (sorts oldest) and the ban itself still loads."""
    raw = {
        "modugno\x1fvolare": {"display": "Modugno - Volare", "banned_by": "operator", "banned_at": "whenever"},
        "al bano\x1ffelicità": {"display": "Al Bano - Felicità", "banned_at": ["not", "a", "number"]},
    }
    blocklist_path(tmp_path).write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_blocklist(tmp_path)
    assert set(loaded) == {("modugno", "volare"), ("al bano", "felicità")}
    assert loaded[("modugno", "volare")]["banned_at"] == 0.0
    assert loaded[("al bano", "felicità")]["banned_at"] == 0.0


def test_save_returns_false_on_unwritable_dir(tmp_path, monkeypatch):
    """A write failure is best-effort: save returns False (so the caller can be
    honest about durability) instead of raising into the request/audio path."""

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("mammamiradio.playlist.blocklist.tempfile.mkstemp", _boom)
    assert save_blocklist(tmp_path, {("a", "b"): block_meta("A - B")}) is False
