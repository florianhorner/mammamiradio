const _base = (() => {
  const p = window.location.pathname.replace(/\/+$/, '');
  // Dashboard is served at /dashboard, strip it to get the base
  return p.endsWith('/dashboard') ? p.slice(0, -10) : (p === '' ? '' : p);
})();
document.addEventListener('DOMContentLoaded', () => {
  const adminLink = document.getElementById('admin-link');
  if (adminLink) adminLink.href = (_base || '') + '/admin';
});

const csrfToken = document.querySelector('meta[name="mammamiradio-csrf-token"]')?.content || '';
const _nativeFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const request = new Request(input, init);
  if (csrfToken && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method.toUpperCase())) {
    const headers = new Headers(request.headers);
    headers.set('X-Radio-CSRF-Token', csrfToken);
    return _nativeFetch(input, {...init, headers});
  }
  return _nativeFetch(input, init);
};

let caps = null;
let latestStatus = null;
let firstDataReceived = false;
let _lastTier = null;

// --- Radio Dial Controller ---
const DEMO_FREQ = 94.7;
let _dialFreq = 98.0; // start mid-band so sweeps use the full range
let _dialTarget = DEMO_FREQ;
let _dialState = 'searching'; // searching | locking | locked | shifting
let _dialCollapseTimer = null;
let _dialCollapseHideTimer = null;
let _dialAnimFrame = null;
let _audioCtx = null;
let _noiseGain = null;
let _noiseSource = null;

function _initDialTicks() {
  const el = document.getElementById('dial-ticks');
  if (!el || el.children.length) return;
  for (let i = 0; i < 41; i++) el.appendChild(document.createElement('i'));
}

function _setSignalBars(n) {
  const bars = document.querySelectorAll('#signal-bars .sb');
  bars.forEach((b, i) => b.classList.toggle('on', i < n));
}

function _freqToPercent(f) {
  return ((f - 87.5) / (108 - 87.5)) * 100;
}

function _setNeedle(freq) {
  _dialFreq = freq;
  const pct = Math.max(2, Math.min(98, _freqToPercent(freq)));
  const needle = document.getElementById('dial-needle');
  needle.style.left = pct + '%';
  // Move the ambient glow to follow the needle
  document.querySelector('.dial-band').style.setProperty('--needle-x', pct + '%');
  document.getElementById('dial-freq-val').textContent = freq.toFixed(1);
}

let _crackleSource = null;
let _crackleGain = null;

function _startStatic() {
  if (_audioCtx && _audioCtx.state !== 'closed') return;
  if (_audioCtx) { _audioCtx = null; _noiseGain = null; _crackleGain = null; }
  try {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const sr = _audioCtx.sampleRate;

    // Layer 1: broadband FM static (shaped white noise)
    const bufSize = sr * 2;
    const buf = _audioCtx.createBuffer(1, bufSize, sr);
    const data = buf.getChannelData(0);
    for (let i = 0; i < bufSize; i++) data[i] = Math.random() * 2 - 1;
    _noiseSource = _audioCtx.createBufferSource();
    _noiseSource.buffer = buf;
    _noiseSource.loop = true;
    // FM radio static: bandpass centered around 1.2kHz with moderate Q
    const bp1 = _audioCtx.createBiquadFilter();
    bp1.type = 'bandpass'; bp1.frequency.value = 1200; bp1.Q.value = 0.7;
    // Add a second highpass to cut the mud
    const hp = _audioCtx.createBiquadFilter();
    hp.type = 'highpass'; hp.frequency.value = 400;
    _noiseGain = _audioCtx.createGain();
    _noiseGain.gain.value = 0.04;
    _noiseSource.connect(bp1).connect(hp).connect(_noiseGain).connect(_audioCtx.destination);
    _noiseSource.start();

    // Layer 2: crackle pops (impulsive noise, like real FM tuning)
    const crackBuf = _audioCtx.createBuffer(1, sr * 2, sr);
    const crackData = crackBuf.getChannelData(0);
    for (let i = 0; i < crackData.length; i++) {
      // Sparse impulses — random pops
      crackData[i] = Math.random() < 0.003 ? (Math.random() - 0.5) * 2 : 0;
    }
    _crackleSource = _audioCtx.createBufferSource();
    _crackleSource.buffer = crackBuf;
    _crackleSource.loop = true;
    const crackFilter = _audioCtx.createBiquadFilter();
    crackFilter.type = 'highpass'; crackFilter.frequency.value = 2000;
    _crackleGain = _audioCtx.createGain();
    _crackleGain.gain.value = 0.08;
    _crackleSource.connect(crackFilter).connect(_crackleGain).connect(_audioCtx.destination);
    _crackleSource.start();
  } catch (e) { /* Web Audio not available */ }
}

