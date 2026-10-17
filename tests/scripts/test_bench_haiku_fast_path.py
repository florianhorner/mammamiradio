from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from scripts import bench_haiku_fast_path as bench


@pytest.fixture(autouse=True)
def _restore_package_log_level() -> Iterator[None]:
    # The corpus builder raises the package logger to CRITICAL for the CLI run; left in place,
    # every later caplog assertion in the session reads an empty capture.
    logger = logging.getLogger("mammamiradio")
    saved = logger.level
    yield
    logger.setLevel(saved)


def test_arms_selection_without_a_candidate_is_rejected(tmp_path, capsys) -> None:
    # Only the incumbent leaves nothing to compare, and the empty verdict set would read as a pass.
    assert bench.main(["--dry-run", "--arms", "A", "--output-dir", str(tmp_path)]) == 2
    assert "at least one candidate arm" in capsys.readouterr().out


def test_dry_run_plan_shows_the_budget_each_arm_sends(tmp_path, capsys) -> None:
    assert bench.main(["--dry-run", "--arms", "F", "--output-dir", str(tmp_path)]) == 0
    plan_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("#")]
    transition_f = [line for line in plan_lines if "transition" in line and "arm F" in line]
    assert transition_f, plan_lines
    assert all(f"max_tokens={bench.CANDIDATE_TRANSITION_MAX_TOKENS}" in line for line in transition_f), transition_f
