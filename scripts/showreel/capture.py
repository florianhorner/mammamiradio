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

Prereq: mock_ha.py running + the station started against it (see README.md).

Usage:
    python scripts/showreel/capture.py --base http://127.0.0.1:8077 \
        --lead-track "Night in Venice" --final scripts/showreel_out/ma-pr-3836.mp3
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


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


def _wait_lead(base: str, lead: str, t0: float, max_start: float = 16.0, timeout: float = 240) -> bool:
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
    """Trigger seg and wait until it's queued (appears in upcoming)."""
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
        if _queued(base, seg) or _now(base)[0] == seg:
            print(f"      queued: {seg}")
            return True
        time.sleep(2)
    print(f"      !! {seg} never queued")
    return False


def _wait_type(base: str, seg: str, t0: float, timeout: float = 200) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _now(base)[0] == seg:
            off = time.time() - t0
            print(f"    [{off:6.1f}s] ON AIR: {seg}")
            return off
        time.sleep(1.0)
    return None


def _wait_until_not(base: str, seg: str, t0: float, timeout: float = 120) -> float:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _now(base)[0] != seg:
            return time.time() - t0
        time.sleep(1.0)
    return time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture a consecutive showreel arc (no stitching).")
    ap.add_argument("--base", default="http://127.0.0.1:8077")
    ap.add_argument("--raw", default="scripts/showreel_out/capture_raw.mp3")
    ap.add_argument("--final", default="scripts/showreel_out/ma-pr-3836.mp3")
    ap.add_argument("--lead-track", default="Night in Venice")
    ap.add_argument("--lead-tail", type=float, default=8.0, help="seconds of music tail to keep before banter")
    ap.add_argument("--settle", type=float, default=18.0)
    ap.add_argument("--max", type=float, default=420.0)
    args = ap.parse_args()

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

    print(f"[rec] recording -> {raw}")
    rec = subprocess.Popen(["curl", "-s", f"{args.base}/stream", "-o", str(raw), "--max-time", str(int(args.max))])
    t0 = time.time()
    offsets: dict[str, float] = {}
    try:
        # Wait for the long lead track near its start, then fire in REVERSE so the
        # queue ends up [banter, ad, news].
        _wait_lead(args.base, args.lead_track, t0)
        for seg in ("news_flash", "ad", "banter"):
            _trigger_queued(args.base, seg)
        # Lead track still playing; banter airs when it ends.
        offsets["banter"] = _wait_type(args.base, "banter", t0) or 0.0
        offsets["ad"] = _wait_type(args.base, "ad", t0) or 0.0
        offsets["news"] = _wait_type(args.base, "news_flash", t0) or 0.0
        news_end = _wait_until_not(args.base, "news_flash", t0)
        offsets["news_end"] = news_end
        time.sleep(2)
    finally:
        for p in (rec, warm):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    print("\n[timeline] on-air offsets:")
    for k, v in offsets.items():
        print(f"    {v:6.1f}s  {k}")

    # Auto-trim the contiguous arc: from (banter - lead_tail) to (news_end + 1).
    b = offsets.get("banter", 0)
    end = offsets.get("news_end", 0)
    if b > 0 and end > b:
        start = max(0, b - args.lead_tail)
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
        print(
            f"       internal: 0-{args.lead_tail:.0f}s music tail | "
            f"banter {args.lead_tail:.0f}s | ad {args.lead_tail + (offsets['ad'] - b):.0f}s | "
            f"news {args.lead_tail + (offsets['news'] - b):.0f}s"
        )
    else:
        print("\n[warn] could not detect a clean arc; inspect the raw capture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
