#!/usr/bin/env python3
"""Check tracked text without including rejected content in diagnostics."""

from __future__ import annotations

import ast
import hashlib
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
_PERSONAL_PATH = re.compile(r"(?:/(?:Users|home)/|[A-Za-z]:[\\/]Users[\\/])([^/\\\s'\"`]+)[/\\]")
_SAMPLE_USERS = {"example", "user", "username", "<user>", "<username>", "your-user", "yourname", "you"}
_ENTITY = re.compile(r"\b(person|device_tracker|lock)\.([a-zA-Z0-9_]+)")
_FIXTURE_ENTITY = re.compile(
    r"\b(?:person|device_tracker|lock|sensor|binary_sensor|switch|light|fan|vacuum|weather|sun|input_select|input_button|media_player|climate|cover)\.[a-zA-Z0-9_]+"
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?:/\d{1,3})?(?![\w:])")
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)
# A container-to-host gateway in the disposable lab, never a deployed host.
_LAB_FILES = {
    "scripts/first-listen-lab.sh",
    "docs/runbooks/first-listen-local-ha.md",
    "tests/repo/test_first_listen_lab_script.py",
}
# Explicit synthetic clients exercise private-network authentication boundaries.
_SYNTHETIC_DESTINATIONS = {
    "tests/web/test_auth.py": {
        "10.0.0.1",
        "172.16.0.1",
        "172.30.32.5",
        "192.168.1.100",
        "100.64.0.1",
        "169.254.10.20",
        "fd00::1234",
        "fc00::1",
        "fe80::1",
    },
    "tests/web/test_streamer_routes.py": {"10.0.0.1", "192.168.1.50", "fd00::50", "fe80::50"},
    "tests/web/test_streamer_routes_extended.py": {
        "10.0.0.1",
        "10.0.0.5",
        "172.30.32.2",
        "172.30.32.5",
        "172.30.32.6",
        "192.168.1.10",
        "192.168.1.11",
        "192.168.1.20",
        "192.168.1.50",
        "192.168.1.77",
    },
    "tests/home/test_ha_context.py": {"192.168.1.44"},
    "tests/home/test_scene_namer.py": {"192.168.1.10"},
    "tests/repo/test_doc_audit_invariants.py": {"172.30.32.5"},
    "tests/scheduling/test_producer_gen_state.py": {"192.168.1.50"},
}


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str

    def diagnostic(self) -> str:
        safe_path = self.path.encode("unicode_escape").decode("ascii")
        return f"FAIL: {safe_path}:{self.line} [{self.rule}]"


def tracked_paths(root: Path) -> list[str]:
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, check=True)
        if Path(os.fsdecode(top.stdout.rstrip(b"\n"))).resolve() != root.resolve():
            raise ValueError("repository root mismatch")
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
        return [os.fsdecode(value) for value in result.stdout.split(b"\0") if value]
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("tracked inventory unavailable") from exc


def identity_units(relative: str, text: str) -> Iterator[tuple[int, str]]:
    if not relative.endswith(".py"):
        yield 1, text
        return
    try:
        # AST constants also join implicit adjacent string literals.
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield node.lineno, node.value
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                yield token.start[0], token.string
            elif token.type == tokenize.STRING:
                try:
                    value = ast.literal_eval(token.string)
                except (ValueError, SyntaxError):
                    value = token.string
                if isinstance(value, str):
                    yield token.start[0], value
    except (tokenize.TokenError, SyntaxError):
        # Malformed Python cannot bypass string checks.
        yield 1, text


