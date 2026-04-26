/*
 * Mamma Mi Radio — Listener Client
 *
 * Drives the five-band listener site (nav, persistent now-playing strip,
 * hero, palinsesto, dediche) against /public-status + /public-listener-requests
 * (brand-engine PR-F: no admin endpoints — works on any public deploy).
 *
 * See DESIGN.md §§ "Listener site composition" for the canonical layout.
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
    if (diff < 1) return 'adesso';
    if (diff < 60) return Math.round(diff) + ' min fa';
    return Math.round(diff / 60) + ' h fa';
  }
  function segmentKindLabel(type) {
    switch ((type || '').toLowerCase()) {
      case 'music': return 'Musica';
      case 'banter': return 'Banter';
      case 'ad': return 'Sponsored';
      case 'news':
      case 'news_flash': return 'News';
      case 'jingle': return 'Jingle';
      case 'welcome': return 'Benvenuto';
      default: return (type || 'In onda').toUpperCase();
    }
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
    const album = stationName + ' \u2014 96,7 FM Milano';
    let title, artist;
    const label = np.label || '';
    if (np.type === 'music') {
      const parts = label.split(' \u2014 ');
      if (parts.length === 2) { artist = parts[0]; title = parts[1]; }
      else { artist = stationName; title = label || 'In onda'; }
    } else if (np.type === 'banter') {
      artist = label || 'Marco & Giulia';
      title = 'In diretta \u2014 banter';
    } else if (np.type === 'ad') {
      artist = (np.metadata && np.metadata.brand) ? np.metadata.brand : 'Sponsored';
      title = 'A word from our sponsors';
    } else if (np.type === 'welcome') {
      artist = stationName; title = 'The station has noticed you';
    } else if (np.type === 'news_flash' || np.type === 'news') {
      artist = stationName; title = label || 'News flash';
    } else {
      artist = stationName; title = label || 'In onda';
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

    if (np.type === 'music') {
      const parts = label.split(' \u2014 ');
      if (parts.length === 2) {
        trackEl.textContent = parts[1];
        artistEl.textContent = parts[0];
      } else {
        trackEl.textContent = label || 'In onda';
        artistEl.textContent = '';
      }
    } else if (np.type === 'banter') {
      trackEl.textContent = label ? label + ' in diretta' : 'I conduttori sono in onda';
      artistEl.textContent = 'Banter';
    } else if (np.type === 'ad') {
      trackEl.textContent = 'Messaggio pubblicitario';
      artistEl.textContent = (np.metadata && np.metadata.brand) ? np.metadata.brand : 'Sponsored';
    } else if (np.type === 'welcome') {
      trackEl.textContent = 'Ben arrivato';
      artistEl.textContent = 'Mamma Mi Radio';
    } else {
      trackEl.textContent = label || 'In onda';
      artistEl.textContent = segmentKindLabel(np.type);
    }
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
    const elapsedSec = (Date.now() - state.sessionStart) / 1000;
    const h = Math.floor(elapsedSec / 3600);
    const m = Math.floor((elapsedSec % 3600) / 60);
    const stat1 = $('stat-airtime');
    if (stat1) stat1.textContent = h + 'h ' + m + 'm';
    const stat2 = $('stat-tracks');
    if (stat2) {
      const tracks = status && status.playlist_size ? status.playlist_size : (status && status.upcoming ? status.upcoming.length : 0);
      stat2.textContent = tracks || '—';
    }
    const stat3 = $('stat-hosts');
    if (stat3 && caps && caps.hosts && caps.hosts.length) {
      stat3.textContent = caps.hosts.join(' · ');
    }
  }

  function renderPalinsesto(status) {
    const container = $('slots');
    if (!container) return;
    const upcoming = (status && status.upcoming) || [];
    const now = status && status.now_streaming;
    const cards = [];

    if (now) {
      const p = splitMusicLabel(now);
      cards.push({ when: 'Ora in onda', live: true, type: now.type, label: p.title, host: p.host });
    }
    upcoming.slice(0, cards.length ? 3 : 4).forEach((seg, i) => {
      const p = splitMusicLabel(seg);
      cards.push({
        when: 'Prossimo \u00b7 ' + (i + 1),
        live: false,
        type: seg.type,
        label: p.title || segmentKindLabel(seg.type),
        host: p.host,
      });
    });

    while (cards.length < 4) {
      cards.push({ when: '—', live: false, type: 'idle', label: 'In costruzione…', host: '' });
    }

    container.innerHTML = cards.slice(0, 4).map((c, i) => {
      const liClass = i === 0 && c.live ? 'now' : i > 0 ? 'next' : '';
      const pillClass = i === 0 && c.live ? 'pill-on' : i > 0 ? 'pill-next' : '';
      const subHtml = c.host ? `<div class="sub">${c.host}</div>` : '';
      const pillHtml = pillClass ? `<span class="pill ${pillClass}">${escHtml(segmentKindLabel(c.type))}</span>` : '';
      return `<li class="${liClass}"><span class="t">${escHtml(c.when)}</span><div class="m"><div class="title">${escHtml(c.label || 'In onda')}</div>${subHtml}</div>${pillHtml}</li>`;
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
    if (stopped && state.wantsPlay) {
      state.wantsPlay = false;
      audio && audio.pause();
    }
    state.wasStopped = stopped;
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
      if (status.current_progress_sec !== undefined && status.current_duration_sec !== undefined) {
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
          sentEl.textContent = d.error || 'Invio non riuscito. Riprova.';
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
