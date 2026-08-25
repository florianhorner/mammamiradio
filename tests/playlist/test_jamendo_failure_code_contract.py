"""Every Jamendo failure code must survive sanitizing and reach human copy.

Three lists have to agree and nothing used to keep them in sync:

1. the codes ``jamendo_transient`` can raise,
2. the sanitizer allowlist in ``web/media_sources`` (anything unlisted was
   silently rewritten to ``api_failed``), and
3. the operator sentence table in ``admin.html``.

The drift was not theoretical. ``metadata_changed``, ``identity_mismatch`` and
``empty_results`` — the three codes the edge install reports most often — were
missing from lists 2 and 3, so the card showed a generic reason for the only
failures that actually happen. Worse, ``api_auth_failed`` (a wrong client ID,
one of the few problems an operator can actually fix) was laundered into
"Jamendo didn't respond — retrying automatically", which is wrong advice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mammamiradio.playlist import jamendo_transient as jt
from mammamiradio.web import media_sources

_ADMIN_TEMPLATE = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"
# Codes reach _last_failure_code by three shapes, not one. Scanning only the
# exception constructors left `_record_transient_failure_unlocked("internal_error")`
# invisible, so that code could be dropped from the SSOT with every test green
# while the sanitizer laundered it into "api_failed".
_RAISE_RE = re.compile(
    r"(?:_(?:TransientError|BlockedError|CandidateRejectedError)\(\s*|"
    r"_record_transient_failure_unlocked\(\s*|"
    r"_last_failure_code = )\"([a-z_]+)\""
)
_HINT_BLOCK_RE = re.compile(r"function jamendoFailureHint\(code\)\{(.*?)\n\}", re.DOTALL)
# Match the closing quote that opened the string, so an apostrophe inside a
# double-quoted sentence ("Jamendo isn't accepting...") does not truncate it.
# ``*`` not ``+`` on the body: an empty sentence must be captured and then fail
# the assertions, not slip past the regex unseen.
_HINT_ENTRY_RE = re.compile(r"(?P<code>[a-z_]+)\s*:\s*(?P<q>[\"'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)")


def _sentences(body: str) -> list[str]:
    return [match.group("text") for match in _HINT_ENTRY_RE.finditer(body)]


def _hint_table() -> dict[str, str]:
    """Map each code to ITS OWN sentence.

    Mapping every key to the whole function body made the coverage test pass on
    an emptied entry (``api_auth_failed:''``), because presence was all it
    checked. The operator would then have got no reason line at all.
    """
    block = _HINT_BLOCK_RE.search(_ADMIN_TEMPLATE.read_text(encoding="utf-8"))
    assert block is not None, "jamendoFailureHint not found — did the signature change?"
    return {match.group("code"): match.group("text") for match in _HINT_ENTRY_RE.finditer(block.group(1))}


def test_declared_code_set_covers_every_raise_site() -> None:
    """The SSOT must not fall behind the code that raises."""
    source = (Path(jt.__file__)).read_text(encoding="utf-8")
    raised = set(_RAISE_RE.findall(source))
    missing = raised - jt.JAMENDO_FAILURE_CODES
    assert not missing, f"raised but absent from JAMENDO_FAILURE_CODES: {sorted(missing)}"


def test_sanitizer_passes_every_declared_code_through_unchanged() -> None:
    """A code the sanitizer does not know is rewritten to api_failed.

    That rewrite is correct for genuinely unknown input and catastrophic for a
    real code: it replaces a specific, actionable reason with a generic one.
    """
    for code in sorted(jt.JAMENDO_FAILURE_CODES):
        assert code in media_sources._JAMENDO_FAILURE_CODES, (
            f"{code!r} would be laundered into 'api_failed' before reaching the operator"
        )


def test_every_declared_code_has_operator_copy() -> None:
    """No code may reach the card with an empty reason line."""
    table = _hint_table()
    missing = sorted(jt.JAMENDO_FAILURE_CODES - set(table))
    assert not missing, f"no operator sentence for: {missing}"
    blank = sorted(code for code in jt.JAMENDO_FAILURE_CODES if not table[code].strip())
    assert not blank, f"present but empty, so the card shows no reason: {blank}"


def test_operator_copy_always_states_whether_to_act() -> None:
    """Leadership principle #5: never name a problem without a way forward.

    Every sentence either says the station is handling it, says explicitly that
    nothing is needed, or names a concrete step the operator can take.
    """
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    block = _HINT_BLOCK_RE.search(body)
    assert block is not None
    sentences = _sentences(block.group(1))
    assert sentences, "no sentences parsed out of jamendoFailureHint"
    for sentence in sentences:
        assert "no action needed" in sentence or "retrying automatically" in sentence or " — check " in sentence, (
            f"names a problem without a way forward: {sentence!r}"
        )


def test_operator_copy_carries_no_machine_words() -> None:
    """The copy is product copy, not a log line."""
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    block = _HINT_BLOCK_RE.search(body)
    assert block is not None
    parsed = _sentences(block.group(1))
    assert parsed, "no sentences parsed out of jamendoFailureHint"
    sentences = " ".join(parsed).lower()
    for machine_word in ("http", "ffmpeg", "ffprobe", "timeout", "null", "admission", "lease", "api "):
        assert machine_word not in sentences, f"machine word reached operator copy: {machine_word!r}"


def test_every_visible_state_has_a_human_word() -> None:
    """A state with no mapping leaks the raw enum onto the chip and into aria-label."""
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    presentation = re.search(r"function jamendoPresentation\(status\)\{(.*?)\n\}", body, re.DOTALL)
    assert presentation is not None
    visible = set(re.findall(r"state:'([a-z_]+)'", presentation.group(1)))
    words = re.search(r"const MEDIA_SOURCE_STATE_WORDS=\{(.*?)\n\};", body, re.DOTALL)
    assert words is not None
    mapped = set(re.findall(r"^\s*([a-z_]+)\s*:", words.group(1), re.MULTILINE))
    assert visible <= mapped, f"raw enum would reach the chip for: {sorted(visible - mapped)}"


def test_jamendo_chip_renders_the_human_word() -> None:
    """Jamendo must reuse the shared chip without leaking raw machine state."""
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    chip = re.search(r"function jamendoStatusChip\(presentation\)\{(.*?)\n\}", body, re.DOTALL)
    assert chip is not None
    assert "return mediaSourceStatusChip(presentation.state)" in chip.group(1)
    assert "createTextNode(presentation.state)" not in chip.group(1)
    assert "'status: '+presentation.state" not in chip.group(1)


def test_blocked_row_never_promises_a_retry() -> None:
    """Blocked schedules no retry, so no blocked sentence may claim one.

    Jamendo's non-retryable contract rejections surface as ``api_failed``, whose
    normal copy says "retrying automatically" — on the blocked row that tells the
    operator to wait for something that will never happen.

    Grepping the branch for the phrases it screens proves nothing: inverting the
    comparison makes the branch pass through exactly the wrong sentences and the
    grep still matches. This asserts the property instead, by running the real
    branch condition over every sentence a blocked row can carry.
    """
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    condition = re.search(r"^\s*if\(!blockedReason\|\|(.+)\)\{\s*$", body, re.MULTILINE)
    assert condition is not None, "blocked screen not found — did the branch change?"
    screened = set(re.findall(r"indexOf\('([^']+)'\)!==-1", condition.group(1)))
    assert screened, "the blocked branch screens nothing"
    for code, sentence in _hint_table().items():
        would_render = not any(phrase in sentence for phrase in screened)
        if not would_render:
            continue
        assert "retrying automatically" not in sentence, (
            f"blocked row would promise a retry it never schedules for {code!r}"
        )
        assert "no action needed" not in sentence, (
            f"blocked row would tell the operator to do nothing about {code!r}, "
            "on a state that does not recover on its own"
        )


def test_operator_copy_is_deployment_neutral() -> None:
    """The provider runs standalone too, so copy must not assume the add-on.

    Telling a standalone operator to check "the add-on's storage" names a place
    that does not exist on their install.
    """
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    # Both operator-facing Jamendo copy tables, not just the failure hints. The
    # error table sat 15 lines above the sentence this rule was written for and
    # broke it, because the test only looked at jamendoFailureHint.
    errors = re.search(r"const JAMENDO_ERROR_COPY=\{(.*?)\n\};", body, re.DOTALL)
    assert errors is not None, "JAMENDO_ERROR_COPY not found"
    surfaces = dict(_hint_table())
    surfaces.update({m.group("code"): m.group("text") for m in _HINT_ENTRY_RE.finditer(errors.group(1))})
    for code, sentence in surfaces.items():
        lowered = sentence.lower()
        for addon_word in ("add-on", "addon", "supervisor", "home assistant"):
            assert addon_word not in lowered, f"{code!r} copy assumes a deployment: {sentence!r}"


def test_announcement_dedup_key_includes_the_reason() -> None:
    """State and label are constant per branch, so the reason must be in the key.

    Without it the live region announces once and then stays silent while the
    provider fails, and a degraded row keeps speaking a stale reason.
    """
    body = _ADMIN_TEMPLATE.read_text(encoding="utf-8")
    key_line = re.search(r"const announcementKey=([^;]+);", body)
    assert key_line is not None
    assert "presentation.announcement" in key_line.group(1)


@pytest.mark.parametrize(
    ("rejections", "expected"),
    [
        ({}, None),
        ({"identity_mismatch": 3}, "identity_mismatch"),
        ({"identity_mismatch": 1, "empty_results": 5}, "empty_results"),
        # Ties resolve by name so the card does not flip between two equally
        # common reasons on consecutive polls.
        ({"empty_results": 2, "identity_mismatch": 2}, "empty_results"),
    ],
)
def test_dominant_code_selection(rejections: dict[str, int], expected: str | None) -> None:
    assert jt._dominant_code(rejections) == expected
