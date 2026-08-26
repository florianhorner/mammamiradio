"""Tests for scripts/ui_copy_lint.py — Principle #5 regression guard."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINT = ROOT / "scripts" / "ui_copy_lint.py"
BASELINE = ROOT / ".config" / "ui-copy-baseline.json"
CHECK_SH = ROOT / "scripts" / "check-ui-copy-lint.sh"


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


def _run_ui_copy_lint(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LINT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ui_copy_lint_audit_runs_on_repo() -> None:
    result = _run_ui_copy_lint("--audit")
    assert result.returncode == 0, result.stderr
    assert "Scanned" in result.stdout


def test_ui_copy_lint_baseline_mode_passes_on_repo() -> None:
    assert BASELINE.is_file(), "baseline must be committed with known backlog"
    result = _run_ui_copy_lint()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UI copy lint clean" in result.stdout


def test_check_ui_copy_lint_shell_wrapper_passes() -> None:
    result = subprocess.run(
        ["bash", str(CHECK_SH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stale_speaker_rule_fires_on_synthetic_string() -> None:
    lint = _load_lint_module()
    refs = [
        lint.StringRef(
            "admin.html",
            1,
            "No ready speaker was found. Bring a speaker online in Home Assistant, then try again.",
            "admin",
            "first_listen_error:no_players",
        )
    ]
    violations = lint.check_strings(refs)
    assert any(v.rule == "stale_speaker_copy" for v in violations)


def test_fingerprint_is_stable_when_only_line_number_moves() -> None:
    lint = _load_lint_module()
    first = lint.Violation("tech_lingo", "listener.html", 10, "A timeout happened", "term='timeout'")
    moved = lint.Violation("tech_lingo", "listener.html", 99, "A timeout happened", "term='timeout'")
    assert first.fingerprint == moved.fingerprint


def test_duplicate_violation_exceeding_baseline_count_is_new() -> None:
    lint = _load_lint_module()
    first = lint.Violation("tech_lingo", "listener.html", 10, "A timeout happened", "term='timeout'")
    duplicate = lint.Violation("tech_lingo", "listener.html", 99, "A timeout happened", "term='timeout'")

    new, fixed = lint._compare_to_baseline([first, duplicate], lint.Counter([first.fingerprint]))

    assert new == [duplicate]
    assert fixed == 0


def test_jamendo_scalar_error_copy_is_collected() -> None:
    lint = _load_lint_module()
    contexts = {ref.context for ref in lint._extract_admin_tables()}
    assert "jamendo_form:jamendo_invalid_request" in contexts
    assert "jamendo_form:jamendo_config_save_failed" in contexts


def test_listener_text_content_assignment_is_collected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lint = _load_lint_module()
    listener_js = tmp_path / "mammamiradio/web/static/listener.js"
    listener_js.parent.mkdir(parents=True)
    listener_js.write_text('notice.textContent = "The request hit a timeout.";\n', encoding="utf-8")
    monkeypatch.setattr(lint, "ROOT", tmp_path)

    refs = lint._extract_listener_js()
    assert [ref.text for ref in refs] == ["The request hit a timeout."]
    assert any(violation.rule == "tech_lingo" for violation in lint.check_strings(refs))


def test_listener_template_static_copy_is_collected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lint = _load_lint_module()
    listener_template = tmp_path / "mammamiradio/web/templates/listener.html"
    listener_template.parent.mkdir(parents=True)
    listener_template.write_text(
        "<p>{{ copy.get('status_error', 'The request hit a timeout.') }}</p>\n"
        "<script>const hidden = 'timeout';</script>\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lint, "ROOT", tmp_path)

    refs = lint._extract_listener_template()
    assert [ref.text for ref in refs] == ["The request hit a timeout."]
    assert any(violation.rule == "tech_lingo" for violation in lint.check_strings(refs))


def test_url_does_not_exempt_the_rest_of_copy_from_lingo_check() -> None:
    lint = _load_lint_module()
    refs = [
        lint.StringRef(
            "listener.html",
            1,
            "The request hit a timeout. See https://example.invalid/help and try again.",
            "listener",
            "listener_template",
        )
    ]
    assert any(violation.rule == "tech_lingo" for violation in lint.check_strings(refs))


def test_informational_phrase_does_not_exempt_other_rules() -> None:
    lint = _load_lint_module()
    refs = [
        lint.StringRef(
            "listener.html",
            1,
            "Runtime status unavailable because the request hit a timeout.",
            "listener",
            "listener_template",
        )
    ]
    assert any(violation.rule == "tech_lingo" for violation in lint.check_strings(refs))


def test_empty_baseline_is_valid(tmp_path: Path, monkeypatch) -> None:
    lint = _load_lint_module()
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps({"fingerprints": [], "violations": []}), encoding="utf-8")
    monkeypatch.setattr(lint, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(lint, "collect_strings", lambda: [])
    monkeypatch.setattr(sys, "argv", ["ui_copy_lint.py"])

    baseline = lint.load_baseline()
    assert baseline is not None
    assert not baseline
    assert lint.main() == 0


def test_baseline_documents_current_stale_speaker_backlog() -> None:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    rules = {v["rule"] for v in data["violations"]}
    assert rules == {"stale_speaker_copy"}
    assert len(data["violations"]) == 6
    files = {v["file"] for v in data["violations"]}
    assert "mammamiradio/web/templates/admin.html" in files
    assert "mammamiradio/web/streamer.py" in files


def test_new_violation_outside_baseline_fails(tmp_path: Path) -> None:
    lint = _load_lint_module()
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"fingerprints": ["deadbeefdeadbeef"], "violations": []}),
        encoding="utf-8",
    )
    lint.BASELINE_PATH = baseline_path
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
