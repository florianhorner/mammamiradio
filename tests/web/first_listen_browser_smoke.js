async (page) => {
  const markerUrl = page.url();
  const markerIndex = markerUrl.indexOf('#');
  const baseUrl = markerIndex >= 0 ? markerUrl.slice(markerIndex + 1).replace(/\/+$/, '') : '';

  function assert(condition, message) {
    if (!condition) throw new Error(`first-listen-browser-smoke: ${message}`);
  }

  assert(/^https?:\/\//.test(baseUrl), `invalid browser smoke marker: ${markerUrl}`);
  const originOf = (value) => (value.match(/^https?:\/\/[^/]+/i) || [''])[0].toLowerCase();
  const baseOrigin = originOf(baseUrl);
  const blockedOffOriginRequests = [];
  const pageErrors = [];
  const playerRequests = [];
  const playRequests = [];
  const receiptRetryRequests = [];
  const verifyRequests = [];
  const previewRequests = [];
  const privacyRequests = [];
  let rejectNextEnable = true;
  let ambientOnlyPreview = false;
  let failNextPlayReceipt = false;
  let receiptRetryFailuresRemaining = 0;
  let discardNextPlayResponse = false;
  let discardedResponseProjection = null;
  let playerCandidatesAvailable = true;
  let playerReceiptRecoveryEntity = '';

  const sourceRows = ({ primary = 'playable', recovery = 'cover_only' } = {}) => [
    { kind: 'charts', label: 'Live charts', status: primary, detail: 'Live chart evidence' },
    { kind: 'jamendo', label: 'Jamendo', status: 'not_configured', detail: 'Optional rights-safe source' },
    { kind: 'local', label: 'Local music', status: 'not_configured', detail: 'Private local folder' },
    { kind: 'demo', label: 'Bundled demo music', status: 'not_bundled', detail: 'No bundled library' },
    { kind: 'recovery', label: 'Recovery cover', status: recovery, detail: 'Transport cover only' },
  ];
  const setupProjection = ({
    audio = false,
    privacy = false,
    privacyEnabled = false,
    primary = 'playable',
    recovery = 'cover_only',
    onboardingRequired = true,
    fresh = true,
    bootstrapReady = true,
    sources = true,
    receiptRecoveryEntity = '',
    durableAttemptId = '',
    durableEntityId = '',
  } = {}) => {
    const rows = sources ? sourceRows({ primary, recovery }) : [];
    const healthy = ['playable', 'on_air'].includes(primary);
    const acceptedAttemptId = durableAttemptId || (audio ? 'browser-attempt-server' : '');
    const selectedEntityId = durableEntityId || (audio ? 'media_player.mac_lab_speaker' : '');
    return {
      detected_mode: 'addon',
      available_modes: [{ id: 'addon', label: 'Home Assistant add-on' }],
      station_mode: { id: 'demo', label: 'Demo Radio' },
      identity: {
        station_name: 'Mamma Mi Radio',
        preview: {
          heard_on_air: 'Mamma Mi Radio',
          seen_by_listeners: 'Mamma Mi Radio',
          seen_in_home_assistant: 'Mamma Mi Radio',
        },
      },
      onboarding_required: onboardingRequired,
      onboarding_steps: [{ id: 'llm', title: 'Add AI Key (Optional)', status: 'todo', detail: 'Optional only.' }],
      essentials: [{ key: 'llm_keys', label: 'AI hosts', status: 'missing', configured_keys: [] }],
      preflight_checks: [],
      launch: { headline: 'Hear the station first.' },
      recommended_next_action: 'Hear it on one speaker.',
      addon_options_snippet: '',
      guided_setup: {
        strip: { items: [], attention_required: onboardingRequired },
        first_listen: {
          install_origin: fresh ? 'fresh' : 'existing',
          fresh_install: fresh,
          bootstrap_ready: bootstrapReady,
          audio_complete: audio,
          privacy_complete: privacy,
          setup_reviewed: privacy,
          accepted_attempt_id: acceptedAttemptId,
          selected_entity_id: selectedEntityId,
          heard_at: audio ? 101 : null,
          privacy_reviewed_at: privacy ? 102 : null,
          show_ai: !fresh || (audio && privacy),
          receipt_recovery: {
            available: Boolean(receiptRecoveryEntity),
            entity_id: receiptRecoveryEntity,
          },
        },
        source_readiness: {
          rows,
          healthy,
          recovery_cover_available: recovery === 'cover_only' || recovery === 'on_air',
          recovery_on_air: recovery === 'on_air',
        },
        speaker: { selected_entity_id: selectedEntityId },
        verification: { status: audio ? 'heard' : 'not_started', heard: audio, attempt_id: acceptedAttemptId },
        privacy: {
          status: privacy ? (privacyEnabled ? 'enabled' : 'off') : 'after_first_listen',
          enabled: privacyEnabled,
          reviewed: privacy,
        },
        ai_hosts: { status: 'missing' },
        home_context: { status: 'not_configured', action: 'none' },
      },
    };
  };
  let setupStatusProjection = setupProjection();

  const bodyOf = (route) => {
    const raw = route.request().postData();
    return raw ? JSON.parse(raw) : {};
  };
  const fulfillJson = (route, body) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });

  page.on('pageerror', (error) => pageErrors.push(error.message || String(error)));
  await page.addInitScript(() => {
    const nativeSetInterval = window.setInterval.bind(window);
    window.__firstListenSmokeIntervals = [];
    window.setInterval = (handler, delay, ...args) => {
      const id = nativeSetInterval(handler, delay, ...args);
      window.__firstListenSmokeIntervals.push({ id, delay });
      return id;
    };
  });
  await page.route('**/*', async (route) => {
    const requestOrigin = originOf(route.request().url());
    if (!requestOrigin || requestOrigin === baseOrigin) {
      await route.fallback();
      return;
    }
    blockedOffOriginRequests.push(route.request().url());
    await route.fulfill({ status: 204, contentType: 'text/plain', body: '' });
  });
  await page.route('**/api/setup/first-listen/players', async (route) => {
    playerRequests.push(bodyOf(route));
    const candidates = playerCandidatesAvailable ? [{
      entity_id: 'media_player.mac_lab_speaker',
      friendly_name: `<img src=x onerror=alert(1)>${'SpeakerWithoutBreak'.repeat(10)}`,
      area: 'Lab room',
      state: 'idle',
      device_class: 'speaker',
      supports_play_media: true,
      available: true,
    }] : [];
    await fulfillJson(route, {
      ok: true,
      media_source_ready: true,
      media_source_uri: 'media-source://mammamiradio/live',
      candidates,
      receipt_recovery: {
        available: Boolean(playerReceiptRecoveryEntity),
        entity_id: playerReceiptRecoveryEntity,
      },
    });
  });
  await page.route('**/api/setup/first-listen/play', async (route) => {
    const body = bodyOf(route);
    playRequests.push(body);
    if (discardNextPlayResponse) {
      discardNextPlayResponse = false;
      playerReceiptRecoveryEntity = body.entity_id;
      setupStatusProjection = discardedResponseProjection || setupProjection({ receiptRecoveryEntity: body.entity_id });
      discardedResponseProjection = null;
      await route.abort('failed');
      return;
    }
    if (failNextPlayReceipt) {
      failNextPlayReceipt = false;
      await fulfillJson(route, {
        ok: false,
        accepted: true,
        receipt_persisted: false,
        station_resumed: true,
        entity_id: 'media_player.mac_lab_speaker',
        error: { code: 'receipt_unavailable' },
      });
      return;
    }
    await fulfillJson(route, {
      ok: true,
      accepted: true,
      receipt_persisted: true,
      station_resumed: true,
      entity_id: 'media_player.mac_lab_speaker',
      attempt_id: `browser-attempt-${playRequests.length}`,
      media_source_uri: 'media-source://mammamiradio/live',
    });
  });
  await page.route('**/api/setup/first-listen/receipt/retry', async (route) => {
    const body = bodyOf(route);
    receiptRetryRequests.push(body);
    if (receiptRetryFailuresRemaining > 0) {
      receiptRetryFailuresRemaining -= 1;
      await fulfillJson(route, {
        ok: false,
        accepted: true,
        receipt_persisted: false,
        station_resumed: true,
        entity_id: body.entity_id,
        error: { code: 'receipt_unavailable' },
      });
      return;
    }
    await fulfillJson(route, {
      ok: true,
      accepted: true,
      receipt_persisted: true,
      station_resumed: true,
      entity_id: body.entity_id,
      attempt_id: `browser-recovered-attempt-${receiptRetryRequests.length}`,
    });
    playerReceiptRecoveryEntity = '';
    setupStatusProjection = setupProjection();
  });
  await page.route('**/api/setup/first-listen/verify', async (route) => {
    const body = bodyOf(route);
    verifyRequests.push(body);
    await fulfillJson(route, {
      ok: true,
      heard: body.heard === true,
      first_listen_achieved: body.heard === true,
      attempt_id: body.attempt_id,
    });
  });
  await page.route('**/api/setup/home-context-preview', async (route) => {
    previewRequests.push(bodyOf(route));
    await fulfillJson(route, ambientOnlyPreview ? {
      ok: true,
      status: 'ambient_only',
      context_value: 'ambient_only',
      useful_context: false,
      entities: [{
        entity_id: 'sun.ambient',
        label: 'Daylight',
        area: '',
        domain: 'sun',
        state_summary: 'above horizon',
        muted: false,
      }],
    } : {
      ok: true,
      status: 'ready',
      context_value: 'useful',
      useful_context: true,
      entities: [{
          entity_id: 'binary_sensor.lab_presence',
          label: '<script>not markup</script> Lab presence',
          area: 'Lab',
          domain: 'binary_sensor',
          state_summary: 'occupied',
          muted: false,
          personal_moment_eligible: true,
          personal_moment_enabled: false,
      }],
    });
  });
  await page.route('**/api/setup/home-context-choice', async (route) => {
    const body = bodyOf(route);
    privacyRequests.push(body);
    if (body.enabled === true && rejectNextEnable) {
      rejectNextEnable = false;
      await fulfillJson(route, { ok: false, error: { code: 'preview_required' } });
      return;
    }
    setupStatusProjection = setupProjection({
      audio: true,
      privacy: true,
      privacyEnabled: body.enabled === true,
      onboardingRequired: false,
    });
    await fulfillJson(route, { ok: true, enabled: body.enabled === true, persisted: true });
  });
  await page.route('**/api/setup/status', async (route) => {
    await fulfillJson(route, setupStatusProjection);
  });
  await page.route('**/api/capabilities', async (route) => {
    await fulfillJson(route, { capabilities: {}, golden_path: {} });
  });

  page.setDefaultTimeout(5000);
  page.setDefaultNavigationTimeout(10000);
  await page.goto(`${baseUrl}/admin`, { waitUntil: 'domcontentloaded', timeout: 10000 });
  await page.waitForFunction(
    () => typeof renderSetup === 'function' && typeof renderFirstListenProgress === 'function',
    null,
    { timeout: 5000 },
  );
  await page.waitForFunction(
    () => document.querySelector('#tab-setup.is-active') && document.getElementById('setupGroup')?.open === true,
    null,
    { timeout: 5000 },
  );
  await page.evaluate(() => {
    (window.__firstListenSmokeIntervals || []).forEach(({ id }) => clearInterval(id));
  });

  const resetUi = async (setup, overrides = {}) => {
    await page.evaluate(({ projection, ui }) => {
      Object.assign(_firstListenUi, {
        projection: null,
        players: [],
        selectedEntityId: '',
        selectedName: '',
        selectionDirty: false,
        attemptId: '',
        discovery: 'untouched',
        dispatch: 'ready',
        verification: 'awaiting',
        privacyPreview: 'untouched',
        privacyPreviewValid: false,
        privacyPreviewUseful: false,
        privacySaving: false,
        receiptSaving: false,
        privacyChoice: null,
        repairOpen: false,
        busy: false,
        ...ui,
      });
      showAdminTab('setup', { render: false, persist: false });
      _lastSetupJson = null;
      renderSetup(projection);
      const details = document.getElementById('setupGroup');
      if (details) details.open = true;
    }, { projection: setup, ui: overrides });
  };

  const assertCurrentStep = async (expectedId) => {
    const state = await page.evaluate(() => [...document.querySelectorAll('.first-listen-step')].map((step) => {
      const body = step.querySelector(':scope > .first-listen-body');
      return {
        id: step.id,
        state: step.dataset.state,
        hidden: body ? body.hidden : true,
        ariaHidden: body ? body.getAttribute('aria-hidden') : null,
        inert: body ? body.inert : true,
      };
    }));
    const current = state.filter((row) => row.state === 'current');
    assert(current.length === 1 && current[0].id === expectedId, `wrong current step: ${JSON.stringify(state)}`);
    assert(!current[0].hidden && current[0].ariaHidden === 'false' && !current[0].inert, 'current body is unavailable');
    assert(
      state.filter((row) => row.id !== expectedId).every((row) => row.hidden && row.ariaHidden === 'true' && row.inert),
      `non-current body stayed interactive: ${JSON.stringify(state)}`,
    );
  };

  assert(await page.locator('#tab-setup').getAttribute('aria-selected') === 'true', 'fresh install did not land on First Listen');
  assert(await page.locator('#first-listen-panel').isVisible(), 'First Listen panel is not the visible first surface');
  assert(await page.locator('#setupGroup').evaluate((element) => element.open === true), 'First Listen path stayed collapsed');
  assert(
    await page.locator('#setupGroup').evaluate((element) => element.parentElement?.id) === 'firstListenPanelMount',
    'First Listen path remained buried in Motore',
  );
  assert(await page.locator('#firstListenFindPlayersBtn').isEnabled(), 'no-key fresh install cannot start speaker discovery');
  assert(
    await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true),
    'optional AI controls appeared before first audio/privacy',
  );
  await assertCurrentStep('firstListenSpeakerStep');

  await page.locator('#tab-scaletta').click();
  await page.evaluate((projection) => {
    _lastSetupJson = null;
    renderSetup(projection);
  }, setupProjection({ primary: 'on_air' }));
  assert(await page.locator('#tab-scaletta').getAttribute('aria-selected') === 'true', 'later setup polling hijacked the operator tab');
  await page.locator('#tab-setup').click();

  await resetUi(setupProjection());
  await assertCurrentStep('firstListenSpeakerStep');
  assert(
    await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true),
    'AI controls unlocked before first audio/privacy',
  );
  await page.locator('.first-listen-uri > summary').click();
  const exactMediaSource = page.locator('.first-listen-uri code');
  assert(await exactMediaSource.isVisible(), 'exact media source stayed hidden after disclosure');
  assert(
    (await exactMediaSource.innerText()).trim() === 'media-source://mammamiradio/live',
    'exact media source URI drifted',
  );

  await page.locator('#firstListenFindPlayersBtn').focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelectorAll('#firstListenPlayerSelect option').length === 2);
  assert(playerRequests.length === 1, `speaker discovery count drifted: ${playerRequests.length}`);
  await page.locator('#firstListenPlayerSelect').selectOption('media_player.mac_lab_speaker');
  assert(await page.locator('#firstListenPath img').count() === 0, 'hostile speaker label became markup');
  await page.locator('#firstListenPlayBtn').click();
  await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
  assert(playRequests.length === 1, `first start count drifted: ${playRequests.length}`);
  assert(
    playRequests[0].entity_id === 'media_player.mac_lab_speaker',
    `wrong HA target: ${JSON.stringify(playRequests[0])}`,
  );
  await assertCurrentStep('firstListenVerifyStep');
  assert(
    await page.evaluate(() => document.activeElement?.id) === 'firstListenVerifyHeading',
    'accepted playback did not focus the listening check',
  );

  await page.locator('#firstListenNotYetBtn').focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => _firstListenUi.verification === 'not_yet' && !_firstListenUi.busy);
  assert(verifyRequests.length === 1 && verifyRequests[0].heard === false, 'Not yet was not recorded explicitly');
  assert(await page.locator('#firstListenRepair').isVisible(), 'Not yet hid warm repair guidance');
  assert(await page.locator('#firstListenRetryBtn').isEnabled(), 'same-speaker retry is unavailable');
  await page.locator('#firstListenRetryBtn').click();
  await page.waitForFunction(() => _firstListenUi.verification === 'awaiting' && !_firstListenUi.busy);
  assert(playRequests.length === 2, `same-speaker retry count drifted: ${playRequests.length}`);
  assert(playRequests[1].entity_id === playRequests[0].entity_id, 'retry changed the selected speaker');
  assert(!(await page.locator('#firstListenRepair').isVisible()), 'successful retry left repair guidance open');
  await page.locator('#firstListenHeardBtn').click();
  await page.waitForFunction(() => _firstListenUi.verification === 'heard' && !_firstListenUi.busy);
  assert(verifyRequests.length === 2 && verifyRequests[1].heard === true, 'Heard was not recorded explicitly');
  await assertCurrentStep('firstListenPrivacyStep');
  assert(
    await page.evaluate(() => document.activeElement?.id) === 'firstListenPrivacyHeading',
    'audible confirmation did not focus privacy review',
  );

  assert(await page.locator('#firstListenKeepOffBtn').isVisible(), 'fresh-install privacy controls stayed hidden after heard proof');
  assert(await page.locator('#firstListenPrivacyLock').isHidden(), 'heard proof left the stale privacy lock visible');
  await page.locator('#firstListenKeepOffBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
  assert(previewRequests.length === 0, 'Keep off fetched Home context without operator preview');
  assert(privacyRequests.length === 1 && privacyRequests[0].enabled === false, 'Keep off did not persist explicit false');
  await assertCurrentStep('firstListenAiStep');
  assert(
    await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === false),
    'optional AI step stayed locked after privacy review',
  );

  await resetUi(
    setupProjection({ audio: true }),
    {
      selectedEntityId: 'media_player.mac_lab_speaker',
      selectedName: 'Mac Lab Speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    },
  );
  await assertCurrentStep('firstListenPrivacyStep');
  await page.locator('#firstListenPreviewBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
  assert(previewRequests.length === 1, 'explicit preview did not call the preview endpoint');
  assert(await page.locator('#haContextPreview script').count() === 0, 'hostile preview label became markup');
  assert(
    (await page.locator('#haContextPreview').innerText()).includes('<script>not markup</script>'),
    'hostile preview label was not rendered as harmless text',
  );
  await page.locator('#firstListenEnableContextBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === false && !_firstListenUi.privacySaving);
  assert(
    await page.evaluate(() => document.activeElement?.id) === 'firstListenPreviewBtn',
    'expired preview proof did not return focus to fresh preview',
  );
  assert(await page.locator('#firstListenEnableContextBtn').isDisabled(), 'expired proof left Enable actionable');
  await page.locator('#firstListenPreviewBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
  await page.locator('#firstListenEnableContextBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyChoice === true && !_firstListenUi.privacySaving);
  assert(
    privacyRequests.filter((entry) => entry.enabled === true).length === 2,
    `preview expiry retry count drifted: ${JSON.stringify(privacyRequests)}`,
  );
  await assertCurrentStep('firstListenAiStep');

  ambientOnlyPreview = true;
  await resetUi(
    setupProjection({ audio: true }),
    {
      selectedEntityId: 'media_player.mac_lab_speaker',
      selectedName: 'Mac Lab Speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    },
  );
  await page.locator('#firstListenPreviewBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyPreview === 'ambient_only');
  assert(
    (await page.locator('#haContextPreview').innerText()).includes('Nothing worth putting on air yet.'),
    'daylight-only preview was sold as meaningful Home context',
  );
  assert(await page.locator('.ha-preview-details').count() === 1, 'ambient detail was not retained for transparency');
  assert(!(await page.locator('.ha-preview-details').getAttribute('open')), 'ambient detail opened like the product payoff');
  assert(
    (await page.locator('#firstListenEnableContextBtn').innerText()).includes('daylight anyway'),
    'ambient-only enable choice was not labeled honestly',
  );
  assert(await page.locator('#firstListenKeepOffBtn').isEnabled(), 'recommended private path is unavailable');
  ambientOnlyPreview = false;

  await resetUi(setupProjection({ audio: true, privacy: true, primary: 'unavailable', recovery: 'cover_only' }));
  const recoveryReadyCopy = await page.locator('#firstListenVerifySummary').innerText();
  assert(recoveryReadyCopy.includes('Recovery cover is available'), 'available recovery was not described as standby');
  assert(!recoveryReadyCopy.includes('Recovery audio follows the opening'), 'standby recovery was falsely described as on air');
  await resetUi(setupProjection({ audio: true, privacy: true, primary: 'unavailable', recovery: 'on_air' }));
  assert(
    (await page.locator('#firstListenVerifySummary').innerText()).includes('Recovery audio follows the opening'),
    'on-air recovery was not described truthfully',
  );

  const completed = setupProjection({ audio: true, privacy: true, onboardingRequired: false });
  await resetUi(completed);
  assert(await page.locator('#engineAlertDot').evaluate((element) => getComputedStyle(element).display) === 'none', 'optional AI kept setup alert active');
  assert(await page.locator('#firstListenTabAlert').evaluate((element) => getComputedStyle(element).display) === 'none', 'optional AI kept tab alert active');

  const assertNoAutomaticLanding = async (projection, label) => {
    await page.evaluate((nextProjection) => {
      _firstListenLandingResolved = false;
      _adminTabUserInteracted = false;
      document.body.dataset.firstListenEntry = 'pending';
      showAdminTab('scaletta', { render: false, persist: false });
      _lastSetupJson = null;
      renderSetup(nextProjection);
    }, projection);
    assert(
      await page.locator('#tab-scaletta').getAttribute('aria-selected') === 'true',
      `${label} was incorrectly sent to First Listen`,
    );
  };
  await assertNoAutomaticLanding(completed, 'completed fresh install');
  await assertNoAutomaticLanding(
    setupProjection({ fresh: false, onboardingRequired: true }),
    'existing install',
  );

  const existingRequestCounts = {
    players: playerRequests.length,
    plays: playRequests.length,
    verifies: verifyRequests.length,
    previews: previewRequests.length,
    privacy: privacyRequests.length,
  };
  await resetUi(setupProjection({ fresh: false, onboardingRequired: true, sources: false }));
  await assertCurrentStep('firstListenPrivacyStep');
  assert(
    await page.locator('#firstListenSpeakerStep').getAttribute('data-state') === 'complete'
      && await page.locator('#firstListenVerifyStep').getAttribute('data-state') === 'complete',
    'existing install was forced through fresh-install speaker proof',
  );
  assert(
    await page.locator('#firstListenSourceStep').getAttribute('data-state') === 'available',
    'unknown source readiness competed with existing-install privacy review',
  );
  assert(
    await page.locator('#firstListenAiStep').getAttribute('data-state') === 'locked'
      && await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true),
    'optional AI unlocked before existing-install privacy review',
  );
  assert(await page.locator('#firstListenKeepOffBtn').isEnabled(), 'existing install cannot review privacy');
  assert(await page.locator('#firstListenKeepOffBtn').isVisible(), 'existing-install privacy controls stayed hidden');
  await page.locator('#firstListenKeepOffBtn').click();
  await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
  assert(playerRequests.length === existingRequestCounts.players, 'existing privacy review discovered speakers');
  assert(playRequests.length === existingRequestCounts.plays, 'existing privacy review started playback');
  assert(verifyRequests.length === existingRequestCounts.verifies, 'existing privacy review created audio proof');
  assert(previewRequests.length === existingRequestCounts.previews, 'Keep private fetched Home context on existing install');
  assert(privacyRequests.length === existingRequestCounts.privacy + 1, 'existing privacy choice was not saved');

  const receiptPlayBaseline = playRequests.length;
  const receiptRetryBaseline = receiptRetryRequests.length;
  failNextPlayReceipt = true;
  receiptRetryFailuresRemaining = 1;
  await resetUi(setupProjection(), {
    players: [{
      entity_id: 'media_player.mac_lab_speaker',
      friendly_name: 'Mac Lab Speaker',
      area: 'Lab room',
      state: 'idle',
      device_class: 'speaker',
      supports_play_media: true,
      available: true,
    }],
    selectedEntityId: 'media_player.mac_lab_speaker',
    selectedName: 'Mac Lab Speaker',
  });
  await page.locator('#firstListenPlayBtn').click();
  await page.waitForFunction(() => _firstListenUi.dispatch === 'receipt_failed' && !_firstListenUi.busy);
  assert(playRequests.length === receiptPlayBaseline + 1, 'receipt failure did not make exactly one explicit play request');
  await assertCurrentStep('firstListenVerifyStep');
  assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'unsaved accepted attempt hid its recovery panel');
  const receiptRepairCopy = await page.locator('#firstListenReceiptRepair').innerText();
  assert(receiptRepairCopy.includes('Home Assistant accepted the show.'), 'receipt recovery lost accepted-play truth');
  assert(receiptRepairCopy.includes('Only this listening check still needs saving.'), 'receipt recovery lost persistence-only guidance');
  assert(receiptRepairCopy.includes('does not send another playback request'), 'receipt recovery did not rule out replay');
  assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'verification unlocked before the accepted attempt was saved');
  assert(await page.locator('#firstListenNotYetBtn').isDisabled(), 'Not yet unlocked before the accepted attempt was saved');
  await page.locator('#firstListenSaveAttemptBtn').click();
  await page.waitForFunction(() => (
    _firstListenUi.dispatch === 'receipt_failed'
      && !_firstListenUi.receiptSaving
      && document.getElementById('firstListenVerifyStatus')?.textContent.includes('attempt was not saved')
  ));
  assert(receiptRetryRequests.length === receiptRetryBaseline + 1, 'first persistence-only retry was not sent');
  assert(playRequests.length === receiptPlayBaseline + 1, 'failed receipt save replayed the station');
  assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'failed receipt save removed its recovery path');
  assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'failed receipt save unlocked verification');
  await page.locator('#firstListenSaveAttemptBtn').click();
  await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.receiptSaving);
  assert(receiptRetryRequests.length === receiptRetryBaseline + 2, 'successful persistence-only retry was not sent');
  assert(playRequests.length === receiptPlayBaseline + 1, 'receipt recovery sent a second playback request');
  assert(
    receiptRetryRequests.slice(receiptRetryBaseline).every((entry) => entry.entity_id === 'media_player.mac_lab_speaker'),
    `receipt recovery changed speaker: ${JSON.stringify(receiptRetryRequests.slice(receiptRetryBaseline))}`,
  );
  assert(!(await page.locator('#firstListenReceiptRepair').isVisible()), 'saved attempt left receipt recovery visible');
  assert(await page.locator('#firstListenHeardBtn').isEnabled(), 'saved accepted attempt did not unlock verification');

  const lostResponsePlayBaseline = playRequests.length;
  const lostResponseRetryBaseline = receiptRetryRequests.length;
  const lostResponseDiscoveryBaseline = playerRequests.length;
  discardNextPlayResponse = true;
  discardedResponseProjection = setupProjection({
    durableAttemptId: 'durable-attempt-a',
    durableEntityId: 'media_player.old_room',
    receiptRecoveryEntity: 'media_player.mac_lab_speaker',
  });
  playerCandidatesAvailable = false;
  await resetUi(setupProjection({
    durableAttemptId: 'durable-attempt-a',
    durableEntityId: 'media_player.old_room',
  }), {
    players: [{
      entity_id: 'media_player.mac_lab_speaker',
      friendly_name: 'Mac Lab Speaker',
      area: 'Lab room',
      state: 'idle',
      device_class: 'speaker',
      supports_play_media: true,
      available: true,
    }],
    selectedEntityId: 'media_player.mac_lab_speaker',
    selectedName: 'Mac Lab Speaker',
    selectionDirty: true,
    verification: 'not_yet',
  });
  await page.locator('#firstListenPlayBtn').click();
  await page.waitForFunction(() => _firstListenUi.dispatch === 'rejected' && !_firstListenUi.busy);
  assert(playRequests.length === lostResponsePlayBaseline + 1, 'discarded response did not originate from exactly one play');

  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => (
    typeof renderSetup === 'function'
      && _firstListenUi.dispatch === 'receipt_failed'
      && _firstListenUi.selectedEntityId === 'media_player.mac_lab_speaker'
      && _firstListenUi.attemptId === ''
  ));
  await page.evaluate(() => {
    (window.__firstListenSmokeIntervals || []).forEach(({ id }) => clearInterval(id));
  });
  assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'page reload lost server-owned receipt recovery');
  assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'pending attempt B unlocked stale durable attempt A');
  assert(await page.locator('#firstListenNotYetBtn').isDisabled(), 'pending attempt B exposed stale Not yet proof for attempt A');
  assert(playRequests.length === lostResponsePlayBaseline + 1, 'page reload replayed the discarded-response attempt');

  await page.evaluate(() => findFirstListenPlayers(document.getElementById('firstListenFindPlayersBtn')));
  await page.waitForFunction(() => _firstListenUi.discovery === 'empty' && !_firstListenUi.busy);
  assert(playerRequests.length === lostResponseDiscoveryBaseline + 1, 'recovery discovery request count drifted');
  assert(
    await page.evaluate(() => _firstListenUi.selectedEntityId) === 'media_player.mac_lab_speaker',
    'unavailable discovery cleared the server-matched pending speaker',
  );
  assert(await page.locator('#firstListenSaveAttemptBtn').isEnabled(), 'pending receipt required the speaker to stay available');
  assert(playRequests.length === lostResponsePlayBaseline + 1, 'recovery discovery replayed the station');
  await page.locator('#firstListenSaveAttemptBtn').click();
  await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.receiptSaving);
  assert(receiptRetryRequests.length === lostResponseRetryBaseline + 1, 'page-reload recovery did not save the pending receipt');
  assert(playRequests.length === lostResponsePlayBaseline + 1, 'discarded-response recovery sent a second playback request');
  assert(await page.locator('#firstListenHeardBtn').isEnabled(), 'page-reload recovery did not unlock verification');
  playerCandidatesAvailable = true;

  const longSpeakerName = `<img src=x onerror=alert(1)>${'SpeakerWithoutBreak'.repeat(20)}`;
  await resetUi(setupProjection(), {
    players: [{
      entity_id: 'media_player.mac_lab_speaker',
      friendly_name: longSpeakerName,
      area: 'Lab room',
      state: 'idle',
      device_class: 'speaker',
      supports_play_media: true,
      available: true,
    }],
    selectedEntityId: 'media_player.mac_lab_speaker',
    selectedName: longSpeakerName,
  });
  await page.setViewportSize({ width: 320, height: 900 });
  const mobileGeometry = await page.evaluate(() => {
    document.documentElement.style.fontSize = '200%';
    const details = document.getElementById('setupGroup');
    if (details) details.open = true;
    document.getElementById('firstListenSpeakerSummary').textContent = 'SpeakerWithoutBreak'.repeat(20);
    document.getElementById('firstListenVerifySummary').textContent = 'VerificationWithoutBreak'.repeat(20);
    const ids = ['firstListenPath', 'firstListenSpeakerSummary', 'firstListenVerifySummary'];
    const wrapping = ids.map((id) => {
      const element = document.getElementById(id);
      return {
        id,
        clientWidth: element?.clientWidth || 0,
        scrollWidth: element?.scrollWidth || 0,
        wrap: element ? getComputedStyle(element).overflowWrap : '',
      };
    });
    const select = document.getElementById('firstListenPlayerSelect');
    const selectRect = select?.getBoundingClientRect();
    const selectParentRect = select?.parentElement?.getBoundingClientRect();
    return {
      wrapping,
      select: {
        width: selectRect?.width || 0,
        left: selectRect?.left || 0,
        right: selectRect?.right || 0,
        parentWidth: selectParentRect?.width || 0,
        viewportWidth: document.documentElement.clientWidth,
        maxWidth: select ? getComputedStyle(select).maxWidth : '',
      },
    };
  });
  assert(
    mobileGeometry.wrapping.every((row) => row.clientWidth > 0 && row.scrollWidth <= row.clientWidth + 1),
    `320px/200% first-listen geometry overflowed: ${JSON.stringify(mobileGeometry)}`,
  );
  assert(
    mobileGeometry.select.width > 0
      && mobileGeometry.select.width <= mobileGeometry.select.parentWidth + 1
      && mobileGeometry.select.left >= -1
      && mobileGeometry.select.right <= mobileGeometry.select.viewportWidth + 1
      && mobileGeometry.select.maxWidth === '100%',
    `320px/200% speaker select escaped its container: ${JSON.stringify(mobileGeometry.select)}`,
  );
  assert(pageErrors.length === 0, `uncaught page errors: ${pageErrors.join(' | ')}`);

  return {
    ok: true,
    checks: 31,
    plays: playRequests.length,
    receipt_retries: receiptRetryRequests.length,
    verifies: verifyRequests.map((entry) => entry.heard),
    previews: previewRequests.length,
    privacy_choices: privacyRequests.map((entry) => entry.enabled),
    blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
  };
}