function _setStaticVolume(vol) {
  if (!_audioCtx) return;
  const t = _audioCtx.currentTime;
  if (_noiseGain) _noiseGain.gain.setTargetAtTime(vol, t, 0.05);
  if (_crackleGain) _crackleGain.gain.setTargetAtTime(vol * 2.5, t, 0.05);
}

function _stopStatic() {
  _setStaticVolume(0);
  setTimeout(() => {
    if (_noiseSource) { try { _noiseSource.stop(); } catch(e){} _noiseSource = null; }
    if (_crackleSource) { try { _crackleSource.stop(); } catch(e){} _crackleSource = null; }
    if (_audioCtx) { _audioCtx.close(); _audioCtx = null; }
    _noiseGain = null; _crackleGain = null;
  }, 600);
}

let _dialVelocity = 0.3;
let _lastSeekTime = 0;
let _seekElapsed = 0;

function _dialSeek(timestamp) {
  if (_dialState !== 'searching') return;
  if (!_lastSeekTime) _lastSeekTime = timestamp;
  const dt = (timestamp - _lastSeekTime) / 1000;
  _lastSeekTime = timestamp;
  _seekElapsed += dt;

  // Phase 1 (first ~4s): big lazy sweeps across the whole band, barely any pull
  // Phase 2 (4-8s): start homing in, sweep narrows
  // Phase 3 (8s+): close in on target
  const phase = Math.min(_seekElapsed / 8, 1); // 0→1 over 8 seconds
  const springK = 0.01 + phase * 0.08;         // very weak → moderate pull
  const noiseAmp = 1.2 - phase * 0.7;          // big drift → small drift
  const damping = 0.97 - phase * 0.03;         // very smooth → slightly tighter

  // Slow sine wave adds a broad sweep feel
  const sweep = Math.sin(_seekElapsed * 0.4) * (3 - phase * 2.5);

  const pull = (_dialTarget - _dialFreq) * springK;
  const noise = (Math.random() - 0.5) * noiseAmp;
  _dialVelocity = (_dialVelocity + pull + noise * 0.08 + sweep * 0.02) * damping;
  _dialVelocity = Math.max(-1.5, Math.min(1.5, _dialVelocity));
  const next = Math.max(88, Math.min(107.5, _dialFreq + _dialVelocity));
  _setNeedle(next);

  // Static: louder when far from target, with crackle bursts
  const dist = Math.abs(_dialFreq - _dialTarget);
  const baseVol = 0.015 + dist * 0.004;
  _setStaticVolume(baseVol);

  _dialAnimFrame = requestAnimationFrame(_dialSeek);
}

function _dialLock(target, label, statusText) {
  _dialState = 'locking';
  _dialTarget = target;
  const needle = document.getElementById('dial-needle');
  needle.style.opacity = '';
  needle.style.animation = 'none';
  needle.classList.remove('seeking');
  needle.classList.add('locked');

  // Slow lock-in: overshoot → drift back → micro-overshoot → settle
  const over1 = target + (Math.random() > 0.5 ? 1.6 : -1.6);
  _setNeedle(over1);
  _setStaticVolume(0.04);

  setTimeout(() => {
    _setNeedle(target - 0.5);
    _setStaticVolume(0.025);
  }, 700);

  setTimeout(() => {
    _setNeedle(target + 0.2);
    _setStaticVolume(0.01);
  }, 1400);

  setTimeout(() => {
    _setNeedle(target);
    _setStaticVolume(0);
    _dialState = 'locked';
    document.getElementById('dial-label').textContent = label;
    document.getElementById('dial-status').textContent = statusText;
    _setSignalBars(5);
  }, 2000);

  // Fade static out gently
  setTimeout(_stopStatic, 3000);
}

