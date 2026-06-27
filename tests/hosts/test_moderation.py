"""Tests for deterministic listener-request moderation."""

from mammamiradio.hosts.moderation import is_blocked


def test_is_blocked_matches_case_insensitive_name():
    assert is_blocked("Una dedica per MELONI", ["meloni"]) is True


def test_is_blocked_matches_accent_folded_name():
    assert is_blocked("Una dedica per Melóni", ["Meloni"]) is True


def test_is_blocked_uses_word_boundaries():
    assert is_blocked("Una dedica al melone sul tavolo", ["Meloni"]) is False


def test_is_blocked_matches_multi_word_phrase_only_as_phrase():
    blocked = ["Giorgia Meloni"]

    assert is_blocked("Questa e per Giorgia Meloni", blocked) is True
    assert is_blocked("Questa e per Giorgia", blocked) is False
    assert is_blocked("Questa e per Meloni", blocked) is False


def test_is_blocked_ignores_empty_and_blank_entries():
    assert is_blocked("Una dedica normale", []) is False
    assert is_blocked("Una dedica normale", ["", "   "]) is False


def test_is_blocked_empty_text_never_matches():
    assert is_blocked("", ["Meloni"]) is False
    assert is_blocked("   ", ["Meloni"]) is False
