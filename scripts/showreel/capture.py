#!/usr/bin/env python3
"""Capture a CONSECUTIVE showreel arc from a locally-running mammamiradio, no stitching.

The segments are NOT sliced from separate spots and glued (that clashes music beds at
the seams). Instead we make banter -> ad -> news air back-to-back in the live stream and
capture that contiguous run, then trim only the OUTER ends. One real music tail at the
front, the producer's real transitions between segments, zero internal seams.

How the back-to-back ordering is achieved: operator triggers front-insert at the queue
HEAD (air-next). Firing them in REVERSE (news, ad, banter) during a long lead track means
they end up queued as [banter, ad, news]; when the lead track ends they air gaplessly.
The lead track must be long enough to cover all three generations (~2-3 min).

The arc is configurable (--arc, comma list in AIR order; default banter,ad,news_flash).
Home-event mode primes the HA baseline, consumes the warmup listener moment, stages a
real mock transition, then waits through the HA cache TTL before recording the final
banter. This makes the reactive directive (e.g. door-unlock "bentornato") deterministic.

Prereq: mock_ha.py running + the station started against it (see README.md).

Usage:
    python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
        --lead-track "Night in Venice" --final scripts/showreel_out/ma-pr-3836.mp3

    python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
        --lead-track "Night in Venice" --arc banter \
        --home-event lock.lock_ultra_8d3c:unlocked --mock-ha http://127.0.0.1:8123 \
        --ha-poll-interval 15 \
        --final scripts/showreel_out/door-bentornato.mp3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SETTLE_SECONDS = 18.0
LEAD_START_WINDOW_SECONDS = 24.0


def _get(base: str, path: str) -> dict:
    with urlopen(f"{base}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = Request(f"{base}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _status(base: str) -> dict:
    try:
        return _get(base, "/public-status")
    except Exception:
        return {}


def _now(base: str) -> tuple[str, str, float]:
    d = _status(base)
    n = d.get("now_streaming") or {}
    return n.get("type", ""), n.get("label", ""), float(d.get("current_progress_sec") or 0.0)


def _queued(base: str, seg: str) -> bool:
    up = _status(base).get("upcoming") or []
    return any(u.get("type") == seg for u in up)


def _queued_segment_id(base: str, seg: str) -> str | None:
    """Return the first queued instance of ``seg`` (operator inserts are at the head)."""
    for upcoming in _status(base).get("upcoming") or []:
        if upcoming.get("type") == seg and upcoming.get("id"):
            return str(upcoming["id"])
    return None


def _now_queue_id(base: str) -> str | None:
    now = (_status(base).get("now_streaming") or {}).get("metadata") or {}
    queue_id = now.get("queue_id")
    return str(queue_id) if queue_id else None


def _operator_force_pending(base: str) -> str | None:
    """Return the local admin surface's in-flight operator trigger, if any."""
    pending = _get(base, "/status").get("operator_force_pending")
    return str(pending) if pending else None


