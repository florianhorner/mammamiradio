"""Tests for scripts/ui_copy_lint.py — Principle #5 regression guard."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.web.test_ui_copy import TECH_LINGO_LISTENER as COPY_GUARD_TECH_LINGO

ROOT = Path(__file__).resolve().parents[2]
LINT = ROOT / "scripts" / "ui_copy_lint.py"
BASELINE = ROOT / ".config" / "ui-copy-baseline.json"


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("ui_copy_lint", LINT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    return module


@pytest.fixture(scope="module")
def lint():
    """One module load for the whole file; tests mutate it only via monkeypatch."""
    return _load_lint_module()


def test_ui_copy_lint_audit_runs_on_repo() -> None:
    result = subprocess.run(
        [sys.executable, str(LINT), "--audit"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Scanned" in result.stdout


def test_tech_lingo_listener_terms_match_the_copy_guard(lint) -> None:
    """The two guards apply different matchers, so the term list must stay one list."""
    assert lint.TECH_LINGO_LISTENER == COPY_GUARD_TECH_LINGO


@pytest.mark.parametrize(
    ("text", "surface", "context", "expected"),
    [
        pytest.param(
            "No ready speaker was found. Bring a speaker online in Home Assistant, then try again.",
            "admin",
            "first_listen_error:no_players",
            {"stale_speaker_copy"},
            id="stale_speaker_phrase_fires",
        ),
        pytest.param(
            "The request hit a timeout. See https://example.invalid/help and try again.",
            "listener",
            "listener_template",
            {"tech_lingo"},
            id="url_does_not_exempt_the_rest_of_the_sentence",
        ),
        pytest.param(
            "Runtime status unavailable because the request hit a timeout.",
            "listener",
            "listener_template",
            {"tech_lingo", "no_way_out"},
            id="informational_phrase_does_not_exempt_other_rules",
        ),
        pytest.param(
            "We couldn't continue because the check failed.",
            "listener",
            "ui_copy:en:playback_failed",
            {"no_way_out"},
            id="incidental_verb_is_not_a_way_out",
        ),
        pytest.param(
            "Annulla la richiesta and join 500 listeners.",
            "listener",
            "",
            set(),
            id="bare_number_is_not_a_status_code",
        ),
        pytest.param(
            "HTTP 500 error.",
            "listener",
            "",
            {"tech_lingo"},
            id="number_in_status_code_context_is_lingo",
        ),
    ],
)
def test_rules_fire_on_the_expected_copy(lint, text: str, surface: str, context: str, expected: set[str]) -> None:
    ref = lint.StringRef("listener.html", 1, text, surface, context)
    assert {v.rule for v in lint.check_strings([ref])} == expected


def test_fingerprint_ignores_line_number_and_derived_detail(lint) -> None:
    first = lint.Violation("tech_lingo", "listener.html", 10, "A timeout happened", "term='timeout'")
    moved = lint.Violation("tech_lingo", "listener.html", 99, "A timeout happened", "term='timeout' ctx=inline")
    assert first.fingerprint == moved.fingerprint


def test_duplicate_violation_exceeding_baseline_count_is_new(lint) -> None:
    first = lint.Violation("tech_lingo", "listener.html", 10, "A timeout happened", "term='timeout'")
    duplicate = lint.Violation("tech_lingo", "listener.html", 99, "A timeout happened", "term='timeout'")

    new, fixed = lint._compare_to_baseline([first, duplicate], lint.Counter([first.fingerprint]))

    assert new == [duplicate]
    assert fixed == 0


def test_jamendo_scalar_error_copy_is_collected(lint) -> None:
    contexts = {ref.context for ref in lint._extract_admin_tables()}
    assert "jamendo_form:jamendo_invalid_request" in contexts
    assert "jamendo_form:jamendo_config_save_failed" in contexts


def test_listener_js_literals_are_collected(lint, tmp_path: Path, monkeypatch) -> None:
    listener_js = tmp_path / "mammamiradio/web/static/listener.js"
    listener_js.parent.mkdir(parents=True)
    listener_js.write_text(
        '_t(\n  \'playback_failed\',\n  "Playback failed.",\n);\nnotice.textContent = "The request hit a timeout.";\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)

    refs = lint._extract_listener_js()
    violations = lint.check_strings(refs)
    assert [ref.text for ref in refs] == ["Playback failed.", "The request hit a timeout."]
    assert {v.rule for v in violations} == {"no_way_out", "tech_lingo"}


def test_listener_template_static_copy_is_collected(lint, tmp_path: Path, monkeypatch) -> None:
    listener_template = tmp_path / "mammamiradio/web/templates/listener.html"
    listener_template.parent.mkdir(parents=True)
    listener_template.write_text(
        '<a\n  class="notice"\n  aria-label="Playback failed due to timeout">\n'
        "{{ copy.get('status_error', 'The request hit a timeout.') }}</a>\n"
        "<script>const hidden = 'timeout';</script>\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)

    refs = lint._extract_listener_template()
    assert {ref.text for ref in refs} == {"The request hit a timeout.", "Playback failed due to timeout"}
    assert not any("class=" in ref.text for ref in refs)
    assert any(violation.rule == "tech_lingo" for violation in lint.check_strings(refs))


def test_all_advertised_copy_sources_are_collected(lint) -> None:
    refs = lint.collect_strings()
    assert {ref.file for ref in refs} == {
        "ha-addon/mammamiradio-edge/translations/en.yaml",
        "ha-addon/mammamiradio/translations/en.yaml",
        "mammamiradio/web/static/listener.js",
        "mammamiradio/web/streamer.py",
        "mammamiradio/web/static/admin.js",
        "mammamiradio/web/templates/admin.html",
        "mammamiradio/web/templates/clip.html",
        "mammamiradio/web/templates/listener.html",
        "mammamiradio/web/ui_copy.py",
    }
    assert any(ref.context == "ui_copy:en:form_network_error" for ref in refs)
    assert not any(ref.text.startswith("Listener UI copy lookup") for ref in refs)
    standalone = next(ref for ref in refs if "not connected to Home Assistant" in ref.text)
    assert "also skip this step" in standalone.text and "Retry connection" in standalone.text


def test_empty_baseline_is_valid(lint, tmp_path: Path, monkeypatch) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"violations": []}), encoding="utf-8")
    monkeypatch.setattr(lint, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(lint, "collect_strings", lambda: [])
    monkeypatch.setattr(lint, "check_coverage", lambda refs: [])
    monkeypatch.setattr(sys, "argv", ["ui_copy_lint.py"])

    baseline = lint.load_baseline()
    assert baseline is not None
    assert not baseline
    assert lint.main() == 0


# The backlog may only shrink. Raising this is a deliberate edit that says
# "we grandfathered more copy", which is exactly the moment worth reviewing.
MAX_BASELINED_VIOLATIONS = 6
MAX_ADVISORY_VIOLATIONS = 0
KNOWN_RULES = {"stale_speaker_copy", "tech_lingo", "no_way_out"}


def test_baseline_backlog_only_shrinks() -> None:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    rows = data["violations"]
    assert len(rows) <= MAX_BASELINED_VIOLATIONS, (
        f"baseline grew to {len(rows)}; fix the copy or raise MAX_BASELINED_VIOLATIONS on purpose"
    )
    assert {row["rule"] for row in rows} <= KNOWN_RULES
    assert all(set(row) == {"rule", "file", "line", "text", "detail"} for row in rows)


def test_baseline_rows_are_the_only_source_of_fingerprints(lint) -> None:
    """A hand-edited baseline cannot disagree with itself: there is one list, not two."""
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert "fingerprints" not in data
    baseline = lint.load_baseline()
    assert baseline is not None
    assert sum(baseline.values()) == len(data["violations"])


def test_new_violation_outside_baseline_fails(lint, tmp_path: Path, monkeypatch) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"violations": []}), encoding="utf-8")
    monkeypatch.setattr(lint, "BASELINE_PATH", baseline_path)
    refs = [
        lint.StringRef(
            "admin.html",
            1,
            "No ready speaker was found. Bring a speaker online, then try again.",
            "admin",
            "first_listen_error:no_players",
        )
    ]
    violations = lint.check_strings(refs)
    baseline = lint.load_baseline()
    assert baseline is not None
    new, _fixed = lint._compare_to_baseline(violations, baseline)
    assert new, "synthetic stale speaker copy must register as a new violation"


def test_lint_fails_when_an_extractor_collects_nothing(lint, monkeypatch, capsys) -> None:
    """The fail-open that mattered most: no coverage must never read as a pass."""
    monkeypatch.setattr(lint, "collect_strings", lambda: [])
    monkeypatch.setattr(sys, "argv", ["ui_copy_lint.py"])

    assert lint.main() == 1
    assert "collected less copy than expected" in capsys.readouterr().err


def test_every_extractor_group_has_a_coverage_floor(lint) -> None:
    """A new extractor without a floor is a new way for the scan to collapse unnoticed."""
    groups = {ref.context.split(":")[0] for ref in lint.collect_strings()}
    assert groups <= set(lint.MIN_STRINGS_PER_GROUP), (
        f"no MIN_STRINGS_PER_GROUP floor for {groups - set(lint.MIN_STRINGS_PER_GROUP)}"
    )
    assert all(lint.MIN_STRINGS_PER_GROUP[group] > 0 for group in groups)


def test_stale_baseline_entry_fails_instead_of_printing_a_note(lint, tmp_path, monkeypatch, capsys) -> None:
    """A fixed violation must force a refresh, or the backlog is one-way forever."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "violations": [
                    {
                        "rule": "no_way_out",
                        "file": "gone.html",
                        "line": 1,
                        "text": "This copy was rewritten.",
                        "detail": "dead-end toast",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lint, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(lint, "collect_strings", lambda: [])
    monkeypatch.setattr(lint, "check_coverage", lambda refs: [])
    monkeypatch.setattr(sys, "argv", ["ui_copy_lint.py"])

    assert lint.main() == 1
    assert "no longer reproduce" in capsys.readouterr().err


def test_write_baseline_names_what_it_newly_accepts(lint, tmp_path, monkeypatch, capsys) -> None:
    """Refreshing is the documented remedy, so it must not swallow a regression silently."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"violations": []}), encoding="utf-8")
    monkeypatch.setattr(lint, "BASELINE_PATH", baseline_path)

    regression = lint.Violation("no_way_out", "admin.html", 4, "It broke.", "dead-end toast")
    lint.print_baseline_delta([regression], lint.load_baseline())

    out = capsys.readouterr().out
    assert "+ now baselined: admin.html:4" in out
    assert "not a regression you just wrote" in out


def test_block_slicing_survives_reformatting_and_new_neighbours(lint) -> None:
    """Byte-exact markers and an EOF-runaway block end both used to drop copy silently."""
    admin = (ROOT / "mammamiradio/web/templates/admin.html").read_text(encoding="utf-8")
    baseline_rows = len(lint._parse_js_object_entries(admin, "FIRST_LISTEN_ERRORS"))
    assert baseline_rows > 10

    spaced = admin.replace("const FIRST_LISTEN_ERRORS={", "const FIRST_LISTEN_ERRORS = {").replace(
        "no_players:{", "no_players: {"
    )
    assert len(lint._parse_js_object_entries(spaced, "FIRST_LISTEN_ERRORS")) == baseline_rows

    # JAMENDO_ERROR_COPY is what a renamed neighbour used to swallow: its block ran to
    # EOF when the declaration after it stopped matching the old three-shape heuristic.
    renamed = admin.replace("function jamendoFailureHint(code){", "const jamendoFailureHint = (code) => {")
    before = lint._parse_js_scalar_entries(admin, "JAMENDO_ERROR_COPY")
    assert len(lint._parse_js_scalar_entries(renamed, "JAMENDO_ERROR_COPY")) == len(before)

    # The same block, reached through the marker rather than its neighbour.
    respaced = admin.replace("const JAMENDO_ERROR_COPY={", "const JAMENDO_ERROR_COPY = {\n")
    assert len(lint._parse_js_scalar_entries(respaced, "JAMENDO_ERROR_COPY")) == len(before)


def test_multi_line_copy_is_graded_as_one_sentence(lint, tmp_path, monkeypatch) -> None:
    """A failure and its way-out on separate source lines are one sentence to a reader."""
    template = tmp_path / "mammamiradio/web/templates/listener.html"
    template.parent.mkdir(parents=True)
    template.write_text(
        "<p>\n  The station could not start playback.\n  Give the tape decks a few seconds and tap again.\n</p>\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)

    refs = lint._extract_listener_template()
    assert [ref.text for ref in refs] == [
        "The station could not start playback. Give the tape decks a few seconds and tap again."
    ]
    assert lint.check_strings(refs) == []


def test_admin_surface_bans_every_listener_machine_word(lint) -> None:
    """Principle #5 gives admin a warmer register, not a shorter banned list."""
    assert set(lint.TECH_LINGO_LISTENER) <= set(lint.TECH_LINGO_ADMIN_HINTS)
    for text in ("Home Assistant rejected the request.", "The station is degraded.", "The buffer is empty."):
        ref = lint.StringRef("admin.html", 1, text, "admin", "toast")
        assert any(v.rule == "tech_lingo" for v in lint.check_strings([ref])), text


def test_centralized_admin_failure_copy_is_scanned(lint) -> None:
    """The constants and helpers most admin toasts route through, not just two niche tables."""
    contexts = {ref.context for ref in lint._extract_admin_tables()}
    assert "admin_copy:STOP_FAILURE_COPY" in contexts
    assert "admin_copy:RESUME_FAILURE_COPY" in contexts
    assert "admin_copy:FIRST_LISTEN_RESUME_FAILURE_COPY" in contexts
    assert "admin_helper:wayOut" in contexts
    assert "admin_helper:offlineMsg" in contexts


def test_url_and_selector_constants_are_not_mistaken_for_copy(lint) -> None:
    assert not lint._is_prose("https://example.invalid/a b")
    assert not lint._is_prose("#skipBtn,.btn-trigger:not([data-x])")
    assert not lint._is_prose("/stream?first_listen=1")
    assert lint._is_prose("The record is cued. Waiting for someone to tune in.")


def test_gate_runs_in_the_quality_workflow() -> None:
    """A guard nobody runs is not a guard; keep it in the lint job with its siblings."""
    workflow = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    assert "bash scripts/check-ui-copy-lint.sh" in workflow


def _main_exit(lint, monkeypatch, tmp_path, refs) -> int:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"violations": []}), encoding="utf-8")
    monkeypatch.setattr(lint, "BASELINE_PATH", baseline)
    monkeypatch.setattr(lint, "collect_strings", lambda: refs)
    monkeypatch.setattr(lint, "check_coverage", lambda _refs: [])
    monkeypatch.setattr(sys, "argv", ["ui_copy_lint.py"])
    return lint.main()