function _dialShiftTo(target, label, statusText) {
  if (_dialState === 'shifting') return;
  _dialState = 'shifting';
  _startStatic();
  _setStaticVolume(0.07);
  const needle = document.getElementById('dial-needle');
  needle.classList.remove('locked');
  needle.classList.add('seeking');
  document.getElementById('dial-status').textContent = 'Retuning...';
  _setSignalBars(1);
  // Cancel any pending collapse and re-expand
  if (_dialCollapseTimer) { clearTimeout(_dialCollapseTimer); _dialCollapseTimer = null; }
  if (_dialCollapseHideTimer) { clearTimeout(_dialCollapseHideTimer); _dialCollapseHideTimer = null; }
  const dial = document.getElementById('radio-dial');
  dial.style.overflow = '';
  dial.style.maxHeight = '';
  dial.style.marginTop = '20px';
  dial.style.transition = 'opacity 0.6s ease';
  dial.style.opacity = '1';

  // Smooth sweep using requestAnimationFrame
  const from = _dialFreq;
  const overshoot = target + (target > from ? 1.8 : -1.8);
  const totalMs = 2000;
  const startTime = performance.now();

  function animate(now) {
    const elapsed = now - startTime;
    const t = Math.min(elapsed / totalMs, 1);
    // Ease: overshoot then settle (cubic bezier approximation)
    let freq;
    if (t < 0.5) {
      const p = t / 0.5;
      freq = from + (overshoot - from) * (p * p * (3 - 2 * p));
    } else {
      const p = (t - 0.5) / 0.5;
      freq = overshoot + (target - overshoot) * (p * p * (3 - 2 * p));
    }
    _setNeedle(freq);
    // Crackling peaks in the middle, fades at edges
    const midness = 1 - Math.abs(t - 0.4) * 2.5;
    _setStaticVolume(Math.max(0, 0.06 * midness));

    if (t < 1) {
      requestAnimationFrame(animate);
    } else {
      _setNeedle(target);
      needle.classList.remove('seeking');
      needle.classList.add('locked');
      _dialState = 'locked';
      document.getElementById('dial-label').textContent = label;
      document.getElementById('dial-status').textContent = statusText;
      _setSignalBars(5);
      _stopStatic();
      // Collapse dial after shift settles: fade opacity, then slide height down
      setTimeout(() => {
        dial.style.transition = 'opacity 1.4s ease';
        dial.style.opacity = '0';
        setTimeout(() => {
          // Capture exact height before collapsing so the slide is smooth
          const h = dial.getBoundingClientRect().height;
          dial.style.overflow = 'hidden';
          dial.style.maxHeight = h + 'px';
          void dial.offsetHeight; // force reflow so transition picks up start value
          dial.style.transition = 'max-height 0.6s cubic-bezier(0.4,0,0.6,1), margin-top 0.6s ease';
          dial.style.maxHeight = '0';
          dial.style.marginTop = '0';
        }, 1500);
      }, 2000);
    }
  }
  requestAnimationFrame(animate);
}

// Initialize dial on load
_initDialTicks();
_startStatic();
const _seekNeedle = document.getElementById('dial-needle');
if (_seekNeedle) _seekNeedle.classList.add('seeking');
requestAnimationFrame(_dialSeek);

// Timeout: if no signal after 20s, show error state with tap-to-retry
const _dialTimeoutId = setTimeout(() => {
  if (_dialState !== 'searching') return;
  if (_dialAnimFrame) { cancelAnimationFrame(_dialAnimFrame); _dialAnimFrame = null; }
  _stopStatic();
  _dialState = 'error';
  const needle = document.getElementById('dial-needle');
  if (needle) {
    needle.classList.remove('seeking');
    needle.style.opacity = '0.3';
  }
  const lbl = document.getElementById('dial-label');
  const status = document.getElementById('dial-status');
  if (lbl) lbl.textContent = 'No signal found';
  if (status) {
    status.innerHTML = '<span style="cursor:pointer;text-decoration:underline" onclick="location.reload()">Tap to retry</span>';
  }
  _setSignalBars(0);
}, 20000);

