/*
 * Mamma Mi Radio — Listener Client
 *
 * Drives the five-band listener site (nav, persistent now-playing strip,
 * hero, palinsesto, dediche) against /public-status + /public-listener-requests
 * (brand-engine PR-F: no admin endpoints — works on any public deploy).
 *
 * See docs/design/system.md §§ "Listener site composition" for the canonical layout.
 * Loads AFTER tokens.css + base.css + listener.css + waveform.js.
 */

(function () {
  'use strict';

  /* ── Base path resolution (supports HA ingress) ── */
  const _base = (() => {
    const p = window.location.pathname.replace(/\/+$/, '');
    if (p.endsWith('/listen')) return p.slice(0, -7);
    return p === '' ? '' : p;
  })();

  /* ── CSRF ── */
  const csrfToken = document.querySelector('meta[name="mammamiradio-csrf-token"]')?.content || '';
  const _nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const request = new Request(input, init);
    if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method.toUpperCase())) {
      const headers = new Headers(request.headers);
      headers.set('X-Radio-CSRF-Token', csrfToken);
      return _nativeFetch(input, { ...init, headers });
    }
    return _nativeFetch(input, init);
  };

  /* Listener-UI copy in the active super_italian_mode. Baked into the page
     by the Jinja template (see #mmr-copy-bootstrap), not refetched on poll. */
  const COPY = (() => {
    const el = document.getElementById('mmr-copy-bootstrap');
    try { return (el && JSON.parse(el.textContent)) || {}; } catch { return {}; }
  })();
  function _t(key, fallback) { return COPY[key] || fallback || ''; }

  /* ── State ── */
  const state = {
    caps: null,
    status: null,
    requests: [],
    isPlaying: false,
    wantsPlay: false,
    firstDataReceived: false,
    wasStopped: false,
    sessionStart: Date.now(),
    currentLabel: null,
    currentProgressMs: 0,
    segmentDurationMs: 0,
    progressTimer: null,
  };

  /* ── DOM refs (cached after DOMContentLoaded) ── */
  let audio, playBtn, playBtnSmall, playIcon, pauseIcon;

  /* ── Helpers ── */
  function $(id) { return document.getElementById(id); }
  function escHtml(v) {
    return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function fmtTime(totalSec) {
    if (!isFinite(totalSec) || totalSec < 0) return '0:00';
    const m = Math.floor(totalSec / 60);
    const s = Math.floor(totalSec % 60).toString().padStart(2, '0');
    return m + ':' + s;
  }
  function relativeMinutes(ts) {
    if (!ts) return '';
    const diff = (Date.now() - ts * 1000) / 60000;
    if (diff < 1) return _t('now', 'now');
    if (diff < 60) return Math.round(diff) + ' ' + _t('minutes_ago', 'min ago');
    return Math.round(diff / 60) + ' ' + _t('hours_ago', 'hr ago');
  }
  function segmentKindLabel(type) {
    switch ((type || '').toLowerCase()) {
      case 'music': return _t('seg_music', 'Music');
      case 'banter': return _t('seg_banter', 'Banter');
      case 'ad': return _t('seg_ad', 'Sponsored');
      case 'news':
      case 'news_flash': return _t('seg_news', 'News');
      case 'jingle': return _t('seg_jingle', 'Jingle');
      case 'welcome': return _t('seg_welcome', 'Welcome');
      case 'idle': return _t('seg_idle', 'Idle');
      default: return (type || _t('seg_default', 'On Air')).toUpperCase();
    }
  }

  function segmentPillClass(type) {
    switch ((type || '').toLowerCase()) {
      case 'music': return 'pill-music';
      case 'banter': return 'pill-banter';
      case 'ad': return 'pill-ad';
      case 'news':
      case 'news_flash': return 'pill-news';
      case 'idle': return 'pill-idle';
      default: return 'pill-idle';
    }
  }

  function _liveChipSuffix(el) {
    if (!el || el.dataset.freqSuffix !== undefined) return el ? el.dataset.freqSuffix : '';
    const raw = (el.textContent || '').trim();
    const idx = raw.indexOf('·');
    const suffix = idx >= 0 ? ' · ' + raw.slice(idx + 1).trim() : '';
    el.dataset.freqSuffix = suffix;
    return suffix;
  }

  function _setLiveChip(el, stopped) {
    if (!el) return;
    const suffix = _liveChipSuffix(el);
    const label = stopped ? _t('np_paused', 'Fermo') : _t('np_live', 'In Onda');
    el.classList.toggle('is-stopped', stopped);
    el.replaceChildren();
    const dot = document.createElement('span');
    dot.className = 'dot';
    el.appendChild(dot);
    el.appendChild(document.createTextNode(' ' + label + suffix));
    const stoppedLabel =
      el.id === 'nav-cta'
        ? _t('listen_resume_aria', 'Resume station')
        : _t('listen_paused_aria', 'Station paused');
    el.setAttribute(
      'aria-label',
      stopped ? stoppedLabel : _t('listen_now_aria', 'Listen now'),
    );
  }

  function _setNowPlayingEyebrow(stopped) {
    const el = $('np-eyebrow');
    if (!el) return;
    const suffix = _liveChipSuffix(el);
    const label = stopped ? _t('np_paused', 'Fermo') : _t('np_on_air', 'Ora in onda');
    el.textContent = label + suffix;
  }

  /* ── Playback ── */
  function startStream() {
    state.wantsPlay = true;
    if (!audio.src || audio.src !== _base + '/stream') {
      audio.src = _base + '/stream';
    }
    audio.play().catch(() => {});
  }

  function togglePlay() {
    if (state.isPlaying) {
      state.wantsPlay = false;
      audio.pause();
    } else {
      startStream();
    }
  }

  function setPlayingUi(isPlaying) {
    state.isPlaying = isPlaying;
    if (playBtnSmall) {
      playBtnSmall.classList.toggle('playing', isPlaying);
      playBtnSmall.innerHTML = isPlaying ? '&#9208;' : '&#9654;';
      playBtnSmall.setAttribute('aria-label', isPlaying ? 'Pause' : 'Play');
      playBtnSmall.setAttribute('aria-pressed', isPlaying ? 'true' : 'false');
    }
    if ('mediaSession' in navigator) {
      navigator.mediaSession.playbackState = isPlaying ? 'playing' : 'paused';
    }
  }

  /* ── Media Session (lock-screen / Bluetooth / Control Center) ── */
  function updateMediaSession(np) {
    if (!('mediaSession' in navigator) || !np) return;
    const stationName = localStorage.getItem('stationName') || 'Mamma Mi Radio';
    // Album shows on lock screen / CarPlay alongside title+artist. Station
    // identity only \u2014 no city/frequency hardcoded here (those belong in
    // radio.toml [brand]; previously leaked stale city values through this
    // surface even after the visible templates were updated).
    const album = stationName;
    let title, artist;
    const label = np.label || '';
    if (np.type === 'music') {
      const parts = label.split(' \u2014 ');
      if (parts.length === 2) { artist = parts[0]; title = parts[1]; }
      else { artist = stationName; title = label || _t('np_on_air', 'On Air'); }
    } else if (np.type === 'banter') {
      artist = label || 'Marco & Giulia';
      title = _t('np_live', 'Live') + ' \u2014 ' + _t('seg_banter', 'Banter');
    } else if (np.type === 'ad') {
      artist = (np.metadata && np.metadata.brand) ? np.metadata.brand : 'Sponsored';
      title = 'A word from our sponsors';
    } else if (np.type === 'welcome') {
      artist = stationName; title = 'The station has noticed you';
    } else if (np.type === 'news_flash' || np.type === 'news') {
      artist = stationName; title = label || 'News flash';
    } else if (np.type === 'stopped') {
      // Idle state — never leak the internal stopped-segment label to the
      // OS-level media surface (lock screen, Bluetooth, CarPlay, AirPlay).
      // Mirrors the DOM-side sanitization in renderNowPlayingStrip().
      artist = stationName; title = _t('np_paused', 'Paused');
    } else {
      artist = stationName; title = label || _t('np_on_air', 'On Air');
    }
    const artUrl = np.metadata && np.metadata.album_art;
    const artwork = artUrl
      ? [
          { src: artUrl, sizes: '512x512', type: 'image/jpeg' },
          { src: artUrl, sizes: '256x256', type: 'image/jpeg' },
        ]
      : [
          { src: (_base || '') + '/static/icon-512.svg', sizes: '512x512', type: 'image/svg+xml' },
          { src: (_base || '') + '/static/icon-192.svg', sizes: '192x192', type: 'image/svg+xml' },
        ];
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title, artist, album, artwork });
    } catch (e) { /* older browsers */ }
  }

  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.setActionHandler('play', () => { if (!state.isPlaying) startStream(); });
      navigator.mediaSession.setActionHandler('pause', () => { audio && audio.pause(); });
      navigator.mediaSession.setActionHandler('stop', () => { audio && audio.pause(); state.wantsPlay = false; });
    } catch (e) { /* ignore */ }
  }

  /* ── Rendering ── */
  function renderNowPlayingStrip(np) {
    if (!np) return;
    const label = np.label || '';
    const trackEl = $('np-track');
    const artistEl = $('np-artist');
    const stopped = np.type === 'stopped' || (state.status && state.status.session_stopped === true);

    if (stopped) {
      trackEl.textContent = _t('np_paused', 'Fermo');
      artistEl.textContent = '';
    } else if (np.type === 'music') {
      const parts = label.split(' \u2014 ');
      if (parts.length === 2) {
        trackEl.textContent = parts[1];
        artistEl.textContent = parts[0];
      } else {
        trackEl.textContent = label || _t('np_on_air', 'On Air');
        artistEl.textContent = '';
      }
    } else if (np.type === 'banter') {
      trackEl.textContent = label ? label + ' ' + _t('np_banter_strip', 'in conversation') : _t('np_banter_idle', 'The hosts are on air');
      artistEl.textContent = _t('seg_banter', 'Banter');
    } else if (np.type === 'ad') {
      trackEl.textContent = _t('np_ad_message', 'Sponsored message');
      artistEl.textContent = (np.metadata && np.metadata.brand) ? np.metadata.brand : _t('seg_ad', 'Sponsored');
    } else if (np.type === 'welcome') {
      trackEl.textContent = _t('np_welcome', 'Welcome aboard');
      artistEl.textContent = 'Mamma Mi Radio';
    } else if (np.type === 'stopped') {
      trackEl.textContent = _t('np_paused', 'Fermo');
      artistEl.textContent = '';
    } else {
      trackEl.textContent = label || _t('np_on_air', 'On Air');
      artistEl.textContent = segmentKindLabel(np.type);
    }
    const fullText = [trackEl.textContent, artistEl.textContent].filter(Boolean).join(' — ');
    trackEl.title = fullText;
    trackEl.setAttribute('aria-label', fullText);
    updateMediaSession(np);
  }

  function renderProgress(progressSec, durationSec) {
    const fill = $('np-fill');
    const tCur = $('np-time-cur');
    const tTot = $('np-time-tot');
    if (!fill) return;
    const pct = durationSec > 0 ? Math.min(100, (progressSec / durationSec) * 100) : 0;
    fill.style.width = pct + '%';
    if (tCur) tCur.textContent = fmtTime(progressSec);
    if (tTot) tTot.textContent = durationSec > 0 ? fmtTime(durationSec) : '—';
  }

  function renderHeroStats(status, caps) {
    const uptimeSec = (status && typeof status.uptime_sec === 'number')
      ? status.uptime_sec
      : (Date.now() - state.sessionStart) / 1000;
    const h = Math.floor(uptimeSec / 3600);
    const m = Math.floor((uptimeSec % 3600) / 60);
    const stat1 = $('stat-airtime');
    if (stat1) {
      stat1.textContent = (h === 0 && m === 0) ? _t('np_live', 'Live') : (h + 'h ' + m + 'm');
    }
    const stat2 = $('stat-tracks');
    if (stat2) {
      const played = status && typeof status.tracks_played === 'number' ? status.tracks_played : null;
      const queued = status && status.upcoming ? status.upcoming.length : 0;
      const value = played !== null ? played : queued;
      stat2.textContent = value > 0 ? value : '—';
    }
    const stat3 = $('stat-hosts');
    if (stat3 && caps && caps.hosts && caps.hosts.length) {
      stat3.textContent = caps.hosts.join(' · ');
    }
  }

  function renderPalinsesto(status) {
    const container = $('slots');
    if (!container) return;
    const stopped = status && status.session_stopped === true;
    const upcoming = (status && status.upcoming) || [];
    const now = !stopped && status && status.now_streaming;
    const cards = [];

    if (now) {
      const p = splitMusicLabel(now);
      cards.push({ when: _t('np_now', 'On now'), current: true, type: now.type, label: p.title, host: p.host });
    } else if (stopped) {
      cards.push({
        when: _t('np_stopped', 'Stopped'),
        current: true,
        type: 'idle',
        label: _t('np_paused', 'Fermo'),
        host: '',
      });
    }
    upcoming.slice(0, cards.length ? 3 : 4).forEach((seg, i) => {
      const p = splitMusicLabel(seg);
      cards.push({
        when: _t('np_next', 'Next') + ' \u00b7 ' + (i + 1),
        current: false,
        type: seg.type,
        label: p.title || segmentKindLabel(seg.type),
        host: p.host,
      });
    });

    while (cards.length < 4) {
      cards.push({
        when: '—',
        current: false,
        type: 'idle',
        label: _t('np_building', 'Building schedule…'),
        host: '',
      });
    }

    container.innerHTML = cards.slice(0, 4).map((c, i) => {
      const liClass = c.current ? 'now' : i > 0 || (i === 0 && !c.current) ? 'next' : '';
      const timingClass = c.current ? 'pill-current' : 'pill-next';
      const typeClass = segmentPillClass(c.type);
      const subHtml = c.host ? `<div class="sub">${c.host}</div>` : '';
      const pillHtml = `<span class="pill ${timingClass} ${typeClass}">${escHtml(segmentKindLabel(c.type))}</span>`;
      return `<li class="${liClass}"><span class="t">${escHtml(c.when)}</span><div class="m"><div class="title">${escHtml(c.label || _t('np_on_air', 'On Air'))}</div>${subHtml}</div>${pillHtml}</li>`;
    }).join('');
  }

  function hostLine(seg) {
    if (!seg || !seg.metadata) return '';
    const m = seg.metadata;
    if (m.brand) return escHtml(m.brand);
    if (m.artist) return escHtml(m.artist);
    if (m.year) return escHtml(String(m.year));
    return '';
  }

  // Producer emits music labels as "Artist \u2014 Title". Rendering the full
  // label in slot-title AND the artist again in slot-host doubles the artist.
  // Split the label for music, fall back to raw label + hostLine otherwise.
  function splitMusicLabel(seg) {
    const label = (seg && seg.label) || '';
    if (seg && seg.type === 'music') {
      const sep = ' \u2014 ';
      const idx = label.indexOf(sep);
      if (idx > 0) {
        return { title: label.slice(idx + sep.length), host: escHtml(label.slice(0, idx)) };
      }
    }
    return { title: label, host: hostLine(seg) };
  }

  function renderDediche(requests) {
    const stack = $('quote-stack');
    if (!stack) return;
    const read = (requests || []).filter(r => r && r.aired_at);
    if (read.length === 0) {
      // Empty-state placeholder is set by the server-side Jinja template
      // (brand-engine PR-C). Don't overwrite — preserves brand voice + station name.
      return;
    }
    stack.innerHTML = read.slice(0, 3).map(r => {
      const name = r.name || 'Un ascoltatore';
      const msg = r.message || '';
      const airTime = r.aired_at ? new Date(r.aired_at * 1000).toTimeString().slice(0, 5) : '';
      const eyebrowParts = [escHtml(name)];
      if (r.city) eyebrowParts.push(escHtml(r.city));
      const sig = airTime ? '— letta in onda alle ' + escHtml(airTime) : '—';
      return `
        <div class="mmr-dedica">
          <div class="eyebrow">${eyebrowParts.join(' · ')}</div>
          <div class="quote">${escHtml(msg)}</div>
          <div class="sig">${sig}</div>
        </div>
      `;
    }).join('');
  }

  /* ── Casa card (HA ambient awareness) ── */
  function updateCasa(ha) {
    const el = $('casa-card');
    if (!el) return;
    if (!ha || (!ha.mood && !ha.weather && !ha.last_event_label)) {
      el.setAttribute('hidden', '');
      return;
    }
    el.removeAttribute('hidden');
    const mood = $('casa-mood');
    const weather = $('casa-weather');
    const event = $('casa-event');
    if (mood) mood.textContent = ha.mood || '';
    if (weather) weather.textContent = ha.weather || '';
    if (event) {
      if (ha.last_event_label) {
        const ago = ha.last_event_ago_min ? ' · rilevato ' + ha.last_event_ago_min + ' min fa' : '';
        event.textContent = ha.last_event_label + ago;
      } else {
        event.textContent = '';
      }
    }
  }

  /* fetchPublicStatus removed (PR-F): /public-status is now the primary fetch
   * in fetchStatus(). HA moments are updated there directly. */

  function renderPalinsestoDate() {
    const el = $('palinsesto-date');
    if (!el) return;
    const now = new Date();
    const days = ['Domenica', 'Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato'];
    const months = ['Gennaio', 'Febbraio', 'Marzo', 'Aprile', 'Maggio', 'Giugno',
                    'Luglio', 'Agosto', 'Settembre', 'Ottobre', 'Novembre', 'Dicembre'];
    el.textContent = days[now.getDay()] + ' ' + now.getDate() + ' ' + months[now.getMonth()] + ' ' + now.getFullYear();
  }

  function renderStoppedState(status) {
    const stopped = status && status.session_stopped === true;
    document.body.setAttribute('data-stopped', stopped ? 'true' : 'false');
    _setLiveChip($('nav-cta'), stopped);
    _setLiveChip(document.querySelector('.mmr-stage-header .mmr-live'), stopped);
    _setNowPlayingEyebrow(stopped);
    const wave = $('mmr-wave-bars');
    if (wave) wave.classList.toggle('paused', stopped);
    const radio = $('mmr-radio');
    if (radio) radio.classList.toggle('is-stopped', stopped);
    if (stopped && state.wantsPlay) {
      state.wantsPlay = false;
      audio && audio.pause();
    }
    state.wasStopped = stopped;
    if (stopped) {
      const nowStreaming = (status && status.now_streaming) || {};
      renderNowPlayingStrip({ ...nowStreaming, type: 'stopped' });
    }
    // Share button visibility: hidden when session stopped, visible otherwise.
    // The button has 4 states: hidden / enabled / loading / shared (handled in doShare).
    const shareBtn = document.getElementById('share-clip-btn');
    if (shareBtn) {
      if (stopped) {
        shareBtn.hidden = true;
        shareBtn.setAttribute('data-state', 'hidden');
      } else {
        shareBtn.hidden = false;
        // Only reset to enabled when not mid-action
        const cur = shareBtn.getAttribute('data-state');
        if (cur === 'hidden' || cur === 'shared') {
          shareBtn.setAttribute('data-state', 'enabled');
        }
      }
    }
  }

  /* ── Toast helper (used by clip sharing) ── */
  let _toastTimer = null;
  function _showToast(msg, durationMs = 2400) {
    let el = document.getElementById('mmr-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'mmr-toast';
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
      el.style.cssText = (
        'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);' +
        'background:color-mix(in srgb, var(--bg) 96%, transparent);color:var(--cream,#F5EDD8);' +
        'padding:10px 18px;border-radius:8px;font-size:14px;font-family:Outfit,sans-serif;' +
        'z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,0.4);' +
        'border:1px solid rgba(244,208,72,0.2);opacity:0;transition:opacity 0.18s ease;'
      );
      document.body.appendChild(el);
    }
    el.textContent = msg;
    requestAnimationFrame(() => { el.style.opacity = '1'; });
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { el.style.opacity = '0'; }, durationMs);
  }

  /* ── Clip sharing: POST /api/clip, share via native sheet or clipboard ── */
  async function doShare() {
    const btn = document.getElementById('share-clip-btn');
    if (!btn || btn.disabled) return;
    const labelEl = btn.querySelector('.mmr-share-btn-label');
    const origLabel = labelEl ? labelEl.textContent : '';
    // Centralized state restoration: every early-exit and the success path set
    // nextState, and the finally block writes it once. Avoids leaving the
    // button stuck at "loading" on API errors or user-cancelled share sheets.
    let nextState = 'enabled';
    btn.disabled = true;
    btn.setAttribute('data-state', 'loading');
    if (labelEl) labelEl.textContent = _t('clip_saving', 'Salvando…');
    try {
      const res = await fetch(_base + '/api/clip', { method: 'POST' });
      const data = await res.json().catch(() => null);
      if (!res.ok || !data || !data.ok) {
        // Warm, actionable copy mapped from backend codes — never raw tech lingo.
        let msg;
        if (data && data.retry_after) {
          msg = _t('clip_rate_limited', 'The tape decks need a moment — give them {s}s and tap again.')
            .replace('{s}', data.retry_after);
        } else if (data && data.reason === 'no_audio') {
          msg = _t('clip_no_audio', 'Nothing to clip just yet — let the radio play for a moment, then tap Share.');
        } else {
          msg = _t('clip_error', "That clip didn't take — give it a moment and tap Share again.");
        }
        _showToast(msg);
        return;
      }
      const shareUrl = window.location.origin + _base + (data.share_url || data.url);
      // Always drop the URL on the clipboard (best-effort) so it's there no matter
      // which share path runs — even if the native sheet is dismissed.
      let copied = false;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        try { await navigator.clipboard.writeText(shareUrl); copied = true; } catch (e) { /* best-effort */ }
      }
      const npEl = document.getElementById('np-track');
      const stationName = localStorage.getItem('stationName') || 'Mamma Mi Radio';
      const title = (npEl && npEl.textContent && npEl.textContent.trim()) || stationName;
      if (navigator.share) {
        try {
          await navigator.share({ title: title + ' — ' + stationName, url: shareUrl });
        } catch (err) {
          if (err && err.name === 'AbortError') {
            // user cancelled the sheet. If the link made it to the clipboard,
            // confirm it; otherwise give them a way out (principle #5) via the
            // last-resort prompt rather than failing silently.
            if (copied) { _showToast(_t('clip_copied', 'Link copied!')); }
            else { window.prompt(_t('clip_copy_prompt', 'Copia il link:'), shareUrl); }
            nextState = 'shared';
            return;
          }
          throw err;
        }
        if (copied) _showToast(_t('clip_copied', 'Link copied!'));
      } else if (copied) {
        _showToast(_t('clip_copied', 'Link copied!'));
      } else {
        // Last-resort fallback: prompt
        window.prompt(_t('clip_copy_prompt', 'Copia il link:'), shareUrl);
      }
      nextState = 'shared';
    } catch (err) {
      console.warn('doShare failed', err);
      _showToast(_t('clip_error', "That clip didn't take — give it a moment and tap Share again."));
    } finally {
      btn.disabled = false;
      if (labelEl) labelEl.textContent = origLabel;
      btn.setAttribute('data-state', nextState);
    }
  }

  /* ── Polling ──
   * Brand-engine PR-F: listener uses /public-status exclusively (no admin endpoints).
   * /public-status returns brand + capabilities + facts in one shape — single fetch
   * replaces the old /status + /api/capabilities pair. Works on any deploy (loopback,
   * LAN, public) without the 401 risk of admin-only routes. */
  async function fetchStatus() {
    try {
      const r = await fetch(_base + '/public-status');
      if (!r.ok) return;
      const status = await r.json();
      // Capabilities live inside the public payload (PR-B). Wrap to match the
      // legacy { capabilities: {...} } shape the rest of listener.js expects.
      const caps = { capabilities: status.capabilities || {} };
      state.status = status;
      state.caps = caps;
      state.firstDataReceived = true;
      // Toggle [data-cap] elements based on capabilities (design D2: client-side
      // capability-conditional rendering).
      if (typeof window.mmrApplyCaps === 'function') {
        window.mmrApplyCaps(status.capabilities || {});
      }
      // First-impression brand-first hero (design D-Design-3): once data arrives,
      // flip warming -> live state.
      if (status.now_streaming) {
        document.body.setAttribute('data-state', 'live');
        renderNowPlayingStrip(status.now_streaming);
      }
      renderHeroStats(status, caps);
      renderPalinsesto(status);
      renderStoppedState(status);
      updateCasa(status.ha_moments);
      if (
        Number.isFinite(status.current_progress_sec) &&
        Number.isFinite(status.current_duration_sec) &&
        status.current_duration_sec > 0
      ) {
        state.currentProgressMs = status.current_progress_sec * 1000;
        state.segmentDurationMs = status.current_duration_sec * 1000;
        renderProgress(status.current_progress_sec, status.current_duration_sec);
      }
    } catch (e) {
      console.warn('fetchStatus failed', e);
    }
  }

  async function fetchRequests() {
    try {
      // Brand-engine PR-F: public listener-requests endpoint (no admin auth).
      const r = await fetch(_base + '/public-listener-requests');
      if (!r.ok) return;
      const d = await r.json();
      state.requests = d.requests || [];
      renderDediche(state.requests);
    } catch (e) {
      renderDediche([]);
    }
  }

  /* ── Request form ── */
  async function submitRequest(ev) {
    ev.preventDefault();
    const name = ($('req-name')?.value || '').trim();
    const msg = ($('req-msg')?.value || '').trim();
    if (!msg) return;
    const formEl = $('request-form');
    const sentEl = $('request-sent');
    try {
      const r = await fetch(_base + '/api/listener-request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name || 'Un ascoltatore', message: msg }),
      });
      const d = await r.json();
      if (formEl) formEl.style.display = 'none';
      if (sentEl) {
        sentEl.style.display = '';
        if (d.ok) {
          sentEl.textContent = d.type === 'song_request'
            ? 'Canzone in arrivo! I conduttori la suoneranno presto.'
            : 'Saluto ricevuto! I conduttori ti menzioneranno presto.';
        } else if (r.status === 429) {
          sentEl.textContent = d.retry_after
            ? `Aspetta ${d.retry_after}s prima di mandare un altro saluto.`
            : 'Coda piena, riprova tra poco.';
        } else {
          sentEl.textContent = 'Il saluto non è partito — aspetta un attimo e riprova.';
        }
      }
      setTimeout(() => {
        if (formEl) formEl.style.display = '';
        if (sentEl) sentEl.style.display = 'none';
        const msgInput = $('req-msg');
        if (msgInput) msgInput.value = '';
      }, 15000);
    } catch (e) {
      if (formEl) formEl.style.display = 'none';
      if (sentEl) {
        sentEl.style.display = '';
        sentEl.textContent = 'Invio non riuscito. Controlla la connessione e riprova.';
      }
      setTimeout(() => {
        if (formEl) formEl.style.display = '';
        if (sentEl) sentEl.style.display = 'none';
      }, 6000);
    }
  }

  /* ── Wire everything on DOMContentLoaded ── */
  document.addEventListener('DOMContentLoaded', () => {
    audio = $('radio-audio');
    playBtn = $('nav-cta');
    playBtnSmall = $('np-play');

    if (playBtn) playBtn.addEventListener('click', (e) => { e.preventDefault(); togglePlay(); });
    if (playBtnSmall) playBtnSmall.addEventListener('click', togglePlay);

    // Hero secondary buttons
    const heroPlay = $('hero-play');
    const heroPal = $('hero-palinsesto');
    if (heroPlay) heroPlay.addEventListener('click', togglePlay);
    if (heroPal) heroPal.addEventListener('click', () => {
      $('palinsesto')?.scrollIntoView({ behavior: 'smooth' });
    });

    // Audio element event wiring
    if (audio) {
      audio.addEventListener('play', () => setPlayingUi(true));
      audio.addEventListener('pause', () => setPlayingUi(false));
      audio.addEventListener('ended', () => {
        if (state.wantsPlay) setTimeout(startStream, 800);
      });
      audio.addEventListener('error', () => {
        if (state.wantsPlay) setTimeout(startStream, 2000);
      });
    }

    // Request form
    const reqForm = $('request-form');
    if (reqForm) reqForm.addEventListener('submit', submitRequest);

    // Clip sharing button
    const shareBtn = document.getElementById('share-clip-btn');
    if (shareBtn) shareBtn.addEventListener('click', doShare);

    // Auto-start on first interaction (bypass autoplay block)
    function autoStartOnce() {
      if (!state.wantsPlay && state.firstDataReceived) {
        startStream();
      }
      document.removeEventListener('click', autoStartOnce);
      document.removeEventListener('touchstart', autoStartOnce);
    }
    document.addEventListener('click', autoStartOnce);
    document.addEventListener('touchstart', autoStartOnce);

    // Service-worker registration (PWA install). Uses _base so HA ingress works.
    if ('serviceWorker' in navigator) {
      try { navigator.serviceWorker.register(_base + '/static/sw.js'); } catch (e) { /* ignore */ }
    }

    // Kick off
    renderPalinsestoDate();
    fetchStatus();
    fetchRequests();
    /* fetchPublicStatus removed in PR-F */
    setInterval(fetchStatus, 3000);
    setInterval(fetchRequests, 60000);
  });
})();
