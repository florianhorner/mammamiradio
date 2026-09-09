"""Tests for scripts/check_model_registry.py, the model registry watch.

Everything here runs offline against trimmed real captures of the provider docs
(tests/scripts/fixtures/model_registry/) plus a few synthetic variants. The one
assertion about the real repo registry is schema only: its age is a release-cut
question (scripts/pre-release-check.sh section 11), never an every-PR one.
"""

from __future__ import annotations

import datetime as dt
import http.client
import shutil
import socket
import urllib.error
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import check_model_registry as watch

FIXTURES = Path(__file__).parent / "fixtures" / "model_registry"
SYNTHETIC = FIXTURES / "synthetic"
REPO_REGISTRY = Path(__file__).resolve().parents[2] / "model_registry.toml"
TODAY = dt.date(2026, 9, 8)

REGISTRY_PINNED_2026 = """[models]
default_profile = "balanced"
last_reviewed = "2026-09-08"

[models.catalog.anthropic]
fable = "claude-fable-5"
opus = "claude-opus-4-8"
sonnet = "claude-sonnet-4-6"
haiku = "claude-haiku-4-5-20251001"

[models.catalog.openai]
large = "gpt-5.5"
small = "gpt-5.4-mini"

[tts.openai]
model = "gpt-4o-mini-tts"
"""

REGISTRY_ALL_CURRENT = """[models]
default_profile = "balanced"
last_reviewed = "2026-09-08"

[models.catalog.anthropic]
opus = "claude-opus-5"
sonnet = "claude-sonnet-5"
haiku = "claude-haiku-4-5-20251001"

[models.catalog.openai]
large = "gpt-5.6-sol"
small = "gpt-5.6-luna"

[tts.openai]
model = "gpt-4o-mini-tts"
"""


def _registry(tmp_path: Path, text: str = REGISTRY_PINNED_2026, **swaps: str) -> Path:
    for old, new in swaps.items():
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "model_registry.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _fixture_dir(tmp_path: Path, **overrides: Path) -> Path:
    """A copy of the real fixtures with individual files swapped for synthetic ones."""
    target = tmp_path / "fixtures"
    target.mkdir()
    for name in watch.FIXTURE_FILES.values():
        shutil.copy(FIXTURES / name, target / name)
    for key, source in overrides.items():
        shutil.copy(source, target / watch.FIXTURE_FILES[key])
    return target


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = watch.main(list(argv))
    return code, capsys.readouterr().out


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture mode must never open a socket; live mode is exercised via patched urlopen."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the checker tried to open a socket in an offline test")

    monkeypatch.setattr(socket, "socket", _refuse)


# --- age ----------------------------------------------------------------------


def test_age_missing_models_table_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, "[tts.openai]\nmodel = 'gpt-4o-mini-tts'\n")
    code, out = _run(capsys, "--age", "--registry", str(path), "--today", "2026-09-08")
    assert code == watch.EXIT_FINDING
    assert "no [models] table" in out


def test_age_missing_stamp_fails_with_the_way_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, **{'last_reviewed = "2026-09-08"\n': ""})
    code, out = _run(capsys, "--age", "--registry", str(path), "--today", "2026-09-08")
    assert code == watch.EXIT_FINDING
    assert "last_reviewed is missing" in out
    assert "--providers" in out


@pytest.mark.parametrize(
    "stamp", ['"2026-9-8"', '"yesterday"', '"20260908"', '"2026-02-30"', "2026-09-08T10:00:00Z", "true"]
)
def test_age_malformed_stamp_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str], stamp: str) -> None:
    path = _registry(tmp_path, **{'last_reviewed = "2026-09-08"': f"last_reviewed = {stamp}"})
    code, out = _run(capsys, "--age", "--registry", str(path), "--today", "2026-09-08")
    assert code == watch.EXIT_FINDING
    assert "is not a YYYY-MM-DD date" in out


def test_age_accepts_a_bare_toml_date(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, **{'last_reviewed = "2026-09-08"': "last_reviewed = 2026-09-08"})
    code, out = _run(capsys, "--age", "--registry", str(path), "--today", "2026-09-08")
    assert code == watch.EXIT_OK
    assert "0 days old" in out


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(watch.MAX_AGE_DAYS, watch.EXIT_OK), (watch.MAX_AGE_DAYS + 1, watch.EXIT_FINDING)],
    ids=["exactly-max-days", "max-plus-one"],
)
def test_age_boundary_is_the_max_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], offset: int, expected: int
) -> None:
    path = _registry(tmp_path)
    today = (TODAY + dt.timedelta(days=offset)).isoformat()
    code, out = _run(capsys, "--age", "--registry", str(path), "--today", today)
    assert code == expected
    assert f"(max {watch.MAX_AGE_DAYS})" in out
    if expected == watch.EXIT_FINDING:
        assert f"{offset} days old" in out
        assert "--providers" in out


