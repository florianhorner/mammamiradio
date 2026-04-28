#!/usr/bin/env python3
"""Coverage ratchet — automatic, per-module coverage floor enforcement.

Three modes:
  check   — fail if any module drops below its floor (CI on PRs)
  update  — ratchet floors up to current coverage (CI on main merge)
  init    — generate initial floors from current coverage

The aggregate floor lives in pyproject.toml [tool.coverage.report] fail_under.
Per-module floors live in .coverage-floors.json.

This script reads coverage JSON output, so run pytest with:
  pytest --cov=mammamiradio --cov-report=json
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

FLOORS_FILE = Path(".coverage-floors.json")
PYPROJECT = Path("pyproject.toml")


def run_coverage() -> tuple[dict[str, int], int]:
    """Run pytest with coverage and parse the term output for per-module percentages.

    Returns (module_coverages, total_pct). Uses the term-missing output which
    matches what `fail_under` checks — the JSON report computes branch coverage
    differently for small files and causes false regressions.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "--cov=mammamiradio",
            "--cov-report=term-missing",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    # Print output so CI shows it
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    modules: dict[str, int] = {}
    total_pct = 0

    for line in result.stdout.splitlines():
        # Match lines like: mammamiradio/core/config.py   224  25  72  13   87%   ...
        # (regex handles any depth under mammamiradio/ post-cathedral)
        match = re.match(r"^(mammamiradio/\S+\.py)\s+.*\s+(\d+)%", line)
        if match:
            filepath = match.group(1)
            pct = int(match.group(2))
            module = filepath.replace("/", ".").removesuffix(".py")
            modules[module] = pct
            continue

        # Match TOTAL line
        total_match = re.match(r"^TOTAL\s+.*\s+(\d+)%", line)
        if total_match:
            total_pct = int(total_match.group(1))

    if result.returncode != 0 and not modules:
        print("ERROR: pytest failed", file=sys.stderr)
        sys.exit(1)

    return modules, total_pct


def load_floors() -> dict[str, int]:
    """Load per-module floors from JSON file."""
    if not FLOORS_FILE.exists():
        return {}
    return json.loads(FLOORS_FILE.read_text())


def save_floors(floors: dict[str, int]) -> None:
    """Save per-module floors to JSON file."""
    FLOORS_FILE.write_text(json.dumps(floors, indent=2, sort_keys=True) + "\n")


def get_aggregate_threshold() -> int:
    """Read fail_under from pyproject.toml."""
    text = PYPROJECT.read_text()
    match = re.search(r"fail_under\s*=\s*(\d+)", text)
    return int(match.group(1)) if match else 60


def set_aggregate_threshold(new_value: int) -> None:
    """Update fail_under in pyproject.toml."""
    text = PYPROJECT.read_text()
    text = re.sub(r"(fail_under\s*=\s*)\d+", rf"\g<1>{new_value}", text)
    PYPROJECT.write_text(text)


def cmd_check() -> int:
    """Check mode: fail if any module dropped below its floor."""
    current, total_pct = run_coverage()
    floors = load_floors()
    aggregate_floor = get_aggregate_threshold()

    regressions = []
    for module, floor in sorted(floors.items()):
        actual = current.get(module)
        if actual is None:
            continue  # module was removed, that's fine
        if actual < floor:
            regressions.append((module, floor, actual))

    if total_pct < aggregate_floor:
        regressions.append(("TOTAL", aggregate_floor, total_pct))

    if regressions:
        print("\n--- COVERAGE REGRESSIONS DETECTED ---")
        for module, floor, actual in regressions:
            print(f"  {module}: {actual}% < {floor}% floor (dropped {floor - actual}pp)")
        print(f"\nFix these before merging. Floors are in {FLOORS_FILE}")
        return 1

    print(f"\nAll coverage floors held. Total: {total_pct}% (floor: {aggregate_floor}%)")

    # Show modules with headroom for transparency
    headroom = []
    for module, floor in sorted(floors.items()):
        actual = current.get(module, 0)
        if actual > floor + 3:
            headroom.append((module, floor, actual))
    if headroom:
        print("\nModules with ratchet headroom (will auto-update on main):")
        for module, floor, actual in headroom[:5]:
            print(f"  {module}: {actual}% (floor: {floor}%)")

    return 0


def cmd_update() -> int:
    """Update mode: ratchet all floors up to current coverage. Never down."""
    current, total_pct = run_coverage()
    floors = load_floors()
    aggregate_floor = get_aggregate_threshold()

    updated = []

    # Update per-module floors
    for module, pct in sorted(current.items()):
        old = floors.get(module, 0)
        if pct > old:
            floors[module] = pct
            if old > 0:
                updated.append(f"  {module}: {old}% -> {pct}%")
            else:
                updated.append(f"  {module}: new at {pct}%")

    # Remove floors for deleted modules
    removed = [m for m in floors if m not in current]
    for m in removed:
        del floors[m]
        updated.append(f"  {m}: removed (module deleted)")

    # Update aggregate floor
    if total_pct > aggregate_floor:
        set_aggregate_threshold(total_pct)
        updated.append(f"  TOTAL: {aggregate_floor}% -> {total_pct}% (pyproject.toml)")

    save_floors(floors)

    if updated:
        print("Coverage floors ratcheted up:")
        for line in updated:
            print(line)
    else:
        print("All floors already at current levels. No changes.")

    return 0


def cmd_init() -> int:
    """Init mode: generate initial floors from current coverage."""
    current, total_pct = run_coverage()
    save_floors(current)
    print(f"Initialized {len(current)} module floors in {FLOORS_FILE} (total: {total_pct}%)")
    for module, pct in sorted(current.items()):
        print(f"  {module}: {pct}%")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"

    if mode == "check":
        return cmd_check()
    elif mode == "update":
        return cmd_update()
    elif mode == "init":
        return cmd_init()
    else:
        print(f"Usage: {sys.argv[0]} [check|update|init]")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