def test_machine_word_fails_the_build(lint, tmp_path, monkeypatch) -> None:
    ref = lint.StringRef("admin.html", 1, "The buffer is empty. Try again.", "admin", "toast")
    assert _main_exit(lint, monkeypatch, tmp_path, [ref]) == 1


def test_dead_end_in_an_authored_table_fails_the_build(lint, tmp_path, monkeypatch) -> None:
    """A table row carries its own action field, so a missing remedy there is real."""
    ref = lint.StringRef("admin.html", 1, "That client ID belongs to another application.", "admin", "jamendo_form:x")
    assert _main_exit(lint, monkeypatch, tmp_path, [ref]) == 1


def test_dead_end_in_free_text_is_advisory_not_a_lint_failure(lint, tmp_path, monkeypatch) -> None:
    """The verb list misjudges free text, so it may report but never fail the lint."""
    ref = lint.StringRef("admin.html", 1, "That song is unavailable. Pick another one.", "admin", "toast")
    assert _main_exit(lint, monkeypatch, tmp_path, [ref]) == 0
    assert any(v.rule == "no_way_out" for v in lint.check_strings([ref]))


def test_free_text_context_is_not_pulled_in_by_a_table_name_prefix(lint) -> None:
    """`jamendo_form_message` is a free-text call site, not a row of `jamendo_form`.

    A `startswith` over the blocking tuple matched it, so misjudged copy at those
    twelve `setJamendoFormMessage` sites would have failed the build with
    "grandfather it in the baseline" as the printed remedy.
    """
    dead_end = lint.Violation("no_way_out", "admin.html", 1, "Saving that failed.", "dead-end")
    assert lint.is_blocking(dead_end, "jamendo_form:jamendo_invalid_request")
    assert not lint.is_blocking(dead_end, "jamendo_form_message")
    assert not lint.is_blocking(dead_end, "first_listen_status")
    assert lint.is_blocking(dead_end, "first_listen_error:no_players")