@pytest.mark.parametrize("mode", ["--age", "--providers", "--report"])
def test_unparseable_toml_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: str) -> None:
    path = _registry(tmp_path, "[models\n")
    code, out = _run(capsys, mode, "--registry", str(path))
    assert code == watch.EXIT_SOURCE
    assert out.startswith("UNREADABLE:")
    assert "could not be parsed" in out


@pytest.mark.parametrize(
    ("today", "expected"),
    [("2026-09-07", watch.EXIT_OK), ("2026-09-06", watch.EXIT_FINDING)],
    ids=["one-day-ahead-tolerated", "two-days-ahead-is-the-future"],
)
def test_age_future_tolerance_is_one_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], today: str, expected: int
) -> None:
    """A maintainer stamping after UTC midnight from a later timezone is not lying."""
    path = _registry(tmp_path)
    code, out = _run(capsys, "--age", "--registry", str(path), "--today", today)
    assert code == expected
    assert ("in the future" in out) == (expected == watch.EXIT_FINDING)


def test_age_missing_registry_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(capsys, "--age", "--registry", str(tmp_path / "nope.toml"))
    assert code == watch.EXIT_FINDING
    assert "not found" in out


# --- parsers ------------------------------------------------------------------


def test_anthropic_overview_capture_parses_current_and_legacy() -> None:
    current, legacy = watch.parse_anthropic_overview((FIXTURES / "anthropic-overview.md").read_text())
    assert current == {"claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"}
    assert {"opus-4-8", "sonnet-4-6", "fable-5"} <= set(legacy)