function escHtml(v) { return String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

let _lastTrackLabel = null;
function _updateNowPlaying(ns) {
  const label = (ns.label && ns.label !== ns.type) ? ns.label : 'Waiting for first segment...';
  const type = ns.type || '';
  const trackEl = document.getElementById('np-track');
  const metaEl = document.getElementById('np-meta');

  // Parse artist and title from "Artist – Title" format used for music segments
  let displayTitle = label;
  let displayArtist = '';
  if (type === 'music' && label.includes(' \u2013 ')) {
    const idx = label.indexOf(' \u2013 ');
    displayArtist = label.slice(0, idx);
    displayTitle = label.slice(idx + 3);
  } else if (type === 'banter') {
    displayArtist = 'Mamma Mi Radio';
  } else if (type === 'ad') {
    displayArtist = (ns.metadata && ns.metadata.brand) ? ns.metadata.brand : 'Sponsored';
  }
  metaEl.textContent = displayArtist;

  // Only re-trigger marquee check when label actually changes
  if (label === _lastTrackLabel) return;
  _lastTrackLabel = label;

  const wrap = trackEl.parentElement;
  trackEl.classList.remove('scrolling');
  trackEl.textContent = displayTitle;

  // Detect overflow after paint and animate if needed
  requestAnimationFrame(() => {
    if (wrap && trackEl.scrollWidth > wrap.clientWidth + 2) {
      const dur = Math.max(6, Math.round(trackEl.scrollWidth / 60));
      trackEl.style.setProperty('--marquee-dur', dur + 's');
      trackEl.textContent = displayTitle + '\u2003\u2022\u2003' + displayTitle;
      trackEl.classList.add('scrolling');
    }
  });
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

function showTransition(text, durationMs) {
  const el = document.getElementById('transition-overlay');
  document.getElementById('transition-text').textContent = text;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), durationMs || 2000);
}

function updateCards() {
  if (!caps) return;
  const c = caps.capabilities;
  const tier = caps.tier;

  // Tier badge
  const badge = document.getElementById('tier-badge');
  const tierChanged = _lastTier !== null && _lastTier !== tier;
  badge.dataset.tier = tier;
  badge.textContent = caps.tier_label;
  if (tierChanged) {
    badge.classList.remove('tier-pulse');
    void badge.offsetWidth;
    badge.classList.add('tier-pulse');
    badge.addEventListener('animationend', () => badge.classList.remove('tier-pulse'), { once: true });
  }
  _lastTier = tier;

  // Unlock AI — show when no anthropic key
  document.getElementById('unlock-ai').style.display = !c.llm ? '' : 'none';
  // HA card hidden by default — it's a power-user feature, not onboarding
  document.getElementById('connect-ha').style.display = 'none';

  const gp = (latestStatus && latestStatus.golden_path) || caps.golden_path || null;
  const gpCard = document.getElementById('golden-path-card');
  const gpState = document.getElementById('golden-path-state');
  const gpHeadline = document.getElementById('golden-path-headline');
  const gpDetail = document.getElementById('golden-path-detail');
  const gpSteps = document.getElementById('golden-path-steps');
  if (!gp) {
    gpCard.style.display = 'none';
    return;
  }
  gpCard.style.display = gp.blocking ? '' : 'none';
  gpState.className = 'golden-path-state ' + (gp.blocking ? 'is-blocked' : 'is-ok');
  gpState.textContent = gp.blocking ? 'Golden path blocked' : 'Golden path ready';
  gpHeadline.textContent = gp.headline || '';
  gpDetail.textContent = gp.detail || '';
  var stepsHtml = (gp.steps || []).map((s) => '<li>' + escHtml(s) + '</li>').join('');
  gpSteps.innerHTML = stepsHtml;
}

