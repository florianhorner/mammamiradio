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
from collections.abc import Iterator
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

# Principle #5 holds admin/addon/server copy to the same no-lingo standard as the
# listener — a warmer register, not a shorter banned list — so these are the
# machine words specific to operator surfaces, ON TOP of every listener term.
# The operator-specific half is aligned with test_jamendo_failure_code_contract.py.
TECH_LINGO_ADMIN_ONLY = (
    "http",
    "ffmpeg",
    "ffprobe",
    "admission",
    "lease",
)
TECH_LINGO_ADMIN_HINTS = TECH_LINGO_LISTENER + TECH_LINGO_ADMIN_ONLY

# First Listen used to ask the operator to pick a speaker or a room; the flow now
# plays on "this device". Copy still phrased around choosing/checking a speaker is
# stale — it describes a step the product no longer has, so it sends the reader
# looking for a control that is not on screen.
#
# Sibling guard: tests/web/test_admin_first_listen.py::
# test_first_listen_step_copy_names_this_device_not_a_speaker_or_room asserts a
# handful of these phrases are ABSENT from the rendered First Listen step. This
# rule is the wider net over every scanned surface; the phrases below are still
# present elsewhere and are grandfathered in the baseline until the copy is
# rewritten. If you rewrite one, drop its phrase here too.
_STALE_SPEAKER_PHRASES = (
    "no ready speaker",
    "that speaker is not available",
    "choose one room",
    "confirm that you heard the speaker",
    "find speakers again",
    "choose a speaker",
    "choose another speaker",
    "start the selected speaker",
    "media player currently reports",
    "bring a speaker online",
    "compatible-looking speaker",
)
STALE_SPEAKER_PATTERNS = tuple(re.compile(rf"\b{phrase}\b", re.I) for phrase in _STALE_SPEAKER_PHRASES)

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

# Admin helpers whose whole job is producing failure copy.
ADMIN_COPY_FUNCTIONS = ("wayOut", "offlineMsg", "metadataOnlyMutationCopy", "transportFailureCopy")

_URL_RE = re.compile(r"(?:https?://|www\.w3\.org)\S+")