def test_anthropic_deprecations_capture_parses_lifecycle_rows() -> None:
    rows = watch.parse_anthropic_deprecations((FIXTURES / "anthropic-deprecations.md").read_text())
    for pin in ("claude-fable-5", "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
        assert rows[pin].state == "active", pin
    assert rows["claude-opus-4-1-20250805"].state == "retired"
    assert "October 15, 2026" in rows["claude-haiku-4-5-20251001"].retirement


def test_anthropic_status_table_is_read_by_header_names() -> None:
    reordered = (
        "| Tentative retirement date | API model name | Current state | Deprecated |\n|---|---|---|---|\n"
        "| Not sooner than October 15, 2026 | claude-haiku-4-5-20251001 | Active | N/A |\n"
    )
    rows = watch.parse_anthropic_deprecations(reordered)
    assert rows["claude-haiku-4-5-20251001"] == watch.AnthropicRow("active", "Not sooner than October 15, 2026")
    with pytest.raises(watch.SourceError, match="lost a column"):
        watch.parse_anthropic_deprecations(reordered.replace("Tentative retirement date", "Sunset"))


def test_anthropic_unknown_lifecycle_state_is_a_source_error() -> None:
    text = (FIXTURES / "anthropic-deprecations.md").read_text().replace("| Active ", "| Supported ", 1)
    with pytest.raises(watch.SourceError, match="unknown lifecycle state"):
        watch.parse_anthropic_deprecations(text)


@pytest.mark.parametrize("model_cell", ["N/A", "No active models"])
def test_anthropic_placeholder_model_rows_are_source_errors(model_cell: str) -> None:
    page = (
        "| API model name | Current state | Deprecated | Tentative retirement date |\n"
        "|---|---|---|---|\n"
        f"| {model_cell} | Active | N/A | N/A |\n"
    )
    with pytest.raises(watch.SourceError, match="invalid API model name"):
        watch.parse_anthropic_deprecations(page)


@pytest.mark.parametrize(("first_state", "second_state"), [("Deprecated", "Active"), ("Active", "Deprecated")])
def test_anthropic_conflicting_duplicate_rows_are_source_errors(first_state: str, second_state: str) -> None:
    page = (
        "| API model name | Current state | Deprecated | Tentative retirement date |\n"
        "|---|---|---|---|\n"
        f"| claude-opus-4-8 | {first_state} | 2026-09-01 | 2026-12-01 |\n"
        f"| claude-opus-4-8 | {second_state} | N/A | N/A |\n"
    )
    with pytest.raises(watch.SourceError, match="conflicting lifecycle rows"):
        watch.parse_anthropic_deprecations(page)


@pytest.mark.parametrize(
    ("parser", "match"),
    [
        (watch.parse_anthropic_overview, "Claude API ID"),
        (watch.parse_anthropic_deprecations, "Model status"),
        (watch.parse_openai_models, "no gpt-"),
        (watch.parse_openai_deprecations, "Shutdown date"),
    ],
    ids=["anthropic-overview", "anthropic-deprecations", "openai-models", "openai-deprecations"],
)
def test_each_parser_fails_closed_on_a_page_without_its_anchor(parser: Callable[[str], object], match: str) -> None:
    with pytest.raises(watch.SourceError, match=match):
        parser((SYNTHETIC / "garbage.txt").read_text())


def test_anthropic_status_table_without_rows_is_a_source_error() -> None:
    text = (
        "## Model status\n\n| API model name | Current state | Deprecated | Tentative retirement date |\n"
        "|---|---|---|---|\n\n## Next\n"
    )
    with pytest.raises(watch.SourceError, match="no rows"):
        watch.parse_anthropic_deprecations(text)


def test_openai_models_capture_yields_the_current_lineup() -> None:
    tokens = watch.parse_openai_models((FIXTURES / "openai-models.html").read_text())
    assert {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-4o-mini-tts"} <= tokens
    assert "gpt-5.5" not in tokens
    assert not any(token.endswith(".png") for token in tokens)


def test_openai_deprecations_capture_parses_dated_rows() -> None:
    shutdowns = watch.parse_openai_deprecations((FIXTURES / "openai-deprecations.md").read_text())
    assert shutdowns["gpt-5-2025-08-07"].date == dt.date(2026, 12, 11)
    assert shutdowns["gpt-3.5-turbo"].date == dt.date(2026, 10, 23)
    assert shutdowns["gpt-3.5-turbo-completions"].date == dt.date(2026, 10, 23)
    assert "gpt-5.5" not in shutdowns
    assert not {"Videos", "API", "New", "fine-tuning", "training", "on"} & shutdowns.keys()


def test_openai_deprecations_html_table_also_parses() -> None:
    """If the site stops negotiating to markdown, the HTML table shape still reads."""
    shutdowns = watch.parse_openai_deprecations((SYNTHETIC / "openai-deprecations-gpt55-shutdown.html").read_text())
    assert shutdowns["gpt-5.5"].date == dt.date(2026, 1, 15)
    assert shutdowns["gpt-3.5-turbo"].date == dt.date(2026, 10, 23)


def test_openai_deprecations_header_only_table_raises_no_rows() -> None:
    page = "| Model / system | Shutdown date |\n|---|---|\n"
    with pytest.raises(watch.SourceError, match="no model shutdown rows"):
        watch.parse_openai_deprecations(page)


def test_openai_deprecations_empty_table_cannot_hide_behind_a_valid_table() -> None:
    page = (
        "| Model / system | Shutdown date |\n|---|---|\n"
        "| `gpt-4o` | Sep 1, 2026 |\n\n"
        "| Model snapshot | Shutdown date |\n|---|---|\n"
    )
    with pytest.raises(watch.SourceError, match="no model shutdown rows"):
        watch.parse_openai_deprecations(page)


def test_openai_deprecations_unknown_source_column_cannot_hide_behind_a_valid_table() -> None:
    page = (
        "| Model / system | Shutdown date |\n|---|---|\n"
        "| `gpt-4o` | Sep 1, 2026 |\n\n"
        "| Retiring asset | Shutdown date |\n|---|---|\n"
        "| `gpt-5.5` | Sep 1, 2026 |\n"
    )
    with pytest.raises(watch.SourceError, match="no recognized source-model column"):
        watch.parse_openai_deprecations(page)


@pytest.mark.parametrize("source_cell", ["No retired models are affected", "N/A", "`N/A`"])
def test_openai_deprecations_placeholder_source_row_is_a_source_error(source_cell: str) -> None:
    page = f"| Model / system | Shutdown date |\n|---|---|\n| {source_cell} | TBD |\n"
    with pytest.raises(watch.SourceError, match="unparseable source-model cell"):
        watch.parse_openai_deprecations(page)


def test_openai_deprecations_html_code_placeholder_is_a_source_error() -> None:
    page = (
        "<table><tr><th>Model / system</th><th>Shutdown date</th></tr>"
        "<tr><td><code>N/A</code></td><td>TBD</td></tr></table>"
    )
    with pytest.raises(watch.SourceError, match="unparseable source-model cell"):
        watch.parse_openai_deprecations(page)


def test_openai_deprecations_header_only_table_fails_liveness_with_exit_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic = tmp_path / "openai-deprecations-empty.md"
    synthetic.write_text("| Model / system | Shutdown date |\n|---|---|\n", encoding="utf-8")
    fixtures = _fixture_dir(tmp_path, openai_deprecations=synthetic)
    path = _registry(tmp_path)
    code, out = _run(
        capsys,
        "--providers",
        "--gate",
        "liveness",
        "--registry",
        str(path),
        "--fixture-dir",
        str(fixtures),
        "--today",
        "2026-09-08",
    )
    assert code == watch.EXIT_SOURCE
    assert out.startswith("UNREADABLE:")
    assert "no model shutdown rows" in out
    assert "OK (liveness):" not in out


@pytest.mark.parametrize("replacement_header", ["Recommended replacement model", "Substitute model"])
def test_openai_deprecations_replacement_column_is_not_retiring_model(
    replacement_header: str,
) -> None:
    page = (
        f"| {replacement_header} | Shutdown date | Model |\n"
        f"|---|---|---|\n"
        "| `gpt-5.6-sol` | Sep 1, 2026 | `gpt-5.5` |\n"
    )
    shutdowns = watch.parse_openai_deprecations(page)
    assert shutdowns["gpt-5.5"] == watch.Shutdown(dt.date(2026, 9, 1), "Sep 1, 2026")
    assert "gpt-5.6-sol" not in shutdowns


def test_openai_deprecations_prefers_deprecated_api_id_over_migration_model() -> None:
    page = (
        "| Shutdown date | Deprecated API ID | Migration model |\n"
        "|---|---|---|\n"
        "| Sep 1, 2026 | `gpt-5.5` | `gpt-5.6-sol` |\n"
    )
    shutdowns = watch.parse_openai_deprecations(page)
    assert shutdowns == {"gpt-5.5": watch.Shutdown(dt.date(2026, 9, 1), "Sep 1, 2026")}


def test_openai_deprecations_ambiguous_source_columns_are_source_errors() -> None:
    page = (
        "| Model / system | Model family / snapshot | Shutdown date |\n"
        "|---|---|---|\n"
        "| `gpt-5.5` | `gpt-5.6-sol` | Sep 1, 2026 |\n"
    )
    with pytest.raises(watch.SourceError, match="ambiguous"):
        watch.parse_openai_deprecations(page)


def test_openai_deprecations_ignores_model_price() -> None:
    page = (
        "| Shutdown date | Deprecated model | Deprecated model price | Recommended replacement |\n"
        "|---|---|---|---|\n"
        "| Sep 1, 2026 | `gpt-5.5` | $1.50 / $6.00 | `gpt-5.6-sol` |\n"
    )
    shutdowns = watch.parse_openai_deprecations(page)
    assert shutdowns == {"gpt-5.5": watch.Shutdown(dt.date(2026, 9, 1), "Sep 1, 2026")}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Dec 11, 2026", dt.date(2026, 12, 11)),
        ("October 23, 2026", dt.date(2026, 10, 23)),
        ("2026\u201103\u201126", dt.date(2026, 3, 26)),
        ("Sept 1, 2026", dt.date(2026, 9, 1)),
        ("September 1st, 2026", dt.date(2026, 9, 1)),
        ("Jan 15, 2027 *", dt.date(2027, 1, 15)),
        ("Jan\xa015, 2027", dt.date(2027, 1, 15)),
        ("Not sooner than October 15, 2026", None),
        ("at earliest 2024-06-13", None),
        ("TBD", None),
    ],
    ids=[
        "abbrev",
        "long",
        "nb-hyphen-iso",
        "sept",
        "ordinal",
        "footnote-star",
        "nbsp",
        "floor-prose",
        "floor-iso",
        "tbd",
    ],
)
def test_docs_date_parsing(text: str, expected: dt.date | None) -> None:
    assert watch.parse_docs_date(text) == expected