function _updatePipelineStatus() {
  if (!caps) return;
  const c = caps.capabilities || {};
  const el = document.getElementById('pipeline-status');
  let html = '';
  // Mirror admin.html three-state logic: an Anthropic key can be configured
  // AND simultaneously auth-suspended (401 → 10 min backoff, OpenAI taking
  // the calls). Showing "AI" here while the dashboard lies about provider
  // health is exactly the regression Item #11 was filed against.
  if (c.anthropic_key && c.anthropic_degraded) {
    html += '<span class="pipeline-dot" title="Anthropic auth failed; falling back to OpenAI"><span class="tri">&#9650;</span>AI Fallback</span>';
  } else if (c.anthropic_key || c.llm) {
    html += '<span class="pipeline-dot"><span class="dot ok"></span>AI</span>';
  } else if (c.openai) {
    html += '<span class="pipeline-dot"><span class="tri">&#9650;</span>AI Fallback</span>';
  } else {
    html += '<span class="pipeline-dot"><span class="tri">&#9650;</span>No AI</span>';
  }
  if (c.ha) {
    html += '<span class="pipeline-dot"><span class="dot ok"></span>HA</span>';
  }
  el.innerHTML = html;
}

let _wasStopped = false;
function _updateStoppedState(d) {
  const stopped = d.session_stopped === true;
  const banner = document.getElementById('stopped-banner');
  const dot = document.getElementById('live-dot');
  const queueCard = document.getElementById('queue-card');
  // Global toggle so CSS can pause animations and quiet the page down.
  document.body.setAttribute('data-stopped', stopped ? 'true' : 'false');
  banner.classList.toggle('show', stopped);
  dot.classList.toggle('stopped', stopped);
  if (stopped) {
    queueCard.style.display = 'none';
    const trackEl = document.getElementById('np-track');
    const metaEl = document.getElementById('np-meta');
    trackEl.textContent = 'Station paused';
    metaEl.textContent = '';
    _lastTrackLabel = 'Station paused';
  } else if (_wasStopped && _wantsPlay) {
    // Session just resumed — reconnect the stream so audio restarts automatically
    setTimeout(_startStream, 300);
  }
  _wasStopped = stopped;
}

function _updateAdMeta(ns) {
  const el = document.getElementById('ad-meta');
  if (!ns || ns.type !== 'ad') { el.style.display = 'none'; return; }
  const meta = ns.metadata || {};
  const parts = [];
  if (meta.formats && meta.formats.length) {
    const unique = [...new Set(meta.formats)].map(f => f.replace(/_/g, ' '));
    parts.push(unique.join(', '));
  }
  if (meta.brands && meta.brands.length) parts.push(meta.brands.join(' & '));
  if (meta.spots) parts.push(meta.spots + ' spot' + (meta.spots > 1 ? 's' : ''));
  if (meta.sonic_worlds && meta.sonic_worlds.length) {
    const unique = [...new Set(meta.sonic_worlds.filter(Boolean))].map(s => s.replace(/_/g, ' '));
    if (unique.length) parts.push(unique.join(', '));
  }
  if (meta.roles_used && meta.roles_used.length) {
    const flat = [].concat(...meta.roles_used).filter(Boolean);
    const unique = [...new Set(flat)].map(r => r.replace(/_/g, ' '));
    if (unique.length) parts.push(unique.join(', '));
  }
  if (parts.length) {
    el.textContent = parts.join(' \u00b7 ');
    el.style.display = '';
  } else {
    el.style.display = 'none';
  }
}

async function doResume() {
  try {
    const r = await fetch(_base + '/api/resume', { method: 'POST' });
    if (r.ok) {
      showToast('Resuming station...');
      setTimeout(refresh, 1000);
    } else {
      showToast('Resume failed');
    }
  } catch (e) { showToast('Resume failed'); }
}

