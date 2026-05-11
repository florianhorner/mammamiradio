"""Tests for the listener UI copy lookup."""

from __future__ import annotations

from mammamiradio.web.ui_copy import COPY, copy_strings, get_copy


def test_key_parity_between_languages():
    """Every key in en must exist in it and vice versa — prevents drift."""
    en_keys = set(COPY["en"].keys())
    it_keys = set(COPY["it"].keys())
    assert en_keys == it_keys, f"missing in it: {en_keys - it_keys}; missing in en: {it_keys - en_keys}"


def test_default_off_returns_english():
    assert get_copy(False, "listen_now") == "Listen Now"
    assert get_copy(False, "stat_tracks") == "Tracks in Rotation"
    assert get_copy(False, "form_message_placeholder").startswith("Dear Radio")


def test_super_italian_on_returns_italian():
    assert get_copy(True, "listen_now") == "Ascolta Ora"
    assert get_copy(True, "stat_tracks") == "Tracce in playlist"
    assert get_copy(True, "form_message_placeholder").startswith("Cara Radio")


def test_missing_key_returns_default():
    assert get_copy(False, "no_such_key") == ""
    assert get_copy(False, "no_such_key", "fallback") == "fallback"
    assert get_copy(True, "no_such_key", "fallback") == "fallback"


def test_copy_strings_returns_full_dict_for_mode():
    en = copy_strings(False)
    it = copy_strings(True)
    assert en["listen_now"] == "Listen Now"
    assert it["listen_now"] == "Ascolta Ora"
    # Returned dict must be a copy — mutating it should not bleed into module state.
    en["listen_now"] = "mutated"
    assert COPY["en"]["listen_now"] == "Listen Now"
