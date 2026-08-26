#!/usr/bin/env python3
"""Audit and lint human-facing product copy (Leadership Principle #5).

Surfaces: listener ui_copy, admin/listener templates, listener.js, HA addon
translations, Jamendo/First Listen operator tables, streamer setup errors.

Run:
  python3 scripts/ui_copy_lint.py --audit          # full report (step 1)
  python3 scripts/ui_copy_lint.py                  # fail on violations outside baseline
  python3 scripts/ui_copy_lint.py --write-baseline # refresh baseline after fixes
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / ".config" / "ui-copy-baseline.json"

TECH_LINGO_LISTENER = (
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

# Operator Jamendo sentences — aligned with test_jamendo_failure_code_contract.py
TECH_LINGO_ADMIN_HINTS = (
    "http",
    "ffmpeg",
    "ffprobe",
    "timeout",
    "null",
    "admission",
    "lease",
    "traceback",
    "exception",
    "undefined",
)

STALE_SPEAKER_PATTERNS = (
    re.compile(r"\bno ready speaker\b", re.I),
    re.compile(r"\bthat speaker is not available\b", re.I),
    re.compile(r"\bchoose one room\b", re.I),
    re.compile(r"\bconfirm that you heard the speaker\b", re.I),
    re.compile(r"\bfind speakers again\b", re.I),
    re.compile(r"\bchoose a speaker\b", re.I),
    re.compile(r"\bchoose another speaker\b", re.I),
    re.compile(r"\bstart the selected speaker\b", re.I),
    re.compile(r"\bmedia player currently reports\b", re.I),
    re.compile(r"\bbring a speaker online\b", re.I),
    re.compile(r"\bcompatible-looking speaker\b", re.I),
)

_WAY_OUT_RE = re.compile(
    r"(?:^|[.!?,;:—–]\s*|\b(?:and|or|then)\s+)(?:try|retry|check(?!\s+fail)|give|wait|press|tap|"
    r"open|skip|refresh|return|choose|repair|confirm|save|enable|add|enter|review|show|listen|start|"
    r"restore|keep|replace|clear|send|use|reload|install|turn|play|set|point|search|select|reconnect|rewrite|"
    r"paste|let|find)\b|\b(?:no action needed|retrying automatically|reappear on next refresh|will finish|"
    r"stays off|has to|needs? to|must(?!\s+have\b))\b|\b(?:riprova|ripassa|aspetta|controll\w*|scegli\w*|"
    r"prova|rimand\w*|tocca|lascia|scrivi|resta|incolla|installa|apri|salta|attiva|aggiungi|"
    r"inserisci|conferma|salva|ripristina)\b",
    re.IGNORECASE,
)

_FAILURE_TEXT_RE = re.compile(
    r"\b(?:fail(?:ed|ure)?|error|unable|unavailable|rejected|declined|could(?:n['’]t| not)|can(?:['’]t|not)|"
    r"did(?:n['’]t| not) (?:finish|work|start)|(?:connection|playback|request) (?:lost|stopped)|"
    r"lost (?:the )?connection|stopped unexpectedly)\b",
    re.IGNORECASE,
)
_FAILURE_KEY_RE = re.compile(
    r"(?:error|failed|declined|unavailable|not_|no_|lost|expired|required|rate_limited|queue_full)"
)

_BRAND_BAD = re.compile(r"mammami[-_ ]radio", re.I)
_BRAND_SQUASHED = re.compile(r"\bMammamiradio\b")

# Labels that are informational, not failures needing a fix step.
_INFO_COPY_OK = (
    "time unavailable",
    "not available in this build",
    "source details are unavailable",
    "source information unavailable",
    "starter catalog information is unavailable",
    "no music is playing right now",
    "no music track is on air right now",
    "the included collection is not available in this build",
    "source details are unavailable for this track",
    "al momento non c'è musica in onda",
    "la collezione inclusa non è disponibile in questa build",
    "i dettagli della fonte non sono disponibili per questo brano",
    "runtime status unavailable",
    "starter collection unavailable",
    "waiting",
    "on air now",
)


@dataclass(frozen=True)
class StringRef:
    file: str
    line: int
    text: str
    surface: str
    context: str = ""


@dataclass(frozen=True)
class Violation:
    rule: str
    file: str
    line: int
    text: str
    detail: str

    @property
    def fingerprint(self) -> str:
        payload = f"{self.rule}|{self.file}|{self.detail}|{self.text}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _line_no(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _extract_ui_copy() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "ui_copy.py"
    refs: list[StringRef] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "COPY"
        ),
        None,
    )
    if assignment is None or not isinstance(assignment.value, ast.Dict):
        return refs
    rel = path.relative_to(ROOT).as_posix()
    for language_node, copy_node in zip(assignment.value.keys, assignment.value.values, strict=True):
        if (
            not isinstance(language_node, ast.Constant)
            or not isinstance(language_node.value, str)
            or not isinstance(copy_node, ast.Dict)
        ):
            continue
        for key_node, text_node in zip(copy_node.keys, copy_node.values, strict=True):
            if key_node is None:
                continue
            try:
                key, text = ast.literal_eval(key_node), ast.literal_eval(text_node)
            except (ValueError, TypeError):
                continue
            if isinstance(key, str) and isinstance(text, str) and len(text) >= 8:
                refs.append(StringRef(rel, text_node.lineno, text, "listener", f"ui_copy:{language_node.value}:{key}"))
    return refs


def _extract_yaml_descriptions() -> list[StringRef]:
    refs: list[StringRef] = []
    for rel in (
        "ha-addon/mammamiradio/translations/en.yaml",
        "ha-addon/mammamiradio-edge/translations/en.yaml",
    ):
        path = ROOT / rel
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s+(name|description):\s*(.+)$", line)
            if not m:
                continue
            raw = m.group(2).strip().strip('"').strip("'")
            if raw:
                refs.append(StringRef(path.relative_to(ROOT).as_posix(), lineno, raw, "addon", "ha_option"))
    return refs


def _parse_js_object_entries(content: str, marker: str) -> list[tuple[int, str, dict[str, str]]]:
    """Return (line, key, {field: value}) for `key:{title:'..',message:'..',action:'..'}` rows."""
    start = content.find(marker)
    if start < 0:
        return []
    tail = content[start:]
    end = len(tail)
    for pat in (r"\nconst [A-Z_]", r"\nfunction ", r"\nlet _"):
        m = re.search(pat, tail[1:])
        if m:
            end = min(end, m.start() + 1)
    block = tail[:end]
    base_line = content.count("\n", 0, start) + 1
    entries: list[tuple[int, str, dict[str, str]]] = []
    row_re = re.compile(
        r"([a-z_]+):\{([^}]+)\}",
        re.DOTALL,
    )
    for match in row_re.finditer(block):
        key = match.group(1)
        body = match.group(2)
        fields: dict[str, str] = {}
        for fm in re.finditer(r"(title|message|action|label|detail):\s*['\"]([^'\"]+)['\"]", body):
            fields[fm.group(1)] = fm.group(2)
        line = base_line + block.count("\n", 0, match.start())
        entries.append((line, key, fields))
    return entries


_JS_STRING_LITERAL = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""


def _decode_js_string_literal(literal: str) -> str | None:
    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _parse_js_scalar_entries(content: str, marker: str) -> list[tuple[int, str, str]]:
    """Return (line, key, value) for ``key: 'sentence'`` object rows."""
    start = content.find(marker)
    if start < 0:
        return []
    tail = content[start:]
    end = len(tail)
    for pat in (r"\nconst [A-Z_]", r"\nfunction ", r"\nlet _"):
        match = re.search(pat, tail[1:])
        if match:
            end = min(end, match.start() + 1)
    block = tail[:end]
    base_line = content.count("\n", 0, start) + 1
    row_re = re.compile(rf"([a-z_]+)\s*:\s*({_JS_STRING_LITERAL})\s*,?")
    entries: list[tuple[int, str, str]] = []
    for match in row_re.finditer(block):
        value = _decode_js_string_literal(match.group(2))
        if value is None:
            continue
        line = base_line + block.count("\n", 0, match.start())
        entries.append((line, match.group(1), value))
    return entries


def _extract_admin_tables() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
    content = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    refs: list[StringRef] = []

    for line, key, fields in _parse_js_object_entries(content, "const FIRST_LISTEN_ERRORS="):
        combined = " ".join(fields.values())
        refs.append(StringRef(rel, line, combined, "admin", f"first_listen_error:{key}"))

    for line, key, text in _parse_js_scalar_entries(content, "const JAMENDO_ERROR_COPY="):
        refs.append(StringRef(rel, line, text, "admin", f"jamendo_form:{key}"))

    m = re.search(r"function jamendoFailureHint\(code\)\{.*?return \{([^}]+)\}", content, re.DOTALL)
    if m:
        body = m.group(1)
        base = content.count("\n", 0, m.start(1)) + 1
        for match in re.finditer(r"([a-z_]+):\s*\"((?:\\.|[^\"])*)\"", body):
            line = base + body.count("\n", 0, match.start())
            text = match.group(2).replace('\\"', '"')
            refs.append(StringRef(rel, line, text, "admin", f"jamendo_hint:{match.group(1)}"))
        for match in re.finditer(r"([a-z_]+):\s*'((?:\\.|[^'])*)'", body):
            line = base + body.count("\n", 0, match.start())
            text = match.group(2).replace("\\'", "'")
            refs.append(StringRef(rel, line, text, "admin", f"jamendo_hint:{match.group(1)}"))

    call_patterns = (
        (r"toast\(\s*['\"]([^'\"]{12,})['\"]", "toast"),
        (r"firstListenSetStatus\(\s*[^,]+,\s*['\"]([^'\"]{12,})['\"]", "first_listen_status"),
        (r"setJamendoFormMessage\(\s*['\"]([^'\"]{12,})['\"]", "jamendo_form_message"),
        (r"offlineMsg\(\)\s*\+\s*['\"]([^'\"]{12,})['\"]", "offline_suffix"),
    )
    for pattern, ctx in call_patterns:
        for match in re.finditer(pattern, content):
            refs.append(StringRef(rel, _line_no(content, match.start()), match.group(1), "admin", ctx))

    # Visible HTML labels (narrow: setup + first listen headings)
    for lineno, html_line in enumerate(content.splitlines(), 1):
        if "first-listen" in html_line or "setup-step" in html_line or 'class="setup-' in html_line:
            for match in re.finditer(r">([^<>{}\n][^<>{}]{7,})<", html_line):
                text = match.group(1).strip()
                if text and not text.startswith("{{"):
                    refs.append(StringRef(rel, lineno, text, "admin", "html"))
    return refs


def _extract_listener_js() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "static" / "listener.js"
    content = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    refs: list[StringRef] = []
    for match in re.finditer(rf"_t\(\s*({_JS_STRING_LITERAL})\s*,\s*({_JS_STRING_LITERAL})\s*,?\s*\)", content):
        key = _decode_js_string_literal(match.group(1))
        text = _decode_js_string_literal(match.group(2))
        if key and text and len(text) >= 8:
            refs.append(StringRef(rel, _line_no(content, match.start()), text, "listener", f"_t:{key}"))
    for pattern in (
        rf"_showToast\(\s*({_JS_STRING_LITERAL})",
        rf"\.textContent\s*=\s*({_JS_STRING_LITERAL})",
    ):
        for match in re.finditer(pattern, content):
            text = _decode_js_string_literal(match.group(1))
            if text:
                refs.append(StringRef(rel, _line_no(content, match.start()), text, "listener", "inline"))
    return refs


def _extract_listener_template() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "templates" / "listener.html"
    content = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()

    def _blank_preserving_lines(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    visible = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>",
        _blank_preserving_lines,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible = re.sub(r"<!--.*?-->", _blank_preserving_lines, visible, flags=re.DOTALL)

    refs: list[StringRef] = []
    for block_match in re.finditer(r"{{(.*?)}}|{%(.*?)%}", visible, flags=re.DOTALL):
        group = 1 if block_match.group(1) is not None else 2
        block = block_match.group(group)
        block_start = block_match.start(group)
        for literal_match in re.finditer(_JS_STRING_LITERAL, block):
            text = _decode_js_string_literal(literal_match.group(0))
            if text and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text) and not re.fullmatch(r"[a-z0-9_.:/-]+", text):
                refs.append(
                    StringRef(
                        rel,
                        _line_no(visible, block_start + literal_match.start()),
                        text,
                        "listener",
                        "listener_template",
                    )
                )
    for match in re.finditer(
        r"\b(?:aria-label|title|placeholder)\s*=\s*(['\"])(.*?)\1", visible, flags=re.IGNORECASE | re.DOTALL
    ):
        value = re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", " ", match.group(2), flags=re.DOTALL)
        text = html.unescape(" ".join(value.split()))
        if text and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
            refs.append(StringRef(rel, _line_no(visible, match.start()), text, "listener", "listener_template"))

    visible = re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", _blank_preserving_lines, visible, flags=re.DOTALL)
    visible = re.sub(r"<[^>]*>", _blank_preserving_lines, visible, flags=re.DOTALL)

    for lineno, line in enumerate(visible.splitlines(), 1):
        text = html.unescape(line)
        text = " ".join(text.split())
        if text and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
            refs.append(StringRef(rel, lineno, text, "listener", "listener_template"))
    return refs


def _extract_streamer_setup_errors() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "streamer.py"
    content = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    refs: list[StringRef] = []
    for node in ast.parse(content).body:
        if not (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in {"_SETUP_ERRORS", "_SETUP_ERRORS_STANDALONE"}
            and isinstance(node.value, ast.Dict)
        ):
            continue
        for key_node, entry_node in zip(node.value.keys, node.value.values, strict=True):
            if key_node is None:
                continue
            try:
                key, entry = ast.literal_eval(key_node), ast.literal_eval(entry_node)
            except (ValueError, TypeError):
                continue
            if not isinstance(key, str) or not isinstance(entry, tuple) or len(entry) != 5:
                continue
            title, message, _retryable, action, _status = entry
            if all(isinstance(value, str) for value in (title, message, action)):
                text = ". ".join(value.rstrip(".") for value in (title, message, action))
                refs.append(StringRef(rel, entry_node.lineno, text, "server", f"setup_error:{key}"))
    return refs


def collect_strings() -> list[StringRef]:
    refs: list[StringRef] = []
    refs.extend(_extract_ui_copy())
    refs.extend(_extract_yaml_descriptions())
    refs.extend(_extract_admin_tables())
    refs.extend(_extract_listener_template())
    refs.extend(_extract_listener_js())
    refs.extend(_extract_streamer_setup_errors())
    seen: set[tuple[str, int, str, str]] = set()
    unique: list[StringRef] = []
    for ref in refs:
        key = (ref.file, ref.line, ref.text, ref.context)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _has_tech_lingo(text: str, surface: str, context: str) -> str | None:
    low = re.sub(r"(?:https?://|www\.w3\.org)\S+", "", text.lower())
    terms = TECH_LINGO_LISTENER if surface == "listener" else TECH_LINGO_ADMIN_HINTS
    if context.startswith("jamendo_hint"):
        terms = TECH_LINGO_ADMIN_HINTS
    for term in terms:
        if term.isdigit():
            if re.search(
                rf"\b(?:http|status|error|code)\s*(?:code\s*)?[:#-]?\s*{term}\b|\b{term}\s+(?:error|status|response|code)\b",
                low,
            ):
                return term
        elif re.search(rf"(?<!\w){re.escape(term)}(?!\w)", low):
            return term
    return None


def _has_way_out(text: str) -> bool:
    return _WAY_OUT_RE.search(text) is not None


def _is_failure_copy(ref: StringRef) -> bool:
    if ref.context.startswith(("first_listen_error", "setup_error", "jamendo_hint", "jamendo_form:")):
        return True
    if ref.context.startswith(("ui_copy:", "_t:")) and _FAILURE_KEY_RE.search(ref.context.rsplit(":", 1)[-1]):
        return True
    return (
        ref.context
        in {
            "toast",
            "first_listen_status",
            "jamendo_form_message",
            "offline_suffix",
            "inline",
            "listener_template",
        }
        and _FAILURE_TEXT_RE.search(ref.text) is not None
    )


def _is_informational_copy(text: str) -> bool:
    normalized = " ".join(text.lower().split()).strip(" .…")
    return normalized in _INFO_COPY_OK


def check_strings(refs: list[StringRef]) -> list[Violation]:
    violations: list[Violation] = []
    for ref in refs:
        informational = _is_informational_copy(ref.text)

        term = _has_tech_lingo(ref.text, ref.surface, ref.context)
        if term:
            violations.append(Violation("tech_lingo", ref.file, ref.line, ref.text, f"term={term!r} ctx={ref.context}"))

        for pat in STALE_SPEAKER_PATTERNS:
            if pat.search(ref.text):
                violations.append(
                    Violation("stale_speaker_copy", ref.file, ref.line, ref.text, f"pattern={pat.pattern}")
                )
                break

        if not informational and _is_failure_copy(ref) and not _has_way_out(ref.text):
            violations.append(Violation("no_way_out", ref.file, ref.line, ref.text, f"dead-end {ref.context}"))

        if _BRAND_BAD.search(ref.text) or _BRAND_SQUASHED.search(ref.text):
            violations.append(Violation("brand_misspell", ref.file, ref.line, ref.text, "use Mamma Mi Radio"))

    return violations


def load_baseline() -> Counter[str] | None:
    if not BASELINE_PATH.is_file():
        return None
    return Counter(json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get("fingerprints", []))


def _compare_to_baseline(violations: list[Violation], baseline: Counter[str]) -> tuple[list[Violation], int]:
    remaining = baseline.copy()
    new: list[Violation] = []
    for violation in violations:
        fingerprint = violation.fingerprint
        if remaining[fingerprint] > 0:
            remaining[fingerprint] -= 1
        else:
            new.append(violation)
    return new, sum(remaining.values())


def write_baseline(violations: list[Violation]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "description": "Known UI copy violations baselined until fixed. CI fails on new fingerprints only.",
        "fingerprints": sorted(v.fingerprint for v in violations),
        "violations": [asdict(v) for v in violations],
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def print_audit(violations: list[Violation], refs: list[StringRef]) -> None:
    by_rule: dict[str, list[Violation]] = {}
    for v in violations:
        by_rule.setdefault(v.rule, []).append(v)
    print(f"Scanned {len(refs)} curated human-facing strings across {len({r.file for r in refs})} files.")
    print(f"Found {len(violations)} violations in {len(by_rule)} rule classes.\n")
    for rule in sorted(by_rule):
        items = by_rule[rule]
        print(f"## {rule} ({len(items)})")
        for v in items:
            print(f"  {v.file}:{v.line}  [{v.detail}]")
            print(f"    {v.text[:120]}{'…' if len(v.text) > 120 else ''}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit/lint human-facing product copy.")
    parser.add_argument("--audit", action="store_true", help="Print full violation report.")
    parser.add_argument("--write-baseline", action="store_true", help="Write baseline from current violations.")
    args = parser.parse_args()

    refs = collect_strings()
    violations = check_strings(refs)

    if args.write_baseline:
        write_baseline(violations)
        print(f"Wrote {len(violations)} violations to {BASELINE_PATH.relative_to(ROOT)}")
        return 0

    if args.audit:
        print_audit(violations, refs)
        return 0

    baseline = load_baseline()
    if baseline is None:
        print(
            f"No baseline at {BASELINE_PATH.relative_to(ROOT)}. Run with --audit first, then --write-baseline.",
            file=sys.stderr,
        )
        return 2

    new, fixed = _compare_to_baseline(violations, baseline)
    if new:
        print(f"FAIL: {len(new)} new UI copy violation(s) outside baseline:", file=sys.stderr)
        for v in new:
            print(f"  {v.file}:{v.line} [{v.rule}] {v.text[:100]}", file=sys.stderr)
        return 1
    if fixed:
        print(
            f"NOTE: {fixed} baselined violation(s) appear fixed — run --write-baseline to shrink baseline.",
            file=sys.stderr,
        )
    print(f"UI copy lint clean ({len(violations)} known violations baselined).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
