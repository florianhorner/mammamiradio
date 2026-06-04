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


def test_clip_copy_keys_present():
    """The clip-sharing copy must exist in both languages (leadership principle #5)."""
    for lang in ("en", "it"):
        for key in ("clip_saving", "clip_copied", "clip_rate_limited", "clip_no_audio", "clip_error"):
            assert COPY[lang].get(key), f"missing {key} in {lang}"
    # The rate-limit string must carry the {s} seconds placeholder the JS fills in.
    assert "{s}" in COPY["en"]["clip_rate_limited"]
    assert "{s}" in COPY["it"]["clip_rate_limited"]


def test_no_tech_lingo_reaches_the_listener():
    """Leadership principle #5: no machine words in listener-facing copy.

    Guards every swappable string in both languages against the dev-lingo that
    has leaked to the UI before ("rate limit", "buffer", HTTP codes, etc.).
    """
    banned = (
        "rate limit",
        "429",
        "503",
        "500",
        "buffer",
        "timeout",
        "rejected",
        "degraded",
        "null",
        "undefined",
        "traceback",
        "exception",
    )
    for lang in ("en", "it"):
        for key, value in COPY[lang].items():
            low = value.lower()
            for term in banned:
                assert term not in low, f"tech lingo '{term}' in COPY[{lang}][{key}]: {value!r}"
