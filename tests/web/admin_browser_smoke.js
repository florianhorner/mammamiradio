async (page) => {
  const markerUrl = page.url();
  const markerIndex = markerUrl.indexOf('#');
  const baseUrl = markerIndex >= 0 ? markerUrl.slice(markerIndex + 1).replace(/\/+$/, '') : '';

  function assert(condition, message) {
    if (!condition) throw new Error(`admin-browser-smoke: ${message}`);
  }

  assert(/^https?:\/\//.test(baseUrl), `invalid ADMIN_BROWSER_SMOKE_URL marker: ${markerUrl}`);
  const httpOrigin = (value) => (value.match(/^https?:\/\/[^/]+/i) || [''])[0].toLowerCase();
  const baseOrigin = httpOrigin(baseUrl);
  const blockedOffOriginRequests = [];
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message || String(error)));
  await page.addInitScript(() => {
    const nativeSetInterval = window.setInterval.bind(window);
    window.__adminSmokeIntervals = [];
    window.setInterval = (handler, delay, ...args) => {
      const id = nativeSetInterval(handler, delay, ...args);
      window.__adminSmokeIntervals.push({ id, delay });
      return id;
    };
  });
  await page.route('**/*', async (route) => {
    const requestUrl = route.request().url();
    const requestOrigin = httpOrigin(requestUrl);
    if (!requestOrigin || requestOrigin === baseOrigin) {
      await route.fallback();
      return;
    }
    blockedOffOriginRequests.push(requestUrl);
    await route.fulfill({ status: 204, contentType: 'text/plain', body: '' });
  });
  page.setDefaultTimeout(5000);
  page.setDefaultNavigationTimeout(10000);

  await page.goto(`${baseUrl}/admin`, { waitUntil: 'domcontentloaded', timeout: 10000 });
  await page.waitForFunction(
    () => typeof renderProduction === 'function' && typeof updateStopState === 'function' && typeof updateRecent === 'function',
    null,
    { timeout: 5000 },
  );
  await page.evaluate(() => {
    (window.__adminSmokeIntervals || [])
      .filter(({ delay }) => delay === 3000 || delay === 30000)
      .forEach(({ id }) => clearInterval(id));
  });

  const seededStoppedFirstPaint = await page.evaluate(() => {
    document.body.setAttribute('data-stopped', 'true');
    stoppedBanner.classList.remove('show');
    stopBtn.style.removeProperty('display');
    resumeBtn.style.removeProperty('display');
    return {
      banner: getComputedStyle(stoppedBanner).display,
      stop: getComputedStyle(stopBtn).display,
      resume: getComputedStyle(resumeBtn).display,
    };
  });
  assert(seededStoppedFirstPaint.banner === 'block', 'server-seeded stopped state hid the paused banner');
  assert(seededStoppedFirstPaint.stop === 'none', 'server-seeded stopped state exposed Stop on first paint');
  assert(
    seededStoppedFirstPaint.resume === 'flex',
    `server-seeded stopped state hid Start on first paint: ${JSON.stringify(seededStoppedFirstPaint)}`,
  );
  await page.evaluate(() => updateStopState(false));

  const productionStates = await page.evaluate(() => {
    const states = [
      ['active', { session_stopped: false, listeners: { active: 1 }, now_streaming: { type: 'music' } }],
      ['paused', { session_stopped: true, listeners: { active: 0 }, now_streaming: { type: 'music' } }],
      ['listenerless', { session_stopped: false, listeners: { active: 0 }, now_streaming: { type: 'music' } }],
    ];
    return states.map(([name, state]) => {
      renderProduction({
        ...state,
        production: { current: { label: 'Writing the next host break', kind: 'banter', elapsed_sec: 8 } },
      });
      return { name, label: productionStateLabel.textContent, feed: productionFeed.innerText };
    });
  });
  assert(productionStates[0].label.endsWith('building ahead'), 'active production did not say building ahead');
  assert(
    productionStates[1].label.endsWith('building ahead · station paused'),
    'paused production did not preserve both active work and station context',
  );
  assert(
    productionStates[2].label.endsWith('building ahead · waiting for listeners'),
    'listenerless production did not preserve both active work and listener context',
  );

  const liveStatusResponse = await page.request.get(`${baseUrl}/status`);
  assert(liveStatusResponse.ok(), 'local /status was unavailable');
  const liveStatus = await liveStatusResponse.json();
  let statusScenario = 'network';
  const statusResponseQueue = [];
  const makeQueuedStatus = (body, { status = 200, held = false } = {}) => {
    let markSeen;
    let release;
    let markDone;
    return {
      body,
      status,
      seen: new Promise((resolve) => { markSeen = resolve; }),
      releaseGate: held ? new Promise((resolve) => { release = resolve; }) : Promise.resolve(),
      done: new Promise((resolve) => { markDone = resolve; }),
      markSeen: () => markSeen(),
      release: () => { if (release) release(); },
      markDone: () => markDone(),
    };
  };
  let failListenerRequests = false;
  let failHosts = false;
  let skipScenario = 'declined';
  const restoredStatus = {
    ...liveStatus,
    session_stopped: false,
    listeners: { ...(liveStatus.listeners || {}), active: 1 },
    listeners_active: 1,
    now_streaming: {
      type: 'music',
      label: 'Mina — Città vuota',
      started: Date.now() / 1000,
      duration_sec: 180,
      metadata: { artist: 'Mina' },
    },
    production: {
      current: { label: 'Writing restored copy', kind: 'banter', elapsed_sec: 4 },
      recent: [],
    },
  };
  let statusPayload = restoredStatus;
  await page.route('**/status*', async (route) => {
    if (statusScenario === 'network') {
      await route.abort();
      return;
    }
    if (statusScenario === 'http_error') {
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"warming"}' });
      return;
    }
    if (statusScenario === 'queued') {
      const response = statusResponseQueue.shift();
      assert(response, 'status response queue was empty');
      response.markSeen();
      await response.releaseGate;
      try {
        await route.fulfill({
          status: response.status,
          contentType: 'application/json',
          body: JSON.stringify(response.body),
        });
      } finally {
        response.markDone();
      }
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(statusPayload) });
  });
  await page.route('**/api/listener-requests', async (route) => {
    if (failListenerRequests) {
      await route.abort();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"requests":[]}' });
  });
  await page.route('**/api/hosts', async (route) => {
    if (failHosts) {
      await route.abort();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"hosts":[]}' });
  });
  await page.route('**/api/skip', async (route) => {
    if (skipScenario === 'network') {
      await route.abort();
      return;
    }
    if (skipScenario === 'declined') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: '{"ok":false,"error":"Station paused by browser smoke"}',
      });
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true,"bridged":false}' });
  });
  await page.evaluate(() => {
    const nativeFetch = window.fetch.bind(window);
    window.__adminSmokeHangPath = '';
    window.__adminSmokeHangingFetches = 0;
    window.fetch = (input, init = {}) => {
      const url = typeof input === 'string' ? input : input.url;
      if (window.__adminSmokeHangPath && url.includes(window.__adminSmokeHangPath)) {
        window.__adminSmokeHangingFetches += 1;
        return new Promise((_resolve, reject) => {
          const abort = () => {
            window.__adminSmokeHangingFetches -= 1;
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          };
          if (init.signal && init.signal.aborted) abort();
          else if (init.signal) init.signal.addEventListener('abort', abort, { once: true });
        });
      }
      return nativeFetch(input, init);
    };
  });
  await page.evaluate(() => renderProduction({
    session_stopped: false,
    listeners: { active: 1 },
    now_streaming: { type: 'music' },
    production: { current: { label: 'Writing stale copy', kind: 'banter', elapsed_sec: 99 } },
  }));
  await page.evaluate(() => refreshFast());
  const failedPoll = await page.evaluate(() => ({
    label: productionStateLabel.textContent,
    feed: productionFeed.innerText,
  }));
  assert(failedPoll.label.endsWith('reconnecting'), 'failed poll kept a stale production-state label');
  assert(!failedPoll.feed.includes('Writing stale copy'), 'failed poll kept stale production copy');
  assert(failedPoll.feed.includes('retry automatically'), 'failed poll did not give the operator a way out');

  statusScenario = 'http_error';
  await page.evaluate(() => renderProduction({
    session_stopped: false,
    listeners: { active: 1 },
    now_streaming: { type: 'music' },
    production: { current: { label: 'Writing stale HTTP copy', kind: 'banter', elapsed_sec: 101 } },
  }));
  await page.evaluate(() => refreshFast());
  const failedHttpPoll = await page.evaluate(() => ({
    label: productionStateLabel.textContent,
    feed: productionFeed.innerText,
  }));
  assert(failedHttpPoll.label.endsWith('reconnecting'), 'HTTP error was treated as a valid production status');
  assert(!failedHttpPoll.feed.includes('Writing stale HTTP copy'), 'HTTP error kept stale production copy');

  statusScenario = 'ok';
  failListenerRequests = true;
  await page.evaluate(() => refreshFast());
  const restoredPoll = await page.evaluate(() => ({
    label: productionStateLabel.textContent,
    feed: productionFeed.innerText,
  }));
  assert(restoredPoll.label.endsWith('building ahead'), 'listener-request failure replaced healthy production state');
  assert(restoredPoll.feed.includes('Writing restored copy'), 'listener-request failure discarded healthy production copy');
  assert(!restoredPoll.feed.includes('reconnecting'), 'listener-request failure falsely marked the producer desk offline');
  failListenerRequests = false;

  failHosts = true;
  await page.evaluate(() => { _hostsOk = false; });
  await page.evaluate(() => refreshFast());
  const hostsFailedPoll = await page.evaluate(() => ({
    label: productionStateLabel.textContent,
    feed: productionFeed.innerText,
  }));
  assert(hostsFailedPoll.label.endsWith('building ahead'), 'hosts failure replaced healthy production state');
  assert(hostsFailedPoll.feed.includes('Writing restored copy'), 'hosts failure discarded healthy production copy');
  assert(!hostsFailedPoll.feed.includes('reconnecting'), 'hosts failure falsely marked the producer desk offline');
  failHosts = false;

  statusPayload = {
    ...restoredStatus,
    production: { current: { label: 'Writing while requests hang', kind: 'banter' }, recent: [] },
  };
  await page.evaluate(() => {
    window.__adminSmokeHangPath = '/api/listener-requests';
    window.__adminSmokeRefreshPromise = refreshFast();
  });
  await page.waitForFunction(
    () => productionFeed.innerText.includes('Writing while requests hang') && window.__adminSmokeHangingFetches === 1,
    null,
    { timeout: 2000 },
  );
  await page.waitForFunction(() => window.__adminSmokeHangingFetches === 0, null, { timeout: 2000 });
  await page.evaluate(() => window.__adminSmokeRefreshPromise);
  const hangingListenerPoll = await page.evaluate(() => ({
    label: productionStateLabel.textContent,
    feed: productionFeed.innerText,
  }));
  assert(
    hangingListenerPoll.feed.includes('Writing while requests hang'),
    'never-settling listener request blocked authoritative status',
  );
  assert(!hangingListenerPoll.label.endsWith('reconnecting'), 'listener timeout falsely marked status unavailable');

  statusPayload = {
    ...restoredStatus,
    production: { current: { label: 'Writing while hosts hang', kind: 'banter' }, recent: [] },
  };
  await page.evaluate(() => {
    window.__adminSmokeHangPath = '/api/hosts';
    _hostsOk = false;
    window.__adminSmokeRefreshPromise = refreshFast();
  });
  await page.waitForFunction(
    () => productionFeed.innerText.includes('Writing while hosts hang') && window.__adminSmokeHangingFetches === 1,
    null,
    { timeout: 2000 },
  );
  await page.waitForFunction(() => window.__adminSmokeHangingFetches === 0, null, { timeout: 2000 });
  await page.evaluate(() => window.__adminSmokeRefreshPromise);
  assert(
    await page.locator('#productionFeed').innerText().then((text) => text.includes('Writing while hosts hang')),
    'never-settling hosts request blocked authoritative status',
  );
  await page.evaluate(() => { window.__adminSmokeHangPath = ''; });

  statusScenario = 'queued';
  const staleSuccess = makeQueuedStatus({
    ...restoredStatus,
    production: { current: { label: 'Stale slow status', kind: 'banter' }, recent: [] },
  }, { held: true });
  const freshSuccess = makeQueuedStatus({
    ...restoredStatus,
    production: { current: { label: 'Newest fast status', kind: 'banter' }, recent: [] },
  });
  statusResponseQueue.push(staleSuccess, freshSuccess);
  await page.evaluate(() => { window.__adminSmokeOldRefresh = refreshFast(); });
  await staleSuccess.seen;
  await page.evaluate(() => { window.__adminSmokeNewRefresh = refreshFast(); });
  await freshSuccess.seen;
  await freshSuccess.done;
  await page.evaluate(() => window.__adminSmokeNewRefresh);
  staleSuccess.release();
  await staleSuccess.done;
  await page.evaluate(() => window.__adminSmokeOldRefresh);
  const afterStaleSuccess = await page.locator('#productionFeed').innerText();
  assert(afterStaleSuccess.includes('Newest fast status'), 'stale status success overwrote the newest response');
  assert(!afterStaleSuccess.includes('Stale slow status'), 'stale status success remained visible');

  const staleFailure = makeQueuedStatus({ detail: 'late outage' }, { status: 503, held: true });
  const successAfterFailure = makeQueuedStatus({
    ...restoredStatus,
    production: { current: { label: 'Healthy after stale failure', kind: 'banter' }, recent: [] },
  });
  statusResponseQueue.push(staleFailure, successAfterFailure);
  await page.evaluate(() => { window.__adminSmokeOldRefresh = refreshFast(); });
  await staleFailure.seen;
  await page.evaluate(() => { window.__adminSmokeNewRefresh = refreshFast(); });
  await successAfterFailure.seen;
  await successAfterFailure.done;
  await page.evaluate(() => window.__adminSmokeNewRefresh);
  staleFailure.release();
  await staleFailure.done;
  await page.evaluate(() => window.__adminSmokeOldRefresh);
  const afterStaleFailure = await page.evaluate(() => ({
    label: productionStateLabel.textContent,
    feed: productionFeed.innerText,
  }));
  assert(afterStaleFailure.feed.includes('Healthy after stale failure'), 'stale status failure displaced healthy state');
  assert(!afterStaleFailure.label.endsWith('reconnecting'), 'stale status failure showed a false reconnecting state');

  await page.evaluate(() => {
    window.__adminSmokeToasts = [];
    window.toast = (message) => window.__adminSmokeToasts.push(message);
    updateStopState(false);
  });
  skipScenario = 'declined';
  await page.evaluate(() => doSkip(skipBtn));
  assert(
    await page.evaluate(() => window.__adminSmokeToasts.at(-1)) === 'Station paused by browser smoke',
    'declined skip showed success instead of the backend error',
  );
  const offlineCopy = await page.evaluate(() => offlineMsg());
  skipScenario = 'network';
  await page.evaluate(() => doSkip(skipBtn));
  assert(
    await page.evaluate(() => window.__adminSmokeToasts.at(-1)) === offlineCopy,
    'network-failed skip did not show the offline recovery message',
  );
  skipScenario = 'success';
  await page.evaluate(() => doSkip(skipBtn));
  assert(
    await page.evaluate(() => window.__adminSmokeToasts.at(-1)) === 'Skip — moving to the next segment',
    'successful skip lost its confirmation',
  );

  const sessionEstimateStates = await page.evaluate(() => {
    const render = (consumption) => {
      updateEngineRoom({ listeners: { active: 0, peak: 0 }, consumption, produced_log: [] }, {});
      return {
        text: engineRuntime.innerText,
        estimates: document.querySelectorAll('#apiCostEl').length,
      };
    };
    return {
      ttsOnly: render({ api_calls: 0, tts_characters: 42, api_cost_estimate_usd: 0.25 }),
      idle: render({ api_calls: 0, tts_characters: 0, api_cost_estimate_usd: 0 }),
      unknownTts: render({ api_calls: 0, tts_characters: 42, api_cost_estimate_usd: null }),
    };
  });
  assert(sessionEstimateStates.ttsOnly.text.includes('AI calls: 0'), 'TTS-only session lost the zero AI-call fact');
  assert(sessionEstimateStates.ttsOnly.text.includes('TTS characters: 42'), 'TTS-only session hid paid characters');
  assert(sessionEstimateStates.ttsOnly.text.includes('Session estimate: <$1 est'), 'TTS-only session hid the session estimate');
  assert(sessionEstimateStates.ttsOnly.estimates === 1, 'TTS-only session rendered more than one session estimate');
  assert(!sessionEstimateStates.idle.text.includes('TTS characters:'), 'idle refresh kept stale TTS characters');
  assert(!sessionEstimateStates.idle.text.includes('Session estimate:'), 'idle refresh kept a stale session estimate');
  assert(sessionEstimateStates.idle.estimates === 0, 'idle refresh kept a stale estimate element');
  assert(sessionEstimateStates.unknownTts.text.includes('Session estimate: —'), 'unknown TTS cost did not use the existing dash');
  assert(sessionEstimateStates.unknownTts.estimates === 1, 'unknown TTS cost lost its session estimate element');

  await page.unroute('**/status*');
  await page.unroute('**/api/listener-requests');
  await page.unroute('**/api/hosts');
  await page.unroute('**/api/skip');

  const stoppedControls = await page.evaluate(() => {
    updateStopState(true);
    const airNext = document.querySelector('.mmr-console-triggers .a-trigger');
    const skip = document.getElementById('skipBtn');
    const quickAction = [...document.querySelectorAll('.quick-actions .btn-chip')]
      .find((button) => button.textContent.includes('Fewer banter'));
    airNext.focus();
    const airNextFocused = document.activeElement === airNext;
    skip.focus();
    const skipFocused = document.activeElement === skip;
    updateNow({ type: 'stopped', label: 'Session stopped', started: Date.now() / 1000, metadata: {} });
    return {
      airNextInert: airNext.inert,
      airNextAria: airNext.getAttribute('aria-disabled'),
      airNextFocused,
      skipInert: skip.inert,
      skipAria: skip.getAttribute('aria-disabled'),
      skipFocused,
      quickInert: quickAction.inert,
      quickAria: quickAction.getAttribute('aria-disabled'),
      resumeDisabled: document.getElementById('resumeBtn').disabled,
      resumeInert: document.getElementById('resumeBtn').inert,
      stoppedElapsed: document.getElementById('nowElapsed').textContent,
    };
  });
  assert(
    stoppedControls.airNextInert && stoppedControls.skipInert && stoppedControls.quickInert,
    'stopped producer controls stayed interactive',
  );
  assert(
    stoppedControls.airNextAria === 'true' && stoppedControls.skipAria === 'true' && stoppedControls.quickAria === 'true',
    'stopped controls lost aria-disabled',
  );
  assert(!stoppedControls.airNextFocused, 'a disabled Air Next control remained keyboard-focusable');
  assert(!stoppedControls.skipFocused, 'a stopped Next-track control remained keyboard-focusable');
  assert(!stoppedControls.resumeDisabled && !stoppedControls.resumeInert, 'Start was disabled with the producer actions');
  assert(stoppedControls.stoppedElapsed === '—', 'the synthetic stopped segment restarted the elapsed timer');

  const dynamicControl = await page.evaluate(async () => {
    const host = document.createElement('div');
    const button = document.createElement('button');
    button.className = 'btn-util';
    button.textContent = 'Dynamic setup action';
    host.appendChild(button);
    document.body.appendChild(host);
    await new Promise((resolve) => queueMicrotask(resolve));
    const stopped = { inert: button.inert, aria: button.getAttribute('aria-disabled') };
    button.disabled = true;
    updateStopState(false);
    const resumed = { inert: button.inert, disabled: button.disabled, aria: button.getAttribute('aria-disabled') };
    host.remove();
    return { stopped, resumed };
  });
  assert(dynamicControl.stopped.inert && dynamicControl.stopped.aria === 'true', 'dynamic stopped control escaped synchronization');
  assert(!dynamicControl.resumed.inert, 'dynamic control stayed inert after resume');
  assert(dynamicControl.resumed.disabled, 'resume overwrote an independent capability-disabled state');
  assert(dynamicControl.resumed.aria === 'true', 'independently disabled control lost aria-disabled on resume');

  const resumedControls = await page.evaluate(() => {
    const airNext = document.querySelector('.mmr-console-triggers .a-trigger');
    const skip = document.getElementById('skipBtn');
    const quickAction = [...document.querySelectorAll('.quick-actions .btn-chip')]
      .find((button) => button.textContent.includes('Fewer banter'));
    return {
      airNextInert: airNext.inert,
      airNextAria: airNext.getAttribute('aria-disabled'),
      skipInert: skip.inert,
      skipAria: skip.getAttribute('aria-disabled'),
      quickInert: quickAction.inert,
      quickAria: quickAction.getAttribute('aria-disabled'),
    };
  });
  assert(
    !resumedControls.airNextInert && !resumedControls.skipInert && !resumedControls.quickInert,
    'resume did not restore producer controls',
  );
  assert(
    resumedControls.airNextAria === null && resumedControls.skipAria === null && resumedControls.quickAria === null,
    'resume left stale aria-disabled',
  );

  for (const width of [320, 375]) {
    await page.setViewportSize({ width, height: 900 });
    const geometry = await page.evaluate(() => {
      updateNow({
        type: 'music',
        label: 'Un titolo molto lungo per verificare che il nome della canzone non venga tagliato sul telefono',
        started: Date.now() / 1000,
        duration_sec: 300,
        metadata: { artist: 'Artista con un nome particolarmente lungo' },
      });
      renderProduction({
        session_stopped: false,
        listeners: { active: 1 },
        now_streaming: { type: 'music' },
        production: {
          current: { label: 'Writing a particularly detailed host break for the next transition', kind: 'banter' },
          recent: [{ label: 'Previous production update remains readable', kind: 'news_flash', ok: true }],
        },
      });
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const controlGeometry = (element, labelElement = element) => {
        const rect = element.getBoundingClientRect();
        return {
          id: element.id,
          text: element.textContent.trim(),
          label: (element.getAttribute('aria-label') || element.getAttribute('title') || element.textContent).trim(),
          visible: visible(element),
          width: rect.width,
          height: rect.height,
          textFits: labelElement.scrollWidth <= labelElement.clientWidth + 1
            && labelElement.scrollHeight <= labelElement.clientHeight + 1,
        };
      };
      const overflow = [...document.querySelectorAll('body *')].filter((element) => {
        if (!visible(element)) return false;
        const rect = element.getBoundingClientRect();
        return rect.left < -0.5 || rect.right > innerWidth + 0.5;
      }).map((element) => ({ tag: element.tagName, id: element.id, className: String(element.className) }));
      const airNext = [...document.querySelectorAll('.mmr-console-triggers .a-trigger')]
        .map((element) => controlGeometry(element, element.querySelector('.lb')));
      updateStopState(false);
      const coreTransport = [skipBtn, stopBtn].map((element) => controlGeometry(element));
      updateStopState(true);
      coreTransport.push(controlGeometry(resumeBtn));
      updateStopState(false);
      return {
        documentClientWidth: document.documentElement.clientWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        overflow,
        airNext,
        coreTransport,
        titleFits: nowTitle.scrollWidth <= nowTitle.clientWidth,
        productionFits: productionFeed.scrollWidth <= productionFeed.clientWidth,
      };
    });
    assert(geometry.documentScrollWidth <= geometry.documentClientWidth, `${width}px page acquired horizontal scroll`);
    assert(geometry.overflow.length === 0, `${width}px viewport clipped ${JSON.stringify(geometry.overflow)}`);
    assert(geometry.titleFits && geometry.productionFits, `${width}px console text clipped internally`);
    assert(geometry.airNext.length === 4, `${width}px lost an Air Next control: ${JSON.stringify(geometry.airNext)}`);
    assert(
      geometry.airNext.map((control) => control.text).join('|') === 'Banter|Ad break|News flash|More chaos',
      `${width}px Air Next labels changed or disappeared: ${JSON.stringify(geometry.airNext)}`,
    );
    assert(
      geometry.coreTransport.length === 3
        && geometry.coreTransport.map((control) => control.id).join('|') === 'skipBtn|stopBtn|resumeBtn',
      `${width}px core transport controls are incomplete: ${JSON.stringify(geometry.coreTransport)}`,
    );
    const measuredControls = [...geometry.airNext, ...geometry.coreTransport];
    assert(
      measuredControls.every((control) => control.visible && control.width >= 44 && control.height >= 44),
      `${width}px touch target fell below 44px: ${JSON.stringify(measuredControls)}`,
    );
    assert(
      measuredControls.every((control) => control.label && control.textFits),
      `${width}px control label clipped internally: ${JSON.stringify(measuredControls)}`,
    );
  }

  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const normalMotionRows = await page.evaluate(() => {
    updateRecent({
      last_banter_script: [
        { host: 'Marco', text: 'A.' },
        { host: 'Giulia', text: 'B.' },
      ],
      last_ad_script: {},
    });
    return [...document.querySelectorAll('#recentBody > div')]
      .filter((element) => !element.classList.contains('card-label'))
      .map((element) => ({ hidden: element.hidden, typing: element.classList.contains('tt-typing'), text: element.textContent }));
  });
  assert(!normalMotionRows[0].hidden && normalMotionRows[0].typing, 'normal motion did not start the first speaker');
  assert(normalMotionRows[1].hidden && !normalMotionRows[1].typing, 'normal motion exposed a future empty speaker row');
  await page.waitForFunction(
    () => [...document.querySelectorAll('#recentBody > div')]
      .filter((element) => !element.classList.contains('card-label'))
      .every((element) => !element.hidden && !element.classList.contains('tt-typing') && element.textContent.endsWith('.')),
    null,
    { timeout: 3000 },
  );

  await page.emulateMedia({ reducedMotion: 'reduce' });
  const reducedRows = await page.evaluate(() => {
    updateRecent({
      last_banter_script: [
        { host: 'Marco', text: 'Reduced motion renders this complete immediately.' },
        { host: 'Giulia', text: 'The second speaker is visible too.' },
      ],
      last_ad_script: {},
    });
    return [...document.querySelectorAll('#recentBody > div')]
      .filter((element) => !element.classList.contains('card-label'))
      .map((element) => ({ hidden: element.hidden, typing: element.classList.contains('tt-typing'), text: element.textContent }));
  });
  assert(reducedRows.every((row) => !row.hidden && !row.typing), 'reduced motion left typewriter rows hidden or animated');
  assert(reducedRows[1].text.includes('second speaker is visible'), 'reduced motion truncated the second speaker row');

  const recentContrast = await page.evaluate(() => {
    renderProduction({
      session_stopped: false,
      listeners: { active: 1 },
      now_streaming: { type: 'music' },
      production: { recent: [{ label: 'Finished host break', kind: 'banter', ok: true }] },
    });
    const row = document.querySelector('.prod-entry.recent');
    return { opacity: getComputedStyle(row).opacity, fontSize: getComputedStyle(row).fontSize };
  });
  assert(recentContrast.opacity === '1', 'recent production text is still faded by ancestor opacity');
  assert(pageErrors.length === 0, `uncaught page errors: ${pageErrors.join(' | ')}`);

  return {
    ok: true,
    checks: 16,
    viewports: [320, 375],
    normalMotionRows: normalMotionRows.length,
    reducedMotionRows: reducedRows.length,
    blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
  };
}
