from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from fakeitaliradio.models import Segment
from fakeitaliradio.scheduler import preview_upcoming

logger = logging.getLogger(__name__)

router = APIRouter()

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radio Italì — Control Plane</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'SF Mono', 'Fira Code', monospace;
    background: #0a0a0a; color: #e0e0e0;
    padding: 24px; max-width: 900px; margin: 0 auto;
  }
  h1 { color: #ff6b35; font-size: 28px; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 13px; margin-bottom: 24px; }
  .status-bar {
    display: flex; gap: 16px; margin-bottom: 24px;
    flex-wrap: wrap;
  }
  .stat {
    background: #1a1a1a; border: 1px solid #333; border-radius: 8px;
    padding: 12px 16px; min-width: 120px;
  }
  .stat-label { font-size: 11px; color: #888; text-transform: uppercase; }
  .stat-value { font-size: 22px; font-weight: bold; margin-top: 2px; }
  .spotify-on { color: #1db954; }
  .spotify-off { color: #ff4444; }

  .section { margin-bottom: 24px; }
  .section-title {
    font-size: 13px; color: #ff6b35; text-transform: uppercase;
    letter-spacing: 1px; margin-bottom: 8px;
    border-bottom: 1px solid #222; padding-bottom: 4px;
  }

  .now-playing {
    background: linear-gradient(135deg, #1a1a1a, #222);
    border: 1px solid #ff6b35; border-radius: 8px;
    padding: 16px; margin-bottom: 24px;
  }
  .now-playing .track { font-size: 18px; color: #fff; }
  .now-playing .meta { font-size: 12px; color: #888; margin-top: 4px; }

  .segment-log { list-style: none; }
  .segment-log li {
    padding: 6px 0; border-bottom: 1px solid #1a1a1a;
    display: flex; align-items: center; gap: 10px;
    font-size: 13px;
  }
  .seg-icon {
    width: 24px; height: 24px; border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; flex-shrink: 0;
  }
  .seg-music { background: #1db954; color: #000; }
  .seg-banter { background: #ff6b35; color: #000; }
  .seg-ad { background: #9b59b6; color: #fff; }
  .seg-time { color: #555; font-size: 11px; min-width: 50px; }
  .seg-label { flex: 1; }

  .upcoming { list-style: none; }
  .upcoming li {
    padding: 4px 0; font-size: 13px; color: #999;
    display: flex; gap: 6px; align-items: center;
  }
  .upcoming .num { color: #555; min-width: 18px; }
  .upcoming .actions { margin-left: auto; display: flex; gap: 2px; opacity: 0.4; }
  .upcoming li:hover .actions { opacity: 1; }
  .act-btn {
    background: none; border: 1px solid #333; border-radius: 3px;
    color: #888; font-size: 10px; padding: 1px 5px; cursor: pointer;
    font-family: inherit;
  }
  .act-btn:hover { border-color: #ff6b35; color: #ff6b35; }

  .script-box {
    background: #111; border: 1px solid #222; border-radius: 6px;
    padding: 12px; font-size: 13px; line-height: 1.6;
  }
  .script-host { color: #ff6b35; font-weight: bold; }
  .script-ad-brand { color: #9b59b6; font-weight: bold; }

  .jokes { list-style: none; }
  .jokes li {
    padding: 4px 0; font-size: 12px; color: #777;
    font-style: italic;
  }

  .player-bar {
    position: fixed; bottom: 0; left: 0; right: 0;
    background: #111; border-top: 1px solid #333;
    padding: 12px 24px; display: flex; align-items: center; gap: 16px;
  }
  .player-bar audio { flex: 1; height: 32px; }
  .player-bar .label { color: #ff6b35; font-size: 13px; white-space: nowrap; }

  .connect-banner {
    background: #331500; border: 1px solid #ff6b35; border-radius: 8px;
    padding: 12px 16px; margin-bottom: 16px; display: none;
    font-size: 13px; line-height: 1.5;
  }
  .connect-banner strong { color: #ff6b35; }

  .controls { display: flex; gap: 8px; margin-bottom: 16px; }
  .btn {
    background: #222; border: 1px solid #444; border-radius: 6px;
    color: #e0e0e0; padding: 8px 16px; cursor: pointer; font-size: 13px;
    font-family: inherit;
  }
  .btn:hover { background: #333; border-color: #ff6b35; }
  .btn:active { background: #ff6b35; color: #000; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 600px) { .grid { grid-template-columns: 1fr; } }

  body { padding-bottom: 80px; }

  .debug-toggle {
    color: #555; font-size: 11px; cursor: pointer; margin-bottom: 8px;
    user-select: none;
  }
  .debug-toggle:hover { color: #ff6b35; }
  .debug-panel { display: none; }
  .debug-panel.open { display: block; }
  .debug-log {
    background: #0d0d0d; border: 1px solid #222; border-radius: 4px;
    padding: 10px; font-size: 12px; color: #999; line-height: 1.8;
    max-height: 350px; overflow-y: auto;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .debug-log .entry { padding: 2px 0; border-bottom: 1px solid #111; }
  .debug-log .ts { color: #555; font-size: 11px; margin-right: 6px; }
  .debug-log .lvl { font-size: 10px; font-weight: bold; padding: 1px 4px;
    border-radius: 2px; margin-right: 6px; }
  .debug-log .lvl-error { background: #441111; color: #ff4444; }
  .debug-log .lvl-warning { background: #332200; color: #ffaa00; }
  .debug-log .lvl-info { background: #112211; color: #66aa66; }
  .debug-log .msg { color: #ccc; }
  .debug-log .detail { color: #666; font-size: 11px; margin-left: 4px; }
</style>
</head>
<body>

<h1>Radio Ital&igrave;</h1>
<div class="subtitle" id="uptime">Loading...</div>

<div class="status-bar">
  <div class="stat">
    <div class="stat-label">Queue</div>
    <div class="stat-value" id="queue">-</div>
  </div>
  <div class="stat">
    <div class="stat-label">Segments</div>
    <div class="stat-value" id="segments">-</div>
  </div>
  <div class="stat">
    <div class="stat-label">Tracks</div>
    <div class="stat-value" id="tracks">-</div>
  </div>
  <div class="stat">
    <div class="stat-label">Spotify</div>
    <div class="stat-value" id="spotify">-</div>
  </div>
</div>

<div class="connect-banner" id="connect-banner">
  <strong>Spotify not connected.</strong> Open Spotify app, tap the speaker icon,
  and select <strong>fakeitaliradio</strong>. Music will use placeholders until connected.
</div>

<div class="controls">
  <button class="btn" onclick="doShuffle()">Shuffle</button>
  <button class="btn" onclick="doSkip()">Skip</button>
  <button class="btn" onclick="doPurge()">Purge Queue</button>
</div>

<div class="now-playing" id="now-playing">
  <div class="track" id="np-track">...</div>
  <div class="meta" id="np-meta"></div>
</div>

<div class="grid">
  <div>
    <div class="section">
      <div class="section-title">Segment History</div>
      <ul class="segment-log" id="log"></ul>
    </div>
  </div>
  <div>
    <div class="section">
      <div class="section-title">Up Next</div>
      <ul class="upcoming" id="upcoming"></ul>
    </div>

    <div class="section">
      <div class="section-title">Last Banter</div>
      <div class="script-box" id="banter">...</div>
    </div>

    <div class="section">
      <div class="section-title">Last Ad</div>
      <div class="script-box" id="ad">...</div>
    </div>

    <div class="section">
      <div class="section-title">Running Jokes</div>
      <ul class="jokes" id="jokes"></ul>
    </div>
  </div>
</div>

<div class="section">
  <div class="debug-toggle" onclick="document.getElementById('debug').classList.toggle('open')">
    ▶ Debug logs (click to expand)
  </div>
  <div class="debug-panel" id="debug">
    <div class="section-title">go-librespot</div>
    <div class="debug-log" id="debug-gl"></div>
    <div class="section-title" style="margin-top:8px">Errors</div>
    <div class="debug-log" id="debug-errors"></div>
  </div>
</div>

<div class="player-bar">
  <div class="label">LISTEN</div>
  <audio id="audio" controls preload="none">
    <source src="/stream" type="audio/mpeg">
  </audio>
</div>

<script>
const icons = { music: '♫', banter: '🎙', ad: '📢' };
const cls = { music: 'seg-music', banter: 'seg-banter', ad: 'seg-ad' };

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function fmt(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
}

function fmtUptime(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm ' + (s % 60) + 's';
}

let apiKeyCache;
function getApiKey() {
  if (apiKeyCache !== undefined) return apiKeyCache;

  const params = new URLSearchParams(window.location.search);
  const queryKey = params.get('key');
  const storedKey = sessionStorage.getItem('dashboard_api_key');
  const key = queryKey || storedKey || '';

  if (queryKey) {
    sessionStorage.setItem('dashboard_api_key', queryKey);
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete('key');
    window.history.replaceState({}, '', cleanUrl.toString());
  }

  apiKeyCache = key;
  return key;
}

async function apiFetch(url, options = {}) {
  const key = getApiKey();
  const headers = { ...(options.headers || {}) };
  if (key) headers['X-API-Key'] = key;
  return fetch(url, { ...options, headers });
}

async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();

    document.getElementById('uptime').textContent =
      'Uptime: ' + fmtUptime(d.uptime_sec);
    document.getElementById('queue').textContent = d.queue_depth;
    document.getElementById('segments').textContent = d.segments_produced;
    document.getElementById('tracks').textContent = d.tracks_played;

    const sp = document.getElementById('spotify');
    sp.textContent = d.spotify_connected ? 'ON' : 'OFF';
    sp.className = 'stat-value ' + (d.spotify_connected ? 'spotify-on' : 'spotify-off');

    document.getElementById('connect-banner').style.display =
      d.spotify_connected ? 'none' : 'block';

    // Now streaming (what the listener actually hears)
    const ns = d.now_streaming || {};
    const npEl = document.getElementById('np-track');
    const npMeta = document.getElementById('np-meta');
    if (ns.label) {
      npEl.textContent = ns.label;
      npMeta.textContent = ns.type ? ns.type.toUpperCase() + ' — streaming now' : '';
    } else {
      npEl.textContent = 'Waiting for first segment...';
      npMeta.textContent = 'Press play on the audio player below';
    }

    // Stream log = what was ACTUALLY PLAYED (newest first)
    const log = document.getElementById('log');
    const streamLog = (d.stream_log || []).slice().reverse();
    log.innerHTML = streamLog.map((e, i) =>
      (() => {
        const safeType = e.type in cls ? e.type : '';
        return '<li' + (i === 0 ? ' style="color:#fff;font-weight:bold"' : '') + '>' +
          '<span class="seg-icon ' + (cls[safeType] || '') + '">' + (icons[safeType] || '?') + '</span>' +
          '<span class="seg-time">' + esc(fmt(e.timestamp)) + '</span>' +
          '<span class="seg-label">' + esc(e.label) + (i === 0 ? ' ← NOW' : '') + '</span>' +
        '</li>';
      })()
    ).join('');

    // Show what's in queue (produced but not yet streamed)
    const prodLog = d.produced_log || [];
    const streamedCount = streamLog.length;
    // Items in queue = last N produced items that haven't streamed yet
    const queueItems = prodLog.slice(streamedCount);
    if (queueItems.length > 0) {
      log.innerHTML += '<li style="color:#555;padding-top:8px;border-top:1px solid #333">' +
        '<span class="seg-icon" style="background:#333;color:#888">⏳</span>' +
        '<span class="seg-label">In queue: ' + queueItems.map(e => esc(e.label)).join(', ') + '</span></li>';
    }

    // Upcoming (full schedule: music + banter + ads)
    const up = document.getElementById('upcoming');
    up.innerHTML = (d.upcoming || []).map((e, i) => {
      const idxNum = Number(e.playlist_index);
      const hasIndex = Number.isInteger(idxNum) && idxNum >= 0;
      const safeType = e.type in cls ? e.type : '';
      const isMusic = safeType === 'music' && hasIndex;
      const actions = isMusic
        ? '<span class="actions">' +
            '<button class="act-btn" onclick="playNext(' + idxNum + ')" title="Play next">▲</button>' +
            '<button class="act-btn" onclick="removeTrack(' + idxNum + ')" title="Remove">✕</button>' +
          '</span>'
        : '';
      return '<li>' +
        '<span class="seg-icon ' + (cls[safeType] || '') + '" style="width:18px;height:18px;font-size:10px">' +
          (icons[safeType] || '?') + '</span> ' +
        '<span class="seg-label">' + esc(e.label) + '</span>' +
        actions +
      '</li>';
    }).join('') || '<li>...</li>';

    // Show banter/ad scripts from the currently or most recently streamed segments
    const banter = document.getElementById('banter');
    // Find the most recent banter in stream_log
    const lastBanter = streamLog.find(e => e.type === 'banter');
    if (lastBanter && lastBanter.metadata && lastBanter.metadata.lines) {
      banter.innerHTML = lastBanter.metadata.lines.map(l =>
        '<div><span class="script-host">' + esc(l.host) + ':</span> ' + esc(l.text) + '</div>'
      ).join('');
    } else if (d.last_banter_script && d.last_banter_script.length) {
      banter.innerHTML = d.last_banter_script.map(l =>
        '<div><span class="script-host">' + esc(l.host) + ':</span> ' + esc(l.text) + '</div>'
      ).join('');
    } else {
      banter.innerHTML = '...';
    }

    // Ad
    const ad = document.getElementById('ad');
    const lastAd = streamLog.find(e => e.type === 'ad');
    if (lastAd && lastAd.metadata && lastAd.metadata.text) {
      ad.innerHTML =
        '<div><span class="script-ad-brand">' + esc(lastAd.metadata.brand || '?') + '</span> ' +
        '(voice: ' + esc(lastAd.metadata.voice || lastAd.metadata.host || '?') + ')</div>' +
        '<div style="margin-top:6px;color:#aaa">' + esc(lastAd.metadata.text) + '</div>';
    } else if (d.last_ad_script && d.last_ad_script.brand) {
      ad.innerHTML =
        '<div><span class="script-ad-brand">' + esc(d.last_ad_script.brand) + '</span> ' +
        '(voice: ' + esc(d.last_ad_script.voice || d.last_ad_script.host || '?') + ')</div>' +
        '<div style="margin-top:6px;color:#aaa">' + esc(d.last_ad_script.text) + '</div>';
    } else {
      ad.innerHTML = '...';
    }

    // Jokes
    const jokes = document.getElementById('jokes');
    jokes.innerHTML = (d.running_jokes || []).map(j =>
      '<li>"' + esc(j) + '"</li>'
    ).join('') || '<li>No running jokes yet...</li>';

    // Debug: go-librespot log (parse structured log format)
    const gl = document.getElementById('debug-gl');
    if (gl && d.go_librespot_log) {
      gl.innerHTML = d.go_librespot_log.map(raw => {
        raw = String(raw || '').trim();
        if (!raw) return '';
        // Parse: time="..." level=info msg="..." key="val"
        const timeM = raw.match(/time="([^"]+)"/);
        const levelM = raw.match(/level=(\\w+)/);
        const msgM = raw.match(/msg="([^"]+)"/);
        const ts = timeM ? timeM[1].split('T')[1]?.split('+')[0] || '' : '';
        const lvlRaw = levelM ? levelM[1].toLowerCase() : 'info';
        const lvl = ['info', 'warning', 'error'].includes(lvlRaw) ? lvlRaw : 'info';
        const msg = msgM ? msgM[1] : raw;
        // Extract extra fields (error=, uri=, etc.)
        const extras = [];
        const errM = raw.match(/error="([^"]+)"/);
        const uriM = raw.match(/uri="([^"]+)"/);
        if (errM) extras.push(errM[1]);
        if (uriM) extras.push(uriM[1].replace('spotify:track:',''));
        const detail = extras.length ? '<span class="detail">' + esc(extras.join(' ')) + '</span>' : '';
        return '<div class="entry">' +
          '<span class="ts">' + esc(ts) + '</span>' +
          '<span class="lvl lvl-' + lvl + '">' + esc(lvl.toUpperCase()) + '</span>' +
          '<span class="msg">' + esc(msg) + '</span>' +
          detail + '</div>';
      }).join('');
      gl.scrollTop = gl.scrollHeight;
    }

    // Debug: producer errors
    const errs = document.getElementById('debug-errors');
    if (errs && d.producer_errors) {
      errs.innerHTML = d.producer_errors.length
        ? d.producer_errors.map(e =>
            '<div class="entry">' +
              '<span class="lvl lvl-error">ERR</span>' +
              '<span class="msg">' + esc(e.type + ': ' + e.label) + '</span>' +
              '<span class="detail">' + esc((e.metadata && e.metadata.error) || '') + '</span>' +
            '</div>'
          ).join('')
        : '<div class="entry"><span class="msg" style="color:#66aa66">No errors</span></div>';
    }

  } catch (e) {
    console.error('refresh failed', e);
  }
}

async function doShuffle() {
  await apiFetch('/api/shuffle', { method: 'POST' });
  refresh();
}

async function doSkip() {
  await apiFetch('/api/skip', { method: 'POST' });
  const audio = document.getElementById('audio');
  audio.pause();
  audio.load();
  audio.play();
  refresh();
}

async function doPurge() {
  await apiFetch('/api/purge', { method: 'POST' });
  refresh();
}

async function removeTrack(idx) {
  await apiFetch('/api/playlist/remove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({index: idx}),
  });
  refresh();
}

async function playNext(idx) {
  await apiFetch('/api/playlist/move_to_next', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({index: idx}),
  });
  refresh();
}

getApiKey();
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""


async def _audio_generator(request: Request):
    """Stream audio at playback rate so dashboard stays in sync with listener."""
    CHUNK = 4096  # smaller chunks for tighter pacing
    segment_queue = request.app.state.queue
    state = request.app.state.station_state
    config = request.app.state.config

    # Throttle to bitrate so server stays in sync with what listener hears
    bytes_per_sec = (config.station.bitrate * 1000) / 8  # 192kbps = 24000 B/s
    chunk_duration = CHUNK / bytes_per_sec  # seconds per chunk

    while True:
        if await request.is_disconnected():
            logger.info("Client disconnected")
            state.now_streaming = {}
            break

        try:
            segment: Segment = await asyncio.wait_for(
                segment_queue.get(), timeout=30.0
            )
        except asyncio.TimeoutError:
            logger.warning("Queue empty for 30s, waiting...")
            continue

        # Mark this segment as NOW STREAMING
        state.on_stream_segment(segment)

        logger.info(
            ">>> NOW STREAMING %s: %s",
            segment.type.value,
            segment.metadata.get("title", segment.metadata),
        )

        try:
            send_start = time.monotonic()
            bytes_sent = 0
            with open(segment.path, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk
                    bytes_sent += len(chunk)

                    # Throttle: sleep to match playback rate
                    elapsed = time.monotonic() - send_start
                    expected = bytes_sent / bytes_per_sec
                    ahead = expected - elapsed
                    if ahead > 0.01:
                        await asyncio.sleep(ahead)
                    else:
                        await asyncio.sleep(0)
        finally:
            segment.path.unlink(missing_ok=True)
            segment_queue.task_done()


def _require_write_auth(request: Request) -> None:
    expected_api_key = getattr(request.app.state.config, "dashboard_api_key", "")
    if not expected_api_key:
        return

    provided_api_key = request.headers.get("x-api-key") or request.query_params.get("key")
    if not provided_api_key or not secrets.compare_digest(provided_api_key, expected_api_key):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@router.get("/stream")
async def stream(request: Request):
    config = request.app.state.config
    headers = {
        "Content-Type": "audio/mpeg",
        "icy-name": config.station.name,
        "icy-genre": config.station.theme[:64],
        "icy-br": str(config.station.bitrate),
        "Cache-Control": "no-cache, no-store",
        "Connection": "keep-alive",
    }
    return StreamingResponse(
        _audio_generator(request),
        headers=headers,
        media_type="audio/mpeg",
    )


@router.get("/api/logs")
async def logs(lines: int = 50):
    """Return recent go-librespot + producer logs."""
    return {
        "go_librespot": _tail_log("tmp/go-librespot.log", lines),
    }


@router.post("/api/shuffle")
async def shuffle_playlist(request: Request):
    """Shuffle upcoming tracks."""
    import random
    _require_write_auth(request)
    state = request.app.state.station_state
    random.shuffle(state.playlist)
    return {"ok": True, "message": "Playlist shuffled"}


@router.post("/api/skip")
async def skip_track(request: Request):
    """Skip the currently streaming segment."""
    _require_write_auth(request)
    state = request.app.state.station_state
    state.now_streaming = {"type": "skipping", "label": "Skipping...", "started": time.time()}
    return {"ok": True}


@router.post("/api/purge")
async def purge_queue(request: Request):
    """Drain all pre-produced segments from the queue."""
    _require_write_auth(request)
    q = request.app.state.queue
    purged = 0
    while not q.empty():
        try:
            seg = q.get_nowait()
            seg.path.unlink(missing_ok=True)
            q.task_done()
            purged += 1
        except Exception:
            break
    return {"ok": True, "purged": purged}


@router.post("/api/playlist/remove")
async def remove_track(request: Request):
    """Remove a track from playlist by index."""
    _require_write_auth(request)
    body = await request.json()
    idx = body.get("index", -1)
    state = request.app.state.station_state
    if not (0 <= idx < len(state.playlist)):
        raise HTTPException(status_code=400, detail="Invalid index")
    if len(state.playlist) <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last track")
    removed = state.playlist.pop(idx)
    return {"ok": True, "removed": removed.display}


@router.post("/api/playlist/move")
async def move_track(request: Request):
    """Move a track in the playlist. body: {from: N, to: N}"""
    _require_write_auth(request)
    body = await request.json()
    src = body.get("from", -1)
    dst = body.get("to", -1)
    state = request.app.state.station_state
    pl = state.playlist
    if 0 <= src < len(pl) and 0 <= dst < len(pl):
        track = pl.pop(src)
        pl.insert(dst, track)
        return {"ok": True, "moved": track.display}
    raise HTTPException(status_code=400, detail="Invalid indices")


@router.post("/api/playlist/move_to_next")
async def move_to_next(request: Request):
    """Move a track to play next (position 0 in upcoming)."""
    _require_write_auth(request)
    body = await request.json()
    idx = body.get("index", -1)
    state = request.app.state.station_state
    pl = state.playlist

    # Find current position
    current_idx = 0
    if state.current_track:
        for i, t in enumerate(pl):
            if t.spotify_id == state.current_track.spotify_id:
                current_idx = i
                break

    # The "next" position is current_idx + 1
    next_pos = (current_idx + 1) % len(pl) if pl else 0

    if 0 <= idx < len(pl):
        track = pl.pop(idx)
        # Adjust next_pos if we popped before it
        if idx < next_pos:
            next_pos -= 1
        pl.insert(next_pos, track)
        return {"ok": True, "moved": track.display, "to_position": next_pos}
    raise HTTPException(status_code=400, detail="Invalid index")


@router.get("/status")
async def status(request: Request):
    state = request.app.state.station_state
    config = request.app.state.config
    segment_queue = request.app.state.queue
    start_time = request.app.state.start_time
    return {
        "station": config.station.name,
        "queue_depth": segment_queue.qsize(),
        "segments_produced": state.segments_produced,
        "tracks_played": len(state.played_tracks),
        "running_jokes": state.running_jokes,
        "uptime_sec": round(time.time() - start_time),
        "spotify_connected": state.spotify_connected,
        # What the listener hears RIGHT NOW
        "now_streaming": state.now_streaming,
        # What the producer has made (queued, waiting to stream)
        "produced_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp}
            for e in state.segment_log
        ],
        # What has actually been streamed to the listener
        "stream_log": [
            {"type": e.type, "label": e.label, "timestamp": e.timestamp,
             "metadata": e.metadata}
            for e in state.stream_log
        ],
        "upcoming": preview_upcoming(state, config.pacing, state.playlist, count=5),
        "last_banter_script": state.last_banter_script,
        "last_ad_script": state.last_ad_script,
        "go_librespot_log": _tail_log("tmp/go-librespot.log", 15),
        "producer_errors": [
            {"type": e.type, "label": e.label, "metadata": e.metadata}
            for e in state.segment_log
            if e.metadata.get("error")
        ][-5:],
    }


def _tail_log(path: str, lines: int = 15) -> list[str]:
    try:
        with open(path, "r") as f:
            return f.readlines()[-lines:]
    except Exception:
        return []
