#!/usr/bin/env python3
"""Model registry watch: is model_registry.toml decided lately, and are its pins alive?

Two questions, answered without provider API keys:

  --age        Did a maintainer decide the pins recently? Reads [models].last_reviewed
               (a YYYY-MM-DD date) and fails when it is missing, malformed, more than a
               day in the future, or older than MAX_AGE_DAYS. Offline.
  --providers  Are the pinned model IDs current, and are they still alive? Reads the
               public provider docs (Anthropic models overview + per-ID lifecycle table;
               OpenAI models page + deprecation tables) and classifies every catalog
               entry as current | legacy | unlisted | deprecated(<date>) | retired.
               --gate drift (default) fails on anything not current; --gate liveness
               fails only on deprecated / retired.
  --report     Both, unconditionally, in one combined report; exit = max of the two.

Exit codes: 0 nothing to report under the chosen gate; 1 a finding; 2 the provider docs
could not be read, lost the anchors this parser relies on, or the checker itself failed.
Exit 2 is deliberate: a broken scraper must look broken, never green.

Where it runs: scripts/pre-release-check.sh section 11 (release cuts and `make
pre-release`) runs --age and --providers --gate liveness; a weekly workflow runs
--report. It is never an every-PR pytest assertion: a calendar gate on unrelated PRs
gets bumped reflexively.

Parsing is deliberately dumb and anchored: tables are read by their own header row,
never by position on the page. Fixtures for offline tests are trimmed real captures
under tests/scripts/fixtures/model_registry/ (--fixture-dir).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import http.client
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

MAX_AGE_DAYS = 45
FUTURE_TOLERANCE_DAYS = 1  # a stamp written after UTC midnight from a later timezone is not "the future"
FETCH_TIMEOUT_SECONDS = 10.0
FETCH_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 2.0
USER_AGENT = "mammamiradio-model-registry-watch/1 (+https://github.com/florianhorner/mammamiradio)"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "model_registry.toml"

SOURCES: dict[str, str] = {
    "anthropic_overview": "https://platform.claude.com/docs/en/models/overview.md",
    "anthropic_deprecations": "https://platform.claude.com/docs/en/about-claude/model-deprecations.md",
    "openai_models": "https://developers.openai.com/api/docs/models",
    "openai_deprecations": "https://developers.openai.com/api/docs/deprecations",
}
FIXTURE_FILES: dict[str, str] = {
    "anthropic_overview": "anthropic-overview.md",
    "anthropic_deprecations": "anthropic-deprecations.md",
    "openai_models": "openai-models.html",
    "openai_deprecations": "openai-deprecations.md",
}

GATES = ("drift", "liveness")
LIVENESS_STATUSES = frozenset({"deprecated", "retired"})
ANTHROPIC_STATES = frozenset({"active", "legacy", "deprecated", "retired"})

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_SOURCE = 2

WAY_OUT = (
    "Run `python scripts/check_model_registry.py --providers`, decide each pin, "
    "then set last_reviewed to today and say why in the comment."
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")
_DASHES = re.compile(r"[‐-―−]")
_ORDINAL = re.compile(r"(\d)(?:st|nd|rd|th)\b")
_BACKTICKED = re.compile(r"`([^`]+)`")
_LEGACY_SLUG = re.compile(r"/models/([a-z0-9\-]+)/overview")
_GPT_TOKEN = re.compile(r"(?<![A-Za-z0-9.\-])gpt-[0-9A-Za-z.\-]+")
_IMAGE_SUFFIX = re.compile(r"\.(?:png|svg|jpe?g|webp|gif)$", re.IGNORECASE)
_TEXT_MODEL = re.compile(r"^gpt-\d+(?:\.\d+)?(?:-[a-z]+)?$")
_HTML_TABLE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_TABLE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_MODEL_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")
_SEPARATOR_CELL = re.compile(r":?-{2,}:?")
_NOISE_TOKENS = frozenset({"or", "and", "n", "a"})


class RegistryError(Exception):
    """model_registry.toml is missing or schema-invalid (exit 1)."""


class SourceError(Exception):
    """A registry or provider source is unreadable or missing an anchor (exit 2)."""


@dataclass(frozen=True)
class Entry:
    label: str
    provider: str
    model_id: str


@dataclass(frozen=True)
class Verdict:
    entry: Entry
    status: str
    detail: str = ""


@dataclass(frozen=True)
class AnthropicRow:
    state: str
    retirement: str


@dataclass(frozen=True)
class Shutdown:
    """An announced OpenAI shutdown; `date` is None when the docs give no readable date."""

    date: dt.date | None
    text: str


@dataclass(frozen=True)
class ProviderDocs:
    anthropic_current: frozenset[str]
    anthropic_legacy_slugs: tuple[str, ...]
    anthropic_rows: dict[str, AnthropicRow]
    openai_models: frozenset[str]
    openai_shutdowns: dict[str, Shutdown]


# --- registry -----------------------------------------------------------------


def load_registry(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RegistryError(f"{path} not found") from exc
    except OSError as exc:
        raise SourceError(f"{path} could not be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SourceError(f"{path} could not be parsed: {exc}") from exc


def registry_entries(raw: dict) -> list[Entry]:
    entries: list[Entry] = []
    models = raw.get("models")
    catalog = models.get("catalog") if isinstance(models, dict) else None
    if isinstance(catalog, dict):
        for provider, keys in catalog.items():
            if not isinstance(keys, dict):
                continue
            for key, model_id in keys.items():
                if isinstance(model_id, str) and model_id.strip():
                    entries.append(Entry(f"{provider}.{key}", str(provider), model_id.strip()))
    tts = raw.get("tts")
    if isinstance(tts, dict):
        for provider, section in tts.items():
            model_id = section.get("model") if isinstance(section, dict) else None
            if isinstance(model_id, str) and model_id.strip():
                entries.append(Entry(f"tts.{provider}", str(provider), model_id.strip()))
    if not entries:
        raise RegistryError("model_registry.toml names no models under [models.catalog] or [tts]")
    return entries


def parse_last_reviewed(models: dict) -> dt.date:
    value = models.get("last_reviewed")
    if value is None:
        raise RegistryError("[models].last_reviewed is missing")
    # datetime subclasses date: a timestamp is not a review day.
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise RegistryError(f"[models].last_reviewed={value!r} is not a YYYY-MM-DD date")


def check_age(raw: dict, today: dt.date) -> tuple[bool, str]:
    models = raw.get("models")
    if not isinstance(models, dict):
        return False, f"model_registry.toml has no [models] table. {WAY_OUT}"
    try:
        reviewed = parse_last_reviewed(models)
    except RegistryError as exc:
        return False, f"model_registry.toml {exc}. {WAY_OUT}"
    if reviewed > today + dt.timedelta(days=FUTURE_TOLERANCE_DAYS):
        return False, (
            f"model_registry.toml last_reviewed={reviewed} is in the future (today is {today} UTC). {WAY_OUT}"
        )
    age = max((today - reviewed).days, 0)
    if age > MAX_AGE_DAYS:
        return False, (
            f"model_registry.toml last_reviewed={reviewed} is {age} days old (max {MAX_AGE_DAYS}). {WAY_OUT}"
        )
    return True, f"model_registry.toml last_reviewed={reviewed} is {age} days old (max {MAX_AGE_DAYS})."


# --- provider docs ------------------------------------------------------------


def _normalize_cell(text: str) -> str:
    """Docs text with entities decoded, exotic dashes and spaces folded, whitespace collapsed."""
    text = html.unescape(_TAG.sub(" ", text)).replace("\xa0", " ")
    text = _DASHES.sub("-", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_docs_date(text: str) -> dt.date | None:
    """A calendar date from the docs, or None for floors and prose like 'Not sooner than ...'."""
    cleaned = _normalize_cell(text).strip("* ").replace("Sept ", "Sep ")
    cleaned = _ORDINAL.sub(r"\1", cleaned)
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _split_pipe_row(line: str) -> list[str]:
    r"""Cells of one markdown table row; `\|` inside a cell is a literal pipe."""
    return [cell.replace("\\|", "|").strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _is_separator(cells: list[str]) -> bool:
    return all(_SEPARATOR_CELL.fullmatch(cell) for cell in cells if cell) and any(cells)


def _markdown_tables(text: str) -> list[list[list[str]]]:
    """Every markdown pipe table on the page as rows of cells, header row first, separators dropped."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            cells = _split_pipe_row(line)
            if not _is_separator(cells):
                current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _html_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for table in _HTML_TABLE.findall(text):
        rows = [[_normalize_cell(cell) for cell in _TABLE_CELL.findall(row)] for row in _TABLE_ROW.findall(table)]
        rows = [row for row in rows if row]
        if rows:
            tables.append(rows)
    return tables


