#!/usr/bin/env python3
"""Structural safety checks for public Markdown documentation.

The shell entrypoint owns the shared editorial regexes. This helper handles the
parts that need Markdown-aware state: recovery instructions that span lines and
relative links whose destinations or fragments need real parsing.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

_PROTECTED_PATH = r"/(?:config|data|addon_configs)(?:/[^\s,;:)]*)?"
_IMPERATIVE_WRITE = (
    r"(?:edit|write|create|overwrite|truncate|delete|remove|clear|modify|export|copy|"
    r"append|replace|patch|touch|mkdir|move|unlink|mv|cp|ln|chmod|chown|rm(?:\s+-[A-Za-z]+)*)"
)
_MUTATION_WORD = re.compile(
    r"\b(?:edit|write|create|overwrite|truncate|delete|remove|clear|modify|export|copy|"
    r"append|replace|patch|touch|mkdir|install|move|unlink|mv|cp|ln|bypass|restart|kill|chmod|chown|rm)\b|"
    r"\bsed\s+(?:-[A-Za-z]*i[A-Za-z]*|--in-place)\b|"
    r"\btee\b|(?:^|\s)(?:>>?|2>)\s*",
    re.IGNORECASE,
)
_RESTART_WORD = re.compile(r"\brestart(?:ing|ed|s)?\b", re.IGNORECASE)
_SSH_WORD = re.compile(r"\bssh\b", re.IGNORECASE)
_COORDINATOR = re.compile(r"\s*(?:or|and|nor)\b", re.IGNORECASE)
_DIRECT_NEGATION = re.compile(
    r"(?:\bdo\s+not|\bdon['’]t|\bnever|\bmust\s+not|\bshould\s+not|\bavoid|\bno)"
    r"(?:\s+(?:ever|use|using|run|running|call|calling))?\s*$",
    re.IGNORECASE,
)
_SIMPLE_DANGERS = (
    re.compile(r"\bMAMMAMIRADIO_SKIP_QUALITY_GATE\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+cp\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+restart\b", re.IGNORECASE),
    re.compile(r"\bha\s+apps\s+restart\b", re.IGNORECASE),
    re.compile(r"\bpkill\b", re.IGNORECASE),
    re.compile(r"\bsed\s+(?:-[A-Za-z]*i[A-Za-z]*|--in-place)\b", re.IGNORECASE),
    re.compile(r"\btee\b", re.IGNORECASE),
    re.compile(
        rf"\b{_IMPERATIVE_WRITE}\b(?:(?![.!?;]).){{0,180}}{_PROTECTED_PATH}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:^|\s)(?:>>?|2>)\s*{_PROTECTED_PATH}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_IMPERATIVE_WRITE}\b(?:(?![.!?;]).){{0,140}}\b(?:live\s+)?cache(?:/|\b)",
        re.IGNORECASE,
    ),
)
_DOCKER_EXEC = re.compile(r"\bdocker\s+exec\b", re.IGNORECASE)
_GENERIC_MUTATION = re.compile(
    r"\b(?:edit|write|create|overwrite|truncate|delete|remove|clear|modify|export|copy|"
    r"append|replace|patch|touch|mkdir|install|move|unlink|mv|cp|ln|bypass|chmod|chown|rm)\b|"
    r"\bsed\s+(?:-[A-Za-z]*i[A-Za-z]*|--in-place)\b|\btee\b",
    re.IGNORECASE,
)

_REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[([^]]+)\]:\s*(.*)$")
_REFERENCE_USAGE = re.compile(r"!?\[([^]]+)]\[([^]]*)]")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_HTML_ANCHOR = re.compile(r"\b(?:id|name)\s*=\s*(['\"])(.*?)\1", re.IGNORECASE)


@dataclass(frozen=True)
class Issue:
    path: Path
    line: int
    kind: str
    detail: str

    def render(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"FAIL: {self.path}:{self.line}  [{self.kind}]{suffix}"


@dataclass(frozen=True)
class Block:
    text: str
    line_offsets: tuple[int, ...]
    source_lines: tuple[int, ...]


@dataclass(frozen=True)
class Link:
    line: int
    target: str


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _blocks(text: str) -> list[Block]:
    blocks: list[Block] = []
    current: list[tuple[int, str]] = []

    def append_block(lines: list[tuple[int, str]]) -> None:
        parts: list[str] = []
        offsets: list[int] = []
        source_lines: list[int] = []
        offset = 0
        for line_number, line in lines:
            part = re.sub(r"\s+", " ", line.strip())
            offsets.append(offset)
            source_lines.append(line_number)
            parts.append(part)
            offset += len(part) + 1
        blocks.append(Block(" ".join(parts), tuple(offsets), tuple(source_lines)))

    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            if current:
                append_block(current)
                current = []
            continue
        current.append((line_number, line))
    if current:
        append_block(current)
    return blocks


def _clauses(text: str) -> list[tuple[int, str]]:
    clauses: list[tuple[int, str]] = []
    cursor = 0
    for match in re.finditer(r"(?<=[.!?])\s+|;\s*|,?\s+\b(?:but|however|instead)\b\s+|,\s*then\s+", text):
        clause = text[cursor : match.start()]
        if clause.strip():
            clauses.append((cursor, clause))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        clauses.append((cursor, tail))
    return clauses


def _directly_negated(clause: str, position: int) -> bool:
    prefix = clause[:position].rstrip(" `*_:-")
    return bool(_DIRECT_NEGATION.search(prefix))


def _uncovered_positions(clause: str, positions: list[int]) -> list[int]:
    """Return dangerous positions not covered by one direct negation.

    A directly negated first action covers that comma segment. It extends over
    an Oxford-style serial list only through the final comma segment introduced
    by ``or``, ``and``, or ``nor``. A trailing comma-splice imperative is a
    separate instruction and remains unsafe.
    """
    if not positions or not _directly_negated(clause, positions[0]):
        return positions

    segment_starts = [positions[0]]
    for match in re.finditer(",", clause[positions[0] :]):
        segment_starts.append(positions[0] + match.end())

    last_covered_segment = 0
    for index, start in enumerate(segment_starts[1:], 1):
        if _COORDINATOR.match(clause[start:]):
            last_covered_segment = index

    return [position for position in positions if bisect_right(segment_starts, position) - 1 > last_covered_segment]


def _danger_positions(clause: str, *, ssh_sequence: bool) -> list[int]:
    positions: set[int] = set()
    for pattern in _SIMPLE_DANGERS:
        search_from = 0
        while match := pattern.search(clause, search_from):
            positions.add(match.start())
            # The protected-path patterns can span a later comma segment. Keep
            # searching from the next character so a second imperative remains
            # available to negation-scope analysis.
            search_from = match.start() + 1

    for match in _DOCKER_EXEC.finditer(clause):
        if _MUTATION_WORD.search(clause[match.end() :]):
            positions.add(match.start())

    if ssh_sequence:
        positions.update(match.start() for match in _SSH_WORD.finditer(clause))
        positions.update(match.start() for match in _GENERIC_MUTATION.finditer(clause))
        positions.update(match.start() for match in _RESTART_WORD.finditer(clause))

    return sorted(positions)


def live_surgery_issues(path: Path, text: str) -> list[Issue]:
    issues: list[Issue] = []
    for block in _blocks(text):
        normalized = block.text
        ssh_sequence = bool(_SSH_WORD.search(normalized) and _RESTART_WORD.search(normalized))
        for offset, clause in _clauses(normalized):
            positions = _danger_positions(clause, ssh_sequence=ssh_sequence)
            uncovered_positions = _uncovered_positions(clause, positions)
            if not uncovered_positions:
                continue
            # A direct warning such as "do not SSH ..., edit ..., or restart"
            # negates the coordinated actions in that clause. A comma-splice or
            # a separate clause such as "Do not wait; SSH ..." does not.
            position = offset + uncovered_positions[0]
            line_index = bisect_right(block.line_offsets, position) - 1
            line = block.source_lines[line_index]
            issues.append(
                Issue(
                    path,
                    line,
                    "unsafe recovery instruction",
                    clause.strip(),
                )
            )
            break
    return issues


def _find_closing(text: str, start: int, opener: str, closer: str) -> int | None:
    depth = 1
    cursor = start + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _destination(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value.startswith("<"):
        end = value.find(">", 1)
        if end == -1:
            return ""
        return value[1:end]

    chars: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char.isspace():
            break
        chars.append(char)
    return "".join(chars)


def _inline_links(line: str, line_number: int) -> list[Link]:
    links: list[Link] = []
    cursor = 0
    while cursor < len(line):
        start = line.find("[", cursor)
        if start == -1:
            break
        if start > 0 and line[start - 1] == "\\":
            cursor = start + 1
            continue
        label_end = _find_closing(line, start, "[", "]")
        if label_end is None:
            break
        destination_start = label_end + 1
        while destination_start < len(line) and line[destination_start].isspace():
            destination_start += 1
        if destination_start < len(line) and line[destination_start] == "(":
            destination_end = _find_closing(line, destination_start, "(", ")")
            if destination_end is not None:
                target = _destination(line[destination_start + 1 : destination_end])
                if target:
                    links.append(Link(line_number, target))
        # Advance one character so nested image links are checked as well.
        cursor = start + 1
    return links


def _reference_target(raw: str) -> str:
    return _destination(raw)


def _reference_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def markdown_links(text: str) -> tuple[list[Link], list[tuple[int, str]]]:
    links: list[Link] = []
    usages: list[tuple[int, str]] = []
    definitions: set[str] = set()
    in_fence = False
    fence_marker = ""
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        fence = re.match(r"(`{3,}|~{3,})", stripped)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue

        definition = _REFERENCE_DEFINITION.match(line)
        if definition:
            definitions.add(_reference_label(definition.group(1)))
            target = _reference_target(definition.group(2))
            if target:
                links.append(Link(line_number, target))
            continue
        links.extend(_inline_links(line, line_number))
        for usage in _REFERENCE_USAGE.finditer(line):
            label = usage.group(2) or usage.group(1)
            usages.append((line_number, _reference_label(label)))
    undefined = [(line_number, label) for line_number, label in usages if label not in definitions]
    return links, undefined


def _github_slug(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"!?(?:\[([^]]*)\])\([^)]*\)", r"\1", value)
    value = value.replace("`", "").replace("*", "")
    kept: list[str] = []
    for char in value.casefold():
        category = unicodedata.category(char)
        if char in {"-", "_"} or char.isspace() or category[0] in {"L", "N", "M"}:
            kept.append(char)
    return re.sub(r"\s+", "-", "".join(kept).strip())


def _anchors(path: Path) -> set[str]:
    try:
        text = _read(path)
    except (OSError, UnicodeError):
        return set()

    anchors = {match.group(2) for match in _HTML_ANCHOR.finditer(text)}
    if path.suffix.casefold() not in {".md", ".markdown"}:
        return anchors

    counts: dict[str, int] = {}
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if not heading:
            continue
        base = _github_slug(heading.group(2))
        if not base:
            continue
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _is_external(target: str) -> bool:
    if target.startswith("//"):
        return True
    parsed = urlsplit(target)
    return bool(parsed.scheme) or target.startswith("/")


def relative_link_issues(path: Path, text: str) -> list[Issue]:
    issues: list[Issue] = []
    links, undefined_references = markdown_links(text)
    for line, label in undefined_references:
        issues.append(
            Issue(
                path,
                line,
                "broken relative Markdown link",
                f"reference is undefined: {label}",
            )
        )
    for link in links:
        target = html.unescape(link.target.strip())
        if not target or _is_external(target):
            continue
        path_part, separator, fragment = target.partition("#")
        path_part = unquote(path_part.split("?", 1)[0])
        fragment = unquote(fragment.split("?", 1)[0]) if separator else ""
        destination = path if not path_part else path.parent / path_part
        if not destination.exists():
            issues.append(
                Issue(
                    path,
                    link.line,
                    "broken relative Markdown link",
                    f"target does not exist: {target}",
                )
            )
            continue
        if fragment and fragment not in _anchors(destination):
            issues.append(
                Issue(
                    path,
                    link.line,
                    "broken relative Markdown link",
                    f"fragment does not exist: {target}",
                )
            )
    return issues


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--copy", nargs="*", default=[], metavar="FILE")
    parser.add_argument("--links", nargs="*", default=[], metavar="FILE")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    issues: list[Issue] = []
    cache: dict[Path, str] = {}

    for raw_path in dict.fromkeys([*args.copy, *args.links]):
        path = Path(raw_path)
        try:
            cache[path] = _read(path)
        except (OSError, UnicodeError) as exc:
            issues.append(Issue(path, 1, "documentation file unreadable", str(exc)))

    for raw_path in args.copy:
        path = Path(raw_path)
        if path in cache:
            issues.extend(live_surgery_issues(path, cache[path]))
    for raw_path in args.links:
        path = Path(raw_path)
        if path in cache:
            issues.extend(relative_link_issues(path, cache[path]))

    for issue in issues:
        print(issue.render())
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
