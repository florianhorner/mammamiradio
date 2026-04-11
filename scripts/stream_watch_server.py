#!/usr/bin/env python3
"""Read-only sidecar monitor for the live station.

Runs independently from the FastAPI app so it can be started during a
no-reload / no-interruption stream window.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


def _upstream_base_url() -> str:
    """Resolve the local app endpoint, following workspace port overrides."""
    port = os.getenv("MAMMAMIRADIO_PORT") or os.getenv("CONDUCTOR_PORT") or "8000"
    return f"http://127.0.0.1:{port}"


def _fetch_json(path: str) -> dict:
    req = Request(f"{_upstream_base_url()}{path}", headers={"Accept": "application/json"})
    with urlopen(req, timeout=5) as resp:
        return json.load(resp)


def _build_summary() -> dict:
    public = _fetch_json("/public-status")
    health = _fetch_json("/healthz")
    ready = _fetch_json("/readyz")

    stream_log = public.get("stream_log", [])
    last_banter = next((item for item in reversed(stream_log) if item.get("type") == "banter"), {})
    banter_meta = last_banter.get("metadata", {}) if isinstance(last_banter, dict) else {}

    ai_working = bool(last_banter) and not banter_meta.get("canned", False) and not banter_meta.get("error")
    ai_mode = "working" if ai_working else "unclear"
    ai_provider = "Recent live banter detected" if ai_working else "Waiting for a live banter segment"

    now = public.get("now_streaming", {}) or {}
    current_source = public.get("current_source", {}) or {}
    golden_path = public.get("golden_path", {}) or {}
    uptime_s = health.get("uptime_s")
    illusion_window_minutes = round(float(uptime_s) / 60, 1) if isinstance(uptime_s, int | float) else None

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "ai": {
            "status": ai_mode,
            "provider_hint": ai_provider,
            "last_banter_timestamp": last_banter.get("timestamp"),
            "last_banter_canned": bool(banter_meta.get("canned", False)),
            "last_banter_lines": len(banter_meta.get("lines", []) or []),
        },
        "music": {
            "source_label": current_source.get("label", ""),
            "source_kind": current_source.get("kind", ""),
            "now_type": now.get("type", ""),
            "now_label": now.get("label", ""),
            "upcoming": [item.get("label", "") for item in public.get("upcoming", [])[:5]],
            "upcoming_mode": public.get("upcoming_mode", ""),
            "queue_depth": ready.get("queue_depth"),
            "readiness": ready.get("status", ""),
            "illusion_window_minutes": illusion_window_minutes,
        },
        "golden_path": {
            "headline": golden_path.get("headline", ""),
            "detail": golden_path.get("detail", ""),
            "blocking": bool(golden_path.get("blocking", False)),
        },
    }


def _html(summary: dict) -> str:
    def e(value: object) -> str:
        return escape("" if value is None else str(value))

    ai = summary["ai"]
    music = summary["music"]
    golden = summary["golden_path"]

    def badge(ok: bool, on: str, off: str) -> str:
        label = on if ok else off
        cls = "ok" if ok else "warn"
        return f'<span class="badge {cls}">{e(label)}</span>'

    if music["upcoming"]:
        upcoming = "".join(f"<li>{e(item)}</li>" for item in music["upcoming"])
    else:
        upcoming = (
            "<li>Building next segments...</li>"
            if music.get("upcoming_mode") == "building"
            else "<li>Nothing queued</li>"
        )
    last_banter = "AI-generated" if not ai["last_banter_canned"] else "canned"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MammaMiRadio Monitor</title>
  <style>
    :root {{
      --bg: #1e1714; --panel: #2d1d18; --panel2: #3a241d; --text: #f5edd8;
      --muted: #c9bfa8; --ok: #2563eb; --warn: #eccc30; --bad: #c44a4a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 24px; font: 14px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
      background: linear-gradient(180deg, #2d1d18 0%, #1e1714 100%); color: var(--text);
    }}
    .wrap {{ max-width: 880px; margin: 0 auto; display: grid; gap: 16px; }}
    .mast {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }}
    h1 {{ margin: 0; font-size: 28px; font-style: italic; }}
    .sub {{ color: var(--muted); }}
    .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
    .card {{ background: var(--panel); border: 1px solid rgba(245,237,216,0.12); border-radius: 12px; padding: 16px; }}
    .card h2 {{
      margin: 0 0 10px; font-size: 12px; text-transform: uppercase;
      letter-spacing: 0.12em; color: var(--muted);
    }}
    .big {{ font-size: 22px; font-weight: 700; }}
    .badge {{ display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 700; }}
    .badge.ok {{ background: rgba(37,99,235,0.18); color: #9bc0ff; }}
    .badge.warn {{ background: rgba(236,204,48,0.18); color: #f4d048; }}
    .badge.bad {{ background: rgba(196,74,74,0.18); color: #ffb0b0; }}
    ul {{ margin: 8px 0 0 18px; padding: 0; }}
    code {{ color: #f4d048; }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="mast">
      <div>
        <h1>MammaMiRadio Monitor</h1>
        <div class="sub">Read-only sidecar. Safe during the 60-minute illusion run.</div>
      </div>
      <div class="muted">Updated: {e(summary["generated_at"])}</div>
    </div>
    <div class="grid">
      <section class="card">
        <h2>AI</h2>
        <div class="big">{badge(ai["status"] == "working", "Working", "Unclear")}</div>
        <div>{e(ai["provider_hint"])}</div>
        <div class="muted">Last banter: {e(last_banter)}, {e(ai["last_banter_lines"])} lines</div>
      </section>
      <section class="card">
        <h2>Now</h2>
        <div class="big">{e(music["now_type"] or "unknown")}</div>
        <div>{e(music["now_label"] or "no label")}</div>
        <div class="muted">Source: {e(music["source_label"] or music["source_kind"])}</div>
        <div class="muted">Queue: {e(music["queue_depth"])} ({e(music["readiness"] or "unknown")})</div>
      </section>
    </div>
    <section class="card">
      <h2>Upcoming</h2>
      <ul>{upcoming}</ul>
    </section>
    <section class="card">
      <h2>Attention</h2>
      <div>{e(golden["headline"])}</div>
      <div class="muted">{e(golden["detail"])}</div>
    </section>
  </div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        summary = _build_summary()
        if self.path == "/api/summary":
            payload = json.dumps(summary).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        page = _html(summary).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8002), Handler)
    print("Monitor listening on http://127.0.0.1:8002", flush=True)
    server.serve_forever()
