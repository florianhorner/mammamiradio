"""Tests for the listener UI copy lookup."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from mammamiradio.web.ui_copy import COPY, copy_strings, get_copy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_HTML = _REPO_ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
_LISTENER_HTML = _REPO_ROOT / "mammamiradio" / "web" / "templates" / "listener.html"
_LISTENER_JS = _REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.js"

_MISSPELLED_BRAND = "Mammami Radio"
_MISSPELLED_BRAND_CASEFOLD = _MISSPELLED_BRAND.casefold()

# Matches "mammami" joined to "radio" by a space, hyphen, or underscore — every
# separator except none. Zero separator ("mammamiradio") is the correct slug
# for URLs, paths, package names, env vars, entity IDs, and container names,
# and must never be flagged.
_MISSPELLED_BRAND_PATTERN = re.compile(r"mammami[-_ ]radio", re.IGNORECASE)

# The zero-separator form is only correct as a technical identifier: all
# lowercase ("mammamiradio") or shouting case in an env var/entity id
# ("MAMMAMIRADIO"). Title Case ("Mammamiradio") only happens when someone
# typed the brand as one word in prose. Case-SENSITIVE on purpose — matching
# this case-insensitively would also flag every legitimate slug occurrence.
_SQUASHED_TITLECASE_PATTERN = re.compile(r"\bMammamiradio\b")


def _brand_violation(text: str) -> bool:
    return bool(_MISSPELLED_BRAND_PATTERN.search(text) or _SQUASHED_TITLECASE_PATTERN.search(text))


# Exclude this file by path because it defines the rejected spelling. Splitting
# the literal is fragile: Ruff can join adjacent string literals during
# formatting. A path-based exclusion survives formatter and lint changes.
_BRAND_GUARD_SELF = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()

# Four files retain the joined spelling until the audio pack is regenerated:
#   - assets/imaging/{manifest.json,ATTRIBUTION.md} contain the same 47
#     committed creator records.
#   - scripts/complete_audio_pack_gate.py generates those rows.
#   - tests/audio/test_sonic_asset_pack.py pins their hashes.
#   - ATTRIBUTION.md ships as package data through pyproject.toml.
# Whole-file exclusions can hide new occurrences in these paths. Regenerate the
# pack and remove the exclusions in a separate change.
_FROZEN_BRAND_PROVENANCE = frozenset(
    {
        "mammamiradio/assets/imaging/manifest.json",
        "mammamiradio/assets/imaging/ATTRIBUTION.md",
        "scripts/complete_audio_pack_gate.py",
        "tests/audio/test_sonic_asset_pack.py",
    }
)

# Keep pre-existing copy fixes out of unrelated plan and proposal PRs. Each
# entry permits one legacy line at its current line number, not a whole file, so
# any new occurrence in the same document still fails. Remove an entry when its
# docs-only correction lands.
_LEGACY_BRAND_LINES = frozenset(
    {
        (
            "docs/2026-07-27-admitted-audio-queue-refactor.md",
            12,
            "Mammami Radio will preserve its current listener, operator, HTTP, and Home",
        ),
        (
            "docs/integrations/ha-privacy-and-upstream-proposals.md",
            38,
            "Mammamiradio local behavior: the add-on pushes a compatibility media-player",
        ),
        (
            "docs/integrations/ha-privacy-and-upstream-proposals.md",
            55,
            "Mammamiradio local behavior: sensitive domains and attributes are filtered",
        ),
    }
)

# Skip known binary suffixes. This keeps extensionless text, Dockerfiles, macOS
# launchers, and SVGs in scope.
_BRAND_BINARY_SUFFIXES = frozenset(
    {
        ".mp3",
        ".wav",
        ".ogg",
        ".flac",
        ".m4a",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".pyc",
    }
)

# The repository scan covered 831 files when this guard was added. An empty or
# narrowed candidate list falls below this floor.
_MIN_BRAND_SCAN_FILES = 500

# Require representative surfaces so a moved root or narrowed scan fails.
_REQUIRED_BRAND_SCAN_FILES = (
    "mammamiradio/web/templates/admin.html",
    "mammamiradio/web/templates/listener.html",
    "mammamiradio/web/static/listener.js",
    "ha-addon/mammamiradio/config.yaml",
    "CHANGELOG.md",
    "README.md",
)


def _tracked_files(root: Path) -> list[str]:
    """Return repository-relative paths tracked by Git under `root`.

    Untracked files enter the scan after `git add`. The guard requires Git
    because a filesystem walk includes ignored paths and produces a different
    candidate set.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    listing = subprocess.run(
        ["git", "-C", str(root), "--no-optional-locks", "ls-files", "-z"],
        capture_output=True,
        check=True,
        timeout=30,
        env=env,
    )
    # Git emits raw path bytes with -z. os.fsdecode preserves platform filename
    # handling; UTF-8 replacement could change a path before resolution.
    return [os.fsdecode(entry) for entry in listing.stdout.split(b"\0") if entry]