def parse_anthropic_overview(text: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """The current lineup (first `| Claude API ID |` row) and the slugs of the legacy line."""
    current: set[str] = set()
    legacy_slugs: tuple[str, ...] = ()
    for line in text.splitlines():
        stripped = line.strip()
        if not current and stripped.startswith("| Claude API ID"):
            current.update(token.strip() for token in _BACKTICKED.findall(stripped))
        elif stripped.startswith("Legacy models"):
            legacy_slugs = tuple(_LEGACY_SLUG.findall(stripped))
    if not current:
        raise SourceError("Anthropic models overview: the `| Claude API ID |` row is missing")
    return frozenset(current), legacy_slugs


def parse_anthropic_deprecations(text: str) -> dict[str, AnthropicRow]:
    """Lifecycle state per model ID from the 'Model status' table, read by its header names."""
    lines = text.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith("|") and "API model name" in line and "Current state" in line
        ),
        None,
    )
    if header_index is None:
        raise SourceError("Anthropic deprecations: the 'Model status' table header is missing")
    header = [cell.lower() for cell in _split_pipe_row(lines[header_index])]
    columns: dict[str, int] = {}
    for name, key in (("model", "api model name"), ("state", "current state"), ("retirement", "retirement")):
        index = next((position for position, cell in enumerate(header) if key in cell), None)
        if index is None:
            raise SourceError("Anthropic deprecations: the 'Model status' table lost a column this parser reads")
        columns[name] = index
    rows: dict[str, AnthropicRow] = {}
    for line in lines[header_index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            continue
        cells = _split_pipe_row(stripped)
        if len(cells) <= max(columns.values()) or _is_separator(cells):
            continue
        state_text = cells[columns["state"]]
        state = state_text.lower()
        retirement = cells[columns["retirement"]]
        for token in re.split(r"[,\s|]+", cells[columns["model"]]):
            model_id = token.strip("`")
            if not model_id:
                continue
            if state not in ANTHROPIC_STATES:
                raise SourceError(f"Anthropic deprecations: unknown lifecycle state {state_text!r} for {model_id}")
            rows[model_id] = AnthropicRow(state=state, retirement=retirement)
    if not rows:
        raise SourceError("Anthropic deprecations: the 'Model status' table has no rows")
    return rows


def parse_openai_models(text: str) -> frozenset[str]:
    """Every gpt-* ID the models page mentions, in visible text or in link/image attributes.

    The page is HTML (it does not negotiate to markdown) and lists some models, the TTS
    and transcription ones among them, only as link targets and icon names, so the scan
    runs over the raw markup. Image file names are dropped; an ID that appears anywhere
    on the page counts as listed, which errs toward `current`, never toward a false alarm.
    """
    tokens: set[str] = set()
    for raw_token in _GPT_TOKEN.findall(text):
        token = raw_token.rstrip(".-")
        if token and not _IMAGE_SUFFIX.search(token):
            tokens.add(token)
    if not tokens:
        raise SourceError("OpenAI models page: no gpt-* model IDs found")
    return frozenset(tokens)


def parse_openai_deprecations(text: str) -> dict[str, Shutdown]:
    """Announced shutdown per model ID, read from every table that has both a
    'Shutdown date' column and a model column, by that table's own header.

    The page serves markdown pipe tables for text/markdown and HTML tables otherwise;
    both shapes are read, so a change in negotiation cannot blind the check. Tables
    without both columns (release-note tables, update logs) are ignored, so a date
    in some other table can never stamp a model. A row whose date cell is not a
    calendar date still counts as an announced shutdown with an unreadable date.
    """
    shutdowns: dict[str, Shutdown] = {}
    saw_table = False
    tables = _markdown_tables(text)
    if "<table" in text.lower():
        tables += _html_tables(text)
    for table in tables:
        header = [cell.lower() for cell in table[0]]
        date_col = next((index for index, cell in enumerate(header) if "shutdown date" in cell), None)
        model_col = next(
            (
                index
                for index, cell in enumerate(header)
                if any(word in cell for word in ("model", "system", "snapshot"))
            ),
            None,
        )
        if date_col is None or model_col is None or date_col == model_col:
            continue
        saw_table = True
        for cells in table[1:]:
            if len(cells) <= max(date_col, model_col):
                continue
            shutdown = Shutdown(date=parse_docs_date(cells[date_col]), text=cells[date_col])
            for raw_token in _MODEL_ID_TOKEN.findall(cells[model_col]):
                token = raw_token.rstrip(".")
                if not token or token.lower() in _NOISE_TOKENS or not re.search(r"[A-Za-z]", token):
                    continue
                known = shutdowns.get(token)  # keep the earliest readable date
                if known is None or (shutdown.date is not None and (known.date is None or shutdown.date < known.date)):
                    shutdowns[token] = shutdown
    if not saw_table:
        raise SourceError("OpenAI deprecations page: no table with 'Shutdown date' and model columns found")
    return shutdowns


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/markdown, text/html;q=0.9, */*;q=0.5"},
    )
    last_error: Exception | None = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise SourceError(f"could not read {url}: HTTP {status}")
                return response.read().decode("utf-8", errors="replace")
        except SourceError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            last_error = exc
            if attempt < FETCH_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)
    raise SourceError(f"could not read {url} after {FETCH_ATTEMPTS} attempts: {last_error}") from last_error


def load_docs(fixture_dir: Path | None) -> ProviderDocs:
    texts: dict[str, str] = {}
    if fixture_dir is None:
        # The four pages are independent; the wall clock is the slowest source, not the sum.
        with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            texts = dict(zip(SOURCES, pool.map(fetch_text, SOURCES.values()), strict=True))
    else:
        for key in SOURCES:
            fixture = fixture_dir / FIXTURE_FILES[key]
            try:
                texts[key] = fixture.read_text(encoding="utf-8")
            except OSError as exc:
                raise SourceError(f"fixture {fixture} unreadable: {exc}") from exc
    current, legacy_slugs = parse_anthropic_overview(texts["anthropic_overview"])
    return ProviderDocs(
        anthropic_current=current,
        anthropic_legacy_slugs=legacy_slugs,
        anthropic_rows=parse_anthropic_deprecations(texts["anthropic_deprecations"]),
        openai_models=parse_openai_models(texts["openai_models"]),
        openai_shutdowns=parse_openai_deprecations(texts["openai_deprecations"]),
    )


# --- classification -----------------------------------------------------------


def _matches_slug(model_id: str, slug: str) -> bool:
    return model_id == f"claude-{slug}" or re.fullmatch(rf"claude-{re.escape(slug)}-\d{{8}}", model_id) is not None


def _current_for_family(current: frozenset[str], model_id: str) -> str:
    match = re.match(r"claude-([a-z]+)", model_id)
    if match is None:
        return ""
    family = match.group(1)
    candidates = sorted(candidate for candidate in current if candidate.startswith(f"claude-{family}-"))
    return candidates[0] if candidates else ""


def classify_anthropic(entry: Entry, docs: ProviderDocs) -> Verdict:
    row = docs.anthropic_rows.get(entry.model_id)
    if row is not None and row.state == "retired":
        return Verdict(entry, "retired", f"retired {row.retirement}")
    if row is not None and row.state == "deprecated":
        return Verdict(entry, "deprecated", f"retires {row.retirement}")
    if entry.model_id in docs.anthropic_current:
        return Verdict(entry, "current")
    suggestion = _current_for_family(docs.anthropic_current, entry.model_id)
    hint = f"(current: {suggestion})" if suggestion else "(not on the models overview)"
    is_legacy = (row is not None and row.state == "legacy") or any(
        _matches_slug(entry.model_id, slug) for slug in docs.anthropic_legacy_slugs
    )
    return Verdict(entry, "legacy" if is_legacy else "unlisted", hint)


def classify_openai(entry: Entry, docs: ProviderDocs, today: dt.date) -> Verdict:
    shutdown = docs.openai_shutdowns.get(entry.model_id)
    if shutdown is not None:
        if shutdown.date is None:
            return Verdict(entry, "deprecated", f"shutdown announced, date unreadable: {shutdown.text!r}")
        if shutdown.date <= today:
            return Verdict(entry, "retired", f"shut down {shutdown.date.isoformat()}")
        return Verdict(entry, "deprecated", f"shuts down {shutdown.date.isoformat()}")
    if entry.model_id in docs.openai_models:
        return Verdict(entry, "current")
    lineup = sorted((token for token in docs.openai_models if _TEXT_MODEL.match(token)), reverse=True)[:6]
    hint = f"(not on the models page; text lineup: {', '.join(lineup)})" if lineup else "(not on the models page)"
    return Verdict(entry, "unlisted", hint)


def classify(entries: list[Entry], docs: ProviderDocs, today: dt.date) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for entry in entries:
        if entry.provider == "anthropic":
            verdicts.append(classify_anthropic(entry, docs))
        elif entry.provider == "openai":
            verdicts.append(classify_openai(entry, docs, today))
        else:
            verdicts.append(Verdict(entry, "unlisted", f"(no docs source for provider {entry.provider!r})"))
    return verdicts


def format_verdict(verdict: Verdict) -> str:
    return f"{verdict.entry.label:<18} {verdict.entry.model_id:<28} {verdict.status:<11} {verdict.detail}".rstrip()


# --- modes --------------------------------------------------------------------


def run_age(raw: dict, today: dt.date) -> int:
    ok, message = check_age(raw, today)
    print(("OK: " if ok else "FAIL: ") + message)
    return EXIT_OK if ok else EXIT_FINDING


def run_providers(raw: dict, today: dt.date, gate: str, fixture_dir: Path | None) -> int:
    try:
        entries = registry_entries(raw)
    except RegistryError as exc:
        print(f"FAIL: {exc}")
        return EXIT_FINDING
    try:
        docs = load_docs(fixture_dir)
    except SourceError as exc:
        print(f"UNREADABLE: {exc}")
        return EXIT_SOURCE
    verdicts = classify(entries, docs, today)
    for verdict in verdicts:
        print(format_verdict(verdict))
    if gate == "liveness":
        flagged = [verdict for verdict in verdicts if verdict.status in LIVENESS_STATUSES]
    else:
        flagged = [verdict for verdict in verdicts if verdict.status != "current"]
    if flagged:
        names = ", ".join(verdict.entry.label for verdict in flagged)
        print(f"FINDINGS ({gate}): {len(flagged)} of {len(verdicts)} pinned models: {names}")
        return EXIT_FINDING
    print(f"OK ({gate}): all {len(verdicts)} pinned models are {'alive' if gate == 'liveness' else 'current'}")
    return EXIT_OK


def run_report(raw: dict, today: dt.date, fixture_dir: Path | None) -> int:
    print("== Review age ==")
    age_code = run_age(raw, today)
    print("")
    print("== Providers (drift) ==")
    providers_code = run_providers(raw, today, "drift", fixture_dir)
    return max(age_code, providers_code)


def _parse_iso_argument(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_model_registry.py",
        description=(
            "Model registry watch: is model_registry.toml decided lately (--age), are its pinned "
            "models current and alive at the providers (--providers), or both (--report)."
        ),
        epilog=(
            "Exit codes: 0 nothing to report under the chosen gate, 1 a finding, "
            "2 provider docs unreadable, missing the anchors this parser relies on, or a checker failure."
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--age",
        action="store_true",
        help=f"fail when last_reviewed is missing, malformed, future, or older than {MAX_AGE_DAYS} days",
    )
    mode.add_argument(
        "--providers", action="store_true", help="classify every pinned model against the public provider docs"
    )
    mode.add_argument(
        "--report", action="store_true", help="run --age and --providers (drift) unconditionally; exit = max"
    )
    parser.add_argument(
        "--gate",
        choices=GATES,
        default=None,
        help="with --providers only. drift (default): anything not current fails; liveness: only deprecated/retired",
    )
    parser.add_argument(
        "--registry", type=Path, default=DEFAULT_REGISTRY, help="path to model_registry.toml (default: repo root)"
    )
    parser.add_argument(
        "--fixture-dir", type=Path, default=None, help="read provider docs from this directory instead of the network"
    )
    parser.add_argument(
        "--today", type=_parse_iso_argument, default=None, help="pretend today is this YYYY-MM-DD date (tests)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gate is not None and not args.providers:
        parser.error("--gate applies to --providers only; --report always reports drift")
    today = args.today or dt.datetime.now(dt.UTC).date()
    try:
        raw = load_registry(args.registry)
    except SourceError as exc:
        print(f"UNREADABLE: {exc}")
        return EXIT_SOURCE
    except RegistryError as exc:
        print(f"FAIL: {exc}")
        return EXIT_FINDING
    try:
        if args.age:
            return run_age(raw, today)
        if args.providers:
            return run_providers(raw, today, args.gate or "drift", args.fixture_dir)
        return run_report(raw, today, args.fixture_dir)
    except Exception as exc:  # a checker bug must read as "not verified", never as a dead or alive pin
        print(f"UNREADABLE: the checker failed: {exc!r}")
        return EXIT_SOURCE


if __name__ == "__main__":
    sys.exit(main())
