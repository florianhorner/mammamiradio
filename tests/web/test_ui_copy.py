"""Tests for the listener UI copy lookup."""

from __future__ import annotations

import re
from pathlib import Path

from mammamiradio.web.ui_copy import COPY, copy_strings, get_copy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_HTML = _REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_LISTENER_HTML = _REPO_ROOT / "mammamiradio" / "web" / "templates" / "listener.html"
_LISTENER_JS = _REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.js"


def test_key_parity_between_languages():
    """Every key in en must exist in it and vice versa — prevents drift."""
    en_keys = set(COPY["en"].keys())
    it_keys = set(COPY["it"].keys())
    assert en_keys == it_keys, f"missing in it: {en_keys - it_keys}; missing in en: {it_keys - en_keys}"


def test_every_listener_copy_reference_exists_in_both_modes():
    """Parity alone is false-green when a key is absent from both dictionaries."""
    js = _LISTENER_JS.read_text(encoding="utf-8")
    html = _LISTENER_HTML.read_text(encoding="utf-8")
    referenced = set(re.findall(r"_t\(\s*['\"]([^'\"]+)", js))
    referenced.update(re.findall(r"copy\.get\(\s*['\"]([^'\"]+)", html))

    for lang in ("en", "it"):
        missing = referenced - set(COPY[lang])
        assert not missing, f"listener copy references missing from {lang}: {sorted(missing)}"


def test_default_off_returns_english():
    assert get_copy(False, "listen_now") == "Listen Now"
    assert get_copy(False, "listen_pause_aria") == "Pause station"
    assert get_copy(False, "stat_tracks") == "Tracks in Rotation"
    assert get_copy(False, "form_message_placeholder").startswith("Dear Radio")
    assert get_copy(False, "form_message_required").startswith("Write a message")
    assert get_copy(False, "form_success_shoutout").startswith("Dedication received")
    assert "{s}" in get_copy(False, "form_rate_limited")
    assert get_copy(False, "form_network_error").startswith("We lost the connection")


def test_super_italian_on_returns_italian():
    assert get_copy(True, "listen_now") == "Ascolta Ora"
    assert get_copy(True, "listen_pause_aria") == "Metti in pausa la radio"
    assert get_copy(True, "stat_tracks") == "Tracce in playlist"
    assert get_copy(True, "form_message_placeholder").startswith("Cara Radio")
    assert get_copy(True, "form_message_required").startswith("Scrivi prima")
    assert get_copy(True, "form_success_shoutout").startswith("Dedica ricevuta")
    assert "{s}" in get_copy(True, "form_rate_limited")
    assert get_copy(True, "form_network_error").startswith("Abbiamo perso la connessione")


def test_request_outcome_copy_is_complete_in_both_modes():
    outcome_keys = (
        "form_success_song",
        "form_success_shoutout",
        "form_rate_limited",
        "form_queue_full",
        "form_declined",
        "form_network_error",
    )
    for lang in ("en", "it"):
        for key in outcome_keys:
            assert COPY[lang].get(key), f"missing request outcome {key} in {lang}"
        assert "{s}" in COPY[lang]["form_rate_limited"]

    text = _LISTENER_JS.read_text(encoding="utf-8")
    for key in outcome_keys:
        assert re.search(rf"_t\(\s*'{key}'", text), f"listener request flow bypasses localized {key} copy"

    hardcoded_italian_receipts = (
        "Saluto ricevuto",
        "Canzone in arrivo",
        "Coda piena",
        "Invio non riuscito",
    )
    assert not any(receipt in text for receipt in hardcoded_italian_receipts)


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


def test_listener_moment_receipt_copy_is_localized():
    for lang in ("en", "it"):
        assert COPY[lang].get("casa_moment_airing"), f"missing casa_moment_airing in {lang}"
        assert "{m}" in COPY[lang].get("casa_moment_minutes_ago", "")
    text = _LISTENER_JS.read_text(encoding="utf-8")
    assert "_t('casa_moment_airing'" in text
    assert "_t('casa_moment_minutes_ago'" in text
    assert "in onda ora" not in text


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

    # Trigger routes return deliberately human, actionable copy (for example,
    # how to resume a paused station). That server copy may reach a toast only
    # through the established r&&r.error path with a wayOut() fallback; every
    # other raw error field remains forbidden.
    for line in text.splitlines():
        if not re.search(r"\br\.error\b", line):
            continue
        assert "r&&r.error" in line and "||wayOut(" in line, (
            "server error copy must use the guarded r&&r.error form and retain "
            "a local wayOut() fallback so an unexpected response never becomes "
            f"a dead end: {line.strip()}"
        )

    # Pattern-based backstop so unanticipated variants (double quotes, new
    # wrappers, raw fields) cannot slip past the exact-string list above.
    patterns = (
        # A toast literal that opens with a machine phrase.
        r"toast\(\s*['\"](?:Error:|Failed |Network error)",
        # A toast that interpolates a raw backend error field. [^;] (no \n
        # exclusion) + DOTALL so a multiline toast() can't slip the field past.
        r"toast\([^;]*\b(?:r\.exception|error_code|r\.detail|resp\.error)\b",
    )
    pattern_hits = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.DOTALL):
            hit = match.group(0)
            pattern_hits.append(hit)
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
