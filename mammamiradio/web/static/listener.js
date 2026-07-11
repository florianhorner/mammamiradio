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
  function stationNameFromStatus(status) {
    return (
      (status && status.identity && status.identity.station_name) ||
      (status && status.brand && status.brand.station_name) ||
      ''
    );
  }
  function currentStationName() {
    return (
      stationNameFromStatus(state.status) ||
      localStorage.getItem('stationName') ||
      document.title ||
      'Mamma Mi Radio'
    );
  }
  function syncStationName(status) {
    const serverName = stationNameFromStatus(status);
    if (!serverName) return;
    // The server payload is authoritative. localStorage is only a boot-time
    // fallback for dynamic browser surfaces (Media Session / clip sharing),
    // so every successful status poll repairs stale admin-written cache data.
    try { localStorage.setItem('stationName', serverName); } catch (_) {}
  }

  /* ── State ── */
  const state = {
    caps: null,
    status: null,
    requests: [],
    isPlaying: false,
    wantsPlay: false,
    playPending: false,
    retryTimer: null,
    firstDataReceived: false,
    wasStopped: false,
    sessionStart: Date.now(),
    currentLabel: null,
    currentProgressMs: 0,
    segmentDurationMs: 0,
    progressTimer: null,
    lastNpKey: null,
  };

  /* ── DOM refs (cached after DOMContentLoaded) ── */
  let audio, playBtn, playBtnSmall, heroPlay;

  /* ── Helpers ── */
  function $(id) { return document.getElementById(id); }
  /* Shared by Act IV (track title roll) and Act VI (dedica stamp + send) —
     JS-sequenced animations must check this BEFORE adding an animation
     class. A CSS-only `animation: none !important` override is not enough
     on its own: a chain waiting on `animationend` would stall forever. */
  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
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
    el.removeAttribute('aria-label');
  }

  function _setNavPlayControl(stopped) {
    const el = $('nav-cta');
    if (!el) return;
    const suffix = _liveChipSuffix(el);
    const hasIntent = !stopped && (state.isPlaying || state.playPending || state.wantsPlay);
    const label = stopped
      ? _t('listen_stopped', 'Station paused')
      : hasIntent
        ? _t('listen_pause', 'Pause')
        : _t('listen_now', 'Listen Now');
    const ariaLabel = stopped
      ? _t('listen_paused_aria', 'Station paused')
      : hasIntent
        ? _t('listen_pause_aria', 'Pause station')
        : _t('listen_now_aria', 'Listen now');

    el.classList.toggle('is-stopped', stopped);
    el.classList.toggle('playing', !stopped && state.isPlaying);
    el.disabled = stopped;
    el.replaceChildren();
    const dot = document.createElement('span');
    dot.className = 'dot';
    el.appendChild(dot);
    const actionIcon = document.createElement('span');
    actionIcon.setAttribute('aria-hidden', 'true');
    actionIcon.textContent = stopped || hasIntent ? '\u2016' : '\u25b6';
    el.appendChild(actionIcon);
    el.appendChild(document.createTextNode(' ' + label + suffix));
    el.setAttribute('aria-label', ariaLabel);
    el.setAttribute('aria-pressed', hasIntent ? 'true' : 'false');
  }

  function _setCompactPlayControl(stopped) {
    if (!playBtnSmall) return;
    const hasIntent = !stopped && (state.isPlaying || state.playPending || state.wantsPlay);
    playBtnSmall.disabled = stopped;
    playBtnSmall.classList.toggle('playing', !stopped && state.isPlaying);
    playBtnSmall.innerHTML = hasIntent || stopped
      ? '<span class="mmr-play-icon">&#9208;</span>'
      : '<span class="mmr-play-icon">&#9654;</span>';
    playBtnSmall.setAttribute(
      'aria-label',
      stopped
        ? _t('listen_paused_aria', 'Station paused')
        : hasIntent
          ? _t('listen_pause_aria', 'Pause station')
          : _t('listen_now_aria', 'Listen now'),
    );
    playBtnSmall.setAttribute('aria-pressed', hasIntent ? 'true' : 'false');
  }

  function _setHeroPlayControl(stopped) {
    if (!heroPlay) return;
    const hasIntent = !stopped && (state.isPlaying || state.playPending || state.wantsPlay);
    heroPlay.disabled = stopped;
    heroPlay.textContent = stopped
      ? _t('listen_stopped', 'Station paused')
      : hasIntent
        ? _t('listen_pause', 'Pause')
        : _t('listen_now', 'Listen Now');
    heroPlay.setAttribute(
      'aria-label',
      stopped
        ? _t('listen_paused_aria', 'Station paused')
        : hasIntent
          ? _t('listen_pause_aria', 'Pause station')
          : _t('listen_now_aria', 'Listen now'),
    );
    heroPlay.setAttribute('aria-pressed', hasIntent ? 'true' : 'false');
  }

  function _setPlaybackControls(stopped) {
    _setNavPlayControl(stopped);
    _setCompactPlayControl(stopped);
    _setHeroPlayControl(stopped);
  }

  function _setNowPlayingEyebrow(stopped) {
    const el = $('np-eyebrow');
    if (!el) return;
    const suffix = _liveChipSuffix(el);
    const label = stopped ? _t('np_paused', 'Fermo') : _t('np_on_air', 'Ora in onda');
    el.textContent = label + suffix;
  }

  /* ── Playback ── */
  function _clearPlaybackRetry() {
    if (state.retryTimer !== null) {
      clearTimeout(state.retryTimer);
      state.retryTimer = null;
    }
  }

  function _stationIsStopped() {
    return Boolean(state.status && state.status.session_stopped);
  }

  function _scheduleStreamRetry(delayMs) {
    if (!state.wantsPlay || state.retryTimer !== null || _stationIsStopped()) return;
    state.retryTimer = setTimeout(() => {
      state.retryTimer = null;
      if (!state.wantsPlay || _stationIsStopped()) return;
      state.playPending = false;
      startStream();
    }, delayMs);
  }

  function startStream() {
    if (!audio || _stationIsStopped() || state.isPlaying || state.playPending) return;
    _clearPlaybackRetry();
    state.wantsPlay = true;
    state.playPending = true;
    _setPlaybackControls(false);
    if (!audio.src || audio.src !== _base + '/stream') {
      audio.src = _base + '/stream';
    }
    const playResult = audio.play();
    if (playResult && typeof playResult.catch === 'function') {
      playResult.catch(() => {
        state.playPending = false;
        _setPlaybackControls(false);
        _scheduleStreamRetry(2000);
      });
    }
  }

  function stopStream() {
    state.wantsPlay = false;
    state.playPending = false;
    _clearPlaybackRetry();
    if (audio) audio.pause();
    setPlayingUi(false);
  }

  function togglePlay() {
    if (_stationIsStopped()) return;
    if (state.isPlaying || state.playPending || state.wantsPlay) {
      stopStream();
    } else {
      startStream();
    }
  }

  function setPlayingUi(isPlaying) {
    state.isPlaying = isPlaying;
    if (isPlaying) {
      state.playPending = false;
      state.wantsPlay = true;
      _clearPlaybackRetry();
    }
    _setPlaybackControls(_stationIsStopped());
    if ('mediaSession' in navigator) {
      navigator.mediaSession.playbackState = isPlaying ? 'playing' : 'paused';
    }
  }

  /* ── Media Session (lock-screen / Bluetooth / Control Center) ── */
  function updateMediaSession(np) {
    if (!('mediaSession' in navigator) || !np) return;
    const stationName = currentStationName();
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
      navigator.mediaSession.setActionHandler('pause', stopStream);
      navigator.mediaSession.setActionHandler('stop', stopStream);
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
      artistEl.textContent = currentStationName();
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

    // Act IV — Il Cambio. Only animate on a genuine content change: the 3s
    // poll re-renders this unconditionally, and without this guard the roll
    // would replay on every tick even when nothing changed.
    const npKey = JSON.stringify([trackEl.textContent, artistEl.textContent]);
    if (state.lastNpKey === null) {
      // First render (page load) — establish the baseline without animating,
      // so there's no spurious roll-up on initial paint.
      state.lastNpKey = npKey;
    } else if (npKey !== state.lastNpKey) {
      state.lastNpKey = npKey;
      if (!reducedMotion()) {
        [trackEl, artistEl].forEach((el) => {
          el.classList.remove('tt-track-roll');
          void el.offsetWidth; // force reflow so the animation restarts
          el.classList.add('tt-track-roll');
        });
      }
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
    const noMusicSource = status && status.golden_path && status.golden_path.stage === 'needs_music_source';
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

    if (!stopped && noMusicSource && cards.length < 4) {
      cards.push({
        status: true,
        current: false,
        type: 'idle',
        label: _t('np_no_source', 'No records are loaded yet — check back once the crate is filled.'),
        host: '',
      });
    } else if (!stopped && status && status.upcoming_mode === 'building' && cards.length < 4) {
      cards.push({
        status: true,
        current: false,
        type: 'idle',
        label: _t('np_building', 'The next records are being cued…'),
        host: '',
      });
    }

    container.innerHTML = cards.slice(0, 4).map((c, i) => {
      if (c.status) {
        return `<li class="status" role="status"><div class="m"><div class="title">${escHtml(c.label || _t('np_building', 'The next records are being cued…'))}</div></div></li>`;
      }
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
          <div class="sig" lang="it">${sig}</div>
        </div>
      `;
    }).join('');
  }

  /* ── Casa card (HA ambient awareness) ── */
  function updateCasa(ha) {
    const el = $('casa-card');
    if (!el) return;
    if (window.mmrLastCaps && window.mmrLastCaps.ha === false) {
      el.setAttribute('hidden', '');
      return;
    }
    const recent = (ha && Array.isArray(ha.recent)) ? ha.recent : [];
    if (!ha || (!ha.mood && !ha.weather && !ha.last_event_label && !recent.length)) {
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
    /* "Live from your home" — Moment Receipts strip. Generic labels + coarse
       age only (the server never sends more). textContent throughout: nothing
       from the wire is ever interpreted as HTML. */
    const momentsWrap = $('casa-moments');
    const momentsRows = $('casa-moments-rows');
    if (momentsWrap && momentsRows) {
      if (!recent.length) {
        momentsWrap.setAttribute('hidden', '');
        momentsRows.textContent = '';
      } else {
        momentsWrap.removeAttribute('hidden');
        momentsRows.textContent = '';
        recent.forEach((m) => {
          const row = document.createElement('div');
          row.className = 'row' + (m.status === 'airing' ? '' : ' dim');
          const ico = document.createElement('span');
          ico.className = 'ico';
          ico.textContent = m.status === 'airing' ? '●' : '·';
          const text = document.createElement('span');
          const minutes = m.ago_min || 1;
          text.textContent = m.status === 'airing'
            ? (m.label || '') + ' · ' + _t('casa_moment_airing', 'on air now')
            : (m.label || '') + ' · ' + _t('casa_moment_minutes_ago', '{m} min ago').replace('{m}', String(minutes));
          row.appendChild(ico);
          row.appendChild(text);
          momentsRows.appendChild(row);
        });
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
    if (stopped && (state.wantsPlay || state.playPending || state.isPlaying)) {
      stopStream();
    }
    _setPlaybackControls(stopped);
    _setLiveChip(document.querySelector('.mmr-stage-header .mmr-live'), stopped);
    _setNowPlayingEyebrow(stopped);
    const wave = $('mmr-wave-bars');
    if (wave) wave.classList.toggle('paused', stopped);
    const radio = $('mmr-radio');
    if (radio) radio.classList.toggle('is-stopped', stopped);
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
        'background:rgba(20,17,15,0.96);color:var(--cream,#F5EDD8);' +
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
      const stationName = currentStationName();
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
      syncStationName(status);
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

  /* ── Request form ──
   * Act VI — Il Francobollo. Genuine success (d.ok === true) gets the
   * animated stamp-press + card-lift sequence; 429/decline/network-failure
   * branches keep the plain instant swap, just crossfaded via
   * .form-sent.is-visible (a CSS transition — already collapsed to 0.01ms
   * under prefers-reduced-motion by base.css's blanket rule, so it needs no
   * separate JS gate). */
  function _setRequestFieldsHidden(formEl, hidden) {
    if (!formEl) return;
    Array.from(formEl.children).forEach((child) => {
      if (child.id !== 'request-sent') child.style.display = hidden ? 'none' : '';
    });
  }

  function _revealSentCrossfade(formEl, sentEl) {
    _setRequestFieldsHidden(formEl, true);
    if (sentEl) {
      delete sentEl.dataset.validation;
      sentEl.style.display = '';
      requestAnimationFrame(() => sentEl.classList.add('is-visible'));
    }
  }

  function _showEmptyRequestMessage() {
    const msgInput = $('req-msg');
    const sentEl = $('request-sent');
    if (msgInput) {
      msgInput.setAttribute('aria-invalid', 'true');
      msgInput.focus();
    }
    if (sentEl) {
      sentEl.dataset.validation = 'empty';
      sentEl.textContent = _t(
        'form_message_required',
        'Write a message first, then send it to the DJ.',
      );
      sentEl.style.display = '';
      sentEl.classList.add('is-visible');
    }
  }

  function _clearEmptyRequestMessage() {
    const msgInput = $('req-msg');
    const sentEl = $('request-sent');
    if (msgInput) msgInput.removeAttribute('aria-invalid');
    if (sentEl && sentEl.dataset.validation === 'empty') {
      delete sentEl.dataset.validation;
      sentEl.style.display = 'none';
      sentEl.classList.remove('is-visible');
      sentEl.textContent = '';
    }
  }

  function _resetRequestForm(formEl, sentEl) {
    if (formEl) {
      formEl.style.display = '';
      _setRequestFieldsHidden(formEl, false);
      formEl.classList.remove('is-sending');
      delete formEl.dataset.submitting;
      const submitBtn = formEl.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = false;
    }
    if (sentEl) {
      delete sentEl.dataset.validation;
      sentEl.style.display = 'none';
      sentEl.classList.remove('is-visible');
    }
    const msgInput = $('req-msg');
    if (msgInput) msgInput.removeAttribute('aria-invalid');
  }

  async function submitRequest(ev) {
    ev.preventDefault();
    const name = ($('req-name')?.value || '').trim();
    const msg = ($('req-msg')?.value || '').trim();
    const formEl = $('request-form');
    const sentEl = $('request-sent');
    if (!msg) {
      _showEmptyRequestMessage();
      return;
    }
    _clearEmptyRequestMessage();
    // Act VI now leaves the form visibly on-screen for up to ~1.7s of
    // stamp-press + card-lift before it hides (previously an instant swap) —
    // that widened window makes a double-click/double-tap resubmission easy
    // to trigger for real, not just a theoretical race. A second submit
    // while one is in flight would otherwise interrupt the first one's
    // animation (a hard display:none from the second call can kill the
    // first's CSS animation before animationend fires, leaving .is-sending
    // stuck and the wrong confirmation text on screen).
    if (formEl?.dataset.submitting === '1') return;
    if (formEl) formEl.dataset.submitting = '1';
    const submitBtn = formEl?.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    // Fetch has no built-in timeout — a stalled connection (flaky wifi,
    // captive portal, hung server) would otherwise leave dataset.submitting
    // set and the button disabled forever, with no error shown and no
    // recovery short of a page reload (adversarial review finding). Bound
    // it so a hang always falls through to the existing catch/reset path.
    const fetchController = new AbortController();
    const fetchTimeout = setTimeout(() => fetchController.abort(), 8000);
    try {
      const r = await fetch(_base + '/api/listener-request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name || 'Un ascoltatore', message: msg }),
        signal: fetchController.signal,
      });
      // Keep the timeout armed through the body read, not just the headers —
      // a response that stalls mid-body would otherwise hang r.json() with
      // no abort path once cleared here (Codex re-review finding). The same
      // AbortSignal cancels an in-flight body read, so this is sufficient.
      const d = await r.json();
      clearTimeout(fetchTimeout);
      let isSuccess = false;
      let text;
      if (r.ok && d.ok) {
        isSuccess = true;
        text = d.type === 'song_request'
          ? _t('form_success_song', 'Song request received! The hosts will cue it soon.')
          : _t('form_success_shoutout', 'Dedication received! The hosts will read it soon.');
      } else if (r.status === 429) {
        text = d.retry_after != null
          ? _t('form_rate_limited', 'Give the DJ {s}s before sending another dedication.')
              .replace('{s}', String(d.retry_after))
          : _t('form_queue_full', 'The dedication queue is full — wait a moment and try again.');
      } else {
        text = _t('form_declined', "That dedication didn't go through — wait a moment and try again.");
      }
      if (sentEl) sentEl.textContent = text;

      if (isSuccess && formEl && !reducedMotion()) {
        formEl.classList.add('is-sending');
        let liftDone = false;
        const finishLift = () => {
          if (liftDone) return; // animationend + fallback timer can both fire — only run once
          liftDone = true;
          formEl.removeEventListener('animationend', onCardLiftEnd);
          clearTimeout(liftFallback);
          formEl.classList.remove('is-sending');
          _setRequestFieldsHidden(formEl, true);
          if (sentEl) {
            // #request-sent is aria-live="polite" — its text was set while
            // still display:none (below), which most screen readers won't
            // announce; re-assigning at reveal time makes the mutation and
            // the visibility change coincident (adversarial review finding).
            sentEl.textContent = text;
            sentEl.style.display = '';
            requestAnimationFrame(() => sentEl.classList.add('is-visible'));
          }
        };
        const onCardLiftEnd = (e) => {
          if (e.animationName !== 'tt-card-lift') return; // ignore tt-stamp-press bubbling up first
          finishLift();
        };
        formEl.addEventListener('animationend', onCardLiftEnd);
        // Backgrounded/inactive tabs can throttle or skip animation frames
        // entirely, so animationend isn't guaranteed to fire — a bounded
        // fallback (stamp-press 320ms + card-lift 1.4s + margin) guarantees
        // the confirmation still reveals instead of leaving the form stuck
        // hidden with no message until the unrelated 15s revert timer.
        const liftFallback = setTimeout(finishLift, 2500);
      } else if (isSuccess) {
        // Reduced motion: instant swap, no animation delay — but .form-sent
        // now defaults to opacity:0 (needs .is-visible to show), so this
        // must add the class directly or the confirmation renders invisible.
        _setRequestFieldsHidden(formEl, true);
        if (sentEl) {
          sentEl.style.display = '';
          sentEl.classList.add('is-visible');
        }
      } else {
        _revealSentCrossfade(formEl, sentEl);
      }

      setTimeout(() => {
        _resetRequestForm(formEl, sentEl);
        if (isSuccess) {
          const msgInput = $('req-msg');
          if (msgInput) msgInput.value = '';
        }
      }, isSuccess ? 15000 : 6000);
    } catch (e) {
      clearTimeout(fetchTimeout);
      if (sentEl) {
        sentEl.textContent = _t(
          'form_network_error',
          'We lost the connection — check it and try again.',
        );
      }
      _revealSentCrossfade(formEl, sentEl);
      setTimeout(() => {
        _resetRequestForm(formEl, sentEl);
      }, 6000);
    }
  }

  /* ── Wire everything on DOMContentLoaded ── */
  document.addEventListener('DOMContentLoaded', () => {
    audio = $('radio-audio');
    playBtn = $('nav-cta');
    playBtnSmall = $('np-play');
    heroPlay = $('hero-play');

    if (playBtn) playBtn.addEventListener('click', (e) => { e.preventDefault(); togglePlay(); });
    if (playBtnSmall) playBtnSmall.addEventListener('click', togglePlay);

    // Hero secondary buttons
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
        state.playPending = false;
        setPlayingUi(false);
        _scheduleStreamRetry(800);
      });
      audio.addEventListener('error', () => {
        state.playPending = false;
        setPlayingUi(false);
        _scheduleStreamRetry(2000);
      });
    }

    // Request form
    const reqForm = $('request-form');
    if (reqForm) reqForm.addEventListener('submit', submitRequest);
    const reqMsg = $('req-msg');
    if (reqMsg) {
      // `required` prevents the submit event on an empty field. Mirror the
      // browser constraint in our visible aria-live region so keyboard and
      // screen-reader users get the same warm, actionable way out.
      reqMsg.addEventListener('invalid', (e) => {
        e.preventDefault();
        _showEmptyRequestMessage();
      });
      reqMsg.addEventListener('input', () => {
        if (reqMsg.value.trim()) _clearEmptyRequestMessage();
      });
    }

    // Clip sharing button
    const shareBtn = document.getElementById('share-clip-btn');
    if (shareBtn) shareBtn.addEventListener('click', doShare);

    // Playback intent is scoped to the three explicit play affordances above.
    // Form, navigation, share, and install interactions must never start audio.

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
