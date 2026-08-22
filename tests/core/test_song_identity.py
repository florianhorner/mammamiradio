"""Regression tests for shared song-identity policy matching."""

from mammamiradio.core import song_identity


def test_blocklist_check_normalizes_candidate_once(monkeypatch):
    candidate = ("Artist", "Song")
    blocked_keys = (("Other Artist", "Other Song"), ("ARTIST", "SONG"))
    calls = []

    def normalize(key):
        calls.append(key)
        return tuple(value.casefold() for value in key)

    monkeypatch.setattr(song_identity, "normalize_song_identity_key", normalize)

    assert song_identity.song_identity_key_is_blocklisted(candidate, blocked_keys)
    assert calls == [candidate, *blocked_keys]


def test_blocklist_check_empty_blocklist_short_circuits(monkeypatch):
    calls = []

    def normalize(key):
        calls.append(key)
        return key

    monkeypatch.setattr(song_identity, "normalize_song_identity_key", normalize)

    assert not song_identity.song_identity_key_is_blocklisted(("Artist", "Song"), ())
    assert calls == []


def test_blocklist_check_returns_false_without_equivalent_key():
    assert not song_identity.song_identity_key_is_blocklisted(
        ("Artist", "Song"),
        (("Other Artist", "Other Song"),),
    )