function updateStatus(d) {
  // Lock the dial on first data — signal found
  if (!firstDataReceived) {
    firstDataReceived = true;
    clearTimeout(_dialTimeoutId);
    const dialNeedle = document.getElementById('dial-needle');
    if (dialNeedle) dialNeedle.style.opacity = '';
    if (_dialAnimFrame) { cancelAnimationFrame(_dialAnimFrame); _dialAnimFrame = null; }
    _dialLock(DEMO_FREQ, 'Mamma Mi Radio', 'Signal locked');
    // Collapse dial gently — fade then height-slide, then reveal now-playing underneath
    const _npEl = document.getElementById('now-playing');
    _npEl.style.opacity = '0';
    _npEl.style.transition = 'opacity 0s';
    _npEl.style.pointerEvents = 'none';
    _dialCollapseTimer = setTimeout(() => {
      const dial = document.getElementById('radio-dial');
      dial.style.transition = 'opacity 1.4s ease';
      dial.style.opacity = '0';
      // Fade now-playing in as dial fades out
      _npEl.style.transition = 'opacity 1.4s ease';
      _npEl.style.opacity = '1';
      _npEl.style.pointerEvents = '';
      _dialCollapseHideTimer = setTimeout(() => {
        const h = dial.getBoundingClientRect().height;
        dial.style.overflow = 'hidden';
        dial.style.maxHeight = h + 'px';
        void dial.offsetHeight;
        dial.style.transition = 'max-height 0.6s cubic-bezier(0.4,0,0.6,1), margin-top 0.6s ease';
        dial.style.maxHeight = '0';
        dial.style.marginTop = '0';
      }, 1500);
    }, 5000);
    _npEl.style.display = '';
    _initTicker();
    // Auto-start stream on first click
    if (!_wantsPlay) {
      _wantsPlay = true;
      setTimeout(_startStream, 1200); // start after dial locks
    }
  }

  // Now playing
  const ns = d.now_streaming || {};
  _updateNowPlaying(ns);
  _updateAdMeta(ns);
  _updateStoppedState(d);
  _updatePipelineStatus();

  // Album art — blurred background wash (thumbnail removed in redesign)
  const artUrl = (ns.metadata && ns.metadata.album_art) || '';
  const artBg = document.getElementById('album-art-bg');
  if (artBg) {
    if (artUrl) {
      artBg.style.backgroundImage = 'url(' + artUrl + ')'; artBg.style.opacity = '0.15';
    } else {
      artBg.style.opacity = '0';
    }
  }

  // Queue
  const upcoming = d.upcoming || [];
  const upcomingMode = d.upcoming_mode || '';
  const queueCard = document.getElementById('queue-card');
  if (upcoming.length || upcomingMode === 'building') {
    queueCard.style.display = '';
    if (upcoming.length) {
      document.getElementById('queue-list').innerHTML = upcoming.slice(0, 3).map(e => {
        const predicted = e.source === 'predicted_from_playlist';
        const cls = predicted ? ' class="predicted"' : '';
        const tag = predicted ? '<span class="queue-source">predicted</span>' : '';
        return '<li' + cls + '>' + escHtml(e.label || e.type || '?') + tag + '</li>';
      }).join('');
    } else {
      document.getElementById('queue-list').innerHTML = '<li>Building next segments...</li>';
    }
  } else {
    queueCard.style.display = 'none';
  }

  // Casa card — HA ambient awareness
  var ha = d.ha_moments;
  var casaEl = document.getElementById('casa-card');
  if (ha) {
    var moodEl = document.getElementById('casa-mood');
    var weatherEl = document.getElementById('casa-weather');
    var evtEl = document.getElementById('casa-event');
    var prevMood = moodEl.textContent;
    moodEl.textContent = ha.mood || '';
    weatherEl.textContent = ha.weather || '';
    if (ha.last_event_label) {
      var agoText = ha.last_event_ago_min ? ' \u00b7 rilevato ' + ha.last_event_ago_min + ' min fa' : '';
      evtEl.innerHTML = escHtml(ha.last_event_label) + '<span aria-hidden="true">' + escHtml(agoText) + '</span>';
    } else {
      evtEl.textContent = '';
    }
    casaEl.style.display = '';
    requestAnimationFrame(function() { casaEl.style.opacity = '1'; });
    // Pulse eyebrow on mood/event change
    if (prevMood && ha.mood && prevMood !== ha.mood) {
      var ey = document.getElementById('casa-eyebrow');
      ey.style.transition = 'opacity 200ms';
      ey.style.opacity = '0.4';
      setTimeout(function() { ey.style.opacity = '1'; }, 200);
      setTimeout(function() { ey.style.opacity = ''; ey.style.transition = ''; }, 600);
    }
  } else {
    casaEl.style.opacity = '0';
    setTimeout(function() { if (casaEl.style.opacity === '0') casaEl.style.display = 'none'; }, 260);
  }
}

