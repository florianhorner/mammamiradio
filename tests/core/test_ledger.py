"""Tests for the provenance ledger writer.

Covers: normal append, disabled-is-silent, bounded drop + heartbeat, daily
gzip rollover, retention prune, sidecar dedup, and isolation (a write failure
never raises). The writer is a daemon thread; ``stop()`` drains then joins, so
assertions run against a flushed file deterministically.
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC
from pathlib import Path

from mammamiradio.core.ledger import ProvenanceLedger


class _Clock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _day_file(d: Path, clock: _Clock) -> Path:
    from datetime import datetime

    date = datetime.fromtimestamp(clock.t, tz=UTC).strftime("%Y-%m-%d")
    return d / f"provenance-{date}.jsonl"


def test_normal_append_writes_jsonl(tmp_path):
    clock = _Clock()
    led = ProvenanceLedger(tmp_path, enabled=True, clock=clock)
    led.start()
    led.record({"record": "llm_call", "llm_call_id": "abc", "ok": True})
    led.stop()

    rows = _read_lines(_day_file(tmp_path, clock))
    assert len(rows) == 1
    assert rows[0]["llm_call_id"] == "abc"


def test_disabled_writes_nothing(tmp_path):
    led = ProvenanceLedger(tmp_path, enabled=False)
    led.start()
    led.record({"record": "llm_call"})
    led.stop()
    assert list(tmp_path.glob("provenance-*")) == []


def test_dir_created_0700_and_file_0600(tmp_path):
    clock = _Clock()
    d = tmp_path / "ledger"
    led = ProvenanceLedger(d, enabled=True, clock=clock)
    led.start()
    led.record({"record": "x"})
    led.stop()
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (_day_file(d, clock).stat().st_mode & 0o777) == 0o600


def test_bounded_drop_emits_heartbeat(tmp_path):
    clock = _Clock()
    led = ProvenanceLedger(tmp_path, enabled=True, queue_max=2, clock=clock)
    # Do not start the thread yet: fill past capacity so drops accumulate,
    # then start to flush. enabled + thread guard: temporarily fake a thread.
    led.start()
    # Stop the drain loop from emptying mid-fill by pausing on the lock is
    # fragile; instead push many rows fast and rely on maxlen drop accounting.
    for i in range(50):
        led.record({"record": "llm_call", "i": i})
    led.stop()

    rows = _read_lines(_day_file(tmp_path, clock))
    # Some rows dropped under the cap of 2; a heartbeat row must report it.
    heartbeats = [r for r in rows if r.get("record") == "ledger_heartbeat"]
    assert heartbeats, "expected at least one ledger_heartbeat after overflow"
    assert sum(h["dropped"] for h in heartbeats) > 0


def test_daily_rollover_gzips_previous_day(tmp_path):
    clock = _Clock()
    led = ProvenanceLedger(tmp_path, enabled=True, clock=clock)
    led.start()
    led.record({"record": "day1"})
    led.stop()
    day1 = _day_file(tmp_path, clock)
    assert day1.exists()

    # Advance one day and write again; the rollover gzips day1.
    clock.t += 86400
    led.start()
    led.record({"record": "day2"})
    led.stop()

    assert not day1.exists(), "day1 .jsonl should be gzipped away"
    gz = day1.with_suffix(".jsonl.gz")
    assert gz.exists()
    with gzip.open(gz, "rt") as fh:
        assert "day1" in fh.read()
    assert _day_file(tmp_path, clock).exists()


def test_retention_prunes_old_day_files(tmp_path):
    clock = _Clock()
    # Seed an ancient day file by hand.
    old = tmp_path / "provenance-2000-01-01.jsonl"
    tmp_path.mkdir(exist_ok=True)
    old.write_text('{"record":"old"}\n')

    led = ProvenanceLedger(tmp_path, enabled=True, retention_days=14, clock=clock)
    # Phase 1: commit _current_date for day1 (flush before advancing the clock).
    led.start()
    led.record({"record": "day1"})
    led.stop()
    # Phase 2: a new day triggers rollover, which gzips day1 and prunes old files.
    clock.t += 86400
    led.start()
    led.record({"record": "day2"})
    led.stop()

    assert not old.exists(), "file older than retention should be pruned"


def test_startup_prunes_and_gzips_without_in_process_rollover(tmp_path):
    # Simulates restart-before-midnight: a fresh process boots on a later date
    # with leftover files from prior runs and must gzip + prune on startup,
    # WITHOUT ever observing an in-process date rollover.
    clock = _Clock()
    tmp_path.mkdir(exist_ok=True)
    # A recent prior-day plaintext file (within retention) — should be gzipped.
    from datetime import datetime, timedelta

    yesterday = (datetime.fromtimestamp(clock.t, tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    recent = tmp_path / f"provenance-{yesterday}.jsonl"
    recent.write_text('{"record":"yesterday"}\n')
    # An ancient file beyond retention — should be pruned.
    ancient = tmp_path / "provenance-2000-01-01.jsonl"
    ancient.write_text('{"record":"ancient"}\n')

    led = ProvenanceLedger(tmp_path, enabled=True, retention_days=14, clock=clock)
    led.start()
    led.record({"record": "today"})
    led.stop()

    # Yesterday's file gzipped on boot (no rollover happened in-process).
    assert not recent.exists()
    assert recent.with_suffix(".jsonl.gz").exists()
    # Ancient file pruned on boot.
    assert not ancient.exists()
    assert not ancient.with_suffix(".jsonl.gz").exists()
    # Today's file is still the live plaintext append target.
    assert _day_file(tmp_path, clock).exists()


def test_start_disables_when_dir_unavailable(tmp_path, monkeypatch):
    # Read-only /data on a restarted Pi addon: mkdir fails. The ledger must
    # disable itself, spawn no thread, and stay silent — never raise into boot.
    led = ProvenanceLedger(tmp_path / "ledger", enabled=True)

    def boom(self, *a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", boom)
    led.start()  # must not raise
    assert led.enabled is False
    assert led._thread is None
    led.record({"record": "llm_call"})  # silent no-op, must not raise
    led.stop()  # safe even though the thread never started


def test_sidecar_not_reappended_across_restarts(tmp_path):
    # The system prompt must be written once, not once per process restart.
    led1 = ProvenanceLedger(tmp_path, enabled=True)
    led1.start()
    led1.record_system_prompt("hashABC", "SYSTEM PROMPT TEXT")
    led1.stop()

    # Fresh ledger object (new process) over the same dir, same prompt hash.
    led2 = ProvenanceLedger(tmp_path, enabled=True)
    led2.start()
    led2.record_system_prompt("hashABC", "SYSTEM PROMPT TEXT")
    led2.stop()

    rows = _read_lines(tmp_path / "system-prompts.jsonl")
    assert len(rows) == 1, "restart re-appended an already-recorded system prompt"


def test_sidecar_dedup_writes_once(tmp_path):
    led = ProvenanceLedger(tmp_path, enabled=True)
    led.start()
    led.record_system_prompt("hash123", "SYSTEM PROMPT TEXT")
    led.record_system_prompt("hash123", "SYSTEM PROMPT TEXT")
    led.record_system_prompt("hash123", "SYSTEM PROMPT TEXT")
    led.stop()

    sidecar = tmp_path / "system-prompts.jsonl"
    rows = _read_lines(sidecar)
    assert len(rows) == 1
    assert rows[0]["system_prompt_hash"] == "hash123"


def test_write_failure_never_raises(tmp_path, monkeypatch):
    led = ProvenanceLedger(tmp_path, enabled=True)
    led.start()

    # Simulate a disk failure on append; record() and the writer must swallow it.
    real_open = Path.open

    def boom(self, *a, **k):
        if self.name.startswith("provenance-"):
            raise OSError("disk full")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", boom)
    led.record({"record": "llm_call"})  # must not raise
    led.stop()  # must not raise
    # No assertion on file content — the point is that nothing blew up.