def test_anthropic_rows_with_several_ids_in_one_cell_are_all_registered() -> None:
    text = (
        (FIXTURES / "anthropic-deprecations.md")
        .read_text()
        .replace(
            "| claude-haiku-4-5-20251001  | Active ", "| claude-haiku-4-5-20251001, claude-haiku-4-5 | Deprecated "
        )
    )
    rows = watch.parse_anthropic_deprecations(text)
    assert rows["claude-haiku-4-5-20251001"].state == "deprecated"
    assert rows["claude-haiku-4-5"].state == "deprecated"


def test_anthropic_table_survives_a_note_in_the_middle() -> None:
    text = (FIXTURES / "anthropic-deprecations.md").read_text()
    marker = "| claude-sonnet-5 "
    assert marker in text
    text = text.replace(marker, "\n<Note>\n  Sonnet rows follow.\n</Note>\n\n" + marker, 1)
    rows = watch.parse_anthropic_deprecations(text)
    assert "claude-sonnet-5" in rows and "claude-haiku-4-5-20251001" in rows


def test_anthropic_overview_reads_only_the_first_api_id_row() -> None:
    text = (FIXTURES / "anthropic-overview.md").read_text()
    text += "\n\n| Claude API ID | `claude-opus-4-8` | `claude-sonnet-4-6` |\n"
    current, _legacy = watch.parse_anthropic_overview(text)
    assert "claude-opus-4-8" not in current and "claude-sonnet-5" in current