async function refresh() {
  try {
    const [statusResp, capsResp] = await Promise.all([
      fetch(_base + '/status'),
      fetch(_base + '/api/capabilities'),
    ]);
    const statusData = await statusResp.json();
    const capsData = await capsResp.json();
    latestStatus = statusData;
    caps = capsData;
    updateStatus(statusData);
    updateCards();
  } catch (e) {
    console.error('refresh failed', e);
  }
}

let _wantsPlay = false;

function togglePlay() {
  const audio = document.getElementById('radio-audio');
  const btn = document.getElementById('play-btn');
  if (audio.paused || !_wantsPlay) {
    _wantsPlay = true;
    _startStream();
  } else {
    _wantsPlay = false;
    audio.pause();
    audio.src = '';
    btn.textContent = '\u25B6';
    btn.classList.remove('playing');
    _syncEq();
  }
}

function _startStream() {
  const audio = document.getElementById('radio-audio');
  const btn = document.getElementById('play-btn');
  audio.src = _base + '/stream';
  audio.play().catch(() => {});
  btn.textContent = '\u23F8';
  btn.classList.add('playing');
  _syncEq();
}

// Auto-reconnect on stream error (hot reload, network blip)
document.addEventListener('DOMContentLoaded', () => {
  const audio = document.getElementById('radio-audio');
  audio.addEventListener('error', () => {
    if (_wantsPlay) setTimeout(_startStream, 2000);
  });
  audio.addEventListener('ended', () => {
    if (_wantsPlay) setTimeout(_startStream, 1000);
  });

  // Auto-start stream on first interaction anywhere on the page
  function _autoStartOnce() {
    if (!_wantsPlay && firstDataReceived) {
      _wantsPlay = true;
      _startStream();
    }
    document.removeEventListener('click', _autoStartOnce);
    document.removeEventListener('touchstart', _autoStartOnce);
  }
  document.addEventListener('click', _autoStartOnce);
  document.addEventListener('touchstart', _autoStartOnce);
});

async function doShuffle() {
  try {
    const r = await fetch(_base + '/api/shuffle', { method: 'POST' });
    if (r.ok) showToast('Shuffled');
    else r.text().then(t => showToast('Error: ' + (t || r.status)));
  } catch(e) { showToast('Request failed'); }
}

async function doSkip() {
  try {
    const r = await fetch(_base + '/api/skip', { method: 'POST' });
    if (r.ok) showToast('Skipped');
    else r.text().then(t => showToast('Error: ' + (t || r.status)));
  } catch(e) { showToast('Request failed'); }
}

async function doPurge() {
  if (!confirm('Clear the entire queue? This cannot be undone.')) return;
  try {
    const r = await fetch(_base + '/api/purge', { method: 'POST' });
    if (r.ok) showToast('Buffer cleared');
    else r.text().then(t => showToast('Error: ' + (t || r.status)));
  } catch(e) { showToast('Request failed'); }
}

async function loadPlaylistUrl() {
  const el = document.getElementById('playlist-url-quick');
  if (!el) return;
  const url = el.value?.trim();
  if (!url) return;
  try {
    const r = await fetch(_base + '/api/playlist/load', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url}),
    });
    const d = await r.json();
    showToast(d.ok ? 'Playlist loaded' : (d.error || 'Failed'));
  } catch (e) {
    console.error('loadPlaylistUrl failed', e);
    showToast('Request failed');
  }
}

