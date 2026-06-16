"""Tests for the listener UI copy lookup."""

from __future__ import annotations

import re
from pathlib import Path

from mammamiradio.web.ui_copy import COPY, copy_strings, get_copy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_HTML = _REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_LISTENER_JS = _REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.js"


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


def test_admin_toasts_have_no_raw_error_dead_ends():
    """Leadership principle #5 (admin register): a failed action shows warm copy
    with a way-out, never a raw error code / exception / 'unknown' / bare 'failed'.

    The swappable COPY dict above is guarded by test_no_tech_lingo_*, but admin
    toasts are inline JS strings outside that dict. Failures must route through
    the wayOut()/offlineMsg() helpers; this guard fails if a raw-error or
    dead-end toast pattern is reintroduced.
    """
    text = _ADMIN_HTML.read_text(encoding="utf-8")
    forbidden = (
        "r.error||'unknown'",
        "r.error || 'unknown'",
        "(r&&r.error)||'unknown'",
        "toast('Network error')",
        "toast('Move failed')",
        "'Saving keys failed'",
        "'Reload failed:",
        "'Remove failed:",
        "'Pacing not saved:",
        "'queue failed'",
        "'Error: connection failed'",
    )
    hits = [frag for frag in forbidden if frag in text]
    assert not hits, (
        "admin.html reintroduced raw-error / dead-end toast copy — route failures "
        "through wayOut()/offlineMsg() (warm + a concrete way-out, principle #5):\n  " + "\n  ".join(hits)
    )

    # Pattern-based backstop so unanticipated variants (double quotes, new
    # wrappers, raw fields) cannot slip past the exact-string list above.
    patterns = (
        # A toast literal that opens with a machine phrase.
        r"toast\(\s*['\"](?:Error:|Failed |Network error)",
        # A toast that interpolates a raw backend error field. [^;] (no \n
        # exclusion) + DOTALL so a multiline toast() can't slip the field past.
        r"toast\([^;]*\b(?:r\.error|r\.exception|error_code|r\.detail|resp\.error)\b",
    )
    pattern_hits = [m.group(0) for p in patterns for m in re.finditer(p, text, re.DOTALL)]
    assert not pattern_hits, (
        "admin.html has a toast() that shows a machine phrase or a raw error "
        "field — use wayOut()/offlineMsg() instead (principle #5):\n  " + "\n  ".join(pattern_hits)
    )


def test_listener_never_shows_raw_server_error():
    """Leadership principles #1 + #5: the public listener never sees a raw server
    error. The dedication/clip paths must render house copy with a way-out, not a
    backend error string.
    """
    text = _LISTENER_JS.read_text(encoding="utf-8")
    assert "d.error" not in text, (
        "listener.js renders a raw server error (d.error) to a listener — show "
        "warm copy with a way-out instead (breaks the illusion + dev-lingo)."
    )
    assert not re.search(r"['\"]Errore clip['\"]", text), (
        "listener.js clip_error fallback is the dead-end 'Errore clip' again — "
        "use way-out copy that tells the listener what to do next."
    )