def scan_text(relative: str, text: str) -> list[Violation]:
    violations: set[tuple[int, str]] = set()
    units = [(1, text), *identity_units(relative, text)]
    for first_line, unit in units:
        for number, line in enumerate(unquote(unit).splitlines(), first_line):
            for match in _PERSONAL_PATH.finditer(line):
                user = match[1]
                if user.casefold() not in _SAMPLE_USERS and not user.startswith(("$", "{", "<")):
                    violations.add((number, "personal-filesystem-path"))
            for pattern in (_IPV4, _IPV6):
                for match in pattern.finditer(line):
                    candidate = match.group()
                    try:
                        if "/" in candidate:
                            network = ipaddress.ip_network(candidate, strict=True)
                            if network.prefixlen < network.max_prefixlen:
                                continue
                            candidate = str(network.network_address)
                        address = ipaddress.ip_address(candidate)
                    except ValueError:
                        continue
                    if not any(
                        address.version == network.version and address in network for network in _PRIVATE_NETWORKS
                    ):
                        continue
                    if relative in _LAB_FILES and candidate == "172.17.0.1":
                        continue
                    if candidate in _SYNTHETIC_DESTINATIONS.get(relative, set()):
                        continue
                    if relative == "scripts/public_tree_safety.py" and candidate in (
                        {"172.17.0.1"} | set().union(*_SYNTHETIC_DESTINATIONS.values())
                    ):
                        continue
                    violations.add((number, "concrete-private-destination"))
    structured = relative.startswith(("tests/fixtures/", "scripts/showreel/")) or relative in {
        f"mammamiradio/home/{name}.py"
        for name in ("bindings", "profile", "migration", "authorization", "catalog", "ha_context", "context_director")
    }
    for first_line, unit in identity_units(relative, text):
        expression = _FIXTURE_ENTITY if structured else _ENTITY
        unit = unquote(unit)
        for match in expression.finditer(unit):
            value = match.group()
            object_id = value.split(".", 1)[1]
            if object_id.startswith("example_") or value in {"sun.sun", "sun.ambient", "weather.ambient"}:
                continue
            if relative == "mammamiradio/home/ha_context.py" and value in {
                "media_player.mammamiradio",
                "sensor.mammamiradio_",
                "sensor.mammamiradio_segment_type",
                "sensor.mammamiradio_listeners",
                "binary_sensor.mammamiradio_on_air",
            }:
                continue
            # Deliberate generic poison values are confined to negative fixtures.
            if (
                relative in {"tests/repo/test_publication_safety.py", "tests/workflows/test_docs_safety.sh"}
                and object_id == "recorded_household_value"
            ):
                continue
            violations.add((first_line + unit[: match.start()].count("\n"), "non-synthetic-entity"))
    return [Violation(relative, line, rule) for line, rule in sorted(violations)]


def scan_repository(root: Path) -> list[Violation]:
    paths = tracked_paths(root)
    manifest = root / "scripts/public-evidence-retention.json"
    retained = json.loads(manifest.read_text())
    if not isinstance(retained, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in retained.items()
    ):
        raise ValueError("invalid retained evidence inventory")
    violations = []
    for relative in sorted(set(paths) | set(retained)):
        path = root / relative
        if relative in retained and relative not in paths:
            violations.append(Violation(relative, 1, "immutable-evidence-untracked"))
            continue
        try:
            raw = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        except FileNotFoundError:
            if relative in retained:
                violations.append(Violation(relative, 1, "immutable-evidence-missing"))
            continue
        except OSError:
            violations.append(Violation(relative, 1, "unreadable-tracked-file"))
            continue
        if relative in retained:
            if hashlib.sha256(raw).hexdigest() != retained[relative]:
                violations.append(Violation(relative, 1, "immutable-evidence-changed"))
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Preserve ASCII identifiers even in text with a different encoding.
            text = raw.decode("utf-8", errors="replace")
        violations.extend(scan_text(relative, text))
    return violations


def main() -> int:
    try:
        violations = scan_repository(ROOT)
    except (OSError, ValueError):
        print("FAIL: repository:1 [public-tree-inventory-unavailable]")
        return 1
    for violation in violations:
        print(violation.diagnostic())
    return int(bool(violations))


if __name__ == "__main__":
    sys.exit(main())