async function saveAnthropicKey() {
  const key = document.getElementById('anthropic-key')?.value?.trim();
  if (!key) return;
  const statusEl = document.getElementById('anthropic-status');
  statusEl.textContent = 'Saving...';
  const r = await fetch(_base + '/api/credentials', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({anthropic_api_key: key}),
  });
  const d = await r.json();
  statusEl.textContent = d.ok ? 'Saved! Reloading...' : (d.error || 'Failed');
  if (d.ok) setTimeout(refresh, 1000);
}

// Listener request
async function sendRequest() {
  const name = (document.getElementById('req-name').value || '').trim();
  const msg = (document.getElementById('req-msg').value || '').trim();
  if (!msg) return;
  const sentEl = document.getElementById('request-sent');
  const formEl = document.getElementById('request-form');
  try {
    const r = await fetch(_base + '/api/listener-request', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name || 'Un ascoltatore', message: msg}),
    });
    const d = await r.json();
    if (d.ok) {
      formEl.style.display = 'none';
      sentEl.style.display = '';
      sentEl.textContent = d.type === 'song_request'
        ? 'Canzone in arrivo! I conduttori la suoneranno presto.'
        : 'Saluto ricevuto! I conduttori ti menzioneranno presto.';
      setTimeout(() => { formEl.style.display = ''; sentEl.style.display = 'none'; document.getElementById('req-msg').value = ''; }, 15000);
    } else if (r.status === 429) {
      sentEl.style.display = ''; formEl.style.display = 'none';
      sentEl.textContent = d.retry_after ? `Aspetta ${d.retry_after}s prima di inviare un altra richiesta.` : 'Coda piena, riprova tra poco.';
      setTimeout(() => { formEl.style.display = ''; sentEl.style.display = 'none'; }, 5000);
    } else {
      sentEl.style.display = ''; formEl.style.display = 'none';
      sentEl.textContent = d.error || 'Invio non riuscito. Riprova.';
      setTimeout(() => { formEl.style.display = ''; sentEl.style.display = 'none'; }, 5000);
    }
  } catch(e) {
    console.warn('Request failed', e);
    sentEl.style.display = ''; formEl.style.display = 'none';
    sentEl.textContent = 'Invio non riuscito. Controlla la connessione e riprova.';
    setTimeout(() => { formEl.style.display = ''; sentEl.style.display = 'none'; }, 5000);
  }
}

// Start polling
refresh();
setInterval(refresh, 5000);
// --- Waveform — golden bars, each bouncing independently ---
(function() {
  const wv = document.getElementById('waveform');
  if (!wv) return;
  for (let i = 0; i < 36; i++) {
    const b = document.createElement('div');
    b.className = 'wb';
    const h = 4 + Math.random() * 20;
    b.style.cssText = `--h:${h}px;--d:${(0.45 + Math.random() * 0.85).toFixed(2)}s;--dl:${(Math.random() * 0.7).toFixed(2)}s;height:${Math.round(h * 0.4)}px`;
    wv.appendChild(b);
  }
})();

// Sync waveform with play state
function _syncEq() {
  const wv = document.getElementById('waveform');
  if (!wv) return;
  wv.classList.toggle('paused', !_wantsPlay);
}

// Populate and show ticker after first data
function _initTicker() {
  const items = [
    'Milano 22\u00B0 soleggiato',
    'Traffico A4: scorrevole',
    'Roma 24\u00B0 parzialmente nuvoloso',
    'Serie A: Napoli 2 \u2013 Juventus 1',
    'Borsa: FTSE MIB +0.8%',
    'Firenze 20\u00B0 sereno',
    'Autostrada A1: rallentamenti zona Bologna',
    'Napoli 25\u00B0 bel tempo',
    'Torino 18\u00B0 coperto',
    'Mare: Liguria calmo, Adriatico mosso',
    'Palermo 27\u00B0 sole splendido',
    'Prossimo treno Roma\u2013Milano: 14:35',
  ];

  const ticker = document.getElementById('ticker');
  // Duplicate items so the scroll loops seamlessly
  const html = items.concat(items).map(t => {
    return '<span class="ti">' + t + '</span><span class="ts"> \u00b7 </span>';
  }).join('');
  ticker.innerHTML = html;
  document.getElementById('ticker-wrap').style.display = '';
}
