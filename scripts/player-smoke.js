async (page) => {
  const markerUrl = page.url();
  const markerIndex = markerUrl.indexOf('#');
  const baseUrl = markerIndex >= 0 ? markerUrl.slice(markerIndex + 1).replace(/\/+$/, '') : '';
  const requestPosts = [];
  const receiptPolls = [];
  const streamRequests = [];
  const statusPolls = [];
  const streamFixture = 'mammamiradio/assets/demo/recovery/continuity_1.mp3';
  const momentFixture = 'mammamiradio/assets/demo/first_listen/first_listen_show.mp3';
  const momentCaptures = [];
  const momentCommits = [];
  const momentReleases = [];
  const legacyClipPosts = [];
  const songReceiptStorageKey = 'mmr.listener.songReceipt.v1';
  const receiptTokens = {
    reloadMatched: 'smoke-reload-matched',
    reloadNotMatched: 'smoke-reload-not-matched',
    expired404: 'smoke-expired-404',
    expired410: 'smoke-expired-410',
    transient: 'smoke-transient',
    immediate: 'smoke-immediate-terminal',
    staleFrame: 'smoke-stale-frame',
    lateLift: 'smoke-late-lift',
    notPlayable: 'smoke-not-playable',
    temporarilyUnavailable: 'smoke-temporarily-unavailable',
    trackingDeadline: 'smoke-tracking-deadline',
  };
  const receiptPlans = new Map();
  let requestScenario = 'success_shoutout';
  let streamScenario = 'audio';
  let sessionStopped = false;
  let casaScenario = 'recent';
  let adExperimentScenario = 'empty';
  let nowStreamingScenario = 'music';
  let momentCaptureScenario = 'ready';
  let momentCaptureSequence = 0;
  let heldMomentCaptureResolve = null;
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

  function setReceiptPlan(token, plan) {
    receiptPlans.set(token, { index: 0, ...plan });
  }

  function searchingReceipt() {
    return { ok: true, type: 'song_request', song_resolution: 'searching' };
  }

  function matchedReceipt(track) {
    return { ok: true, type: 'song_request', song_resolution: 'matched', song_track: track };
  }

  function notMatchedReceipt() {
    return {
      ok: true,
      type: 'song_request',
      song_resolution: 'not_matched',
      outcome_reason: 'no_verified_match',
    };
  }

  function failedReceipt(songResolution, outcomeReason) {
    return {
      ok: true,
      type: 'song_request',
      song_resolution: songResolution,
      outcome_reason: outcomeReason,
    };
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
  await page.route('**/public-listener-requests/*', async (route) => {
    const token = decodeURIComponent(route.request().url().split('?', 1)[0].split('/').at(-1));
    const plan = receiptPlans.get(token);
    const step = plan
      ? (typeof plan.next === 'function' ? plan.next() : plan.steps[plan.index++])
      : null;
    receiptPolls.push({ token, step: plan ? plan.index : -1 });
    if (!step) {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: '{"ok":false,"error":"unexpected player-smoke receipt poll"}',
      });
      return;
    }
    if (step.hold) {
      await new Promise((resolve) => { step.release = resolve; });
    }
    if (step.abort) {
      await route.abort('failed');
      return;
    }
    await route.fulfill({
      status: step.status || 200,
      contentType: 'application/json',
      body: JSON.stringify(step.body || { ok: false, error: 'request_not_found' }),
    });
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
      song_reload_matched: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.reloadMatched, song_resolution: 'searching' },
      ],
      song_reload_not_matched: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.reloadNotMatched, song_resolution: 'searching' },
      ],
      song_expired_404: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.expired404, song_resolution: 'searching' },
      ],
      song_expired_410: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.expired410, song_resolution: 'searching' },
      ],
      song_transient: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.transient, song_resolution: 'searching' },
      ],
      song_immediate_terminal: [
        200,
        {
          ok: true,
          type: 'song_request',
          public_token: receiptTokens.immediate,
          song_resolution: 'not_matched',
          outcome_reason: 'no_verified_match',
        },
      ],
      song_stale_frame: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.staleFrame, song_resolution: 'searching' },
      ],
      song_late_lift: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.lateLift, song_resolution: 'searching' },
      ],
      song_not_playable: [
        200,
        { ok: true, type: 'song_request', public_token: receiptTokens.notPlayable, song_resolution: 'searching' },
      ],
      song_temporarily_unavailable: [
        200,
        {
          ok: true,
          type: 'song_request',
          public_token: receiptTokens.temporarilyUnavailable,
          song_resolution: 'searching',
        },
      ],
      rate_limited: [429, { ok: false, retry_after: 12 }],
      queue_full: [429, { ok: false, error: 'queue_full' }],
      declined: [400, { ok: false, error: 'request not accepted' }],
    };
    const [status, body] = responses[requestScenario] || responses.declined;
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.route('**/captures/*.mp3', async (route) => {
    await route.fulfill({
      status: 206,
      contentType: 'audio/mpeg',
      headers: {
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store, private, max-age=0',
        'Content-Length': '649197',
        'Content-Range': 'bytes 0-649196/649197',
      },
      path: momentFixture,
    });
  });
  await page.route('**/api/clip/capture/*', async (route) => {
    if (route.request().method() !== 'DELETE') {
      await route.fallback();
      return;
    }
    const captureId = decodeURIComponent(route.request().url().split('/').at(-1));
    momentReleases.push(captureId);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, released: true }),
    });
  });
  function momentCapturePayload(captureId, firstChapter = 'Prima voce', secondChapter = 'Seconda voce') {
    return {
      ok: true,
      capture_id: captureId,
      audio_path: `/captures/${captureId}.mp3`,
      expires_in: 600,
      choices: [
        {
          choice_id: 'moment',
          label: 'Il momento',
          in_sec: 0.25,
          out_sec: 2.75,
          duration_sec: 2.5,
          chapter_ids: ['chapter-1', 'chapter-2'],
        },
        {
          choice_id: 'context',
          label: 'Con contesto',
          in_sec: 0.25,
          out_sec: 4.25,
          duration_sec: 4,
          chapter_ids: ['chapter-1', 'chapter-2'],
        },
      ],
      chapters: [
        { chapter_id: 'chapter-1', label: firstChapter },
        { chapter_id: 'chapter-2', label: secondChapter },
      ],
    };
  }
  await page.route('**/api/clip/capture', async (route) => {
    const scenario = momentCaptureScenario;
    momentCaptures.push(scenario);
    if (scenario === 'no_audio') {
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, reason: 'no_audio' }),
      });
      return;
    }
    let captureId;
    let firstChapter = 'Prima voce';
    let secondChapter = 'Seconda voce';
    if (scenario === 'stale_success') {
      captureId = 'smoke-stale-capture-a';
      firstChapter = 'Stale A prima';
      secondChapter = 'Stale A seconda';
      await new Promise((resolve) => { heldMomentCaptureResolve = resolve; });
    } else if (scenario === 'fresh_after_stale') {
      captureId = 'smoke-fresh-capture-b';
      firstChapter = 'Fresh B prima';
      secondChapter = 'Fresh B seconda';
    } else {
      momentCaptureSequence += 1;
      captureId = `smoke-capture-${momentCaptureSequence}`;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(momentCapturePayload(captureId, firstChapter, secondChapter)),
    });
  });
  await page.route('**/api/clip/commit', async (route) => {
    momentCommits.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        clip_id: 'smoke-final',
        share_url: '/clips/smoke-final',
        track_title: 'Frozen Host Moment',
        track_artist: 'Giulia',
      }),
    });
  });
  await page.route('**/api/clip', async (route) => {
    legacyClipPosts.push(route.request().method());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        clip_id: 'starter-final',
        share_url: '/clips/starter-final',
        track_title: 'Starter Track',
        track_artist: 'Starter Artist',
      }),
    });
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
    const nativeSetTimeout = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay, ...args) => nativeSetTimeout(
      callback,
      delay === 3000 ? 40 : delay,
      ...args,
    );

    const nativeRequestAnimationFrame = window.requestAnimationFrame.bind(window);
    const nativeCancelAnimationFrame = window.cancelAnimationFrame.bind(window);
    let nextHeldFrameId = -1;
    let heldRequestFrames = [];
    window.__playerSmokeHoldRequestFrames = false;
    window.__playerSmokeHeldRequestFrameCount = () => heldRequestFrames.length;
    window.__playerSmokeFlushRequestFrames = () => {
      const frames = heldRequestFrames;
      heldRequestFrames = [];
      frames.forEach(({ callback }) => callback(performance.now()));
    };
    window.requestAnimationFrame = (callback) => {
      if (!window.__playerSmokeHoldRequestFrames) return nativeRequestAnimationFrame(callback);
      const id = nextHeldFrameId--;
      heldRequestFrames.push({ id, callback });
      return id;
    };
    window.cancelAnimationFrame = (id) => {
      if (id >= 0) {
        nativeCancelAnimationFrame(id);
        return;
      }
      heldRequestFrames = heldRequestFrames.filter((frame) => frame.id !== id);
    };
    try { localStorage.setItem('stationName', '__stale_station_identity__'); } catch (_) {}

    window.__playerSmokeMomentShares = [];
    window.__playerSmokeMomentShareMode = 'cancel';
    Object.defineProperty(navigator, 'share', {
      configurable: true,
      value: async (payload) => {
        window.__playerSmokeMomentShares.push(payload);
        if (window.__playerSmokeMomentShareMode === 'cancel') {
          throw new DOMException('cancelled by player smoke', 'AbortError');
        }
      },
    });

    window.__playerSmokeMediaActions = {};
    if (!('mediaSession' in navigator)) {
      Object.defineProperty(navigator, 'mediaSession', {
        configurable: true,
        value: { metadata: null, playbackState: 'none' },
      });
    }
    Object.defineProperty(navigator.mediaSession, 'setActionHandler', {
      configurable: true,
      value: (action, handler) => { window.__playerSmokeMediaActions[action] = handler; },
    });

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

  async function reloadPage() {
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 10000 });
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

  async function exerciseMomentPicker() {
    let stage = 'start live radio';
    try {
      streamScenario = 'audio';
      if (await page.locator('#radio-audio').evaluate((audio) => audio.paused)) {
        await page.locator('#nav-cta').click();
      }
      await page.waitForFunction(
        () => document.getElementById('radio-audio')?.paused === false,
        null,
        { timeout: 7000 },
      );

      stage = 'open ready capture';
      momentCaptureScenario = 'ready';
      await page.locator('#share-clip-btn').click();
      await page.waitForFunction(
        () => document.getElementById('moment-picker')?.dataset.state === 'ready',
        null,
        { timeout: 7000 },
      );
      assert(momentCaptures.length === 1, 'opening the picker did not create exactly one capture');
      const provenance = await page.locator('#moment-picker-provenance').textContent();
      assert(
        provenance.includes('Prima voce') && provenance.includes('Seconda voce'),
        'frozen Moment provenance did not render',
      );

      stage = 'choose context and restore collapsed focus';
      await page.locator('#moment-picker-context-toggle').click();
      const contextOption = page.locator('#moment-picker-choices button').nth(1);
      await contextOption.focus();
      await contextOption.press('Enter');
      const collapsedChoice = await page.evaluate(() => ({
        hidden: document.getElementById('moment-picker-choices')?.hidden,
        expanded: document.getElementById('moment-picker-context-toggle')?.getAttribute('aria-expanded'),
        focused: document.activeElement?.id,
        choice: document.getElementById('moment-picker-choice')?.textContent,
        shareDisabled: document.getElementById('moment-picker-share')?.disabled,
      }));
      assert(
        collapsedChoice.hidden && collapsedChoice.expanded === 'false' &&
          collapsedChoice.focused === 'moment-picker-context-toggle',
        `collapsed Moment choices stranded keyboard focus: ${JSON.stringify(collapsedChoice)}`,
      );
      assert(collapsedChoice.choice.includes('Con contesto'), 'keyboard choice selection did not update the Moment');
      assert(collapsedChoice.shareDisabled, 'changing Moment context did not reset the listen-before-share gate');

      stage = 'start audition';
      await page.locator('#moment-picker-listen').click();
      await page.waitForFunction(
        () => document.getElementById('moment-picker-audio')?.paused === false,
        null,
        { timeout: 7000 },
      );
      assert(
        await page.locator('#radio-audio').evaluate((audio) => audio.paused),
        'live radio mixed with the Moment audition',
      );
      await page.waitForFunction(
        () => !document.getElementById('moment-picker-share')?.disabled,
        null,
        { timeout: 7000 },
      );
      const progress = await page.locator('#moment-picker-progress-fill').evaluate((fill) => ({
        className: fill.className,
        width: fill.style.width,
        pixels: fill.getBoundingClientRect().width,
      }));
      assert(
        progress.className.includes('mmr-moment-picker__progress-fill'),
        'Moment progress fill lost its styled class',
      );
      assert(
        parseFloat(progress.width) > 0 && progress.pixels > 0,
        `Moment progress fill stayed invisible: ${JSON.stringify(progress)}`,
      );

      stage = 'pause audition through Media Session';
      await page.evaluate(() => window.__playerSmokeMediaActions.pause());
      await page.waitForFunction(
        () => document.getElementById('moment-picker-audio')?.paused === true &&
          document.getElementById('radio-audio')?.paused === false,
        null,
        { timeout: 7000 },
      );

      stage = 'resume audition through Media Session and reclaim focus';
      await page.evaluate(() => window.__playerSmokeMediaActions.play());
      await page.waitForFunction(
        () => document.getElementById('moment-picker-audio')?.paused === false,
        null,
        { timeout: 7000 },
      );
      assert(
        await page.locator('#radio-audio').evaluate((audio) => audio.paused),
        'resumed Moment audition did not reclaim audio focus',
      );

      stage = 'stop both transports through Media Session';
      await page.evaluate(() => window.__playerSmokeMediaActions.stop());
      await page.waitForFunction(
        () => document.getElementById('moment-picker-audio')?.paused === true &&
          document.getElementById('radio-audio')?.paused === true,
        null,
        { timeout: 7000 },
      );
      await page.evaluate(() => window.__playerSmokeMediaActions.play());
      await page.waitForFunction(
        () => document.getElementById('moment-picker-audio')?.paused === false,
        null,
        { timeout: 7000 },
      );
      assert(
        await page.locator('#radio-audio').evaluate((audio) => audio.paused),
        'Media Session play mixed radio under the Moment audition',
      );

      stage = 'commit without consuming native share activation';
      await page.locator('#moment-picker-share').click();
      await page.waitForFunction(
        () => document.getElementById('moment-picker')?.dataset.state === 'committed',
        null,
        { timeout: 7000 },
      );
      assert(momentCommits.length === 1, 'first Moment share did not commit exactly once');
      assert(
        (await page.evaluate(() => window.__playerSmokeMomentShares.length)) === 0,
        'Moment commit attempted native share after its click activation expired',
      );

      stage = 'cancel native share from a fresh click';
      await page.locator('#moment-picker-share').click();
      await page.waitForFunction(
        () => window.__playerSmokeMomentShares.length === 1 &&
          document.getElementById('moment-picker')?.dataset.state === 'committed' &&
          !document.getElementById('moment-picker-share')?.disabled,
        null,
        { timeout: 7000 },
      );
      const cancelledState = await page.evaluate(() => ({
        listenDisabled: document.getElementById('moment-picker-listen')?.disabled,
        shareDisabled: document.getElementById('moment-picker-share')?.disabled,
        contextDisabled: document.getElementById('moment-picker-context-toggle')?.disabled,
        choicesDisabled: Array.from(document.querySelectorAll('#moment-picker-choices button'))
          .every((button) => button.disabled),
        shares: window.__playerSmokeMomentShares,
      }));
      assert(cancelledState.listenDisabled, 'native-share cancellation re-enabled consumed replay');
      assert(!cancelledState.shareDisabled, 'native-share cancellation did not allow URL retry');
      assert(
        cancelledState.contextDisabled && cancelledState.choicesDisabled,
        'native-share cancellation re-enabled consumed choices',
      );
      assert(
        cancelledState.shares[0]?.title === `Frozen Host Moment — ${authoritativeName}`,
        'native Moment share used mutable now-playing title',
      );

      stage = 'retain committed lock after late media error';
      const postErrorState = await page.evaluate(() => {
        document.getElementById('moment-picker-audio')?.dispatchEvent(new Event('error'));
        return {
          dialogState: document.getElementById('moment-picker')?.dataset.state,
          listenDisabled: document.getElementById('moment-picker-listen')?.disabled,
          shareDisabled: document.getElementById('moment-picker-share')?.disabled,
          contextDisabled: document.getElementById('moment-picker-context-toggle')?.disabled,
          choicesDisabled: Array.from(document.querySelectorAll('#moment-picker-choices button'))
            .every((button) => button.disabled),
        };
      });
      assert(postErrorState.dialogState === 'committed', 'late media error changed consumed Moment state');
      assert(postErrorState.listenDisabled, 'late media error re-enabled consumed replay');
      assert(!postErrorState.shareDisabled, 'late media error disabled frozen URL retry');
      assert(
        postErrorState.contextDisabled && postErrorState.choicesDisabled,
        'late media error re-enabled consumed choices',
      );

      stage = 'retry frozen native share';
      await page.evaluate(() => { window.__playerSmokeMomentShareMode = 'shared'; });
      await page.locator('#moment-picker-share').click();
      await page.waitForFunction(
        () => !document.getElementById('moment-picker')?.open,
        null,
        { timeout: 7000 },
      );
      assert(momentCommits.length === 1, 'native Moment share retry recommitted the consumed capture');
      assert(
        (await page.evaluate(() => window.__playerSmokeMomentShares.length)) === 2,
        'native Moment share retry did not reuse the frozen URL',
      );
      assert(!momentReleases.includes('smoke-capture-1'), 'committed capture was incorrectly released');

      stage = 'release ready capture on close';
      await page.locator('#share-clip-btn').click();
      await page.waitForFunction(
        () => document.getElementById('moment-picker')?.dataset.state === 'ready',
        null,
        { timeout: 7000 },
      );
      await page.locator('#moment-picker-close').click();
      await waitForRouteCount(
        () => momentReleases.length,
        1,
        5000,
        'closing a ready Moment did not release its capability',
      );
      assert(
        momentReleases[0] === 'smoke-capture-2',
        `wrong Moment capture released: ${JSON.stringify(momentReleases)}`,
      );

      stage = 'legacy complete-track fallback';
      momentCaptureScenario = 'no_audio';
      await page.locator('#share-clip-btn').click();
      await page.waitForFunction(
        () => document.getElementById('moment-picker')?.dataset.state === 'committed',
        null,
        { timeout: 7000 },
      );
      assert(legacyClipPosts.length === 1, 'no_audio did not reach the legacy complete-track endpoint');
      assert(
        (await page.evaluate(() => window.__playerSmokeMomentShares.length)) === 2,
        'legacy commit attempted native share after its click activation expired',
      );
      await page.locator('#moment-picker-share').click();
      await page.waitForFunction(
        () => !document.getElementById('moment-picker')?.open,
        null,
        { timeout: 7000 },
      );
      const shares = await page.evaluate(() => window.__playerSmokeMomentShares);
      assert(
        shares.at(-1)?.title === `Starter Track — ${authoritativeName}`,
        'legacy fallback did not share frozen starter metadata',
      );

      stage = 'hold capture A across close';
      const captureCountBeforeRace = momentCaptures.length;
      momentCaptureScenario = 'stale_success';
      await page.locator('#share-clip-btn').click();
      await waitForRouteCount(
        () => momentCaptures.length,
        captureCountBeforeRace + 1,
        5000,
        'stale capture A request never reached the server',
      );
      assert(typeof heldMomentCaptureResolve === 'function', 'stale capture A was not held');
      assert(
        await page.locator('#moment-picker').getAttribute('data-state') === 'preparing',
        'held capture A left the picker in the wrong state',
      );

      stage = 'close A and reopen fresh B';
      await page.locator('#moment-picker-close').click();
      await page.waitForFunction(
        () => !document.getElementById('moment-picker')?.open &&
          !document.body.classList.contains('mmr-moment-picker-open'),
        null,
        { timeout: 5000 },
      );
      momentCaptureScenario = 'fresh_after_stale';
      await page.locator('#share-clip-btn').click();
      await page.waitForFunction(
        () => document.getElementById('moment-picker')?.dataset.state === 'ready' &&
          document.getElementById('moment-picker-audio')?.src.endsWith('/captures/smoke-fresh-capture-b.mp3'),
        null,
        { timeout: 7000 },
      );
      assert(
        (await page.locator('#moment-picker-provenance').textContent()).includes('Fresh B prima'),
        'fresh capture B did not render before stale A completed',
      );

      stage = 'resolve stale capture A behind B';
      heldMomentCaptureResolve();
      heldMomentCaptureResolve = null;
      await waitForRouteCount(
        () => momentReleases.filter((captureId) => captureId === 'smoke-stale-capture-a').length,
        1,
        5000,
        'stale successful capture was not released',
      );
      const freshAfterStale = await page.evaluate(() => ({
        open: document.getElementById('moment-picker')?.open,
        dialogState: document.getElementById('moment-picker')?.dataset.state,
        audioSrc: document.getElementById('moment-picker-audio')?.src,
        provenance: document.getElementById('moment-picker-provenance')?.textContent,
        listenDisabled: document.getElementById('moment-picker-listen')?.disabled,
        shareDisabled: document.getElementById('moment-picker-share')?.disabled,
      }));
      assert(
        freshAfterStale.open && freshAfterStale.dialogState === 'ready' &&
          freshAfterStale.audioSrc?.endsWith('/captures/smoke-fresh-capture-b.mp3') &&
          freshAfterStale.provenance?.includes('Fresh B prima') &&
          !freshAfterStale.listenDisabled && freshAfterStale.shareDisabled,
        `stale capture completion disturbed reopened picker B: ${JSON.stringify(freshAfterStale)}`,
      );
      assert(
        !momentReleases.includes('smoke-fresh-capture-b'),
        'fresh capture B was released before its picker closed',
      );

      stage = 'close fresh capture B';
      await page.locator('#moment-picker-close').click();
      await waitForRouteCount(
        () => momentReleases.filter((captureId) => captureId === 'smoke-fresh-capture-b').length,
        1,
        5000,
        'fresh capture B was not released on close',
      );

      if (await page.locator('#nav-cta').getAttribute('aria-pressed') === 'true') {
        await page.locator('#nav-cta').click();
      }
      await page.waitForFunction(
        () => document.getElementById('radio-audio')?.paused === true,
        null,
        { timeout: 2000 },
      );
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const state = await page.evaluate(() => ({
        dialogState: document.getElementById('moment-picker')?.dataset.state,
        status: document.getElementById('moment-picker-status')?.textContent,
        radioPaused: document.getElementById('radio-audio')?.paused,
        momentPaused: document.getElementById('moment-picker-audio')?.paused,
        momentCurrentTime: document.getElementById('moment-picker-audio')?.currentTime,
      })).catch(() => ({}));
      throw new Error(`Moment Picker ${stage}: ${detail}; state=${JSON.stringify(state)}`);
    }
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

  async function waitForReceiptText(expected, timeout = 3000) {
    await page.waitForFunction(
      (text) => {
        const receipt = document.getElementById('request-sent');
        return receipt && receipt.offsetParent !== null && receipt.textContent.trim() === text;
      },
      expected,
      { timeout, polling: 20 },
    );
  }

  async function receiptUiState() {
    return page.evaluate((storageKey) => {
      const form = document.getElementById('request-form');
      const name = document.getElementById('req-name');
      const message = document.getElementById('req-msg');
      const receipt = document.getElementById('request-sent');
      const submit = form && form.querySelector('button[type="submit"]');
      return {
        name: name ? name.value : '',
        message: message ? message.value : '',
        messageVisible: Boolean(message && message.offsetParent !== null),
        receipt: receipt ? receipt.textContent.trim() : '',
        receiptVisible: Boolean(receipt && receipt.offsetParent !== null),
        submitDisabled: Boolean(submit && submit.disabled),
        submitting: form ? form.dataset.submitting || '' : '',
        stored: sessionStorage.getItem(storageKey),
      };
    }, songReceiptStorageKey);
  }

  async function startTrackedSong(scenario, message, name = 'Anna', expectedReceipt = copy.form_song_searching) {
    requestScenario = scenario;
    await page.evaluate((storageKey) => sessionStorage.removeItem(storageKey), songReceiptStorageKey);
    await loadFreshPage();
    const pollsBefore = receiptPolls.length;
    await page.locator('#req-name').fill(name);
    await page.locator('#req-msg').fill(message);
    await page.locator('#request-form button[type="submit"]').click();
    await waitForReceiptText(expectedReceipt);
    return { pollsBefore, message, name };
  }

  async function exerciseSongReceiptScenarios() {
    let stage = 'reload terminal outcomes';

    async function exerciseReloadedReceipt({ scenario, token, terminalBody, expectedText, label }) {
      stage = `${label} receipt reload`;
      let reloaded = false;
      const terminalStep = { hold: true, body: terminalBody };
      setReceiptPlan(token, {
        next: () => (reloaded ? terminalStep : { body: searchingReceipt() }),
      });
      const started = await startTrackedSong(scenario, `Smoke ${label} request`);
      await waitForRouteCount(
        () => receiptPolls.length,
        started.pollsBefore + 1,
        2000,
        `${label} searching receipt was never polled before reload`,
      );
      await reloadPage();
      await waitForReceiptText(copy.form_song_searching);
      const restored = await receiptUiState();
      assert(restored.message === started.message, `${label} searching receipt lost its message across reload`);
      assert(restored.submitDisabled, `${label} searching receipt enabled duplicate submission after reload`);
      assert(
        restored.stored && JSON.parse(restored.stored).public_token === token,
        `${label} receipt token did not survive reload`,
      );

      reloaded = true;
      const pollsAfterReload = receiptPolls.length;
      await waitForRouteCount(
        () => receiptPolls.length,
        pollsAfterReload + 1,
        2000,
        `${label} receipt did not resume polling after reload`,
      );
      assert(typeof terminalStep.release === 'function', `${label} terminal poll was not held`);
      terminalStep.release();
      await waitForReceiptText(expectedText);
      const terminal = await receiptUiState();
      assert(terminal.stored === null, `${label} terminal receipt remained in session storage`);
    }

    async function exerciseRetryableTerminalReceipt({ scenario, token, terminalBody, expectedText, label }) {
      stage = `${label} retryable terminal receipt`;
      await page.emulateMedia({ reducedMotion: 'reduce' });
      const terminalStep = { hold: true, body: terminalBody };
      setReceiptPlan(token, { steps: [{ body: searchingReceipt() }, terminalStep] });
      const started = await startTrackedSong(scenario, `Smoke ${label} retry`, 'Lucia');
      await waitForRouteCount(
        () => receiptPolls.filter((poll) => poll.token === token).length,
        2,
        2000,
        `${label} terminal receipt was never polled`,
      );
      assert(typeof terminalStep.release === 'function', `${label} terminal receipt was not held`);
      terminalStep.release();
      await waitForReceiptText(expectedText);

      const terminal = await receiptUiState();
      assert(terminal.receipt === expectedText, `${label} did not use localized terminal copy`);
      assert(terminal.name === started.name, `${label} did not restore the original name`);
      assert(terminal.message === started.message, `${label} did not restore the original message`);
      assert(terminal.messageVisible, `${label} left the retry input hidden`);
      assert(!terminal.submitDisabled && !terminal.submitting, `${label} did not enable retry submit`);
      assert(terminal.stored === null, `${label} retained a terminal tracking token`);

      const pollsAtTerminal = receiptPolls.filter((poll) => poll.token === token).length;
      await page.waitForTimeout(120);
      assert(
        receiptPolls.filter((poll) => poll.token === token).length === pollsAtTerminal,
        `${label} continued polling after its terminal receipt`,
      );
    }

    async function exerciseTerminalAnimationRace({ scenario, token, deferFrame }) {
      stage = `${scenario} animation race`;
      const terminalStep = { hold: true, body: matchedReceipt('Franco Battiato – Centro di gravità permanente') };
      setReceiptPlan(token, { steps: [terminalStep] });
      requestScenario = scenario;
      await page.evaluate((storageKey) => sessionStorage.removeItem(storageKey), songReceiptStorageKey);
      await page.emulateMedia({ reducedMotion: 'no-preference' });
      await loadFreshPage();
      assert(
        !(await page.evaluate(() => matchMedia('(prefers-reduced-motion: reduce)').matches)),
        `${scenario} could not enable the animated receipt path`,
      );
      await page.addStyleTag({
        content: '.mmr-dedica-form.is-sending { animation-play-state: paused !important; }',
      });
      const pollsBefore = receiptPolls.length;
      const postsBefore = requestPosts.length;
      await page.locator('#req-msg').fill(`Smoke ${scenario}`);
      await page.locator('#request-form button[type="submit"]').click();
      await waitForRouteCount(
        () => requestPosts.length,
        postsBefore + 1,
        2000,
        `${scenario} POST was never requested`,
      );

      stage = `${scenario} animation start`;
      try {
        await page.waitForFunction(
          () => document.getElementById('request-form').classList.contains('is-sending'),
          null,
          { timeout: 2000 },
        );
      } catch (error) {
        const formState = await page.evaluate(() => {
          const form = document.getElementById('request-form');
          const receipt = document.getElementById('request-sent');
          return {
            className: form ? form.className : '',
            submitting: form ? form.dataset.submitting || '' : '',
            receipt: receipt ? receipt.textContent.trim() : '',
          };
        });
        throw new Error(`${error.message}; form=${JSON.stringify(formState)}`);
      }
      await page.evaluate(() => { window.__playerSmokeHoldRequestFrames = true; });
      await waitForRouteCount(
        () => receiptPolls.length,
        pollsBefore + 1,
        2000,
        `${scenario} terminal poll was never requested`,
      );

      if (deferFrame) {
        await page.locator('#request-form').evaluate((form) => {
          form.dispatchEvent(new AnimationEvent('animationend', { animationName: 'tt-card-lift', bubbles: true }));
        });
        stage = `${scenario} deferred searching frame`;
        await page.waitForFunction(
          () => window.__playerSmokeHeldRequestFrameCount() === 1,
          null,
          { timeout: 2000, polling: 20 },
        );
      }

      terminalStep.release();
      stage = `${scenario} terminal completion`;
      await page.waitForFunction(
        (storageKey) => sessionStorage.getItem(storageKey) === null,
        songReceiptStorageKey,
        { timeout: 2000, polling: 20 },
      );

      if (!deferFrame) {
        stage = `${scenario} terminal frame`;
        await page.waitForFunction(
          () => window.__playerSmokeHeldRequestFrameCount() === 1,
          null,
          { timeout: 2000, polling: 20 },
        );
        await page.locator('#request-form').evaluate((form) => {
          form.dispatchEvent(new AnimationEvent('animationend', { animationName: 'tt-card-lift', bubbles: true }));
        });
        assert(
          await page.evaluate(() => window.__playerSmokeHeldRequestFrameCount()) === 1,
          'late lift callback queued a stale searching frame',
        );
      } else {
        stage = `${scenario} terminal frame after stale frame`;
        await page.waitForFunction(
          () => window.__playerSmokeHeldRequestFrameCount() === 2,
          null,
          { timeout: 2000, polling: 20 },
        );
      }

      await page.evaluate(() => window.__playerSmokeFlushRequestFrames());
      const expected = copy.form_song_matched.replace(
        '{track}',
        'Franco Battiato – Centro di gravità permanente',
      );
      await waitForReceiptText(expected);
      await page.waitForTimeout(80);
      assert((await receiptUiState()).receipt === expected, `${scenario} stale animation overwrote terminal outcome`);
      await page.evaluate(() => { window.__playerSmokeHoldRequestFrames = false; });
    }

    try {
      const matchedTrack = 'Mina – Città vuota';
      await exerciseReloadedReceipt({
        scenario: 'song_reload_matched',
        token: receiptTokens.reloadMatched,
        terminalBody: matchedReceipt(matchedTrack),
        expectedText: copy.form_song_matched.replace('{track}', matchedTrack),
        label: 'matched',
      });
      await exerciseReloadedReceipt({
        scenario: 'song_reload_not_matched',
        token: receiptTokens.reloadNotMatched,
        terminalBody: notMatchedReceipt(),
        expectedText: copy.form_song_no_verified_match,
        label: 'not-matched',
      });

      for (const expiryStatus of [404, 410]) {
        stage = `${expiryStatus} expired receipt`;
        const token = expiryStatus === 404 ? receiptTokens.expired404 : receiptTokens.expired410;
        const scenario = expiryStatus === 404 ? 'song_expired_404' : 'song_expired_410';
        const expiryStep = { hold: true, status: expiryStatus };
        setReceiptPlan(token, { steps: [expiryStep] });
        const started = await startTrackedSong(scenario, `Smoke expiry ${expiryStatus}`, 'Lucia');
        await waitForRouteCount(
          () => receiptPolls.length,
          started.pollsBefore + 1,
          2000,
          `${expiryStatus} expiry was never polled`,
        );
        expiryStep.release();
        // A pruned record is gone; only the client deadline can claim the hosts
        // still hold the message. The two branches carry different copy.
        await waitForReceiptText(copy.form_song_tracking_lost);
        const expired = await receiptUiState();
        assert(expired.name === started.name, `${expiryStatus} expiry did not restore the original name`);
        assert(expired.message === started.message, `${expiryStatus} expiry did not restore the original input`);
        assert(expired.messageVisible, `${expiryStatus} expiry left the retry input hidden`);
        assert(!expired.submitDisabled && !expired.submitting, `${expiryStatus} expiry did not enable retry submit`);
        assert(expired.stored === null, `${expiryStatus} expiry retained a dead tracking token`);
      }

      stage = 'transient receipt retry';
      const transientRetry = { hold: true, body: matchedReceipt('Lucio Dalla – Anna e Marco') };
      setReceiptPlan(receiptTokens.transient, {
        steps: [{ abort: true }, transientRetry],
      });
      const transientStart = await startTrackedSong('song_transient', 'Smoke transient retry');
      // The retry after a transport failure backs off to 6s, and the init
      // script only compresses the 3s cadence, so this wait is real time.
      await waitForRouteCount(
        () => receiptPolls.length,
        transientStart.pollsBefore + 2,
        9000,
        'transient receipt failure did not retry',
      );
      const retrying = await receiptUiState();
      assert(retrying.receipt === copy.form_song_searching, 'transient poll failure replaced the searching receipt');
      assert(retrying.stored !== null, 'transient poll failure erased the resumable receipt');
      transientRetry.release();
      await waitForReceiptText(copy.form_song_matched.replace('{track}', 'Lucio Dalla – Anna e Marco'));

      stage = 'immediate terminal POST';
      const immediatePollCount = receiptPolls.length;
      await startTrackedSong(
        'song_immediate_terminal',
        'Smoke immediate terminal',
        'Anna',
        copy.form_song_no_verified_match,
      );
      await page.waitForTimeout(120);
      assert(receiptPolls.length === immediatePollCount, 'immediate terminal POST scheduled a receipt poll');
      const immediate = await receiptUiState();
      assert(immediate.stored === null, 'immediate terminal POST stored a tracking token');
      assert(!immediate.submitDisabled, 'immediate terminal POST left retry submit disabled');

      await exerciseRetryableTerminalReceipt({
        scenario: 'song_not_playable',
        token: receiptTokens.notPlayable,
        terminalBody: failedReceipt('not_matched', 'not_playable'),
        expectedText: copy.form_song_not_playable,
        label: 'not-playable',
      });
      await exerciseRetryableTerminalReceipt({
        scenario: 'song_temporarily_unavailable',
        token: receiptTokens.temporarilyUnavailable,
        terminalBody: failedReceipt('failed', 'temporarily_unavailable'),
        expectedText: copy.form_song_temporarily_unavailable,
        label: 'temporarily-unavailable',
      });

      await exerciseTerminalAnimationRace({
        scenario: 'song_stale_frame',
        token: receiptTokens.staleFrame,
        deferFrame: true,
      });
      await exerciseTerminalAnimationRace({
        scenario: 'song_late_lift',
        token: receiptTokens.lateLift,
        deferFrame: false,
      });
      // A stored receipt older than the tracking deadline must stop the loop on
      // sight — before any request goes out — and hand the form back. The
      // source-level guard in tests/web/ can only see that the constants exist;
      // this is what proves the deadline is enforced and that an expired
      // receipt costs the station zero further polls.
      stage = 'tracking deadline on resume';
      setReceiptPlan(receiptTokens.trackingDeadline, { steps: [{ body: searchingReceipt() }] });
      await page.evaluate(
        ([storageKey, token]) => sessionStorage.setItem(storageKey, JSON.stringify({
          public_token: token,
          name: 'Anna',
          message: 'Smoke tracking deadline',
          started_at: Date.now() - 700000,
        })),
        [songReceiptStorageKey, receiptTokens.trackingDeadline],
      );
      const deadlinePollsBefore = receiptPolls.length;
      await loadFreshPage();
      await waitForReceiptText(copy.form_song_tracking_expired);
      const deadline = await receiptUiState();
      assert(deadline.message === 'Smoke tracking deadline', 'tracking deadline lost the listener input');
      assert(deadline.messageVisible, 'tracking deadline left the retry input hidden');
      assert(
        !deadline.submitDisabled && !deadline.submitting,
        'tracking deadline left the request form locked',
      );
      assert(deadline.stored === null, 'tracking deadline retained a dead tracking token');
      await page.waitForTimeout(200);
      assert(
        receiptPolls.length === deadlinePollsBefore,
        'an already-expired receipt still polled the station',
      );

      stage = 'receipt scenario reset';
      await page.evaluate((storageKey) => sessionStorage.removeItem(storageKey), songReceiptStorageKey);
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await loadFreshPage();
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new Error(`player-smoke: ${stage}: ${detail}`);
    }
  }

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

  await exerciseSongReceiptScenarios();

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

  await exerciseMomentPicker();

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
    receipt_polls: receiptPolls.length,
    moment_captures: momentCaptures.length,
    moment_commits: momentCommits.length,
    moment_releases: momentReleases,
    legacy_clip_posts: legacyClipPosts.length,
    blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
  };
}