def test_openai_shutdowns_bind_columns_by_each_tables_header() -> None:
    page = (
        "| Date | Update |\n|---|---|\n"
        "| Nov 30, 2026 | `gpt-5.5` moves to legacy status; `gpt-4o-mini-tts` gets a new icon |\n\n"
        "| Announced | Shutdown date | Model | Replacement |\n|---|---|---|---|\n"
        "| Sep 1, 2026 | May 1, 2027 | `gpt-5.5`. | `gpt-5.6-sol` |\n\n"
        "| Model / system | Shutdown date |\n|---|---|\n"
        "| `gpt-5.4-mini` | at earliest 2026-12-01 |\n"
    )
    shutdowns = watch.parse_openai_deprecations(page)
    assert "gpt-4o-mini-tts" not in shutdowns, "an update-log table must never stamp a model"
    assert shutdowns["gpt-5.5"] == watch.Shutdown(dt.date(2027, 5, 1), "May 1, 2027")
    assert shutdowns["gpt-5.4-mini"].date is None
    assert "May" not in shutdowns and "2027" not in shutdowns


def test_openai_unreadable_shutdown_date_is_a_deprecation_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page = (
        "| Shutdown date | Model / system | Recommended replacement |\n|---|---|---|\n"
        "| TBD | `gpt-5.5` | `gpt-5.6-sol` |\n"
    )
    synthetic = tmp_path / "unreadable.md"
    synthetic.write_text(page, encoding="utf-8")
    fixtures = _fixture_dir(tmp_path, openai_deprecations=synthetic)
    path = _registry(tmp_path)
    code, out = _run(
        capsys,
        "--providers",
        "--gate",
        "liveness",
        "--registry",
        str(path),
        "--fixture-dir",
        str(fixtures),
        "--today",
        "2026-09-08",
    )
    assert code == watch.EXIT_FINDING
    line = next(line for line in out.splitlines() if line.startswith("openai.large"))
    assert "deprecated" in line and "date unreadable: 'TBD'" in line


# --- classification against the real captures --------------------------------


def test_pinned_registry_is_a_generation_behind_but_alive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path)
    code, out = _run(
        capsys, "--providers", "--registry", str(path), "--fixture-dir", str(FIXTURES), "--today", "2026-09-08"
    )
    assert code == watch.EXIT_FINDING
    lines = {line.split()[0]: line for line in out.splitlines() if line and not line.startswith(("FINDINGS", "OK"))}
    assert "legacy" in lines["anthropic.opus"] and "(current: claude-opus-5)" in lines["anthropic.opus"]
    assert "legacy" in lines["anthropic.sonnet"] and "(current: claude-sonnet-5)" in lines["anthropic.sonnet"]
    assert "legacy" in lines["anthropic.fable"] and "(current: claude-fable-5-1)" in lines["anthropic.fable"]
    assert "current" in lines["anthropic.haiku"]
    assert "unlisted" in lines["openai.large"] and "gpt-5.6-sol" in lines["openai.large"]
    assert "unlisted" in lines["openai.small"]
    assert "current" in lines["tts.openai"]
    assert "FINDINGS (drift): 5 of 7" in out

    code, out = _run(
        capsys,
        "--providers",
        "--gate",
        "liveness",
        "--registry",
        str(path),
        "--fixture-dir",
        str(FIXTURES),
        "--today",
        "2026-09-08",
    )
    assert code == watch.EXIT_OK
    assert "OK (liveness): all 7 pinned models are alive" in out