def test_audit_labels_each_row_blocking_or_advisory(lint, capsys) -> None:
    """A rule-level label cannot say which `no_way_out` rows actually gate a build."""
    table = lint.Violation("no_way_out", "admin.html", 1, "It broke.", "dead-end")
    free = lint.Violation("no_way_out", "admin.html", 2, "It broke too.", "dead-end")
    lint.print_audit([table], [free], [])

    out = capsys.readouterr().out
    assert "## no_way_out (2: 1 blocking, 1 advisory)" in out
    assert "[blocking] admin.html:1" in out
    assert "[advisory] admin.html:2" in out


def test_advisory_backlog_only_shrinks(lint) -> None:
    """Nothing else bounds advisory violations, so they could balloon unseen.

    This assertion is the only build failure free-text `no_way_out` can cause, so it
    names the offending strings: "advisory grew to 3" sends an author hunting, and the
    honest remedies are to fix the copy or to raise the ceiling on purpose.
    """
    _blocking, advisory = lint.split_blocking(lint.collect_strings())
    named = "\n".join(f"  {v.file}:{v.line} [{v.rule}] {v.text[:100]}" for v in advisory)
    assert len(advisory) <= MAX_ADVISORY_VIOLATIONS, (
        f"advisory copy grew to {len(advisory)} (ceiling {MAX_ADVISORY_VIOLATIONS}):\n{named}\n"
        "Give each one a next step, or raise MAX_ADVISORY_VIOLATIONS if the check misjudged it."
    )


def test_every_declared_copy_helper_still_holds_copy(lint) -> None:
    """A helper listed but collecting nothing means the scan broke, not that it is silent."""
    contexts = {ref.context for ref in lint._extract_admin_tables()}
    for name in lint.ADMIN_COPY_FUNCTIONS:
        assert f"admin_helper:{name}" in contexts, f"{name} collected no copy"
