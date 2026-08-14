async (page) => {
  const markerUrl = page.url();
  const markerIndex = markerUrl.indexOf('#');
  const baseUrl = markerIndex >= 0 ? markerUrl.slice(markerIndex + 1).replace(/\/+$/, '') : '';
  const requestPosts = [];
  const streamRequests = [];
  const statusPolls = [];
  const streamFixture = 'mammamiradio/assets/demo/recovery/continuity_1.mp3';
  let requestScenario = 'success_shoutout';
  let streamScenario = 'audio';
  let sessionStopped = false;
  let casaScenario = 'recent';
  let adExperimentScenario = 'empty';
  let nowStreamingScenario = 'music';
  let tracksPlayed = 5;
  let rotationTrackCount = 84;
  // Deliberately stale: the hero must render the live rotation count instead.
  const currentSource = {
    kind: 'charts',
    label: 'Italian charts',
    track_count: 999,
  };
  const casaReceipts = {
    recent: [
      { label: 'One minute ritual', ago_min: 1, status: 'aired' },
      { label: 'Fifty-nine minute ritual', ago_min: 59, status: 'aired' },
      { label: 'One hour ritual', ago_min: 60, status: 'aired' },
      { label: 'Twenty-three hour ritual', ago_min: 1439, status: 'aired' },
      { label: 'Yesterday ritual', ago_min: 1440, status: 'aired' },
      { label: 'Late-yesterday ritual', ago_min: 2879, status: 'aired' },
      { label: 'Two-day ritual', ago_min: 2880, status: 'aired' },
      { label: 'Private dropped ritual', ago_min: 2, status: 'dropped' },
    ],
    stale: [
      { label: 'Yesterday ritual', ago_min: 1440, status: 'aired' },
      { label: 'Two-day ritual', ago_min: 2880, status: 'aired' },
      { label: 'Private dropped ritual', ago_min: 2, status: 'dropped' },
    ],
    airing: [
      { label: 'Live ritual', ago_min: 3000, status: 'airing' },
      { label: 'Private dropped ritual', ago_min: 2, status: 'dropped' },
    ],
  };
  const hostileBrand = '<img src=x onerror="window.__adRosterXss=1">';
  const adExperiments = {
    empty: {
      scope: 'runtime',
      completed_breaks: 0,
      completed_spots: 0,
      brands: [],
    },
    one: {
      scope: 'runtime',
      completed_breaks: 1,
      completed_spots: 1,
      brands: [{ brand: 'Prezzoforte', completed_airings: 1 }],
    },
    many: {
      scope: 'runtime',
      completed_breaks: 2,
      completed_spots: 3,
      brands: [
        { brand: hostileBrand, completed_airings: 2 },
        { brand: 'TeleCuore', completed_airings: 1 },
      ],
    },
  };

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
    statusPolls.push(Date.now());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        identity: { station_name: authoritativeName, source: 'player-smoke' },
        brand: { station_name: authoritativeName },
        capabilities: { ha: true },
        session_stopped: sessionStopped,
        uptime_sec: 90,
        tracks_played: tracksPlayed,
        rotation_track_count: rotationTrackCount,
        current_source: currentSource,
        now_streaming: sessionStopped
          ? { type: 'stopped', label: 'Session stopped', metadata: {} }
          : nowStreamingScenario === 'ad-roster'
            ? { type: 'ad', label: 'Ad break', metadata: { brands: ['Prezzoforte', 'TeleCuore'] } }
            : nowStreamingScenario === 'ad-generic'
              ? { type: 'ad', label: 'Ad break', metadata: {} }
              : { type: 'music', label: 'Mina — Città vuota', metadata: {} },
        upcoming: [],
        upcoming_mode: 'building',
        current_progress_sec: 3,
        current_duration_sec: 180,
        ha_moments: {
          mood: '',
          weather: '',
          last_event_label: '',
          recent: casaReceipts[casaScenario],
        },
        ad_experiment: adExperiments[adExperimentScenario],
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

    const nativeSetInterval = window.setInterval;
    window.setInterval = (callback, delay, ...args) => {
      const id = Reflect.apply(nativeSetInterval, window, [callback, delay, ...args]);
      if (delay === 3000 && callback?.name === 'fetchStatus') {
        window.__playerSmokeFetchStatus = callback;
        window.__playerSmokeStatusInterval = id;
      }
      return id;
    };

    const nativeJson = Response.prototype.json;
    Response.prototype.json = async function (...args) {
      const value = await Reflect.apply(nativeJson, this, args);
      const gate = window.__playerSmokeStatusJsonGate;
      if (gate && !gate.claimed && this.url && new URL(this.url).pathname.endsWith('/public-status')) {
        gate.claimed = true;
        gate.seen = true;
        await gate.promise;
      }
      return value;
    };
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

  // A value that is expected to CHANGE proves itself: the wait can only succeed
  // once the new payload has rendered. A value expected NOT to move cannot, so
  // it passes settle — the page repolls /public-status every 3s, and a second
  // counted poll means the render for the first one has already landed.
  async function expectRotationStat(expected, message, { settle = false } = {}) {
    if (settle) {
      const before = statusPolls.length;
      await waitForRouteCount(
        () => statusPolls.length,
        before + 2,
        10000,
        `${message} (the page never refetched)`,
      );
    }
    await page.waitForFunction(
      (value) => (document.getElementById('stat-tracks')?.textContent || '').trim() === value,
      expected,
      { timeout: 5000, polling: 250 },
    ).catch(() => assert(false, message));
  }

  await loadFreshPage();

  await expectRotationStat('84', 'Tracks in Rotation ignored live rotation size');
  tracksPlayed = 9;
  await expectRotationStat('84', 'Tracks in Rotation changed with played history', { settle: true });
  rotationTrackCount = 27;
  await expectRotationStat('27', 'Tracks in Rotation did not update with playlist mutation');
  rotationTrackCount = 0;
  await expectRotationStat('0', 'Known empty rotation did not render zero');
  rotationTrackCount = null;
  await expectRotationStat('—', 'Missing rotation count fell back to source or played history');

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

  await page.setViewportSize({ width: 320, height: 640 });
  const mobileNavGeometry = await page.evaluate(() => {
    const nav = document.querySelector('.mmr-nav-inner');
    const brand = document.querySelector('.mmr-brand');
    const status = document.getElementById('nav-cta');
    const viewportWidth = document.documentElement.clientWidth;
    const rect = (element) => {
      const box = element?.getBoundingClientRect();
      return box ? { left: box.left, right: box.right, width: box.width } : null;
    };
    return {
      viewportWidth,
      documentWidth: document.documentElement.scrollWidth,
      navWidth: nav?.clientWidth || 0,
      navScrollWidth: nav?.scrollWidth || 0,
      brand: rect(brand),
      status: rect(status),
    };
  });
  assert(
    mobileNavGeometry.status && mobileNavGeometry.status.right <= mobileNavGeometry.viewportWidth + 1,
    `320px nav status pill escaped viewport: ${JSON.stringify(mobileNavGeometry)}`,
  );
  assert(
    mobileNavGeometry.brand && mobileNavGeometry.brand.left >= -1,
    `320px nav brand escaped viewport: ${JSON.stringify(mobileNavGeometry)}`,
  );
  assert(
    mobileNavGeometry.navScrollWidth <= mobileNavGeometry.navWidth + 1,
    `320px nav content overflowed its inner row: ${JSON.stringify(mobileNavGeometry)}`,
  );
  await page.setViewportSize({ width: 1280, height: 720 });

  const copy = await page.evaluate(() => {
    const el = document.getElementById('mmr-copy-bootstrap');
    return el ? JSON.parse(el.textContent) : {};
  });

  async function waitForStatusRender(predicate, argument, message) {
    const before = statusPolls.length;
    await waitForRouteCount(
      () => statusPolls.length,
      before + 1,
      10000,
      `${message} (the page never refetched)`,
    );
    await page.waitForFunction(predicate, argument, { timeout: 5000, polling: 50 })
      .catch(() => assert(false, message));
  }

  const emptyAdReceipt = await page.evaluate(() => {
    const details = document.getElementById('ad-session-receipt');
    return {
      hidden: details?.hidden,
      summary: document.getElementById('ad-session-summary')?.textContent || '',
      announcement: document.getElementById('ad-session-announcement')?.textContent || '',
      rows: document.querySelectorAll('#ad-session-brands li').length,
    };
  });
  assert(emptyAdReceipt.hidden, 'empty runtime ad receipt was visible');
  assert(emptyAdReceipt.summary === '' && emptyAdReceipt.rows === 0, 'empty runtime ad receipt kept stale content');
  assert(emptyAdReceipt.announcement === '', 'empty runtime ad announcement kept stale content');

  adExperimentScenario = 'one';
  await waitForStatusRender(
    (expected) => {
      const details = document.getElementById('ad-session-receipt');
      return details && !details.hidden &&
        document.getElementById('ad-session-summary')?.textContent === expected &&
        document.getElementById('ad-session-announcement')?.textContent === expected;
    },
    copy.ad_session_summary_one,
    'first completed ad receipt did not reveal with singular copy',
  );
  const singularAdReceipt = await page.evaluate(() => ({
    open: document.getElementById('ad-session-receipt')?.open,
    announcement: document.getElementById('ad-session-announcement')?.textContent || '',
    rows: Array.from(document.querySelectorAll('#ad-session-brands li')).map((row) => ({
      brand: row.querySelector('.mmr-ad-session-brand')?.textContent,
      airings: row.querySelector('.mmr-ad-session-count')?.textContent,
    })),
  }));
  assert(!singularAdReceipt.open, 'first completed ad receipt expanded itself');
  assert(singularAdReceipt.announcement === copy.ad_session_summary_one, 'first completed ad receipt was not announced');
  assert(
    singularAdReceipt.rows.length === 1 &&
      singularAdReceipt.rows[0].brand === 'Prezzoforte' &&
      singularAdReceipt.rows[0].airings === copy.ad_session_airings_one,
    `singular ad receipt rendered the wrong row: ${JSON.stringify(singularAdReceipt.rows)}`,
  );

  await page.locator('#ad-session-summary').click();
  assert(await page.locator('#ad-session-receipt').getAttribute('open') !== null, 'ad receipt did not expand');

  // Change the rotation count to prove this poll reached listener.js without rewriting the receipt.
  rotationTrackCount = 26;
  await waitForStatusRender(
    (expected) => {
      const details = document.getElementById('ad-session-receipt');
      const rows = Array.from(document.querySelectorAll('#ad-session-brands li')).map((row) => ({
        brand: row.querySelector('.mmr-ad-session-brand')?.textContent,
        airings: row.querySelector('.mmr-ad-session-count')?.textContent,
      }));
      return document.getElementById('stat-tracks')?.textContent === expected.rotation &&
        details?.open &&
        document.getElementById('ad-session-summary')?.textContent === expected.summary &&
        document.getElementById('ad-session-announcement')?.textContent === expected.announcement &&
        rows.length === 1 &&
        rows[0].brand === 'Prezzoforte' &&
        rows[0].airings === expected.airings;
    },
    {
      rotation: '26',
      summary: copy.ad_session_summary_one,
      announcement: copy.ad_session_summary_one,
      airings: copy.ad_session_airings_one,
    },
    'unchanged status refresh collapsed or rewrote the expanded ad receipt',
  );

  adExperimentScenario = 'many';
  const manySummary = copy.ad_session_summary.replace('{n}', '3');
  await waitForStatusRender(
    (expected) => document.getElementById('ad-session-summary')?.textContent === expected &&
      document.getElementById('ad-session-announcement')?.textContent === expected,
    manySummary,
    'updated ad receipt did not use plural copy',
  );
  const manyAdReceipt = await page.evaluate(() => ({
    open: document.getElementById('ad-session-receipt')?.open,
    announcement: document.getElementById('ad-session-announcement')?.textContent || '',
    rows: Array.from(document.querySelectorAll('#ad-session-brands li')).map((row) => ({
      brand: row.querySelector('.mmr-ad-session-brand')?.textContent,
      airings: row.querySelector('.mmr-ad-session-count')?.textContent,
    })),
    injectedImageCount: document.querySelectorAll('#ad-session-brands img').length,
    xssMarker: window.__adRosterXss || 0,
  }));
  assert(manyAdReceipt.open, 'status refresh collapsed the expanded ad receipt');
  assert(manyAdReceipt.announcement === manySummary, 'updated ad receipt was not announced with plural copy');
  assert(manyAdReceipt.rows.length === 2, `plural ad receipt rendered the wrong rows: ${JSON.stringify(manyAdReceipt.rows)}`);
  assert(manyAdReceipt.rows[0].brand === hostileBrand, 'wire brand text was changed or dropped');
  assert(
    manyAdReceipt.rows[0].airings === copy.ad_session_airings.replace('{n}', '2'),
    'plural completed-airing copy was wrong',
  );
  assert(manyAdReceipt.rows[1].airings === copy.ad_session_airings_one, 'singular row copy regressed inside plural receipt');
  assert(manyAdReceipt.injectedImageCount === 0 && manyAdReceipt.xssMarker === 0, 'wire brand text executed as markup');

  nowStreamingScenario = 'ad-roster';
  await waitForStatusRender(
    (expected) => document.getElementById('np-track')?.textContent === expected,
    'Prezzoforte · TeleCuore',
    'live ad roster did not replace generic sponsored copy',
  );
  const rosterSurfaces = await page.evaluate(() => ({
    title: document.getElementById('np-track')?.textContent,
    secondary: document.getElementById('np-artist')?.textContent,
    mediaTitle: navigator.mediaSession?.metadata?.title || '',
    mediaArtist: navigator.mediaSession?.metadata?.artist || '',
  }));
  assert(rosterSurfaces.title === 'Prezzoforte · TeleCuore', 'visible live ad roster lost source order');
  assert(rosterSurfaces.secondary === copy.np_ad_break, 'visible live ad roster used the wrong secondary copy');
  assert(rosterSurfaces.mediaTitle === rosterSurfaces.title, 'Media Session ad roster did not match the visible roster');
  assert(rosterSurfaces.mediaArtist === rosterSurfaces.secondary, 'Media Session ad label did not match the visible roster');

  nowStreamingScenario = 'ad-generic';
  await waitForStatusRender(
    (expected) => document.getElementById('np-track')?.textContent === expected,
    copy.np_ad_message,
    'brandless ad did not retain generic sponsored copy',
  );
  const genericAdSurfaces = await page.evaluate(() => ({
    title: document.getElementById('np-track')?.textContent,
    secondary: document.getElementById('np-artist')?.textContent,
    mediaTitle: navigator.mediaSession?.metadata?.title || '',
    mediaArtist: navigator.mediaSession?.metadata?.artist || '',
  }));
  assert(genericAdSurfaces.secondary === copy.seg_ad, 'brandless ad used the wrong generic label');
  assert(genericAdSurfaces.mediaTitle === genericAdSurfaces.title, 'generic Media Session title did not match the visible title');
  assert(genericAdSurfaces.mediaArtist === genericAdSurfaces.secondary, 'generic Media Session label did not match the visible label');

  // Hold poll N after JSON parsing, render poll N+1, then release N. This puts
  // the stale response beyond AbortController cancellation and tests the generation guard.
  await page.evaluate(() => {
    if (typeof window.__playerSmokeFetchStatus !== 'function') {
      throw new Error('player-smoke: status poll callback was not captured');
    }
    clearInterval(window.__playerSmokeStatusInterval);
    let release;
    const gate = { claimed: false, seen: false };
    gate.promise = new Promise((resolve) => { release = resolve; });
    gate.release = release;
    window.__playerSmokeStatusJsonGate = gate;
    window.__playerSmokeOldStatusPoll = window.__playerSmokeFetchStatus();
  });
  await page.waitForFunction(
    () => window.__playerSmokeStatusJsonGate?.seen,
    null,
    { timeout: 5000, polling: 20 },
  ).catch(() => assert(false, 'stale status race never held poll N after JSON parsing'));

  adExperimentScenario = 'empty';
  rotationTrackCount = 28;
  await page.evaluate(() => window.__playerSmokeFetchStatus());
  await page.waitForFunction(
    () => {
      const details = document.getElementById('ad-session-receipt');
      return document.getElementById('stat-tracks')?.textContent === '28' &&
        details && details.hidden && !details.open &&
        document.getElementById('ad-session-summary')?.textContent === '' &&
        document.getElementById('ad-session-announcement')?.textContent === '' &&
        document.querySelectorAll('#ad-session-brands li').length === 0;
    },
    null,
    { timeout: 5000, polling: 50 },
  ).catch(() => assert(false, 'runtime reset did not hide, collapse, and clear the stale ad receipt'));

  await page.evaluate(async () => {
    window.__playerSmokeStatusJsonGate.release();
    await window.__playerSmokeOldStatusPoll;
  });
  const postRaceReceipt = await page.evaluate(() => {
    const details = document.getElementById('ad-session-receipt');
    return {
      rotation: document.getElementById('stat-tracks')?.textContent || '',
      hidden: details?.hidden,
      open: details?.open,
      summary: document.getElementById('ad-session-summary')?.textContent || '',
      announcement: document.getElementById('ad-session-announcement')?.textContent || '',
      rows: document.querySelectorAll('#ad-session-brands li').length,
    };
  });
  assert(
    postRaceReceipt.rotation === '28' && postRaceReceipt.hidden && !postRaceReceipt.open &&
      postRaceReceipt.summary === '' && postRaceReceipt.announcement === '' && postRaceReceipt.rows === 0,
    `stale status poll restored a cleared ad receipt: ${JSON.stringify(postRaceReceipt)}`,
  );
  assert(
    await page.locator('#ad-session-announcement').textContent() === '',
    'runtime reset did not clear the ad receipt announcement',
  );
  nowStreamingScenario = 'music';

  async function casaState() {
    await page.waitForFunction(
      () => {
        const card = document.getElementById('casa-moments');
        return card && !card.hasAttribute('hidden') && document.querySelectorAll('#casa-moments-rows .row').length > 0;
      },
      null,
      { timeout: 5000 },
    );
    return page.evaluate(() => ({
      title: document.querySelector('.casa-moments-eyebrow')?.textContent.trim(),
      helper: document.querySelector('.casa-moments-helper')?.textContent.trim(),
      rows: Array.from(document.querySelectorAll('#casa-moments-rows .row')).map((row) => row.textContent.trim()),
      staleHidden: document.getElementById('casa-moments-stale')?.hasAttribute('hidden'),
    }));
  }

  const recentCasa = await casaState();
  assert(recentCasa.title === copy.casa_moments_title, 'Casa receipt title did not use active-language copy');
  assert(recentCasa.helper === copy.casa_moments_helper, 'Casa receipt helper did not explain the on-air-only record');
  assert(recentCasa.rows.length === 7, `Casa receipt exposed a non-on-air row: ${JSON.stringify(recentCasa.rows)}`);
  assert(!recentCasa.rows.some((row) => row.includes('Private dropped ritual')), 'Casa receipt exposed a dropped private row');
  assert(recentCasa.rows.some((row) => row.includes(copy.casa_moment_minutes_ago.replace('{m}', '1'))), 'Casa one-minute boundary was not humanized');
  assert(recentCasa.rows.some((row) => row.includes(copy.casa_moment_minutes_ago.replace('{m}', '59'))), 'Casa 59-minute boundary was not humanized');
  assert(recentCasa.rows.some((row) => row.includes(copy.casa_moment_hours_ago.replace('{h}', '1'))), 'Casa one-hour boundary was not humanized');
  assert(recentCasa.rows.some((row) => row.includes(copy.casa_moment_hours_ago.replace('{h}', '23'))), 'Casa 23-hour boundary was not humanized');
  assert(recentCasa.rows.filter((row) => row.includes(copy.casa_moment_yesterday)).length === 2, 'Casa yesterday boundaries were not humanized');
  assert(recentCasa.rows.some((row) => row.includes(copy.casa_moment_days_ago.replace('{d}', '2'))), 'Casa whole-day boundary was not humanized');
  assert(recentCasa.staleHidden, 'Casa stale note appeared despite a newer receipt');

  casaScenario = 'stale';
  await loadFreshPage();
  const staleCasa = await casaState();
  assert(!staleCasa.staleHidden, 'Casa stale note did not appear after a day without an on-air receipt');
  assert(
    await page.locator('#casa-moments-stale').textContent() === copy.casa_moment_stale,
    'Casa stale note did not use active-language copy',
  );

  casaScenario = 'airing';
  await loadFreshPage();
  const airingCasa = await casaState();
  assert(airingCasa.rows.length === 1 && airingCasa.rows[0].includes(copy.casa_moment_airing), 'Casa on-air receipt did not render');
  assert(airingCasa.staleHidden, 'Casa stale note remained while a receipt was on air');

  casaScenario = 'recent';
  await loadFreshPage();
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
  await page.waitForFunction(
    () => document.getElementById('radio-audio').paused === false,
    null,
    { timeout: 6000 },
  );
  assert(
    await page.locator('#radio-audio').evaluate((el) => el.paused === false),
    'play click left the audio element paused',
  );

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

  // A device/browser pause must clear listener intent, so one tap means play
  // again rather than an invisible second pause. The finite fixture is removed
  // after the synthetic pause so the resumed live-stream request is observable.
  const externalPauseStartCount = playCount();
  await page.locator('#radio-audio').evaluate((el) => {
    if (el.ended || el.error) throw new Error('external-pause fixture was not actively playable');
    el.dispatchEvent(new Event('pause'));
    el.pause();
    el.removeAttribute('src');
    el.load();
  });
  await page.waitForFunction(
    () => ['nav-cta', 'np-play', 'hero-play'].every(
      (id) => document.getElementById(id).getAttribute('aria-pressed') === 'false',
    ),
    null,
    { timeout: 2000 },
  );
  const externallyPausedControls = await page.evaluate(() => ['nav-cta', 'np-play', 'hero-play'].map((id) => ({
    id,
    pressed: document.getElementById(id).getAttribute('aria-pressed'),
    label: document.getElementById(id).getAttribute('aria-label'),
    text: document.getElementById(id).textContent.trim(),
  })));
  externallyPausedControls.forEach((control) => {
    assert(control.pressed === 'false', `${control.id} stayed active after an external pause`);
    assert(control.label === copy.listen_now_aria, `${control.id} did not offer Listen Now after an external pause`);
  });
  assert(externallyPausedControls[0].text.includes(copy.listen_now), 'nav external pause did not restore listen copy');
  assert(externallyPausedControls[2].text === copy.listen_now, 'hero external pause did not restore listen copy');
  await page.locator('#nav-cta').click();
  await waitForRouteCount(
    playCount,
    externalPauseStartCount + 1,
    2000,
    'one click after an external pause did not request the stream again',
  );
  assert(playCount() === externalPauseStartCount + 1, 'external pause recovery created duplicate stream requests');

  await page.locator('#hero-play').click();
  await page.waitForFunction(
    () => ['nav-cta', 'np-play', 'hero-play'].every(
      (id) => document.getElementById(id).getAttribute('aria-pressed') === 'false',
    ),
    null,
    { timeout: 2000 },
  );
  assert(await page.locator('#hero-play').textContent() === copy.listen_now, 'hero pause did not restore listen copy');
  await page.waitForFunction(
    () => document.getElementById('radio-audio').paused === true,
    null,
    { timeout: 2000 },
  );
  assert(
    await page.locator('#radio-audio').evaluate((el) => el.paused === true),
    'pause cancel left the audio element playing',
  );

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
  await waitForRouteCount(playCount, errorStartCount + 2, 6000, 'bounded stream retry never fired');
  assert(playCount() === errorStartCount + 2, 'repeated errors scheduled duplicate retries');
  await page.locator('#nav-cta').click();
  const countAtPause = playCount();
  await page.waitForTimeout(3000);
  assert(playCount() === countAtPause, 'scheduled retry restarted audio after explicit pause');

  sessionStopped = true;
  await page.waitForFunction(
    () => ['nav-cta', 'np-play', 'hero-play'].every((id) => document.getElementById(id).disabled),
    null,
    { timeout: 10000 },
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
    stream_intent_ms: streamIntentMs,
    identity: authoritativeName,
    request_scenarios: requestPosts.map((entry) => entry.scenario),
    blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
  };
}