def test_retired_anthropic_pin_fails_liveness(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, **{'opus = "claude-opus-4-8"': 'opus = "claude-opus-4-1-20250805"'})
    code, out = _run(
        capsys, "--providers", "--gate", "liveness", "--registry", str(path), "--fixture-dir", str(FIXTURES)
    )
    assert code == watch.EXIT_FINDING
    line = next(line for line in out.splitlines() if line.startswith("anthropic.opus"))
    assert "retired" in line and "retired August 5, 2026" in line


def test_anthropic_legacy_row_state_classifies_as_legacy() -> None:
    text = (
        (FIXTURES / "anthropic-deprecations.md")
        .read_text()
        .replace("| claude-opus-4-8            | Active ", "| claude-ancient-1 | Legacy ", 1)
    )
    rows = watch.parse_anthropic_deprecations(text)
    current, _legacy = watch.parse_anthropic_overview((FIXTURES / "anthropic-overview.md").read_text())
    docs = watch.ProviderDocs(current, (), rows, frozenset({"gpt-5.6"}), {})
    verdict = watch.classify_anthropic(watch.Entry("anthropic.old", "anthropic", "claude-ancient-1"), docs)
    assert verdict.status == "legacy"
    assert verdict.detail == "(not on the models overview)"


def test_registry_without_models_is_a_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, '[models]\ndefault_profile = "balanced"\nlast_reviewed = "2026-09-08"\n')
    code, out = _run(capsys, "--providers", "--registry", str(path), "--fixture-dir", str(FIXTURES))
    assert code == watch.EXIT_FINDING
    assert "names no models under [models.catalog] or [tts]" in out


def test_all_current_registry_is_clean_under_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, REGISTRY_ALL_CURRENT)
    code, out = _run(
        capsys, "--providers", "--registry", str(path), "--fixture-dir", str(FIXTURES), "--today", "2026-09-08"
    )
    assert code == watch.EXIT_OK
    assert "OK (drift): all 6 pinned models are current" in out


def test_deprecated_anthropic_pin_fails_liveness(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path)
    page = tmp_path / "haiku-deprecated.md"
    page.write_text(
        (FIXTURES / "anthropic-deprecations.md")
        .read_text()
        .replace(
            "| claude-haiku-4-5-20251001  | Active ",
            "| claude-haiku-4-5-20251001  | Deprecated    | September 15, 2026 | November 15, 2026 ",
        ),
        encoding="utf-8",
    )
    fixtures = _fixture_dir(tmp_path, anthropic_deprecations=page)
    code, out = _run(
        capsys,
        "--providers",
        "--gate",
        "liveness",
        "--registry",
        str(path),
        "--fixture-dir",
        str(fixtures),
        "--today",
        "2026-09-08",
    )
    assert code == watch.EXIT_FINDING
    assert "anthropic.haiku" in out and "deprecated" in out and "retires November 15, 2026" in out
    assert "FINDINGS (liveness): 1 of 7" in out


@pytest.mark.parametrize(
    ("today", "status", "phrase"),
    [("2026-09-08", "retired", "shut down 2026-01-15"), ("2025-12-01", "deprecated", "shuts down 2026-01-15")],
)
def test_openai_shutdown_date_classifies_by_today(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], today: str, status: str, phrase: str
) -> None:
    path = _registry(tmp_path)
    fixtures = _fixture_dir(tmp_path, openai_deprecations=SYNTHETIC / "openai-deprecations-gpt55-shutdown.html")
    code, out = _run(
        capsys,
        "--providers",
        "--gate",
        "liveness",
        "--registry",
        str(path),
        "--fixture-dir",
        str(fixtures),
        "--today",
        today,
    )
    assert code == watch.EXIT_FINDING
    line = next(line for line in out.splitlines() if line.startswith("openai.large"))
    assert status in line and phrase in line


def test_unknown_provider_is_reported_not_guessed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, REGISTRY_ALL_CURRENT + '\n[models.catalog.gemini]\nflash = "gemini-9-flash"\n')
    code, out = _run(
        capsys, "--providers", "--registry", str(path), "--fixture-dir", str(FIXTURES), "--today", "2026-09-08"
    )
    assert code == watch.EXIT_FINDING
    assert "gemini.flash" in out and "no docs source for provider 'gemini'" in out


# --- fail closed ----------------------------------------------------------------