def _scan_brand(root: Path, relpaths: Iterable[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (offenders, unreadable, scanned) for `relpaths` under `root`."""
    offenders: list[str] = []
    unreadable: list[str] = []
    scanned: list[str] = []
    for rel in relpaths:
        if rel in _FROZEN_BRAND_PROVENANCE or rel == _BRAND_GUARD_SELF:
            continue
        path = root / rel
        if path.suffix.lower() in _BRAND_BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            unreadable.append(f"{rel} ({type(exc).__name__})")
            continue
        scanned.append(rel)
        if not _brand_violation(text):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _brand_violation(line) and (rel, lineno, line) not in _LEGACY_BRAND_LINES:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    return offenders, unreadable, scanned


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


def test_listener_js_fallback_strings_match_the_english_copy():
    """A listener must read the same words whether or not the payload loaded.

    Every `_t('key', 'literal')` call carries an inline English fallback used
    when `/public-status` copy has not arrived. Nothing kept those literals in
    step with `COPY["en"]`, so a copy edit on one side silently forked the two
    surfaces — which is exactly what a hand-edit across both files invites.
    """
    js = _LISTENER_JS.read_text(encoding="utf-8")
    # Both quote styles: matching only single-quoted literals silently skipped
    # four calls, one of which had forked into a different licensing statement.
    # A fallback built from concatenation or a template literal is still skipped
    # rather than guessed at.
    pairs = [
        (match.group(2), match.group(4))
        for match in re.finditer(
            r"""_t\(\s*(['"])([^'"]+?)\1\s*,\s*(['"])((?:(?!\3)[^\\]|\\.)*)\3\s*[,)]""",
            js,
        )
    ]
    assert pairs, "no inline copy fallbacks found — the extraction pattern has drifted"
    drift = {}
    for key, literal in pairs:
        fallback = literal.replace("\\'", "'").replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        expected = COPY["en"].get(key)
        if expected is not None and fallback != expected:
            drift[key] = (fallback, expected)
    # These forked before the guard existed. Each is a listener-visible wording
    # decision, not a mechanical fix, so they are named rather than silently
    # skipped — and the assertion below fails if one is repaired without being
    # removed here, so the list can only shrink. A listener reads whichever
    # version wins the race between the page and its /public-status payload.
    known_drift = {
        "clip_copied",
        "clip_copy_prompt",
        "clip_rate_limited",
        "clip_saving",
        "credits_catalog_unavailable",
        "credits_licensed_under",
        "credits_no_current_music",
        "credits_provided_by_jamendo",
        # Deliberate, not an oversight: the inline fallback is the explicit
        # fail-closed licensing disclaimer pinned by
        # tests/web/test_media_ui.py::test_listener_credit_data_has_no_html_sink_and_links_fail_closed,
        # while the served string is the softer "rights remain their
        # responsibility". Reconciling them is a copy decision, not a rename.
        "local_credit",
        "normalized_notice",
        "np_on_air",
        "np_paused",
        "provider_reported_notice",
        "source_unavailable",
    }
    new_drift = sorted(set(drift) - known_drift)
    assert not new_drift, f"listener.js fallback copy has drifted from COPY['en']: {new_drift}"
    repaired = sorted(known_drift - set(drift))
    assert not repaired, (
        f"these keys no longer drift (repaired, or their _t call was removed) — drop them from known_drift: {repaired}"
    )


def test_default_off_returns_english():
    assert get_copy(False, "listen_now") == "Listen Now"
    assert get_copy(False, "listen_pause_aria") == "Pause station"
    assert get_copy(False, "stat_tracks") == "Tracks in Rotation"
    assert get_copy(False, "form_message_placeholder").startswith("Dear Radio")
    assert get_copy(False, "form_message_required").startswith("Write a message")
    assert get_copy(False, "form_success_song").startswith("Request received")
    assert "checking the catalogue" in get_copy(False, "form_song_searching")
    assert get_copy(False, "form_success_shoutout").startswith("Dedication received")
    assert "{s}" in get_copy(False, "form_rate_limited")
    assert get_copy(False, "form_network_error").startswith("We lost the connection")


def test_super_italian_on_returns_italian():
    assert get_copy(True, "listen_now") == "Ascolta Ora"
    assert get_copy(True, "listen_pause_aria") == "Metti in pausa la radio"
    assert get_copy(True, "stat_tracks") == "Tracce in playlist"
    assert get_copy(True, "form_message_placeholder").startswith("Cara Radio")
    assert get_copy(True, "form_message_required").startswith("Scrivi prima")
    assert get_copy(True, "form_success_song").startswith("Richiesta ricevuta")
    assert "Cerchiamo in catalogo" in get_copy(True, "form_song_searching")
    assert get_copy(True, "form_success_shoutout").startswith("Dedica ricevuta")
    assert "{s}" in get_copy(True, "form_rate_limited")
    assert get_copy(True, "form_network_error").startswith("Abbiamo perso la connessione")


def test_request_outcome_copy_is_complete_in_both_modes():
    outcome_keys = (
        "form_success_song",
        "form_success_shoutout",
        "form_song_searching",
        "form_song_matched",
        "form_song_matched_generic",
        "form_song_no_verified_match",
        "form_song_not_playable",
        "form_song_temporarily_unavailable",
        "form_song_tracking_expired",
        "form_song_tracking_lost",
        "form_rate_limited",
        "form_queue_full",
        "form_declined",
        "form_network_error",
    )
    for lang in ("en", "it"):
        for key in outcome_keys:
            assert COPY[lang].get(key), f"missing request outcome {key} in {lang}"
        assert "{s}" in COPY[lang]["form_rate_limited"]
        assert "{track}" in COPY[lang]["form_song_matched"]

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

    premature_promises = (
        "The hosts will cue it soon",
        "metteranno presto la canzone in scaletta",
        "queued behind the audio",
        "in coda dopo l’audio",
    )
    listener_copy = "\n".join((*COPY["en"].values(), *COPY["it"].values()))
    assert not any(promise in text or promise in listener_copy for promise in premature_promises)
    assert "hosts to introduce" in COPY["en"]["form_song_matched"]
    assert "conduttori" in COPY["it"]["form_song_matched"]

    infrastructure_claims = (
        "catalogue isn’t reachable",
        "catalogo musicale non è raggiungibile",
    )
    assert not any(
        claim in COPY[lang]["form_song_temporarily_unavailable"]
        for lang in ("en", "it")
        for claim in infrastructure_claims
    )
    assert "dedication" in COPY["en"]["form_song_temporarily_unavailable"]
    assert "dedica" in COPY["it"]["form_song_temporarily_unavailable"]


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
    keys = (
        "casa_moments_title",
        "casa_moments_helper",
        "casa_moment_airing",
        "casa_moment_age_unknown",
        "casa_moment_minutes_ago",
        "casa_moment_hours_ago",
        "casa_moment_yesterday",
        "casa_moment_days_ago",
        "casa_moment_stale",
    )
    for lang in ("en", "it"):
        for key in keys:
            assert COPY[lang].get(key), f"missing {key} in {lang}"
        assert "{m}" in COPY[lang].get("casa_moment_minutes_ago", "")
        assert "{h}" in COPY[lang].get("casa_moment_hours_ago", "")
        assert "{d}" in COPY[lang].get("casa_moment_days_ago", "")
    assert COPY["en"]["casa_moments_title"] == "On-air moments from your home"
    assert "not every change at home" in COPY["en"]["casa_moments_helper"]

    text = _LISTENER_JS.read_text(encoding="utf-8")
    assert "_t('casa_moment_airing'" in text
    assert "_t('casa_moment_minutes_ago'" in text
    assert "_t('casa_moment_hours_ago'" in text
    assert "_t('casa_moment_yesterday'" in text
    assert "_t('casa_moment_days_ago'" in text
    assert "in onda ora" not in text


def test_listener_moment_receipts_stay_private_and_readable():
    """The browser renders only public receipts with human time and no HTML sink."""
    text = _LISTENER_JS.read_text(encoding="utf-8")
    html = _LISTENER_HTML.read_text(encoding="utf-8")
    age_fn = text[text.index("function formatCasaMomentAge(") : text.index("function segmentKindLabel(")]
    casa_fn = text[text.index("function updateCasa(") : text.index("function renderPalinsestoDate(")]

    assert "m.status === 'airing' || m.status === 'aired'" in casa_fn
    assert "if (minutes < 60)" in age_fn
    assert "if (minutes < 24 * 60)" in age_fn
    assert "if (minutes < 48 * 60)" in age_fn
    assert "Math.floor(minutes / (24 * 60))" in age_fn
    assert "!hasAiring" in casa_fn
    assert "latestReceiptAge >= 24 * 60" in casa_fn
    assert "textContent" in casa_fn
    assert "innerHTML" not in casa_fn
    assert 'id="casa-moments-stale"' in html
    assert "casa_moments_helper" in html
    assert "casa_moment_stale" in html


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


def test_station_brand_name_is_never_misspelled():
    """Reject the joined station name in tracked text files.

    The original typo shipped in a First Listen error. Scanning docs and
    changelogs also prevents new references from repeating it.
    """
    offenders, unreadable, scanned = _scan_brand(_REPO_ROOT, _tracked_files(_REPO_ROOT))

    assert not offenders, f'use "Mamma Mi Radio"; found invalid name "{_MISSPELLED_BRAND}":\n  ' + "\n  ".join(
        offenders
    )
    assert not unreadable, (
        "the brand guard could not read these tracked files; classify expected "
        "binary suffixes in _BRAND_BINARY_SUFFIXES:\n  " + "\n  ".join(unreadable)
    )
    assert len(scanned) >= _MIN_BRAND_SCAN_FILES, (
        f"brand scan covered {len(scanned)} files; minimum is {_MIN_BRAND_SCAN_FILES}"
    )
    missing = [rel for rel in _REQUIRED_BRAND_SCAN_FILES if rel not in set(scanned)]
    assert not missing, f"brand scan missed required surfaces: {missing}"


def test_brand_guard_flags_a_synthetic_offender(tmp_path):
    """Exercise detection, case folding, separator variants, and path exclusions."""
    uppercase_misspelling = _MISSPELLED_BRAND.upper()
    (tmp_path / "page.html").write_text(f"<p>{_MISSPELLED_BRAND} is on air</p>", encoding="utf-8")
    (tmp_path / "uppercase.html").write_text(f"<p>{uppercase_misspelling} is on air</p>", encoding="utf-8")
    (tmp_path / "hyphenated.json").write_text('{"name": "mammami-radio-explainer"}', encoding="utf-8")
    (tmp_path / "underscored.py").write_text("PACKAGE = 'mammami_radio_tools'\n", encoding="utf-8")
    (tmp_path / "squashed.md").write_text("Mammamiradio local behavior: does X.\n", encoding="utf-8")
    (tmp_path / "clean.html").write_text("<p>Mamma Mi Radio is on air</p>", encoding="utf-8")
    (tmp_path / "song.mp3").write_bytes(_MISSPELLED_BRAND.encode("utf-8"))
    frozen = tmp_path / "mammamiradio" / "assets" / "imaging"
    frozen.mkdir(parents=True)
    (frozen / "ATTRIBUTION.md").write_text(_MISSPELLED_BRAND, encoding="utf-8")
    legacy_rel, legacy_lineno, legacy_line = sorted(_LEGACY_BRAND_LINES)[0]
    legacy = tmp_path / legacy_rel
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        ("\n" * (legacy_lineno - 1)) + f"{legacy_line}\n{_MISSPELLED_BRAND} appears again.\n",
        encoding="utf-8",
    )

    offenders, unreadable, scanned = _scan_brand(
        tmp_path,
        [
            "page.html",
            "uppercase.html",
            "hyphenated.json",
            "underscored.py",
            "squashed.md",
            "clean.html",
            "song.mp3",
            "mammamiradio/assets/imaging/ATTRIBUTION.md",
            legacy_rel,
        ],
    )

    assert offenders == [
        f"page.html:1: <p>{_MISSPELLED_BRAND} is on air</p>",
        f"uppercase.html:1: <p>{uppercase_misspelling} is on air</p>",
        'hyphenated.json:1: {"name": "mammami-radio-explainer"}',
        "underscored.py:1: PACKAGE = 'mammami_radio_tools'",
        "squashed.md:1: Mammamiradio local behavior: does X.",
        f"{legacy_rel}:{legacy_lineno + 1}: {_MISSPELLED_BRAND} appears again.",
    ]
    assert not unreadable
    assert set(scanned) == {
        "page.html",
        "uppercase.html",
        "hyphenated.json",
        "underscored.py",
        "squashed.md",
        "clean.html",
        legacy_rel,
    }


def test_brand_guard_ignores_the_correct_joined_slug(tmp_path):
    """The zero-separator slug is correct in lowercase or shouting case; only Title Case is a violation."""
    (tmp_path / "urls.py").write_text(
        "ENTITY_ID = 'media_player.mammamiradio'\nCONTAINER = 'mammamiradio'\nENV_VAR = 'MAMMAMIRADIO_BIND_HOST'\n",
        encoding="utf-8",
    )

    offenders, unreadable, scanned = _scan_brand(tmp_path, ["urls.py"])

    assert not offenders
    assert not unreadable
    assert scanned == ["urls.py"]


def test_brand_guard_reports_undecodable_files_as_unreadable(tmp_path):
    """A non-UTF-8 text file must surface in `unreadable`, not vanish silently."""
    (tmp_path / "latin1.txt").write_bytes("café".encode("latin-1"))

    offenders, unreadable, scanned = _scan_brand(tmp_path, ["latin1.txt"])

    assert not offenders
    assert unreadable == ["latin1.txt (UnicodeDecodeError)"]
    assert not scanned


def test_frozen_brand_provenance_allowlist_has_no_dead_entries():
    """Limit the allowlist to files that still contain the joined spelling."""
    stale = []
    for rel in sorted(_FROZEN_BRAND_PROVENANCE):
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale.append(f"{rel} (missing)")
        elif _MISSPELLED_BRAND not in path.read_text(encoding="utf-8"):
            stale.append(f"{rel} (already correct)")
    assert not stale, "remove stale paths from _FROZEN_BRAND_PROVENANCE:\n  " + "\n  ".join(stale)


def test_legacy_brand_line_allowlist_has_no_dead_entries():
    """Require each scoped legacy allowance to match one exact tracked line."""
    stale = []
    for rel, allowed_lineno, allowed_line in sorted(_LEGACY_BRAND_LINES):
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale.append(f"{rel} (missing)")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        actual_line = lines[allowed_lineno - 1] if allowed_lineno <= len(lines) else None
        if actual_line != allowed_line or not _brand_violation(allowed_line):
            stale.append(f"{rel}:{allowed_lineno} (legacy line moved or changed)")
    assert not stale, "remove or update stale entries in _LEGACY_BRAND_LINES:\n  " + "\n  ".join(stale)


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
