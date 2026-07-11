async (page) => {
  const markerUrl = page.url();
  const markerIndex = markerUrl.indexOf('#');
  const baseUrl = markerIndex >= 0 ? markerUrl.slice(markerIndex + 1).replace(/\/+$/, '') : '';
  const requestPosts = [];
  const streamRequests = [];
  const streamFixture = 'mammamiradio/assets/demo/recovery/continuity_1.mp3';
  let requestScenario = 'success_shoutout';
  let streamScenario = 'audio';
  let sessionStopped = false;

  function assert(condition, message) {
    if (!condition) throw new Error(`player-smoke: ${message}`);
  }

  async function waitForRouteCount(getCount, expected, timeoutMs, message) {
    const deadline = Date.now() + timeoutMs;
    while (getCount() < expected && Date.now() < deadline) {
      await page.waitForTimeout(20);
    }
    assert(getCount() >= expected, message);
  }

  assert(/^https?:\/\//.test(baseUrl), `invalid PLAYER_SMOKE_URL marker: ${markerUrl}`);

  const httpOrigin = (value) => (value.match(/^https?:\/\/[^/]+/i) || [''])[0].toLowerCase();
  const baseOrigin = httpOrigin(baseUrl);
  const blockedOffOriginRequests = [];
  const pageErrors = [];
  page.on('pageerror', (error) => pageErrors.push(error.message || String(error)));
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
  await page.emulateMedia({ reducedMotion: 'no-preference' });

  const liveStatusResponse = await page.request.get(`${baseUrl}/public-status`, { timeout: 5000 });
  assert(liveStatusResponse.ok(), `authoritative /public-status returned ${liveStatusResponse.status()}`);
  const liveStatus = await liveStatusResponse.json();
  const authoritativeName =
    (liveStatus.identity && liveStatus.identity.station_name) ||
    (liveStatus.brand && liveStatus.brand.station_name) ||
    '';
  assert(authoritativeName, 'authoritative /public-status has no station identity');

  await page.route('**/public-status', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        identity: { station_name: authoritativeName, source: 'player-smoke' },
        brand: { station_name: authoritativeName },
        capabilities: {},
        session_stopped: sessionStopped,
        uptime_sec: 90,
        tracks_played: 1,
        now_streaming: sessionStopped
          ? { type: 'stopped', label: 'Session stopped', metadata: {} }
          : { type: 'music', label: 'Mina — Città vuota', metadata: {} },
        upcoming: [],
        upcoming_mode: 'building',
        current_progress_sec: 3,
        current_duration_sec: 180,
        ha_moments: null,
      }),
    });
  });
  await page.route('**/public-listener-requests', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: '{"requests":[]}' });
  });
  await page.route('**/api/listener-request', async (route) => {
    requestPosts.push({ scenario: requestScenario, body: route.request().postDataJSON() });
    if (requestScenario === 'network') {
      await route.abort('failed');
      return;
    }
    const responses = {
      success_shoutout: [200, { ok: true, type: 'shoutout' }],
      success_song: [200, { ok: true, type: 'song_request' }],
      rate_limited: [429, { ok: false, retry_after: 12 }],
      queue_full: [429, { ok: false, error: 'queue_full' }],
      declined: [400, { ok: false, error: 'request not accepted' }],
    };
    const [status, body] = responses[requestScenario] || responses.declined;
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.route('**/stream', async (route) => {
    streamRequests.push({ at: Date.now(), url: route.request().url() });
    if (streamScenario === 'abort') {
      await route.abort('failed');
      return;
    }
    if (streamScenario === 'delayed') await page.waitForTimeout(300);
    try {
      await route.fulfill({ status: 200, contentType: 'audio/mpeg', path: streamFixture });
    } catch (_) {
      // A rapid second click can cancel the media request before the delayed
      // fixture is fulfilled. The cancellation is the behavior under test.
    }
  });

  await page.addInitScript(() => {
    try { localStorage.setItem('stationName', '__stale_station_identity__'); } catch (_) {}
  });

  async function waitForLivePage() {
    await page.waitForFunction(
      () => document.body.dataset.state === 'live',
      null,
      { timeout: 5000 },
    );
  }

  async function loadFreshPage() {
    await page.goto(`${baseUrl}/`, { waitUntil: 'domcontentloaded', timeout: 10000 });
    await waitForLivePage();
  }

  await loadFreshPage();

  const identityState = await page.evaluate(() => ({
    title: document.title.trim(),
    navWordmark: document.querySelector('.mmr-brand')?.textContent.replace(/\s+/g, ' ').trim(),
    footerWordmark: document.querySelector('.wordmark')?.textContent.replace(/\s+/g, ' ').trim(),
    cached: localStorage.getItem('stationName'),
  }));
  assert(identityState.title === authoritativeName, 'visible document title disagrees with authoritative identity');
  assert(identityState.navWordmark === authoritativeName, 'nav wordmark disagrees with authoritative identity');
  assert(identityState.footerWordmark === authoritativeName, 'footer wordmark disagrees with authoritative identity');
  assert(identityState.cached === authoritativeName, 'server identity did not repair stale localStorage');

  const copy = await page.evaluate(() => {
    const el = document.getElementById('mmr-copy-bootstrap');
    return el ? JSON.parse(el.textContent) : {};
  });
  const playCount = () => streamRequests.length;
  const initialPlayCount = playCount();
  await page.locator('#req-name').click();
  await page.waitForTimeout(100);
  assert(playCount() === initialPlayCount, 'focusing the dedication form started audio');

  const initialPostCount = requestPosts.length;
  await page.locator('#request-form button[type="submit"]').click();
  await page.waitForFunction(
    (expected) => {
      const el = document.getElementById('request-sent');
      return el && el.dataset.validation === 'empty' && el.offsetParent !== null && el.textContent.trim() === expected;
    },
    copy.form_message_required,
    { timeout: 2000 },
  );
  assert(await page.locator('#request-form').isVisible(), 'empty validation hid the form instead of offering a way out');
  assert(requestPosts.length === initialPostCount, 'empty dedication reached the request API');
  assert(playCount() === initialPlayCount, 'empty dedication submit started audio');

  async function submitScenario(scenario, expectedText, { verifyReset = false } = {}) {
    requestScenario = scenario;
    await loadFreshPage();
    const postsBefore = requestPosts.length;
    const streamsBefore = playCount();
    const message = `Smoke request ${scenario}`;
    await page.locator('#req-msg').fill(message);
    await page.locator('#request-form button[type="submit"]').click();
    await page.waitForFunction(
      (expected) => {
        const el = document.getElementById('request-sent');
        return el && el.offsetParent !== null && el.textContent.trim() === expected;
      },
      expectedText,
      { timeout: 3000 },
    );
    assert(await page.locator('#request-form').isVisible(), `${scenario} receipt hid its form ancestor`);
    assert(requestPosts.length === postsBefore + 1, `${scenario} was not submitted exactly once`);
    assert(requestPosts.at(-1).body.message === message, `${scenario} payload changed`);
    assert(playCount() === streamsBefore, `${scenario} submission started audio`);
    if (!scenario.startsWith('success_')) {
      assert(await page.locator('#req-msg').inputValue() === message, `${scenario} erased the retry message`);
    }
    if (verifyReset) {
      await page.waitForFunction(
        () => {
          const receipt = document.getElementById('request-sent');
          const messageInput = document.getElementById('req-msg');
          return receipt && receipt.offsetParent === null && messageInput && messageInput.offsetParent !== null;
        },
        null,
        { timeout: 7000 },
      );
      assert(await page.locator('#req-msg').inputValue() === message, `${scenario} reset erased the retry message`);
    }
  }

  // Exercise the default animated receipt path first. The reduced-motion path
  // is a separate branch and cannot stand in for the behavior most listeners run.
  await submitScenario('success_shoutout', copy.form_success_shoutout);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await submitScenario('success_song', copy.form_success_song);
  await submitScenario('rate_limited', copy.form_rate_limited.replace('{s}', '12'));
  await submitScenario('queue_full', copy.form_queue_full);
  await submitScenario('declined', copy.form_declined);
  await submitScenario('network', copy.form_network_error, { verifyReset: true });

  // A second click while play() is pending cancels the one in-flight request;
  // it must not create a duplicate request or leave an active playback intent.
  streamScenario = 'delayed';
  const pendingStartCount = playCount();
  await page.locator('#nav-cta').click();
  await waitForRouteCount(
    playCount,
    pendingStartCount + 1,
    2000,
    'pending play did not create its first stream request',
  );
  assert(await page.locator('#nav-cta').getAttribute('aria-pressed') === 'true', 'pending play was not exposed');
  await page.locator('#nav-cta').click();
  await page.waitForTimeout(450);
  assert(playCount() === pendingStartCount + 1, 'rapid play toggle created duplicate stream requests');
  assert(await page.locator('#nav-cta').getAttribute('aria-pressed') === 'false', 'pending play was not cancellable');

  streamScenario = 'audio';
  const startedAt = Date.now();
  const requestCountBeforePlay = playCount();
  await page.locator('#nav-cta').click();
  await page.waitForFunction(
    () => document.getElementById('nav-cta').getAttribute('aria-pressed') === 'true',
    null,
    { timeout: 2000 },
  );
  await waitForRouteCount(
    playCount,
    requestCountBeforePlay + 1,
    2000,
    'play affordance did not request the public MP3 stream',
  );
  assert(playCount() === requestCountBeforePlay + 1, 'play affordance did not request the public MP3 stream');
  const streamIntentMs = streamRequests.at(-1).at - startedAt;
  assert(streamIntentMs < 2000, `stream request intent took ${streamIntentMs}ms (limit: <2000ms)`);
  assert(streamRequests.at(-1).url.endsWith('/stream'), 'play affordance used the wrong stream URL');

  const activeControls = await page.evaluate(() => ({
    nav: {
      pressed: document.getElementById('nav-cta').getAttribute('aria-pressed'),
      label: document.getElementById('nav-cta').getAttribute('aria-label'),
      text: document.getElementById('nav-cta').textContent.trim(),
    },
    compact: {
      pressed: document.getElementById('np-play').getAttribute('aria-pressed'),
      label: document.getElementById('np-play').getAttribute('aria-label'),
    },
    hero: {
      pressed: document.getElementById('hero-play').getAttribute('aria-pressed'),
      label: document.getElementById('hero-play').getAttribute('aria-label'),
      text: document.getElementById('hero-play').textContent.trim(),
    },
  }));
  for (const [name, control] of Object.entries(activeControls)) {
    assert(control.pressed === 'true', `${name} control did not expose pressed=true for active intent`);
    assert(control.label === copy.listen_pause_aria, `${name} control did not announce the pause action`);
  }
  assert(activeControls.nav.text.includes(copy.listen_pause), 'nav control did not show a visible pause action');
  assert(activeControls.hero.text === copy.listen_pause, 'hero control did not show a visible pause action');

  await page.locator('#hero-play').click();
  await page.waitForFunction(
    () => ['nav-cta', 'np-play', 'hero-play'].every(
      (id) => document.getElementById(id).getAttribute('aria-pressed') === 'false',
    ),
    null,
    { timeout: 2000 },
  );
  assert(await page.locator('#hero-play').textContent() === copy.listen_now, 'hero pause did not restore listen copy');

  // Error retries collapse to one timer, and an explicit pause cancels the
  // scheduled retry so sound cannot restart behind the listener's back.
  streamScenario = 'abort';
  const errorStartCount = playCount();
  await page.locator('#nav-cta').click();
  await waitForRouteCount(playCount, errorStartCount + 1, 2000, 'error probe did not request the stream');
  await page.locator('#radio-audio').evaluate((el) => {
    el.dispatchEvent(new Event('error'));
    el.dispatchEvent(new Event('error'));
  });
  assert(
    await page.locator('#nav-cta').getAttribute('aria-pressed') === 'true',
    'failed stream did not retain a cancellable playback intent',
  );
  await waitForRouteCount(playCount, errorStartCount + 2, 2500, 'bounded stream retry never fired');
  assert(playCount() === errorStartCount + 2, 'repeated errors scheduled duplicate retries');
  await page.locator('#nav-cta').click();
  const countAtPause = playCount();
  await page.waitForTimeout(2200);
  assert(playCount() === countAtPause, 'scheduled retry restarted audio after explicit pause');

  sessionStopped = true;
  await page.waitForFunction(
    () => ['nav-cta', 'np-play', 'hero-play'].every((id) => document.getElementById(id).disabled),
    null,
    { timeout: 4000 },
  );
  const stoppedControls = await page.evaluate(() => ['nav-cta', 'np-play', 'hero-play'].map((id) => ({
    id,
    pressed: document.getElementById(id).getAttribute('aria-pressed'),
    label: document.getElementById(id).getAttribute('aria-label'),
  })));
  stoppedControls.forEach((control) => {
    assert(control.pressed === 'false', `${control.id} stayed pressed while station was stopped`);
    assert(control.label === copy.listen_paused_aria, `${control.id} advertised an action while station was stopped`);
  });
  const stoppedRequestCount = playCount();
  await page.locator('#nav-cta').evaluate((el) => el.click());
  await page.waitForTimeout(100);
  assert(playCount() === stoppedRequestCount, 'disabled stopped control requested audio');
  assert(pageErrors.length === 0, `uncaught page errors: ${pageErrors.join(' | ')}`);

  return {
    ok: true,
    checks: 12,
    stream_intent_ms: streamIntentMs,
    identity: authoritativeName,
    request_scenarios: requestPosts.map((entry) => entry.scenario),
    blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
  };
}