@pytest.mark.parametrize("key", list(watch.SOURCES))
def test_garbage_page_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str) -> None:
    path = _registry(tmp_path)
    fixtures = _fixture_dir(tmp_path, **{key: SYNTHETIC / "garbage.txt"})
    code, out = _run(
        capsys, "--providers", "--gate", "liveness", "--registry", str(path), "--fixture-dir", str(fixtures)
    )
    assert code == watch.EXIT_SOURCE
    assert out.startswith("UNREADABLE:")


def test_missing_fixture_file_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path)
    code, out = _run(capsys, "--providers", "--registry", str(path), "--fixture-dir", str(tmp_path / "absent"))
    assert code == watch.EXIT_SOURCE
    assert "unreadable" in out


def test_fetch_failure_fails_closed_and_names_the_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _boom(request: object, **_kwargs: object) -> None:
        calls.append(getattr(request, "full_url", "?"))
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(watch, "RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(watch.urllib.request, "urlopen", _boom)
    path = _registry(tmp_path)
    code, out = _run(capsys, "--providers", "--registry", str(path))
    assert code == watch.EXIT_SOURCE
    assert "after 2 attempts" in out and "name resolution failed" in out
    assert any(url in out for url in watch.SOURCES.values())
    # Pages fetch concurrently, so the first failure can surface before every page was tried;
    # each page that was tried got exactly FETCH_ATTEMPTS attempts.
    assert calls and set(Counter(calls).values()) == {watch.FETCH_ATTEMPTS}


def test_mid_body_http_failure_is_unreadable_not_a_dead_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _truncated(*_args: object, **_kwargs: object) -> None:
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(watch, "RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(watch.urllib.request, "urlopen", _truncated)
    path = _registry(tmp_path)
    code, out = _run(capsys, "--providers", "--gate", "liveness", "--registry", str(path))
    assert code == watch.EXIT_SOURCE
    assert out.startswith("UNREADABLE:") and "IncompleteRead" in out


def test_one_transient_failure_is_retried_and_the_run_completes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    by_url = {url: (FIXTURES / watch.FIXTURE_FILES[key]).read_bytes() for key, url in watch.SOURCES.items()}
    failed_once: list[str] = []

    class _Response:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, amount: int = -1) -> bytes:
            return self._body if amount < 0 else self._body[:amount]

    def _flaky(request: object, **_kwargs: object) -> _Response:
        url = getattr(request, "full_url", "")
        if url not in failed_once:
            failed_once.append(url)
            raise urllib.error.URLError("transient")
        return _Response(by_url[url])

    monkeypatch.setattr(watch, "RETRY_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(watch.urllib.request, "urlopen", _flaky)
    path = _registry(tmp_path)
    code, out = _run(capsys, "--providers", "--registry", str(path), "--today", "2026-09-08")
    assert code == watch.EXIT_FINDING
    assert "UNREADABLE" not in out and "FINDINGS (drift): 5 of 7" in out
    assert len(failed_once) == len(watch.SOURCES)


@pytest.mark.parametrize("advertise_length", [False, True], ids=["bounded-read", "content-length"])
def test_oversized_provider_response_is_a_source_error(monkeypatch: pytest.MonkeyPatch, advertise_length: bool) -> None:
    class _Response:
        status = 200

        def __init__(self) -> None:
            self.headers = {"Content-Length": str(watch.MAX_SOURCE_BYTES + 1)} if advertise_length else {}

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self, amount: int = -1) -> bytes:
            assert amount == watch.MAX_SOURCE_BYTES + 1
            return b"x" * amount

    monkeypatch.setattr(watch.urllib.request, "urlopen", lambda *_a, **_k: _Response())
    with pytest.raises(watch.SourceError, match="exceeds"):
        watch.fetch_text("https://example.test/provider-docs")


def test_a_checker_bug_reads_as_not_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("parser regression")

    monkeypatch.setattr(watch, "classify", _explode)
    path = _registry(tmp_path)
    code, out = _run(
        capsys, "--providers", "--gate", "liveness", "--registry", str(path), "--fixture-dir", str(FIXTURES)
    )
    assert code == watch.EXIT_SOURCE
    assert "the checker failed" in out and "parser regression" in out


def test_non_200_response_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Response:
        status = 503

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    monkeypatch.setattr(watch.urllib.request, "urlopen", lambda *_a, **_k: _Response())
    path = _registry(tmp_path)
    code, out = _run(capsys, "--providers", "--registry", str(path))
    assert code == watch.EXIT_SOURCE
    assert "HTTP 503" in out


# --- report and CLI --------------------------------------------------------------


def test_report_runs_both_checks_and_exits_with_the_worse_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale_but_current = _registry(tmp_path, REGISTRY_ALL_CURRENT)
    code, out = _run(
        capsys,
        "--report",
        "--registry",
        str(stale_but_current),
        "--fixture-dir",
        str(FIXTURES),
        "--today",
        "2027-01-01",
    )
    assert code == watch.EXIT_FINDING
    assert "== Review age ==" in out and "== Providers (drift) ==" in out
    assert "FAIL: model_registry.toml last_reviewed=2026-09-08" in out
    assert "OK (drift)" in out

    fresh_but_behind = _registry(tmp_path)
    code, out = _run(
        capsys, "--report", "--registry", str(fresh_but_behind), "--fixture-dir", str(FIXTURES), "--today", "2026-09-08"
    )
    assert code == watch.EXIT_FINDING
    assert "OK: model_registry.toml last_reviewed=2026-09-08" in out
    assert "FINDINGS (drift)" in out


def test_report_exit_two_when_docs_are_unreadable_even_if_age_is_fine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _registry(tmp_path)
    code, out = _run(
        capsys, "--report", "--registry", str(path), "--fixture-dir", str(tmp_path / "absent"), "--today", "2026-09-08"
    )
    assert code == watch.EXIT_SOURCE
    assert "OK: model_registry.toml" in out
    assert "UNREADABLE:" in out


def test_report_is_exit_ok_when_both_gates_are_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _registry(tmp_path, REGISTRY_ALL_CURRENT)
    code, out = _run(
        capsys, "--report", "--registry", str(path), "--fixture-dir", str(FIXTURES), "--today", "2026-09-08"
    )
    assert code == watch.EXIT_OK
    assert "OK: model_registry.toml" in out and "OK (drift)" in out


def test_today_argument_rejects_an_invalid_date(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        watch.main(["--age", "--registry", str(_registry(tmp_path)), "--today", "not-a-date"])
    assert excinfo.value.code == 2


def test_a_mode_flag_is_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        watch.main(["--registry", str(_registry(tmp_path))])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("mode", ["--age", "--report"])
def test_gate_is_rejected_outside_providers(tmp_path: Path, mode: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        watch.main([mode, "--gate", "liveness", "--registry", str(_registry(tmp_path))])
    assert excinfo.value.code == 2


def test_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        watch.main(["--age", "--providers", "--registry", str(_registry(tmp_path))])
    assert excinfo.value.code == 2


def test_real_registry_carries_a_parseable_stamp_schema_only() -> None:
    """Never assert the live registry's age here: that is the cut's question, not every PR's."""
    raw = watch.load_registry(REPO_REGISTRY)
    assert isinstance(watch.parse_last_reviewed(raw["models"]), dt.date)
    assert len(watch.registry_entries(raw)) >= 5


@pytest.mark.parametrize(
    "text",
    [
        REGISTRY_PINNED_2026.replace('large = "gpt-5.5"', "large = 55"),
        (
            '[models]\ndefault_profile = "balanced"\nlast_reviewed = "2026-09-08"\n'
            'catalog = "broken"\n\n[tts.openai]\nmodel = "gpt-4o-mini-tts"\n'
        ),
        REGISTRY_PINNED_2026.replace('model = "gpt-4o-mini-tts"', "model = false"),
    ],
    ids=["catalog-entry", "catalog-table", "tts-model"],
)
def test_partially_malformed_registry_entries_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    path = _registry(tmp_path, text)
    code, out = _run(capsys, "--providers", "--registry", str(path), "--fixture-dir", str(FIXTURES))
    assert code == watch.EXIT_FINDING
    assert out.startswith("FAIL: model_registry.toml")


@pytest.mark.parametrize(
    "text",
    [
        '[models]\nlast_reviewed = "2026-09-08"\n\n[tts.openai]\nmodel = "gpt-4o-mini-tts"\n',
        REGISTRY_PINNED_2026.split("[tts.openai]", 1)[0],
    ],
    ids=["missing-catalog", "missing-tts"],
)
def test_missing_required_registry_model_sections_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    path = _registry(tmp_path, text)
    code, out = _run(capsys, "--providers", "--registry", str(path), "--fixture-dir", str(FIXTURES))
    assert code == watch.EXIT_FINDING
    assert out.startswith("FAIL: model_registry.toml")