def _wait_lead(
    base: str,
    lead: str,
    t0: float,
    max_start: float = LEAD_START_WINDOW_SECONDS,
    timeout: float = 240,
) -> bool:
    """Wait until the long lead track is airing near its start."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        typ, label, prog = _now(base)
        if typ == "music" and lead.lower() in label.lower() and prog <= max_start:
            print(f"    [{time.time() - t0:6.1f}s] lead '{label}' airing (prog {prog:.0f}s) — firing sequence")
            return True
        time.sleep(2)
    print(f"    !! lead track '{lead}' not caught near start")
    return False


def _trigger_queued(base: str, seg: str, timeout: float = 150) -> bool:
    """Trigger seg and wait for its render to clear the operator guard into the queue."""
    print(f"    [trigger] {seg}")
    try:
        resp = _post(base, "/api/trigger", {"type": seg})
        if not resp.get("ok"):  # one-at-a-time guard busy — wait and retry once
            print(f"      busy ({resp.get('error')}); waiting 8s, retrying")
            time.sleep(8)
            resp = _post(base, "/api/trigger", {"type": seg})
    except Exception as e:  # offline tool: network blip shouldn't abort the run
        print(f"      !! trigger {seg} failed: {e}")
        return False
    if not resp.get("ok"):
        print(f"      !! {seg} rejected: {resp.get('error')}")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        # ``upcoming`` alone is insufficient: an automatic banter can already be
        # present while the forced banter is still rendering. The server clears this
        # guard only when the operator-requested segment front-inserts successfully.
        try:
            pending = _operator_force_pending(base)
        except Exception as exc:
            print(f"      !! could not verify operator queue state: {exc}")
            return False
        if pending is None and (_queued(base, seg) or _now(base)[0] == seg):
            print(f"      queued: {seg}")
            return True
        time.sleep(2)
    print(f"      !! {seg} never completed its operator queue")
    return False


def _wait_type(
    base: str,
    seg: str,
    t0: float,
    timeout: float = 200,
    queue_id: str | None = None,
) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _now(base)[0] == seg and (queue_id is None or _now_queue_id(base) == queue_id):
            off = time.time() - t0
            print(f"    [{off:6.1f}s] ON AIR: {seg}")
            return off
        time.sleep(1.0)
    return None


def _wait_until_not(
    base: str,
    seg: str,
    t0: float,
    timeout: float = 120,
    queue_id: str | None = None,
) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _now(base)[0] != seg or (queue_id is not None and _now_queue_id(base) != queue_id):
            return time.time() - t0
        time.sleep(1.0)
    return None


def _parse_arc(raw: str) -> list[str]:
    return [segment.strip() for segment in raw.split(",") if segment.strip()]


def _parse_home_event(raw: str) -> tuple[str, str] | None:
    if not raw:
        return None
    entity, sep, state = raw.partition(":")
    if not sep or not entity or not state:
        raise ValueError("--home-event must be ENTITY:STATE")
    return entity, state


def _prepare_home_event(base: str, mock_ha: str, entity: str, state: str, poll_interval: float) -> bool:
    """Prime the real HA diff and listener state before recording a reactive moment."""
    print("[home-event] priming baseline with news_flash")
    if not _trigger_queued(base, "news_flash"):
        print("      !! baseline refresh did not queue")
        return False
    print("[home-event] draining warmup listener moment with banter")
    if not _trigger_queued(base, "banter"):
        print("      !! warmup banter did not queue")
        return False

    print(f"[home-event] {entity} -> {state}")
    try:
        response = _post(mock_ha, "/__set", {"entity_id": entity, "state": state})
    except Exception as exc:
        print(f"      !! home-event flip failed: {exc}")
        return False
    if (
        not response.get("ok")
        or response.get("entity_id") != entity
        or response.get("new") != state
        or response.get("old") == state
    ):
        print(f"      !! home-event flip was not a real transition: {response}")
        return False

    # fetch_home_context is TTL-gated. Waiting longer than the configured interval
    # guarantees the final banter reads the changed state instead of the baseline cache.
    wait_seconds = poll_interval + 1.0
    print(f"[home-event] waiting {wait_seconds:.0f}s for the HA context cache to expire")
    time.sleep(wait_seconds)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Capture a consecutive showreel arc (no stitching).")
    ap.add_argument("--base", default="http://127.0.0.1:8077")
    ap.add_argument("--raw", default="scripts/showreel_out/capture_raw.mp3")
    ap.add_argument("--final", default="scripts/showreel_out/ma-pr-3836.mp3")
    ap.add_argument("--lead-track", default="Night in Venice")
    ap.add_argument("--lead-tail", type=float, default=8.0, help="seconds of music tail to keep before banter")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS)
    ap.add_argument("--max", type=float, default=420.0)
    ap.add_argument(
        "--arc",
        default="banter,ad,news_flash",
        help="segment types in AIR order, comma-separated (triggers fire in reverse)",
    )
    ap.add_argument(
        "--home-event",
        default="",
        metavar="ENTITY:STATE",
        help="flip a mock-HA entity right after the lead track is caught (e.g. lock.lock_ultra_8d3c:unlocked)",
    )
    ap.add_argument("--mock-ha", default="http://127.0.0.1:8123", help="mock_ha.py base URL for --home-event")
    ap.add_argument(
        "--ha-poll-interval",
        type=float,
        default=15.0,
        help="station HA context poll interval in seconds for --home-event (default: 15)",
    )
    ap.add_argument(
        "--first-wait",
        type=float,
        default=300.0,
        help="max seconds to wait for the first arc segment to air — must cover the "
        "REMAINDER of the lead track after the triggers fire (a lead caught at its "
        "start airs the arc a full track-length later)",
    )
    args = ap.parse_args(argv)

    arc = _parse_arc(args.arc)
    if not arc:
        print("!! --arc must name at least one segment type")
        return 1
    if len(set(arc)) != len(arc):
        print("!! --arc segment types must be unique (offsets are keyed by type)")
        return 1
    try:
        home_event = _parse_home_event(args.home_event)
    except ValueError as exc:
        print(f"!! {exc}")
        return 1
    if home_event and arc[0] != "banter":
        print("!! --home-event requires an arc that begins with banter")
        return 1
    if args.ha_poll_interval <= 0:
        print("!! --ha-poll-interval must be positive")
        return 1

    raw = REPO_ROOT / args.raw if not Path(args.raw).is_absolute() else Path(args.raw)
    final = REPO_ROOT / args.final if not Path(args.final).is_absolute() else Path(args.final)
    raw.parent.mkdir(parents=True, exist_ok=True)

    # Warmup listener so the captured banter isn't "first-listener" themed.
    # +10s so the warmup keeps the producer awake just past the recorder, then self-expires
    # (the finally block also reaps it; the short buffer bounds any orphan on signal).
    warm = subprocess.Popen(
        ["curl", "-s", f"{args.base}/stream", "-o", "/dev/null", "--max-time", str(int(args.max) + 10)]
    )
    print("[warmup] listener connected; settling")
    time.sleep(args.settle)

    rec: subprocess.Popen | None = None
    t0 = time.time()
    offsets: dict[str, float] = {}
    arc_queue_ids: dict[str, str] = {}
    try:
        # In home-event mode, stage every precondition before recording so the raw
        # file contains only the final lead tail and requested on-air arc.
        if not _wait_lead(args.base, args.lead_track, t0):
            return 1
        if home_event:
            entity, state = home_event
            if not _prepare_home_event(args.base, args.mock_ha, entity, state, args.ha_poll_interval):
                return 1

        print(f"[rec] recording -> {raw}")
        rec = subprocess.Popen(["curl", "-s", f"{args.base}/stream", "-o", str(raw), "--max-time", str(int(args.max))])
        t0 = time.time()
        for seg in reversed(arc):
            if not _trigger_queued(args.base, seg):
                return 1
            queue_id = _queued_segment_id(args.base, seg)
            if queue_id is None:
                print(f"      !! {seg} queued without a trackable segment id")
                return 1
            arc_queue_ids[seg] = queue_id
        # Lead track still playing; the arc airs when it ends. The FIRST segment's
        # wait must survive the whole remaining lead (up to a full track length);
        # the rest follow gaplessly and keep the tighter default.
        first_offset = _wait_type(args.base, arc[0], t0, timeout=args.first_wait, queue_id=arc_queue_ids[arc[0]])
        if first_offset is None:
            print(f"      !! first arc segment {arc[0]!r} never aired")
            return 1
        offsets[arc[0]] = first_offset
        for seg in arc[1:]:
            offset = _wait_type(args.base, seg, t0, queue_id=arc_queue_ids[seg])
            if offset is None:
                print(f"      !! arc segment {seg!r} never aired")
                return 1
            offsets[seg] = offset
        arc_end = _wait_until_not(args.base, arc[-1], t0, queue_id=arc_queue_ids[arc[-1]])
        if arc_end is None:
            print(f"      !! final arc segment {arc[-1]!r} did not finish")
            return 1
        offsets["arc_end"] = arc_end
        time.sleep(2)
    finally:
        for p in (rec, warm):
            if p is None:
                continue
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    print("\n[timeline] on-air offsets:")
    for k, v in offsets.items():
        print(f"    {v:6.1f}s  {k}")

    # Auto-trim the contiguous arc: from (first segment - lead_tail) to (arc_end + 1).
    first = offsets.get(arc[0], 0)
    end = offsets.get("arc_end", 0)
    if first > 0 and end > first:
        start = max(0, first - args.lead_tail)
        dur = (end - start) + 1.0
        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.2f}",
                "-t",
                f"{dur:.2f}",
                "-i",
                str(raw),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(final),
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(f"\n[error] ffmpeg trim failed; raw capture kept at {raw}\n{r.stderr[-600:]}")
            return 1
        print(f"\n[done] final continuous clip -> {final}  (~{dur:.0f}s, no stitching)")
        internal = " | ".join(
            f"{seg} {args.lead_tail + (offsets[seg] - first):.0f}s" for seg in arc if offsets.get(seg)
        )
        print(f"       internal: 0-{args.lead_tail:.0f}s music tail | {internal}")
    else:
        print("\n[warn] could not detect a clean arc; inspect the raw capture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