def _compile_lingo(terms: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Pair each banned term with its matcher; digits only count in a status-code context."""
    matchers: list[tuple[str, re.Pattern[str]]] = []
    for term in terms:
        if term.isdigit():
            pattern = (
                rf"\b(?:http|status|error|code)\s*(?:code\s*)?[:#-]?\s*{term}\b"
                rf"|\b{term}\s+(?:error|status|response|code)\b"
            )
        else:
            # Inflections count: "Buffering" is the machine word "buffer" wearing a suffix.
            pattern = rf"(?<!\w){re.escape(term)}(?:s|es|ed|ing)?(?!\w)"
        matchers.append((term, re.compile(pattern)))
    return tuple(matchers)


_LINGO_LISTENER = _compile_lingo(TECH_LINGO_LISTENER)
_LINGO_ADMIN = _compile_lingo(TECH_LINGO_ADMIN_HINTS)

# Labels that are informational, not failures needing a fix step.
_INFO_COPY_OK = frozenset(
    {
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
    }
)


# Coverage floors, one per extractor group (the `_MIN_BRAND_SCAN_FILES` idea from
# tests/web/test_ui_copy.py). Without these the lint fails OPEN: an extractor that
# stops matching after a template reformat collects nothing, finds no violations,
# and reports "clean". Set below today's counts so deleting copy is fine, but a
# collapsed extractor is not. Raise a floor when a group grows a lot.
MIN_STRINGS_PER_GROUP = {
    "ui_copy": 120,
    "listener_template": 50,
    "ha_option": 48,
    "html": 40,
    "_t": 45,
    "toast": 20,
    "jamendo_hint": 15,
    "setup_error": 15,
    "first_listen_error": 14,
    "first_listen_status": 8,
    "jamendo_form": 5,
    "admin_copy": 3,
    "offline_suffix": 2,
    "inline": 2,
    "admin_helper": 2,
    "jamendo_form_message": 2,
}


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
        payload = f"{self.rule}|{self.file}|{self.text}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _line_no(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _annotated_dicts(tree: ast.Module, names: set[str]) -> Iterator[ast.Dict]:
    """Yield the dict literal of every top-level ``NAME: T = {...}`` assignment in ``names``."""
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in names
            and isinstance(node.value, ast.Dict)
        ):
            yield node.value


def _dict_rows(node: ast.Dict) -> Iterator[tuple[str, ast.expr]]:
    """Yield (string key, unevaluated value node) for each row of a dict literal."""
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            continue
        try:
            key = ast.literal_eval(key_node)
        except (ValueError, TypeError):
            continue
        if isinstance(key, str):
            yield key, value_node


def _extract_ui_copy() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "ui_copy.py"
    rel = path.relative_to(ROOT).as_posix()
    refs: list[StringRef] = []
    for copy_table in _annotated_dicts(ast.parse(path.read_text(encoding="utf-8")), {"COPY"}):
        for language, language_node in _dict_rows(copy_table):
            if not isinstance(language_node, ast.Dict):
                continue
            for key, text_node in _dict_rows(language_node):
                try:
                    text = ast.literal_eval(text_node)
                except (ValueError, TypeError):
                    continue
                if isinstance(text, str) and len(text) >= 8:
                    refs.append(StringRef(rel, text_node.lineno, text, "listener", f"ui_copy:{language}:{key}"))
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


_JS_STRING_LITERAL = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""

_JS_TEMPLATE_LITERAL = r"`(?:\\.|[^`\\])*`"
_JS_INTERPOLATION_RE = re.compile(r"\$\{[^{}]*\}")
_JS_COPY_LITERAL_RE = re.compile(rf"{_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL}")
_JS_OBJECT_ROW_RE = re.compile(r"([a-z_]+)\s*:\s*\{([^{}]*)\}")
_JS_OBJECT_FIELD_RE = re.compile(r"(title|message|action|label|detail)\s*:\s*['\"]([^'\"]+)['\"]")
_JS_SCALAR_ROW_RE = re.compile(rf"([a-z_]+)\s*:\s*({_JS_STRING_LITERAL})\s*,?")


def _decode_js_string_literal(literal: str) -> str | None:
    try:
        value = ast.literal_eval(literal)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _decode_js_copy_literal(literal: str) -> str | None:
    """Decode a quoted or backtick literal; ``${...}`` holes collapse to whitespace."""
    if literal.startswith("`"):
        return " ".join(_JS_INTERPOLATION_RE.sub(" ", literal[1:-1]).split())
    return _decode_js_string_literal(literal)


def _is_prose(text: str) -> bool:
    """True for a human sentence, false for a URL, path, CSS selector or bare token."""
    stripped = text.strip()
    if " " not in stripped or not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", stripped):
        return False
    return not re.match(r"https?://|[/#.{]", stripped)


def _matching_brace(content: str, open_index: int) -> int | None:
    """Index just past the ``}`` closing the ``{`` at ``open_index``, skipping strings/comments.

    Brace matching, not "scan until the next declaration that happens to follow":
    a reformat or a new neighbouring declaration cannot silently redraw the block
    boundary and drop (or swallow) copy.
    """
    depth = 0
    i = open_index
    end = len(content)
    while i < end:
        char = content[i]
        if char in "\"'`":
            quote = char
            i += 1
            while i < end and content[i] != quote:
                i += 2 if content[i] == "\\" else 1
            i += 1
            continue
        if char == "/" and i + 1 < end:
            if content[i + 1] == "/":
                newline = content.find("\n", i)
                i = end if newline < 0 else newline
                continue
            if content[i + 1] == "*":
                close = content.find("*/", i + 2)
                i = end if close < 0 else close + 2
                continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _js_declaration(content: str, name: str) -> tuple[str, int] | None:
    """Slice the object literal assigned to ``const NAME``, with its 1-based start line.

    Whitespace-tolerant: ``const NAME={``, ``const NAME = {`` and a line-wrapped
    opening brace all parse, so reformatting the template cannot blind the scan.
    """
    opener = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*\{{", content)
    if opener is None:
        return None
    start = opener.end() - 1
    end = _matching_brace(content, start)
    if end is None:
        return None
    return content[start:end], content.count("\n", 0, start) + 1


def _js_function_body(content: str, name: str) -> tuple[str, int] | None:
    """Slice the body of ``function NAME(...) {...}``, with its 1-based start line."""
    opener = re.search(rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", content)
    if opener is None:
        return None
    start = opener.end() - 1
    end = _matching_brace(content, start)
    if end is None:
        return None
    return content[start:end], content.count("\n", 0, start) + 1


def _parse_js_object_entries(content: str, name: str) -> list[tuple[int, str, str]]:
    """Return (line, key, joined text) for `key:{title:'..',message:'..',action:'..'}` rows."""
    sliced = _js_declaration(content, name)
    if sliced is None:
        return []
    block, base_line = sliced
    entries: list[tuple[int, str, str]] = []
    for match in _JS_OBJECT_ROW_RE.finditer(block):
        text = " ".join(field.group(2) for field in _JS_OBJECT_FIELD_RE.finditer(match.group(2)))
        entries.append((base_line + block.count("\n", 0, match.start()), match.group(1), text))
    return entries


def _parse_js_scalar_entries(content: str, name: str) -> list[tuple[int, str, str]]:
    """Return (line, key, value) for ``key: 'sentence'`` object rows."""
    sliced = _js_declaration(content, name)
    if sliced is None:
        return []
    block, base_line = sliced
    entries: list[tuple[int, str, str]] = []
    for match in _JS_SCALAR_ROW_RE.finditer(block):
        value = _decode_js_string_literal(match.group(2))
        if value is not None:
            entries.append((base_line + block.count("\n", 0, match.start()), match.group(1), value))
    return entries


def _extract_admin_tables() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "templates" / "admin.html"
    content = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    refs: list[StringRef] = []

    for line, key, text in _parse_js_object_entries(content, "FIRST_LISTEN_ERRORS"):
        refs.append(StringRef(rel, line, text, "admin", f"first_listen_error:{key}"))

    for line, key, text in _parse_js_scalar_entries(content, "JAMENDO_ERROR_COPY"):
        refs.append(StringRef(rel, line, text, "admin", f"jamendo_form:{key}"))

    m = re.search(r"function\s+jamendoFailureHint\s*\([^)]*\)\s*\{.*?return\s*\{([^}]+)\}", content, re.DOTALL)
    if m:
        body = m.group(1)
        base = content.count("\n", 0, m.start(1)) + 1
        for match in _JS_SCALAR_ROW_RE.finditer(body):
            hint = _decode_js_string_literal(match.group(2))
            if hint is None:
                continue
            line = base + body.count("\n", 0, match.start())
            refs.append(StringRef(rel, line, hint, "admin", f"jamendo_hint:{match.group(1)}"))

    # Centralized failure copy: the constants and helpers most admin toasts route through.
    for match in re.finditer(rf"^const\s+([A-Z_]+)\s*=\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})", content, re.M):
        constant = _decode_js_copy_literal(match.group(2))
        if constant and _is_prose(constant):
            refs.append(
                StringRef(rel, _line_no(content, match.start()), constant, "admin", f"admin_copy:{match.group(1)}")
            )

    # A copy helper is graded on everything it can say: its literals are joined, because
    # `wayOut()` builds one sentence out of a failure half and a way-out half.
    for name in ADMIN_COPY_FUNCTIONS:
        sliced = _js_function_body(content, name)
        if sliced is None:
            continue
        body, base = sliced
        spoken = [
            decoded
            for decoded in (_decode_js_copy_literal(literal.group(0)) for literal in _JS_COPY_LITERAL_RE.finditer(body))
            if decoded and _is_prose(decoded)
        ]
        if spoken:
            refs.append(StringRef(rel, base, " ".join(spoken), "admin", f"admin_helper:{name}"))

    call_patterns = (
        (rf"toast\(\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})", "toast"),
        (rf"firstListenSetStatus\(\s*[^,]+,\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})", "first_listen_status"),
        (rf"setJamendoFormMessage\(\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})", "jamendo_form_message"),
        (rf"offlineMsg\(\)\s*\+\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})", "offline_suffix"),
    )
    for pattern, ctx in call_patterns:
        for match in re.finditer(pattern, content):
            spoken_line = _decode_js_copy_literal(match.group(1))
            if spoken_line and len(spoken_line) >= 12:
                refs.append(StringRef(rel, _line_no(content, match.start()), spoken_line, "admin", ctx))

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


def _extract_html_template(relative_path: str, surface: str) -> list[StringRef]:
    path = ROOT / relative_path
    if not path.is_file():
        return []
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
                        surface,
                        "listener_template",
                    )
                )
    for match in re.finditer(
        r"\b(?:aria-label|title|placeholder)\s*=\s*(['\"])(.*?)\1", visible, flags=re.IGNORECASE | re.DOTALL
    ):
        value = re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", " ", match.group(2), flags=re.DOTALL)
        text = html.unescape(" ".join(value.split()))
        if text and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
            refs.append(StringRef(rel, _line_no(visible, match.start()), text, surface, "listener_template"))

    visible = re.sub(r"{{.*?}}|{%.*?%}|{#.*?#}", _blank_preserving_lines, visible, flags=re.DOTALL)
    visible = re.sub(r"<[^>]*>", _blank_preserving_lines, visible, flags=re.DOTALL)

    run: list[str] = []
    run_line = 0
    for lineno, line in enumerate([*visible.splitlines(), ""], 1):
        text = " ".join(html.unescape(line).split())
        if text and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
            if not run:
                run_line = lineno
            run.append(text)
            continue
        if run:
            refs.append(StringRef(rel, run_line, " ".join(run), surface, "listener_template"))
            run = []
    return refs


def _extract_listener_template() -> list[StringRef]:
    return _extract_html_template("mammamiradio/web/templates/listener.html", "listener")


def _extract_clip_template() -> list[StringRef]:
    """The share page a listener actually lands on, unauthenticated."""
    return _extract_html_template("mammamiradio/web/templates/clip.html", "listener")


def _extract_admin_js() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "static" / "admin.js"
    if not path.is_file():
        return []
    content = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    refs: list[StringRef] = []
    for pattern in (
        rf"toast\(\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})",
        rf"\.textContent\s*=\s*({_JS_STRING_LITERAL}|{_JS_TEMPLATE_LITERAL})",
    ):
        for match in re.finditer(pattern, content):
            text = _decode_js_copy_literal(match.group(1))
            if text and re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text):
                refs.append(StringRef(rel, _line_no(content, match.start()), text, "admin", "inline"))
    return refs


def _extract_streamer_setup_errors() -> list[StringRef]:
    path = ROOT / "mammamiradio" / "web" / "streamer.py"
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    refs: list[StringRef] = []
    for table in _annotated_dicts(tree, {"_SETUP_ERRORS", "_SETUP_ERRORS_STANDALONE"}):
        for key, entry_node in _dict_rows(table):
            try:
                entry = ast.literal_eval(entry_node)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, tuple) or len(entry) != 5:
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
    refs.extend(_extract_clip_template())
    refs.extend(_extract_listener_js())
    refs.extend(_extract_admin_js())
    refs.extend(_extract_streamer_setup_errors())
    return list(dict.fromkeys(refs))


def _has_tech_lingo(text: str, surface: str) -> str | None:
    low = _URL_RE.sub("", text.lower())
    for term, pattern in _LINGO_LISTENER if surface == "listener" else _LINGO_ADMIN:
        if pattern.search(low):
            return term
    return None


def _has_way_out(text: str) -> bool:
    return _WAY_OUT_RE.search(text) is not None


def _is_failure_copy(ref: StringRef) -> bool:
    if ref.context.startswith(("first_listen_error", "setup_error", "jamendo_hint", "jamendo_form:")):
        return True
    if ref.context.startswith(("admin_copy:", "admin_helper:")):
        return _FAILURE_TEXT_RE.search(ref.text) is not None
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
            "admin_copy",
            "admin_helper",
        }
        and _FAILURE_TEXT_RE.search(ref.text) is not None
    )


def _is_informational_copy(text: str) -> bool:
    normalized = " ".join(text.lower().split()).strip(" .…")
    return normalized in _INFO_COPY_OK


def check_strings(refs: list[StringRef]) -> list[Violation]:
    violations: list[Violation] = []
    for ref in refs:
        term = _has_tech_lingo(ref.text, ref.surface)
        if term:
            violations.append(Violation("tech_lingo", ref.file, ref.line, ref.text, f"term={term!r} ctx={ref.context}"))

        for pat in STALE_SPEAKER_PATTERNS:
            if pat.search(ref.text):
                violations.append(
                    Violation("stale_speaker_copy", ref.file, ref.line, ref.text, f"pattern={pat.pattern}")
                )
                break

        if _is_failure_copy(ref) and not _has_way_out(ref.text) and not _is_informational_copy(ref.text):
            violations.append(Violation("no_way_out", ref.file, ref.line, ref.text, f"dead-end {ref.context}"))

    return violations


def check_coverage(refs: list[StringRef]) -> list[str]:
    """Report every extractor group that collected less than its floor."""
    collected = Counter(ref.context.split(":")[0] for ref in refs)
    return [
        f"{group}: collected {collected[group]}, floor {floor}"
        for group, floor in sorted(MIN_STRINGS_PER_GROUP.items())
        if collected[group] < floor
    ]


def load_baseline() -> Counter[str] | None:
    """Fingerprints are derived from the readable rows, so the file cannot disagree with itself."""
    if not BASELINE_PATH.is_file():
        return None
    rows = json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get("violations", [])
    return Counter(Violation(**row).fingerprint for row in rows)


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
        "violations": [asdict(v) for v in sorted(violations, key=lambda v: (v.file, v.line, v.rule, v.text))],
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def print_baseline_delta(violations: list[Violation], previous: Counter[str] | None) -> None:
    """Name every violation this refresh newly accepts.

    Refreshing is the documented answer to a red lint, so it must not be able to
    swallow an unrelated regression in silence.
    """
    if previous is None:
        return
    added, dropped = _compare_to_baseline(violations, previous)
    for violation in added:
        print(f"  + now baselined: {violation.file}:{violation.line} [{violation.rule}] {violation.text[:100]}")
    if dropped:
        print(f"  - dropped {dropped} entr{'y' if dropped == 1 else 'ies'} that no longer reproduce")
    if added:
        print("Check every '+' line is copy you meant to grandfather, not a regression you just wrote.")


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
    gaps = check_coverage(refs)

    if args.write_baseline:
        previous = load_baseline()
        write_baseline(violations)
        print(f"Wrote {len(violations)} violations to {BASELINE_PATH.relative_to(ROOT)}")
        print_baseline_delta(violations, previous)
        return 0

    if args.audit:
        print_audit(violations, refs)
        for gap in gaps:
            print(f"COVERAGE GAP {gap}")
        return 0

    # Coverage first: with a broken extractor there are no violations to compare,
    # and "no violations" must never be reported as a pass.
    if gaps:
        print(f"FAIL: {len(gaps)} UI copy extractor(s) collected less copy than expected:", file=sys.stderr)
        for gap in gaps:
            print(f"  {gap}", file=sys.stderr)
        print(
            "An extractor stopped matching — reformatting the source it scans is the usual cause. "
            "Repair it in scripts/ui_copy_lint.py, or lower the floor in MIN_STRINGS_PER_GROUP "
            "if that copy was genuinely deleted.",
            file=sys.stderr,
        )
        return 1

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
        print("Fix the copy, or run scripts/check-ui-copy-lint.sh --write-baseline to grandfather it.", file=sys.stderr)
        return 1
    if fixed:
        # Stale rows are a failure, not a note: they are what keeps the backlog
        # one-way, and a silent NOTE is how a collapsed extractor looked clean.
        print(f"FAIL: {fixed} baselined violation(s) no longer reproduce.", file=sys.stderr)
        print(
            "That is good news if you fixed the copy — run "
            "scripts/check-ui-copy-lint.sh --write-baseline to shrink the baseline, "
            "and check the printed '+' lines before committing.",
            file=sys.stderr,
        )
        return 1
    print(f"UI copy lint clean ({len(violations)} known violations baselined).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
