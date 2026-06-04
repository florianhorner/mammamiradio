"""Provenance ledger: a forensic, opt-in record of how each aired moment was made.

Writes three kinds of newline-delimited JSON rows to a daily-rotated file under
the ledger dir, joined by correlation IDs so an operator can reconstruct *why the
listener heard what they heard*:

    Tier 1  llm_call          raw provider attempt (success + failure)
    Tier 2  segment_prepared  the FINAL spoken script, post-processing
    Tier 3  stream_result     the true aired outcome (bytes, skip, listeners)

Design constraints (from /office-hours + two review passes):

* **Never break the illusion (#1/#2).** Every operation is best-effort. A full
  disk, a read-only dir, or a saturated queue must NEVER raise into the audio
  path. The worst case is a dropped row, logged at DEBUG.
* **Addon-safe on a Pi.** A single daemon thread drains a bounded deque and does
  all blocking IO (append / gzip / prune). Nothing touches the event loop, so a
  midnight gzip can never stutter the live stream. Enqueue is a lock + append,
  callable from sync (``on_stream_segment``) or async contexts alike.
* **Bounded + visible.** The deque caps memory; on overflow it drops the OLDEST
  row (keep recent failures during a storm) and bumps a counter that surfaces as
  a ``ledger_heartbeat`` row — saturation is never a silent blind spot.
* **Private.** Dir is created ``0700``, files ``0600``. Default OFF; the operator
  opts in knowing it records home + listener context locally.

        record() ──► deque(maxlen) ──► [writer thread] ──► provenance-YYYY-MM-DD.jsonl
                       (drop oldest                          (rollover→gzip prev,
                        on overflow)                          prune > retention)
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"
_PROVENANCE_PREFIX = "provenance-"
_PROVENANCE_GLOB = "provenance-*.jsonl"
_SIDECAR_FILENAME = "system-prompts.jsonl"
_STOP = object()  # sentinel pushed on stop() to wake + drain the writer


class ProvenanceLedger:
    """Single-writer, best-effort, daily-rotated JSONL provenance ledger."""

    def __init__(
        self,
        ledger_dir: Path,
        *,
        enabled: bool = False,
        retention_days: int = 14,
        queue_max: int = 2000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ledger_dir = Path(ledger_dir)
        self.enabled = enabled
        self.retention_days = max(1, int(retention_days))
        self._clock = clock
        self._maxlen: int = max(1, int(queue_max))
        self._records: deque = deque(maxlen=self._maxlen)
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._dropped = 0
        self._seen_prompt_hashes: set[str] = set()
        self._current_date: str | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the writer thread. Safe to call when disabled (no-op writer)."""
        if self._thread is not None:
            return
        try:
            self.ledger_dir.mkdir(parents=True, exist_ok=True)
            # Tighten perms even if the dir already existed with looser bits.
            self.ledger_dir.chmod(0o700)
        except OSError as exc:
            logger.warning("Provenance ledger dir unavailable (%s); ledger disabled", exc)
            self.enabled = False
            return
        # Seed the sidecar dedup set from disk BEFORE the thread (and thus any
        # record_system_prompt caller) can run, so a restart does not re-append
        # the multi-KB system prompt that is already recorded. Cheap one-time
        # read; the slow gzip/prune work happens on the writer thread instead.
        self._seed_seen_prompt_hashes()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="provenance-ledger", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the writer to drain remaining rows, then join."""
        thread = self._thread
        if thread is None:
            return
        with self._cond:
            self._stopping = True
            # Mirror record()'s drop accounting: if the deque is already at cap,
            # appending the sentinel evicts the oldest real row — count it so the
            # shutdown-time loss surfaces as a heartbeat rather than vanishing.
            if len(self._records) >= self._maxlen:
                self._dropped += 1
            self._records.append(_STOP)
            self._cond.notify()
        thread.join(timeout=timeout)
        self._thread = None

    # ── enqueue (sync-safe, best-effort) ───────────────────────────────────

    def record(self, row: dict) -> None:
        """Enqueue one ledger row. No-op when disabled. Never raises."""
        if not self.enabled or self._thread is None:
            return
        try:
            with self._cond:
                # deque(maxlen) drops the oldest on overflow; count it so the
                # loss is visible as a heartbeat row rather than silent.
                if len(self._records) >= self._maxlen:
                    self._dropped += 1
                self._records.append(row)
                self._cond.notify()
        except Exception as exc:  # pragma: no cover - defensive, must never raise
            logger.debug("Provenance ledger enqueue failed: %s", exc)

    def record_system_prompt(self, prompt_hash: str, system_prompt: str) -> None:
        """Enqueue a one-time sidecar row mapping a system-prompt hash to its text.

        Deduped in-memory: the large static prompt is written once per version,
        not on every call. Routed through the same writer (no separate racy
        temp+rename), so there is no read/modify race between hashes.
        """
        if not self.enabled or self._thread is None or not prompt_hash:
            return
        with self._cond:
            if prompt_hash in self._seen_prompt_hashes:
                return
            self._seen_prompt_hashes.add(prompt_hash)
            if len(self._records) >= self._maxlen:
                self._dropped += 1
            self._records.append(
                {
                    "__sidecar__": True,
                    "schema_version": SCHEMA_VERSION,
                    "system_prompt_hash": prompt_hash,
                    "system_prompt": system_prompt,
                }
            )
            self._cond.notify()

    # ── writer thread ──────────────────────────────────────────────────────

    def _run(self) -> None:
        # Enforce retention on boot, off the startup/audio path. Without this,
        # gzip+prune only ever fire when the LONG-RUNNING process observes an
        # in-process UTC midnight — so an addon that restarts before midnight
        # (HA watchdog, updates, daily reboot) would never prune, and plaintext
        # provenance would accumulate past retention_days forever.
        try:
            self._startup_maintenance()
        except Exception as exc:  # pragma: no cover - maintenance must never kill the writer
            logger.debug("Provenance startup maintenance failed: %s", exc)
        while True:
            with self._cond:
                while not self._records:
                    if self._stopping:
                        return
                    self._cond.wait(timeout=1.0)
                batch = list(self._records)
                self._records.clear()
                dropped = self._dropped
                self._dropped = 0
            if dropped:
                self._safe_write_line(
                    self._provenance_path(),
                    {
                        "schema_version": SCHEMA_VERSION,
                        "ts": self._clock(),
                        "record": "ledger_heartbeat",
                        "dropped": dropped,
                    },
                )
            for row in batch:
                if row is _STOP:
                    return
                self._dispatch(row)

    def _dispatch(self, row: dict) -> None:
        if row.get("__sidecar__"):
            payload = {k: v for k, v in row.items() if k != "__sidecar__"}
            self._safe_write_line(self.ledger_dir / _SIDECAR_FILENAME, payload)
            return
        self._safe_write_line(self._provenance_path(), row)

    # ── startup maintenance (writer-thread only) ───────────────────────────

    def _startup_maintenance(self) -> None:
        """Boot-time retention pass: pin today's date, gzip any leftover
        plaintext day-files from a prior run, then prune expired files.

        This makes retention robust to restart-before-midnight: a process that
        never lives across an in-process date rollover still compresses and
        prunes on every boot. Runs on the writer thread so the (potentially slow
        on a Pi SD card) gzip never blocks app startup or the audio path.
        """
        today = datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y-%m-%d")
        self._current_date = today
        try:
            stale = list(self.ledger_dir.glob(_PROVENANCE_GLOB))
        except OSError:
            stale = []
        for path in stale:
            date_str = path.name[len(_PROVENANCE_PREFIX) :].split(".", 1)[0]
            # Today's file is the active append target — leave it plaintext.
            if date_str != today:
                self._gzip_day(date_str)
        self._prune()

    def _seed_seen_prompt_hashes(self) -> None:
        """Load already-recorded sidecar hashes so a restart does not re-append
        a system prompt that is already on disk. Best-effort; a read failure
        just means the worst case (one redundant sidecar row) on this boot."""
        sidecar = self.ledger_dir / _SIDECAR_FILENAME
        try:
            if not sidecar.exists():
                return
            with sidecar.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        prompt_hash = json.loads(line).get("system_prompt_hash")
                    except (ValueError, TypeError):
                        continue
                    if prompt_hash:
                        self._seen_prompt_hashes.add(prompt_hash)
        except OSError as exc:
            logger.debug("Provenance sidecar seed failed: %s", exc)

    # ── rotation / files (writer-thread only) ──────────────────────────────

    def _provenance_path(self) -> Path:
        """Today's file path; rolls over (gzip prev + prune) on date change."""
        date = datetime.fromtimestamp(self._clock(), tz=UTC).strftime("%Y-%m-%d")
        if date != self._current_date:
            prev = self._current_date
            self._current_date = date
            if prev is not None:
                self._gzip_day(prev)
                self._prune()
        return self.ledger_dir / f"{_PROVENANCE_PREFIX}{date}.jsonl"

    def _gzip_day(self, date: str) -> None:
        src = self.ledger_dir / f"{_PROVENANCE_PREFIX}{date}.jsonl"
        if not src.exists():
            return
        dst = src.with_suffix(".jsonl.gz")
        tmp = src.with_suffix(".jsonl.gz.tmp")
        try:
            with src.open("rb") as f_in, gzip.open(tmp, "wb") as f_out:
                while chunk := f_in.read(65536):
                    f_out.write(chunk)
            tmp.replace(dst)  # atomic; a half-written .gz never replaces good data
            src.unlink()
        except OSError as exc:
            logger.debug("Provenance gzip of %s failed: %s", src, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune(self) -> None:
        cutoff = self._clock() - self.retention_days * 86400
        try:
            candidates = list(self.ledger_dir.glob(_PROVENANCE_GLOB)) + list(
                self.ledger_dir.glob(_PROVENANCE_GLOB + ".gz")
            )
        except OSError:
            return
        for path in candidates:
            date_str = path.name[len(_PROVENANCE_PREFIX) :].split(".", 1)[0]
            try:
                day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                continue
            if day.timestamp() < cutoff:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.debug("Provenance prune of %s failed: %s", path, exc)

    def _safe_write_line(self, path: Path, payload: dict) -> None:
        """Append one JSON line. Best-effort; a write failure drops the row."""
        try:
            line = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.debug("Provenance row not serialisable, dropping: %s", exc)
            return
        try:
            existed = path.exists()
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if not existed:
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
        except OSError as exc:
            logger.debug("Provenance write to %s failed (row dropped): %s", path, exc)
