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
import os
import re
import subprocess
import sys
from pathlib import Path

FLOORS_FILE = Path(".coverage-floors.json")
PYPROJECT = Path("pyproject.toml")
COVERAGE_SNAPSHOT = (
    Path(os.environ["COVERAGE_RATCHET_SNAPSHOT"]) if os.environ.get("COVERAGE_RATCHET_SNAPSHOT") else None
)
COVERAGE_INPUT = Path(os.environ["COVERAGE_RATCHET_INPUT"]) if os.environ.get("COVERAGE_RATCHET_INPUT") else None
# Repo root for mapping a module key back to its source file. scripts/ -> repo root.
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _module_source_exists(module: str) -> bool:
    """True if the source .py for a dotted module key still exists on disk.

    Module keys are dotted source paths (the inverse of the parse in
    run_coverage): ``mammamiradio.web.streamer`` <-> ``mammamiradio/web/streamer.py``.
    A floor is only deleted when the module is both absent from coverage AND its
    file is gone — a zero-coverage/excluded module that still exists keeps its floor.
    """
    return (SOURCE_ROOT / (module.replace(".", "/") + ".py")).exists()


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

    if result.returncode != 0:
        print(
            f"ERROR: pytest failed (returncode={result.returncode}). "
            "Coverage parsing is not a pass signal — fix tests before re-running.",
            file=sys.stderr,
        )
        sys.exit(1)

    if COVERAGE_SNAPSHOT:
        COVERAGE_SNAPSHOT.write_text(
            json.dumps({"modules": modules, "total_pct": total_pct}, indent=2, sort_keys=True) + "\n"
        )
        print(f"Wrote coverage ratchet snapshot to {COVERAGE_SNAPSHOT}")

    return modules, total_pct


def _validate_pct(value: object, label: str) -> int:
    """A coverage percentage must be a real int in 0..100.

    bool is an int subclass in Python, so ``isinstance(True, int)`` is True —
    reject it explicitly so a stray ``true``/``false`` in a snapshot can't pose
    as 1/0 and quietly ratchet a floor. ``None`` (a missing field) also fails
    the int check, which is the behaviour we want.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}: coverage percentage must be an int, got {value!r}")
    if not 0 <= value <= 100:
        raise ValueError(f"{label}: coverage percentage {value} out of range 0..100")
    return value


def _validate_coverage_map(modules: object, source: str, *, require_nonempty: bool) -> dict[str, int]:
    """Validate a ``{module: pct}`` mapping and return it unchanged.

    Rejects a non-dict, non-string or empty module keys, bool/non-int values,
    and out-of-range percentages. ``require_nonempty`` guards the snapshot path
    (a coverage run that produced zero modules must never be treated as
    authoritative — see ``cmd_update``); the floors file may legitimately be
    empty at init, so it passes ``require_nonempty=False``.
    """
    if not isinstance(modules, dict):
        raise ValueError(f"{source}: 'modules' must be an object, got {type(modules).__name__}")
    if require_nonempty and not modules:
        raise ValueError(
            f"{source}: 'modules' is empty — refusing to treat a coverage run that "
            "produced zero modules as authoritative (it would wipe every floor)."
        )
    validated: dict[str, int] = {}
    for module, pct in modules.items():
        if not isinstance(module, str) or not module.strip():
            raise ValueError(f"{source}: module key must be a non-empty string, got {module!r}")
        validated[module] = _validate_pct(pct, f"{source}: module {module!r}")
    return validated


def load_coverage_input() -> tuple[dict[str, int], int] | None:
    """Load a coverage snapshot produced by a read-only quality job."""
    if not COVERAGE_INPUT:
        return None
    try:
        data = json.loads(COVERAGE_INPUT.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid coverage ratchet snapshot {COVERAGE_INPUT}: malformed JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid coverage ratchet snapshot {COVERAGE_INPUT}: top level must be an object")
    modules = _validate_coverage_map(data.get("modules"), f"snapshot {COVERAGE_INPUT}", require_nonempty=True)
    total_pct = _validate_pct(data.get("total_pct"), f"snapshot {COVERAGE_INPUT}: total_pct")
    return modules, total_pct


def current_coverage() -> tuple[dict[str, int], int]:
    """Return current coverage, either from an artifact snapshot or fresh pytest."""
    loaded = load_coverage_input()
    if loaded is not None:
        print(f"Loaded coverage ratchet snapshot from {COVERAGE_INPUT}")
        return loaded
    return run_coverage()


def load_floors() -> dict[str, int]:
    """Load per-module floors from JSON file.

    The committed floors file is just as load-bearing as the snapshot — a
    corrupt or out-of-range value here would poison every comparison in
    ``cmd_check`` — so it gets the same validation. An empty file is allowed
    (the init/no-floors-yet state).
    """
    if not FLOORS_FILE.exists():
        return {}
    try:
        data = json.loads(FLOORS_FILE.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid coverage floors file {FLOORS_FILE}: malformed JSON ({exc})") from exc
    return _validate_coverage_map(data, str(FLOORS_FILE), require_nonempty=False)


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
    current, total_pct = current_coverage()
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
    current, total_pct = current_coverage()
    if not current:
        # An empty coverage map would make every existing floor look "deleted"
        # below and wipe the whole file — then commit the wipe under the
        # main-push write token. A coverage run that yields zero modules is a
        # parser/snapshot failure, never a real state; refuse instead of ratchet.
        print(
            "ERROR: current coverage has zero modules — refusing to ratchet. "
            "An empty coverage map would delete every per-module floor. This "
            "usually means the coverage parser or snapshot failed upstream.",
            file=sys.stderr,
        )
        return 1
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

    # Remove floors only for modules whose source file is actually gone. A
    # module absent from coverage but still on disk (excluded, fully-skipped,
    # 0%) keeps its floor — dropping it would silently retire a guard (#636).
    removed = [m for m in floors if m not in current and not _module_source_exists(m)]
    for m in removed:
        del floors[m]
        updated.append(f"  {m}: removed (source file deleted)")

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
    current, total_pct = current_coverage()
    if not current:
        # Same wipe risk as cmd_update: an empty coverage map would write an
        # empty floors file. init is bootstrap-only, but refuse all the same.
        print(
            "ERROR: current coverage has zero modules — refusing to initialize "
            "floors from an empty coverage map (parser/snapshot failure).",
            file=sys.stderr,
        )
        return 1
    save_floors(current)
    print(f"Initialized {len(current)} module floors in {FLOORS_FILE} (total: {total_pct}%)")
    for module, pct in sorted(current.items()):
        print(f"  {module}: {pct}%")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"

    handlers = {"check": cmd_check, "update": cmd_update, "init": cmd_init}
    handler = handlers.get(mode)
    if handler is None:
        print(f"Usage: {sys.argv[0]} [check|update|init]")
        return 1
    try:
        return handler()
    except ValueError as exc:
        # A corrupt snapshot or floors file raises ValueError from the loaders.
        # Surface the clean one-line reason (matching every other failure path)
        # instead of a raw traceback, and fail the job.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
