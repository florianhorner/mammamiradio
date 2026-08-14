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
  const guideAudioRequests = [];
  const ingressPrefix = '/api/hassio_ingress/first-listen-smoke';
  const RECEIPT_FAILURE_STATUS = 503;
  const DISCOVERY_UNAVAILABLE_STATUS = 503;
  const PREVIEW_REQUIRED_STATUS = 409;
  let rejectNextEnable = true;
  let failNextPrivacyReceipt = false;
  let nextPrivacyChoiceFailure = '';
  let ambientOnlyPreview = false;
  let failNextGuideKey = '';
  let failNextPlayReceipt = false;
  let nextPlayResponse = '';
  let receiptRetryFailuresRemaining = 0;
  let nextReceiptRetryResponse = '';
  let discardNextPlayResponse = false;
  let discardedResponseProjection = null;
  let nextVerifyResponse = '';
  let playerCandidatesAvailable = true;
  let failNextPlayerDiscovery = false;
  let abortNextPlayerDiscovery = false;
  let nextPlayerResponse = '';
  let nextPreviewResponse = '';
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
    privacyChoiceExplicit = false,
    primary = 'playable',
    recovery = 'cover_only',
    onboardingRequired = true,
    fresh = true,
    sources = true,
    llmKeys = [],
    receiptRecoveryEntity = '',
    durableAttemptId = '',
    durableEntityId = '',
  } = {}) => {
    const rows = sources ? sourceRows({ primary, recovery }) : [];
    const healthy = ['playable', 'on_air'].includes(primary);
    const resolvedInstallOrigin = fresh ? 'fresh' : 'existing';
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
      essentials: [
        { key: 'llm_keys', label: 'AI hosts', status: llmKeys.length ? 'configured' : 'missing', configured_keys: llmKeys },
        { key: 'tts_keys', label: 'Voice providers', status: 'missing', configured_keys: [] },
      ],
      preflight_checks: [],
      launch: { headline: 'Hear the station first.' },
      recommended_next_action: 'Hear it on one speaker.',
      addon_options_snippet: '',
      guided_setup: {
        strip: { items: [], attention_required: onboardingRequired },
        first_listen: {
          install_origin: resolvedInstallOrigin,
          fresh_install: fresh,
          bootstrap_ready: true,
          audio_complete: audio,
          privacy_complete: privacy,
          setup_reviewed: privacy,
          accepted_attempt_id: acceptedAttemptId,
          selected_entity_id: selectedEntityId,
          heard_at: audio ? 101 : null,
          privacy_reviewed_at: privacy ? 102 : null,
          show_ai: resolvedInstallOrigin === 'existing' || (audio && privacy),
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
          choice_explicit: privacyChoiceExplicit,
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
  const fulfillJson = (route, body, status = 200) => route.fulfill({
    status,
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
  await page.route('**/static/audio/first_listen/*.mp3*', async (route) => {
    const requestAddress = route.request().url();
    const requestPath = requestAddress.slice(originOf(requestAddress).length);
    const filename = requestPath.split('?', 1)[0].split('/').at(-1) || '';
    const guideKey = filename.replace(/\.mp3$/i, '');
    guideAudioRequests.push(requestPath);
    if (failNextGuideKey === guideKey) {
      failNextGuideKey = '';
      await route.abort('failed');
      return;
    }
    if (requestPath.startsWith(`${ingressPrefix}/static/audio/first_listen/`)) {
      const directPath = requestPath.slice(ingressPrefix.length);
      const response = await route.fetch({ url: `${baseUrl}${directPath}` });
      await route.fulfill({ response });
      return;
    }
    await route.fallback();
  });
  await page.route('**/api/setup/first-listen/players', async (route) => {
    playerRequests.push(bodyOf(route));
    if (abortNextPlayerDiscovery) {
      abortNextPlayerDiscovery = false;
      await route.abort('failed');
      return;
    }
    if (failNextPlayerDiscovery) {
      failNextPlayerDiscovery = false;
      await fulfillJson(route, { ok: false, error: { code: 'ha_unreachable' } }, DISCOVERY_UNAVAILABLE_STATUS);
      return;
    }
    const candidates = playerCandidatesAvailable ? [
      {
        entity_id: 'media_player.mac_lab_speaker',
        friendly_name: `<img src=x onerror=alert(1)>${'SpeakerWithoutBreak'.repeat(10)}`,
        area: 'Lab room',
        state: 'idle',
        device_class: 'speaker',
        supports_play_media: true,
        available: true,
      },
      {
        entity_id: 'media_player.kitchen_speaker',
        friendly_name: 'Kitchen speaker',
        area: 'Kitchen',
        state: 'idle',
        device_class: 'speaker',
        supports_play_media: true,
        available: true,
      },
    ] : [];
    if (nextPlayerResponse) {
      const responseMode = nextPlayerResponse;
      nextPlayerResponse = '';
      await fulfillJson(route, {
        ok: true,
        media_source_ready: responseMode !== 'media_source_not_ready',
        media_source_uri: responseMode === 'media_source_wrong_uri'
          ? 'media-source://another-provider/live'
          : 'media-source://mammamiradio/live',
        candidates,
        receipt_recovery: { available: false, entity_id: '' },
      });
      return;
    }
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
    if (nextPlayResponse) {
      const responseMode = nextPlayResponse;
      nextPlayResponse = '';
      const partial = responseMode === 'partial_entity_mismatch';
      await fulfillJson(route, partial ? {
        ok: false,
        accepted: true,
        receipt_persisted: false,
        station_resumed: true,
        entity_id: 'media_player.response_from_another_room',
        error: { code: 'receipt_unavailable' },
      } : {
        ok: true,
        accepted: true,
        receipt_persisted: true,
        station_resumed: true,
        entity_id: 'media_player.response_from_another_room',
        attempt_id: `browser-mismatched-attempt-${playRequests.length}`,
        media_source_uri: 'media-source://mammamiradio/live',
      }, partial ? RECEIPT_FAILURE_STATUS : 200);
      return;
    }
    if (failNextPlayReceipt) {
      failNextPlayReceipt = false;
      await fulfillJson(route, {
        ok: false,
        accepted: true,
        receipt_persisted: false,
        station_resumed: true,
        entity_id: body.entity_id,
        error: { code: 'receipt_unavailable' },
      }, RECEIPT_FAILURE_STATUS);
      return;
    }
    await fulfillJson(route, {
      ok: true,
      accepted: true,
      receipt_persisted: true,
      station_resumed: true,
      entity_id: body.entity_id,
      attempt_id: `browser-attempt-${playRequests.length}`,
      media_source_uri: 'media-source://mammamiradio/live',
    });
  });
  await page.route('**/api/setup/first-listen/receipt/retry', async (route) => {
    const body = bodyOf(route);
    receiptRetryRequests.push(body);
    if (nextReceiptRetryResponse) {
      const responseMode = nextReceiptRetryResponse;
      nextReceiptRetryResponse = '';
      await fulfillJson(route, {
        ok: true,
        ...(responseMode === 'missing_accepted' ? {} : { accepted: true }),
        receipt_persisted: true,
        station_resumed: true,
        entity_id: responseMode === 'entity_mismatch'
          ? 'media_player.response_from_another_room'
          : body.entity_id,
        attempt_id: `browser-malformed-recovered-attempt-${receiptRetryRequests.length}`,
      });
      return;
    }
    if (receiptRetryFailuresRemaining > 0) {
      receiptRetryFailuresRemaining -= 1;
      await fulfillJson(route, {
        ok: false,
        accepted: true,
        receipt_persisted: false,
        station_resumed: true,
        entity_id: body.entity_id,
        error: { code: 'receipt_unavailable' },
      }, RECEIPT_FAILURE_STATUS);
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
    const responseMode = nextVerifyResponse;
    nextVerifyResponse = '';
    const heard = responseMode === 'heard_mismatch' ? body.heard !== true : body.heard === true;
    const attemptId = responseMode === 'attempt_mismatch' ? `${body.attempt_id}-mismatch` : body.attempt_id;
    const achievementFields = responseMode === 'achievement_missing'
      ? {}
      : { first_listen_achieved: responseMode === 'achievement_false' ? false : body.heard === true };
    await fulfillJson(route, {
      ok: true,
      heard,
      ...achievementFields,
      attempt_id: attemptId,
    });
  });
  await page.route('**/api/setup/home-context-preview', async (route) => {
    previewRequests.push(bodyOf(route));
    const responseMode = nextPreviewResponse;
    nextPreviewResponse = '';
    const preview = ambientOnlyPreview ? {
      ok: true,
      fresh: true,
      status: 'ambient_only',
      context_value: 'ambient_only',
      useful_context: false,
      sent_now: [],
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
      fresh: true,
      status: 'ready',
      context_value: 'useful',
      useful_context: true,
      sent_now: [],
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
    };
    if (responseMode === 'stale') preview.fresh = false;
    if (responseMode === 'sent_now_nonempty') {
      preview.sent_now = [{
        entity_id: 'binary_sensor.lab_presence',
        label: 'Lab presence',
        sent_to_prompt: true,
      }];
    }
    if (responseMode === 'unknown_context_value') preview.context_value = 'surprising';
    await fulfillJson(route, preview);
  });
  await page.route('**/api/setup/home-context-choice', async (route) => {
    const body = bodyOf(route);
    privacyRequests.push(body);
    if (nextPrivacyChoiceFailure) {
      const responseMode = nextPrivacyChoiceFailure;
      nextPrivacyChoiceFailure = '';
      const enabled = body.enabled === true;
      const success = {
        ok: true,
        enabled,
        persisted: true,
        privacy_reviewed: true,
      };
      const receipt = {
        ok: false,
        enabled,
        persisted: true,
        error: { code: 'privacy_receipt_unavailable' },
      };
      if (responseMode === 'forbidden') {
        await fulfillJson(route, { detail: 'CSRF token is no longer valid' }, 403);
      } else if (responseMode === 'missing_ok') {
        const { ok, ...missingOk } = success;
        await fulfillJson(route, missingOk);
      } else if (responseMode === 'success_missing_persisted') {
        const { persisted, ...missingPersisted } = success;
        await fulfillJson(route, missingPersisted);
      } else if (responseMode === 'success_persisted_false') {
        await fulfillJson(route, { ...success, persisted: false });
      } else if (responseMode === 'success_missing_privacy_reviewed') {
        const { privacy_reviewed, ...missingPrivacyReviewed } = success;
        await fulfillJson(route, missingPrivacyReviewed);
      } else if (responseMode === 'success_privacy_reviewed_false') {
        await fulfillJson(route, { ...success, privacy_reviewed: false });
      } else if (responseMode === 'success_missing_enabled') {
        const { enabled: discardedEnabled, ...missingEnabled } = success;
        await fulfillJson(route, missingEnabled);
      } else if (responseMode === 'success_enabled_mismatch') {
        await fulfillJson(route, { ...success, enabled: !enabled });
      } else if (responseMode === 'receipt_missing_persisted') {
        const { persisted, ...missingPersisted } = receipt;
        await fulfillJson(route, missingPersisted, RECEIPT_FAILURE_STATUS);
      } else if (responseMode === 'receipt_persisted_false') {
        await fulfillJson(route, { ...receipt, persisted: false }, RECEIPT_FAILURE_STATUS);
      } else if (responseMode === 'receipt_missing_enabled') {
        const { enabled: discardedEnabled, ...missingEnabled } = receipt;
        await fulfillJson(route, missingEnabled, RECEIPT_FAILURE_STATUS);
      } else if (responseMode === 'receipt_enabled_mismatch') {
        await fulfillJson(route, { ...receipt, enabled: !enabled }, RECEIPT_FAILURE_STATUS);
      } else if (responseMode === 'receipt_wrong_code') {
        await fulfillJson(route, { ...receipt, error: { code: 'persistence_failed' } }, RECEIPT_FAILURE_STATUS);
      } else if (responseMode === 'receipt_http_200') {
        await fulfillJson(route, receipt);
      } else if (responseMode === 'receipt_ok_true') {
        await fulfillJson(route, { ...receipt, ok: true }, RECEIPT_FAILURE_STATUS);
      } else if (responseMode === 'preview_required_active_claim') {
        await fulfillJson(route, {
          ok: false,
          enabled: true,
          persisted: true,
          error: { code: 'preview_required' },
        }, PREVIEW_REQUIRED_STATUS);
      } else if (responseMode === 'preview_required_unpersisted_off') {
        await fulfillJson(route, {
          ok: false,
          enabled: false,
          persisted: false,
          error: { code: 'preview_required' },
        }, PREVIEW_REQUIRED_STATUS);
      } else if (responseMode === 'preview_required_active_off') {
        await fulfillJson(route, {
          ok: false,
          enabled: false,
          persisted: true,
          error: { code: 'preview_required' },
        }, PREVIEW_REQUIRED_STATUS);
      } else {
        await fulfillJson(route, { ok: false, error: { code: 'invalid_request' } }, 400);
      }
      return;
    }
    if (failNextPrivacyReceipt) {
      failNextPrivacyReceipt = false;
      await fulfillJson(route, {
        ok: false,
        enabled: body.enabled === true,
        persisted: true,
        error: { code: 'privacy_receipt_unavailable' },
      }, RECEIPT_FAILURE_STATUS);
      return;
    }
    if (body.enabled === true && rejectNextEnable) {
      rejectNextEnable = false;
      await fulfillJson(route, { ok: false, error: { code: 'preview_required' } }, PREVIEW_REQUIRED_STATUS);
      return;
    }
    setupStatusProjection = setupProjection({
      audio: true,
      privacy: true,
      privacyEnabled: body.enabled === true,
      onboardingRequired: false,
    });
    await fulfillJson(route, {
      ok: true,
      enabled: body.enabled === true,
      persisted: true,
      privacy_reviewed: true,
    });
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

  async function runCalmJourneySmoke() {
    const resetUi = async (setup, overrides = {}) => {
      await page.evaluate(({ projection, ui }) => {
        stopFirstListenGuide();
        Object.assign(_firstListenUi, {
          projection: null,
          players: [],
          selectedEntityId: '',
          selectedName: '',
          selectionDirty: false,
          retestPending: false,
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
          privacyReceiptChoice: null,
          repairOpen: false,
          busy: false,
          reviewStep: '',
          optionalStep: '',
          reviewTrigger: null,
          showSuccess: false,
          ...ui,
        });
        showAdminTab('setup', { render: false, persist: false });
        _lastSetupJson = null;
        renderSetup(projection);
        const group = document.getElementById('setupGroup');
        if (group) group.open = true;
        const technical = document.getElementById('setupAdvancedDetails');
        if (technical) technical.open = false;
        document.documentElement.style.fontSize = '';
      }, { projection: setup, ui: overrides });
    };

    const journeyState = () => page.evaluate(() => {
      const visible = (element) => Boolean(element && !element.hidden && element.getClientRects().length);
      const rows = [...document.querySelectorAll('.first-listen-step')].map((step) => {
        const body = step.querySelector(':scope > .first-listen-body');
        return {
          id: step.id,
          current: step.getAttribute('aria-current'),
          bodyVisible: visible(body),
          bodyAriaHidden: body?.getAttribute('aria-hidden') || null,
          bodyInert: body?.inert ?? true,
        };
      });
      const primary = [...document.querySelectorAll('#journeySurface .btn-trigger')]
        .filter((button) => visible(button) && !button.disabled)
        .map((button) => button.id || button.textContent.trim());
      return {
        rows,
        current: rows.filter((row) => row.current === 'step'),
        bodies: rows.filter((row) => row.bodyVisible),
        primary,
      };
    });

    const assertUnfinished = async (expectedId, expectedPrimary) => {
      const state = await journeyState();
      assert(
        state.current.length === 1 && state.current[0].id === expectedId,
        `unfinished journey has the wrong current step: ${JSON.stringify(state)}`,
      );
      assert(
        state.bodies.length === 1 && state.bodies[0].id === expectedId
          && state.bodies[0].bodyAriaHidden === 'false' && !state.bodies[0].bodyInert,
        `unfinished journey does not have exactly one available body: ${JSON.stringify(state)}`,
      );
      assert(
        state.rows.filter((row) => row.id !== expectedId).every((row) => (
          !row.bodyVisible && row.bodyAriaHidden === 'true' && row.bodyInert
        )),
        `collapsed journey bodies remained interactive: ${JSON.stringify(state)}`,
      );
      assert(
        state.primary.length === 1 && (!expectedPrimary || state.primary[0] === expectedPrimary),
        `unfinished journey does not have one obvious action: ${JSON.stringify(state.primary)}`,
      );
    };

    const assertCompleted = async () => {
      const state = await journeyState();
      assert(state.current.length === 0, `completed journey forced a current step: ${JSON.stringify(state)}`);
      assert(state.bodies.length === 0, `completed journey forced an expanded body: ${JSON.stringify(state)}`);
      assert(state.primary.length === 0, `completed journey forced a primary journey action: ${JSON.stringify(state)}`);
    };

    const audioReadyOverrides = () => ({
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });

    const assertPrivacyDidNotAdvance = async (label, { receiptChoice = null } = {}) => {
      const state = await page.evaluate(() => ({
        privacyChoice: _firstListenUi.privacyChoice,
        privacyReceiptChoice: _firstListenUi.privacyReceiptChoice,
        showSuccess: _firstListenUi.showSuccess,
      }));
      assert(state.privacyChoice === null, `${label} set the durable privacy choice`);
      assert(state.privacyReceiptChoice === receiptChoice, `${label} fabricated an active privacy choice`);
      assert(state.showSuccess === false, `${label} triggered the success celebration`);
      assert(await page.locator('#journeySurface').isVisible(), `${label} hid the unfinished journey`);
      assert(await page.locator('#firstListenSuccess').isHidden(), `${label} exposed the completed success screen`);
      assert(
        await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true),
        `${label} unlocked optional AI`,
      );
      const journey = await journeyState();
      assert(
        journey.current.length === 1 && journey.current[0].id === 'firstListenPrivacyStep'
          && journey.bodies.length === 1 && journey.bodies[0].id === 'firstListenPrivacyStep',
        `${label} advanced beyond the privacy stage: ${JSON.stringify(journey)}`,
      );
    };

    const assertRejectedPrivacyContract = async ({ mode, label, enabled = false }) => {
      await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
      await page.evaluate(() => firstListenSetStatus('firstListenPrivacyStatus', ''));
      if (enabled) {
        await page.locator('#firstListenPreviewBtn').click();
        await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
      }
      const baseline = privacyRequests.length;
      nextPrivacyChoiceFailure = mode;
      await page.locator(enabled ? '#firstListenEnableContextBtn' : '#firstListenKeepOffBtn').click();
      await page.waitForFunction(() => (
        !_firstListenUi.privacySaving
          && document.getElementById('firstListenPrivacyStatus')?.dataset.tone === 'blocked'
      ));
      assert(privacyRequests.length === baseline + 1, `${label} did not send exactly one request`);
      await assertPrivacyDidNotAdvance(label);
    };

    const assertRejectedPlayContract = async (mode, label) => {
      const playCount = playRequests.length;
      await resetUi(setupProjection(), {
        players: [{
          entity_id: 'media_player.kitchen_speaker',
          friendly_name: 'Kitchen speaker',
          area: 'Kitchen',
          state: 'idle',
          device_class: 'speaker',
          supports_play_media: true,
          available: true,
        }],
        selectedEntityId: 'media_player.kitchen_speaker',
        selectedName: 'Kitchen speaker',
        selectionDirty: true,
      });
      await page.evaluate(() => {
        renderFirstListenPlayers(_firstListenUi.players);
        renderFirstListenProgress();
      });
      nextPlayResponse = mode;
      const readyJourney = await journeyState();
      assert(
        await page.locator('#firstListenPlayBtn').isVisible(),
        `${label} did not leave the play action visible: ${JSON.stringify(readyJourney)}`,
      );
      await page.locator('#firstListenPlayBtn').click();
      await page.waitForFunction(() => _firstListenUi.dispatch === 'rejected' && !_firstListenUi.busy);
      assert(playRequests.length === playCount + 1, `${label} did not send exactly one play request`);
      const state = await page.evaluate(() => ({
        attemptId: _firstListenUi.attemptId,
        verification: _firstListenUi.verification,
        repairOpen: _firstListenUi.repairOpen,
      }));
      assert(state.attemptId === '', `${label} trusted a mismatched attempt`);
      assert(state.verification === 'awaiting', `${label} fabricated a listening result`);
      assert(state.repairOpen === false, `${label} exposed receipt repair for the wrong room`);
      assert(await page.locator('#firstListenHeardBtn').isDisabled(), `${label} unlocked human proof`);
      assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), `${label} unlocked optional AI`);
      await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    };

    const assertRejectedReceiptRetryContract = async (mode, label) => {
      const playCount = playRequests.length;
      const retryCount = receiptRetryRequests.length;
      await resetUi(setupProjection(), {
        selectedEntityId: 'media_player.kitchen_speaker',
        selectedName: 'Kitchen speaker',
        selectionDirty: false,
        dispatch: 'receipt_failed',
        verification: 'awaiting',
        repairOpen: true,
      });
      nextReceiptRetryResponse = mode;
      await page.locator('#firstListenSaveAttemptBtn').click();
      await page.waitForFunction(() => (
        !_firstListenUi.receiptSaving
          && document.getElementById('firstListenVerifyStatus')?.dataset.tone === 'degraded'
      ));
      assert(receiptRetryRequests.length === retryCount + 1, `${label} did not send exactly one receipt retry`);
      assert(playRequests.length === playCount, `${label} replayed the station`);
      const state = await page.evaluate(() => ({
        attemptId: _firstListenUi.attemptId,
        dispatch: _firstListenUi.dispatch,
        verification: _firstListenUi.verification,
      }));
      assert(state.attemptId === '' && state.dispatch === 'receipt_failed', `${label} trusted an unbound receipt`);
      assert(state.verification === 'awaiting', `${label} fabricated a listening result`);
      assert(await page.locator('#firstListenHeardBtn').isDisabled(), `${label} unlocked human proof`);
      assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), `${label} unlocked optional AI`);
      await assertUnfinished('firstListenVerifyStep', 'firstListenSaveAttemptBtn');
    };

    const assertRejectedVerifyContract = async (mode, label) => {
      const verifyCount = verifyRequests.length;
      await resetUi(setupProjection(), {
        selectedEntityId: 'media_player.kitchen_speaker',
        selectedName: 'Kitchen speaker',
        selectionDirty: false,
        attemptId: 'browser-proof-attempt',
        dispatch: 'accepted',
        verification: 'awaiting',
      });
      nextVerifyResponse = mode;
      await page.locator('#firstListenHeardBtn').click();
      await page.waitForFunction(() => (
        !_firstListenUi.busy
          && document.getElementById('firstListenVerifyStatus')?.dataset.tone === 'blocked'
      ));
      assert(verifyRequests.length === verifyCount + 1, `${label} did not send exactly one verification`);
      assert(await page.evaluate(() => _firstListenUi.verification) === 'awaiting', `${label} advanced the listening proof`);
      assert(await page.locator('#firstListenPrivacyStep').getAttribute('data-state') !== 'current', `${label} unlocked privacy`);
      assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), `${label} unlocked optional AI`);
      assert(await page.evaluate(() => _firstListenUi.showSuccess) === false, `${label} triggered the success celebration`);
      await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');
    };

    await resetUi(setupProjection());
    assert(await page.locator('#tab-setup').getAttribute('aria-selected') === 'true', 'fresh install did not land on First Listen');
    assert(await page.locator('#first-listen-panel').isVisible(), 'First Listen is not the primary setup surface');
    assert(await page.locator('#journeySurface').isVisible(), 'calm First Listen surface is missing');
    assert(await page.locator('.first-listen-step').count() === 5, 'all five stages do not read as one journey');
    assert(await page.locator('#firstListenQuickAction').count() === 0, 'legacy duplicate quick action returned');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('preload') === 'none', 'local host guide may preload unexpectedly');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('autoplay') === null, 'local host guide may autoplay');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');
    assert(
      await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true),
      'optional AI unlocked before the required journey',
    );

    const welcomeGuide = page.locator('.guide-audio[data-guide="welcome"]');
    const welcomeGuideButton = welcomeGuide.locator('.guide-audio-play');
    const welcomeRequestBaseline = guideAudioRequests.length;
    await welcomeGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing');
    assert(guideAudioRequests.length === welcomeRequestBaseline + 1, 'welcome guide did not request its local audio asset');
    assert(
      /^\/static\/audio\/first_listen\/welcome\.mp3\?v=[0-9a-f]{12}$/.test(guideAudioRequests.at(-1)),
      `welcome guide used the wrong local URL: ${guideAudioRequests.at(-1)}`,
    );
    assert((await welcomeGuideButton.innerText()) === 'Pause example', 'welcome guide did not expose pause after playback started');
    await welcomeGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'paused');
    assert((await welcomeGuideButton.innerText()) === 'Continue example', 'welcome guide did not expose resume after pause');
    await welcomeGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing');
    await page.locator('#firstListenGuideAudio').evaluate((audio) => audio.dispatchEvent(new Event('ended')));
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'ended');
    assert((await welcomeGuideButton.innerText()) === 'Play again', 'ended guide did not expose replay');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, 'ended guide retained its audio URL');

    const speakerGuide = page.locator('.guide-audio[data-guide="speaker"]');
    const speakerGuideButton = speakerGuide.locator('.guide-audio-play');
    const speakerGuideRequestBaseline = guideAudioRequests.length;
    failNextGuideKey = 'speaker';
    await speakerGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'error');
    assert(guideAudioRequests.length === speakerGuideRequestBaseline + 1, 'speaker guide error fixture did not request audio once');
    assert((await speakerGuideButton.innerText()) === 'Try example again', 'failed guide did not expose retry');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, 'failed guide retained its terminal audio URL');
    await speakerGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'playing');
    assert(guideAudioRequests.length === speakerGuideRequestBaseline + 2, 'guide retry did not request a fresh audio response');
    await speakerGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'paused');

    await resetUi(setupProjection({ primary: 'unavailable', recovery: 'cover_only' }));
    await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');
    assert((await page.locator('#firstListenSourceChip').innerText()) === 'BACKUP READY', 'degraded source lost its honest runtime status');
    assert((await page.locator('#firstListenSourceSummary').innerText()).includes('Backup music'), 'degraded source did not explain what the listener gets');
    assert((await page.locator('#firstListenSourceRepair').innerText()).includes('continue'), 'degraded source blocked an otherwise usable First Listen');

    await resetUi(setupProjection());
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'ready' && !_firstListenUi.busy);
    assert(playerRequests.length >= 1, 'speaker discovery was not requested');
    assert(await page.locator('#firstListenPlayerChoices input').count() === 2, 'speaker choices were not rendered as deliberate options');
    assert(await page.locator('#firstListenPath img').count() === 0, 'hostile long speaker name became markup');
    await page.locator('#firstListenPlayerChoices input').first().click();
    assert((await page.evaluate(() => _firstListenUi.selectedName)).includes('<img src=x'), 'long speaker name was not retained as harmless text');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    assert(!(await page.locator('#firstListenSpeakerSummary').innerText()).includes('media_player.'), 'raw entity ID leaked into the calm journey');

    await page.locator('#firstListenSpeakerBody .guide-audio-play').click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'playing');
    assert(await page.locator('#firstListenPlayBtn').isDisabled(), 'guide playback left room proof actionable');
    await page.locator('#firstListenSpeakerBody .guide-audio-play').click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'paused');
    assert(await page.locator('#firstListenPlayBtn').isEnabled(), 'stopped guide did not restore the room action');

    const playBaseline = playRequests.length;
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    assert(playRequests.length === playBaseline + 1, 'explicit room choice did not send exactly one play request');
    assert(playRequests.at(-1).entity_id === 'media_player.mac_lab_speaker', 'play request did not target the exact selected speaker');
    await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');
    assert(await page.evaluate(() => document.activeElement?.id) === 'firstListenVerifyHeading', 'accepted playback did not focus the human sound check');

    await page.locator('#firstListenVerifyBody > .guide-audio .guide-audio-play').click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'playing');
    const proofCountWhileGuidePlays = verifyRequests.length;
    assert(
      await page.locator('#firstListenHeardBtn').isDisabled() && await page.locator('#firstListenNotYetBtn').isDisabled(),
      'guide playback left human proof actionable',
    );
    await page.evaluate(() => verifyFirstListen(true, document.getElementById('firstListenHeardBtn')));
    assert(verifyRequests.length === proofCountWhileGuidePlays, 'guide playback coexisted with human sound proof');
    const beforePageHide = await page.evaluate(() => ({
      dispatch: _firstListenUi.dispatch,
      verification: _firstListenUi.verification,
    }));
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      document.dispatchEvent(new Event('visibilitychange'));
      delete document.hidden;
    });
    assert(await page.evaluate(() => _firstListenUi.guideKey) === '', 'page-hidden guide did not stop');
    assert(
      await page.locator('#firstListenHeardBtn').isEnabled() && await page.locator('#firstListenNotYetBtn').isEnabled(),
      'page-hidden guide did not unlock proof controls',
    );
    assert(
      JSON.stringify(await page.evaluate(() => ({
        dispatch: _firstListenUi.dispatch,
        verification: _firstListenUi.verification,
      }))) === JSON.stringify(beforePageHide),
      'page-hidden guide changed proof state',
    );

    await page.locator('#firstListenNotYetBtn').click();
    await page.waitForFunction(() => _firstListenUi.verification === 'not_yet' && !_firstListenUi.busy);
    assert(verifyRequests.at(-1)?.heard === false, 'Not yet was not recorded explicitly');
    assert(await page.locator('#firstListenRepair').isVisible(), 'Not yet did not expose contextual recovery');
    await assertUnfinished('firstListenVerifyStep', 'firstListenRetryBtn');

    await page.locator('#firstListenRepair .guide-audio-play').click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="not-yet"]')?.dataset.state === 'playing');
    assert(await page.locator('#firstListenRetryBtn').isDisabled(), 'guide playback left retry proof actionable');
    await page.locator('#firstListenRepair .guide-audio-play').click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="not-yet"]')?.dataset.state === 'paused');

    await page.locator('#firstListenChooseAnotherBtn').click();
    assert(await page.locator('#firstListenPlayerChoices input:checked').count() === 0, 'choose another speaker left a generated radio checked');
    assert(await page.evaluate(() => _firstListenUi.selectedEntityId) === '', 'choose another speaker retained the old target');
    await page.locator('#firstListenPlayerChoices input').nth(1).click();
    assert(await page.locator('#firstListenPlayerChoices input:checked').count() === 1, 'cleared speaker radio could not be selected again');
    assert(await page.evaluate(() => _firstListenUi.selectedEntityId) === 'media_player.kitchen_speaker', 'speaker re-selection used the wrong target');
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    await page.locator('#firstListenNotYetBtn').click();
    await page.waitForFunction(() => _firstListenUi.verification === 'not_yet' && !_firstListenUi.busy);
    const retryBaseline = playRequests.length;
    await page.locator('#firstListenRetryBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && _firstListenUi.verification === 'awaiting' && !_firstListenUi.busy);
    assert(playRequests.length === retryBaseline + 1, 'explicit same-room retry did not send one new play request');
    assert(playRequests.at(-1).entity_id === playRequests.at(-2).entity_id, 'same-room retry changed the selected speaker');

    const receiptPlayBaseline = playRequests.length;
    const receiptRetryBaseline = receiptRetryRequests.length;
    failNextPlayReceipt = true;
    await resetUi(setupProjection(), {
      players: [{
        entity_id: 'media_player.kitchen_speaker',
        friendly_name: 'Kitchen speaker',
        area: 'Kitchen',
        state: 'idle',
        device_class: 'speaker',
        supports_play_media: true,
        available: true,
      }],
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      selectionDirty: true,
    });
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'receipt_failed' && !_firstListenUi.busy);
    assert(playRequests.length === receiptPlayBaseline + 1, 'receipt fixture did not begin with exactly one play request');
    await assertUnfinished('firstListenVerifyStep', 'firstListenSaveAttemptBtn');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'receipt-only recovery is hidden');
    assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'receipt-only recovery unlocked human confirmation too soon');
    receiptRetryFailuresRemaining = 1;
    await page.locator('#firstListenSaveAttemptBtn').click();
    await page.waitForFunction(() => (
      _firstListenUi.dispatch === 'receipt_failed'
        && !_firstListenUi.receiptSaving
        && document.getElementById('firstListenVerifyStatus')?.textContent.includes('not saved')
    ));
    assert(receiptRetryRequests.length === receiptRetryBaseline + 1, 'failed persistence-only retry was not sent once');
    assert(playRequests.length === receiptPlayBaseline + 1, 'failed receipt recovery replayed the station');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'failed receipt recovery removed its only repair path');
    assert(await page.locator('#firstListenSaveAttemptBtn').isEnabled(), 'failed receipt recovery disabled its retry action');
    assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'failed receipt recovery unlocked stale human proof');
    await page.locator('#firstListenSaveAttemptBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.receiptSaving);
    assert(receiptRetryRequests.length === receiptRetryBaseline + 2, 'successful persistence-only retry was not sent once');
    assert(playRequests.length === receiptPlayBaseline + 1, 'receipt recovery replayed the station');
    assert((await page.locator('#firstListenVerifyStatus').innerText()).includes('did not play again'), 'receipt recovery lost its no-replay confirmation');

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
    assert(playRequests.length === lostResponsePlayBaseline + 1, 'discarded response did not originate from one playback request');

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
    assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'pending attempt unlocked stale durable proof after reload');
    assert(playRequests.length === lostResponsePlayBaseline + 1, 'page reload replayed the lost-response attempt');

    await page.evaluate(() => findFirstListenPlayers(document.getElementById('firstListenFindPlayersBtn')));
    await page.waitForFunction(() => _firstListenUi.discovery === 'empty' && !_firstListenUi.busy);
    assert(playerRequests.length === lostResponseDiscoveryBaseline + 1, 'pending-receipt discovery request count drifted');
    assert(
      await page.evaluate(() => _firstListenUi.selectedEntityId) === 'media_player.mac_lab_speaker',
      'empty discovery cleared the server-matched pending speaker',
    );
    assert(await page.locator('#firstListenSaveAttemptBtn').isEnabled(), 'pending receipt required the speaker to be rediscovered');
    assert(playRequests.length === lostResponsePlayBaseline + 1, 'pending-receipt discovery replayed the station');
    await page.locator('#firstListenSaveAttemptBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.receiptSaving);
    assert(receiptRetryRequests.length === lostResponseRetryBaseline + 1, 'page-reload recovery did not save the pending receipt');
    assert(playRequests.length === lostResponsePlayBaseline + 1, 'lost-response recovery sent a second playback request');
    assert(await page.locator('#firstListenHeardBtn').isEnabled(), 'page-reload recovery did not unlock human verification');
    playerCandidatesAvailable = true;

    for (const [mode, label] of [
      ['success_entity_mismatch', 'play success with a mismatched entity'],
      ['partial_entity_mismatch', 'play receipt failure with a mismatched entity'],
    ]) {
      await assertRejectedPlayContract(mode, label);
    }
    for (const [mode, label] of [
      ['missing_accepted', 'receipt retry without accepted proof'],
      ['entity_mismatch', 'receipt retry with a mismatched entity'],
    ]) {
      await assertRejectedReceiptRetryContract(mode, label);
    }
    for (const [mode, label] of [
      ['attempt_mismatch', 'verification with a mismatched attempt echo'],
      ['heard_mismatch', 'verification with a mismatched heard echo'],
      ['achievement_missing', 'heard verification without achievement proof'],
      ['achievement_false', 'heard verification with false achievement proof'],
    ]) {
      await assertRejectedVerifyContract(mode, label);
    }

    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    const forbiddenPrivacyBaseline = privacyRequests.length;
    nextPrivacyChoiceFailure = 'forbidden';
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => (
      !_firstListenUi.privacySaving
        && document.getElementById('firstListenPrivacyStatus')?.dataset.tone === 'blocked'
    ));
    assert(privacyRequests.length === forbiddenPrivacyBaseline + 1, 'forbidden privacy choice did not send exactly one request');
    assert(await page.evaluate(() => _firstListenUi.privacyChoice) === null, 'HTTP 403 advanced the privacy choice');
    assert(await page.evaluate(() => _firstListenUi.showSuccess) === false, 'HTTP 403 triggered the success celebration');
    assert(await page.locator('#journeySurface').isVisible(), 'HTTP 403 hid the unfinished journey');
    assert(await page.locator('#firstListenSuccess').isHidden(), 'HTTP 403 exposed the completed success screen');
    assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), 'HTTP 403 unlocked optional AI');
    assert((await page.locator('#firstListenPrivacyStatus').innerText()).includes('couldn’t finish that'), 'HTTP 403 did not show a blocked way forward');

    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    await page.evaluate(() => firstListenSetStatus('firstListenPrivacyStatus', ''));
    const missingOkPrivacyBaseline = privacyRequests.length;
    nextPrivacyChoiceFailure = 'missing_ok';
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => (
      !_firstListenUi.privacySaving
        && document.getElementById('firstListenPrivacyStatus')?.dataset.tone === 'blocked'
    ));
    assert(privacyRequests.length === missingOkPrivacyBaseline + 1, 'missing-ok privacy choice did not send exactly one request');
    assert(await page.evaluate(() => _firstListenUi.privacyChoice) === null, 'missing-ok response advanced the privacy choice');
    assert(await page.evaluate(() => _firstListenUi.showSuccess) === false, 'missing-ok response triggered the success celebration');
    assert(await page.locator('#journeySurface').isVisible(), 'missing-ok response hid the unfinished journey');
    assert(await page.locator('#firstListenSuccess').isHidden(), 'missing-ok response exposed the completed success screen');
    assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), 'missing-ok response unlocked optional AI');

    for (const [mode, label] of [
      ['success_missing_persisted', 'privacy success without persisted proof'],
      ['success_persisted_false', 'privacy success with false persisted proof'],
      ['success_missing_privacy_reviewed', 'privacy success without reviewed proof'],
      ['success_privacy_reviewed_false', 'privacy success with false reviewed proof'],
      ['success_missing_enabled', 'privacy success without a boolean choice echo'],
      ['success_enabled_mismatch', 'privacy success with a mismatched choice echo'],
      ['receipt_missing_persisted', 'privacy receipt repair without persisted proof'],
      ['receipt_persisted_false', 'privacy receipt repair with false persisted proof'],
      ['receipt_missing_enabled', 'privacy receipt repair without a boolean choice echo'],
      ['receipt_enabled_mismatch', 'privacy receipt repair with a mismatched choice echo'],
      ['receipt_wrong_code', 'privacy receipt repair with the wrong error code'],
      ['receipt_http_200', 'privacy receipt repair over HTTP 200'],
      ['receipt_ok_true', 'privacy receipt repair with ok true'],
    ]) {
      await assertRejectedPrivacyContract({ mode, label });
    }
    for (const [mode, label] of [
      ['preview_required_active_claim', 'preview-required response claiming Home context is active'],
      ['preview_required_unpersisted_off', 'preview-required response with an unpersisted active-off claim'],
    ]) {
      await assertRejectedPrivacyContract({ mode, label, enabled: true });
    }

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
    const compensatedPrivacyBaseline = privacyRequests.length;
    nextPrivacyChoiceFailure = 'preview_required_active_off';
    await page.locator('#firstListenEnableContextBtn').click();
    await page.waitForFunction(() => (
      !_firstListenUi.privacySaving
        && _firstListenUi.privacyReceiptChoice === false
        && document.getElementById('firstListenPrivacyStatus')?.dataset.tone === 'blocked'
    ));
    assert(privacyRequests.length === compensatedPrivacyBaseline + 1, 'canonical active-off compensation did not send one privacy request');
    await assertPrivacyDidNotAdvance('canonical active-off compensation', { receiptChoice: false });

    for (const [mode, label] of [
      ['stale', 'stale Home context preview'],
      ['sent_now_nonempty', 'Home context preview that already sent data'],
      ['unknown_context_value', 'Home context preview with an unknown value class'],
    ]) {
      await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
      const malformedPreviewBaseline = previewRequests.length;
      nextPreviewResponse = mode;
      await page.locator('#firstListenPreviewBtn').click();
      await page.waitForFunction(() => (
        _firstListenUi.privacyPreview === 'unavailable'
          && _firstListenUi.privacyPreviewValid === false
      ));
      assert(previewRequests.length === malformedPreviewBaseline + 1, `${label} did not send exactly one preview request`);
      assert(await page.locator('#firstListenEnableContextBtn').isDisabled(), `${label} exposed Enable Home context`);
      assert(await page.locator('#haContextPreview .ha-preview-row').count() === 0, `${label} rendered untrusted preview rows`);
      assert(!(await page.locator('#haContextPreview').innerText()).includes('Lab presence'), `${label} rendered untrusted Home data`);
      await assertPrivacyDidNotAdvance(label);
    }

    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    await assertUnfinished('firstListenPrivacyStep', 'firstListenKeepOffBtn');
    const previewBaseline = previewRequests.length;
    const privacyBaseline = privacyRequests.length;
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
    assert(previewRequests.length === previewBaseline, 'private path requested a Home preview');
    assert(privacyRequests.length === privacyBaseline + 1 && privacyRequests.at(-1).enabled === false, 'private path did not save an explicit false');
    assert(await page.locator('#firstListenSuccess').isVisible(), 'fresh private completion did not reach the success moment');
    assert(await page.locator('#journeySurface').isHidden(), 'success moment left the journey competing on screen');
    assert((await page.locator('#firstListenSuccessPrivacy').innerText()) === 'Home stays private', 'success receipt lost the private choice');
    assert(await page.locator('#firstListenSuccess .btn-trigger:visible').count() === 1, 'success page does not have one obvious action');

    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
    assert(previewRequests.length === previewBaseline + 1, 'enabled path skipped the fresh filtered preview');
    assert(await page.locator('#haContextPreview script').count() === 0, 'hostile Home preview label became markup');
    assert((await page.locator('#haContextPreview').innerText()).includes('<script>not markup</script>'), 'Home preview did not preserve hostile text safely');
    assert(await page.locator('#firstListenEnableContextBtn').isVisible(), 'fresh preview did not expose explicit enable confirmation');
    await page.locator('#firstListenEnableContextBtn').click();
    await page.waitForFunction(() => !_firstListenUi.privacyPreviewValid && !_firstListenUi.privacySaving);
    assert(await page.evaluate(() => _firstListenUi.privacyChoice) === null, 'expired preview changed the privacy choice');
    assert(await page.evaluate(() => document.activeElement?.id) === 'firstListenPreviewBtn', 'expired preview did not return focus to a fresh preview');
    assert((await page.locator('#haContextPreview').innerText()).includes('out of date'), 'expired preview did not explain why another preview is needed');
    assert(await page.locator('#firstListenEnableContextBtn').isDisabled(), 'expired preview left enable actionable');
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
    await page.locator('#firstListenEnableContextBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === true && !_firstListenUi.privacySaving);
    assert(
      privacyRequests.slice(privacyBaseline + 1).filter((entry) => entry.enabled === true).length === 2,
      'preview expiry did not require exactly one fresh-preview retry',
    );
    assert(await page.locator('#firstListenSuccess').isVisible(), 'fresh enabled completion did not reach the success moment');
    assert((await page.locator('#firstListenSuccessPrivacy').innerText()) === 'Home context is on', 'success receipt lost the enabled choice');

    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
    const enabledReceiptBaseline = privacyRequests.length;
    failNextPrivacyReceipt = true;
    await page.locator('#firstListenEnableContextBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyReceiptChoice === true && !_firstListenUi.privacySaving);
    assert(privacyRequests.length === enabledReceiptBaseline + 1, 'enabled receipt failure did not send one privacy choice');
    assert(await page.evaluate(() => _firstListenUi.privacyChoice) === null, 'enabled receipt failure claimed the review was saved');
    const enabledReceiptChip = await page.locator('#firstListenPrivacyChip').innerText();
    assert(enabledReceiptChip.toLowerCase() === 'review not saved', `enabled receipt failure hid unsaved progress: ${enabledReceiptChip}`);
    assert((await page.locator('#firstListenPrivacySummary').innerText()).includes('Home context is on'), 'enabled receipt repair lost the active live choice');
    assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), 'enabled receipt failure unlocked optional AI');
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
    assert((await page.locator('#firstListenEnableContextBtn').innerText()) === 'Save shared choice again', 'enabled receipt repair lost its persistence-only action');
    await page.locator('#firstListenEnableContextBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === true && _firstListenUi.privacyReceiptChoice === null && !_firstListenUi.privacySaving);
    assert(privacyRequests.length === enabledReceiptBaseline + 2, 'enabled receipt repair did not retry exactly once');

    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    const privateReceiptBaseline = privacyRequests.length;
    failNextPrivacyReceipt = true;
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyReceiptChoice === false && !_firstListenUi.privacySaving);
    assert(privacyRequests.length === privateReceiptBaseline + 1, 'private receipt failure did not send one privacy choice');
    assert(await page.evaluate(() => _firstListenUi.privacyChoice) === null, 'private receipt failure claimed the review was saved');
    assert((await page.locator('#haContextPreview').innerText()).includes('Home context stays off'), 'private receipt repair lost the safe live state');
    assert((await page.locator('#firstListenKeepOffBtn').innerText()) === 'Save private choice again', 'private receipt repair lost its persistence-only action');
    assert(await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true), 'private receipt failure unlocked optional AI');
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && _firstListenUi.privacyReceiptChoice === null && !_firstListenUi.privacySaving);
    assert(privacyRequests.length === privateReceiptBaseline + 2, 'private receipt repair did not retry exactly once');

    await resetUi(setupProjection({
      audio: true,
      privacyEnabled: true,
      privacyChoiceExplicit: true,
    }));
    const reloadedPrivacyChip = await page.locator('#firstListenPrivacyChip').innerText();
    assert(reloadedPrivacyChip.toLowerCase() === 'review not saved', `reloaded active choice lost receipt recovery: ${reloadedPrivacyChip}`);
    assert((await page.locator('#firstListenPrivacySummary').innerText()).includes('Home context is on'), 'reloaded active choice lost live privacy truth');

    ambientOnlyPreview = true;
    await resetUi(setupProjection({ audio: true }), {
      selectedEntityId: 'media_player.kitchen_speaker',
      selectedName: 'Kitchen speaker',
      attemptId: 'browser-attempt-server',
      dispatch: 'accepted',
      verification: 'heard',
    });
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreview === 'ambient_only');
    assert((await page.locator('#haContextPreview').innerText()).includes('Nothing worth putting on air yet.'), 'ambient-only preview was sold as meaningful Home context');
    assert(await page.locator('.ha-preview-details').count() === 1, 'ambient-only detail was not retained for transparency');
    assert((await page.locator('.ha-preview-details').evaluate((el) => el.open)) === false, 'ambient-only detail opened like the product payoff');
    assert((await page.locator('#firstListenEnableContextBtn').innerText()).includes('daylight only'), 'ambient-only enable choice was not labeled honestly');
    ambientOnlyPreview = false;

    const completed = setupProjection({
      audio: true,
      privacy: true,
      privacyChoiceExplicit: true,
      onboardingRequired: false,
    });
    await resetUi(completed);
    await assertCompleted();
    assert(await page.locator('#firstListenSuccess').isHidden(), 'completed return replayed the one-time success moment');
    assert(await page.locator('.first-listen-step[data-state="complete"] > .first-listen-head > .first-listen-review:visible').count() === 4, 'completed choices are not visibly revisitable');
    const speakerReview = page.locator('#firstListenSpeakerStep > .first-listen-head > .first-listen-review');
    await speakerReview.click();
    let reviewState = await journeyState();
    assert(reviewState.current.length === 0, 'review expansion invented a current step');
    assert(reviewState.bodies.length === 1 && reviewState.bodies[0].id === 'firstListenSpeakerStep', 'speaker review did not expand inline');
    assert(await speakerReview.getAttribute('aria-expanded') === 'true', 'speaker review disclosure semantics drifted');
    await speakerReview.click();
    await assertCompleted();
    assert(await page.evaluate(() => document.activeElement?.getAttribute('data-review-step')) === 'speaker', 'closing review did not restore focus');

    const retestPlayBaseline = playRequests.length;
    failNextPlayReceipt = true;
    const verifyReview = page.locator('#firstListenVerifyStep > .first-listen-head > .first-listen-review');
    await verifyReview.click();
    assert(await page.locator('#firstListenRetestBtn').isVisible(), 'completed sound proof has no deliberate retest action');
    await page.locator('#firstListenRetestBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'receipt_failed' && !_firstListenUi.busy);
    assert(playRequests.length === retestPlayBaseline + 1, 'same-speaker retest did not send exactly one explicit play request');
    await assertUnfinished('firstListenVerifyStep', 'firstListenSaveAttemptBtn');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'same-speaker retest receipt failure reused old durable heard proof');
    assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'same-speaker retest receipt failure unlocked stale human proof');

    await resetUi(setupProjection({ fresh: false, onboardingRequired: true }));
    const noSavedSpeakerPlayBaseline = playRequests.length;
    const noSavedSpeakerDiscoveryBaseline = playerRequests.length;
    const noSavedSpeakerVerifyReview = page.locator('#firstListenVerifyStep > .first-listen-head > .first-listen-review');
    await noSavedSpeakerVerifyReview.click();
    assert(await page.locator('#firstListenChooseSpeakerToRetestBtn').isVisible(), 'existing install without a saved speaker hid the deliberate selection action');
    assert(await page.locator('#firstListenRetestBtn').isHidden(), 'existing install without a saved speaker exposed a dead retest action');
    await page.locator('#firstListenChooseSpeakerToRetestBtn').click();
    await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');
    assert(await page.evaluate(() => _firstListenUi.retestPending) === true, 'speaker selection route reused legacy proof');
    assert(playRequests.length === noSavedSpeakerPlayBaseline, 'choosing a speaker to retest started playback automatically');
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'ready' && !_firstListenUi.busy);
    assert(playerRequests.length === noSavedSpeakerDiscoveryBaseline + 1, 'speaker retest route did not start deliberate discovery');
    assert(await page.locator('#firstListenPlayerChoices input').count() === 2, 'speaker retest discovery did not expose deliberate choices');
    assert(playRequests.length === noSavedSpeakerPlayBaseline, 'speaker retest discovery started playback before selection');

    const existingCounts = {
      players: playerRequests.length,
      plays: playRequests.length,
      verifies: verifyRequests.length,
      previews: previewRequests.length,
    };
    await resetUi(setupProjection({ fresh: false, onboardingRequired: true, sources: false }));
    await assertUnfinished('firstListenPrivacyStep', 'firstListenKeepOffBtn');
    assert(await page.locator('#firstListenSpeakerStep').getAttribute('data-state') === 'complete', 'existing install was forced through speaker choice');
    assert(await page.locator('#firstListenVerifyStep').getAttribute('data-state') === 'complete', 'existing install was forced through audible proof');
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
    assert(playerRequests.length === existingCounts.players, 'existing install discovered speakers automatically');
    assert(playRequests.length === existingCounts.plays, 'existing install replayed the station');
    assert(verifyRequests.length === existingCounts.verifies, 'existing install fabricated a new sound check');
    assert(previewRequests.length === existingCounts.previews, 'existing private path fetched Home details');
    assert(await page.locator('#firstListenSuccess').isHidden(), 'existing install received a fresh-only success takeover');
    await assertCompleted();

    await resetUi(setupProjection({
      audio: true,
      privacy: true,
      onboardingRequired: false,
      llmKeys: ['ANTHROPIC_API_KEY'],
    }));
    await assertCompleted();
    const aiChipText = await page.locator('#firstListenAiChip').innerText();
    assert(aiChipText.toLowerCase() === 'ai service connected', `configured AI provider was not summarized safely: ${aiChipText}`);
    assert(!(await page.locator('#firstListenAiSummary').innerText()).includes('ANTHROPIC_API_KEY'), 'configured AI summary exposed a raw key name');
    const aiReview = page.locator('#firstListenAiStep > .first-listen-head > .first-listen-review');
    assert((await aiReview.innerText()) === 'Review AI setup', 'configured AI has no deliberate review action');
    await aiReview.click();
    assert(await page.locator('#setupKeysEditBtn').isVisible(), 'configured AI provider has no deliberate edit action');
    assert(await page.locator('#setupKeysForm').isHidden(), 'configured AI opened replacement fields without an edit action');
    assert(await page.locator('#setupAnthropicKey').inputValue() === '', 'stored AI key value was rendered into the page');
    assert(await page.locator('#setupAnthropicKey').getAttribute('placeholder') === 'Leave blank to keep saved key', 'blank-field safety meaning is missing');

    for (const [mode, label] of [
      ['media_source_not_ready', 'discovery without a ready canonical media source'],
      ['media_source_wrong_uri', 'discovery with the wrong media source URI'],
    ]) {
      await resetUi(setupProjection());
      const malformedDiscoveryBaseline = playerRequests.length;
      const malformedDiscoveryPlayBaseline = playRequests.length;
      nextPlayerResponse = mode;
      await page.locator('#firstListenFindPlayersBtn').click();
      await page.waitForFunction(() => _firstListenUi.discovery === 'error' && !_firstListenUi.busy);
      assert(playerRequests.length === malformedDiscoveryBaseline + 1, `${label} did not send exactly one discovery request`);
      assert(playRequests.length === malformedDiscoveryPlayBaseline, `${label} started playback`);
      assert(await page.evaluate(() => _firstListenUi.players.length) === 0, `${label} trusted candidate data`);
      assert(await page.locator('#firstListenPlayerChoices input').count() === 0, `${label} exposed speaker choices`);
      assert(await page.locator('#firstListenSpeakerStatus').getAttribute('data-tone') === 'blocked', `${label} lost its blocked treatment`);
      await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');
    }

    await resetUi(setupProjection());
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'ready' && !_firstListenUi.busy);
    await page.locator('#firstListenPlayerChoices input').nth(1).click();
    assert(await page.evaluate(() => _firstListenUi.selectedEntityId) === 'media_player.kitchen_speaker', 'transport-abort fixture did not select its stale candidate');
    assert(await page.locator('#firstListenPlayBtn').isEnabled(), 'transport-abort fixture did not begin with an actionable selected room');
    const abortedDiscoveryBaseline = playerRequests.length;
    const abortedDiscoveryPlayBaseline = playRequests.length;
    abortNextPlayerDiscovery = true;
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'error' && !_firstListenUi.busy);
    assert(playerRequests.length === abortedDiscoveryBaseline + 1, 'transport-aborted Search again did not send exactly one discovery request');
    assert(await page.evaluate(() => _firstListenUi.players.length) === 0, 'transport-aborted Search again retained stale player data');
    assert(await page.locator('#firstListenPlayerChoices input').count() === 0, 'transport-aborted Search again retained stale radios');
    assert(await page.evaluate(() => _firstListenUi.selectedEntityId) === '', 'transport-aborted Search again retained a stale selected target');
    assert(await page.locator('#firstListenSpeakerChoicesWrap').isHidden(), 'transport-aborted Search again kept stale choices visible');
    assert(await page.locator('#firstListenPlayBtn').isHidden(), 'transport-aborted Search again kept stale playback actionable');
    await page.evaluate(() => {
      renderFirstListenProgress();
      return startFirstListen(document.getElementById('firstListenPlayBtn'));
    });
    assert(playRequests.length === abortedDiscoveryPlayBaseline, 'stale selected target played after transport-aborted discovery');
    assert(await page.evaluate(() => _firstListenUi.selectedEntityId) === '', 'rendering re-enabled the stale selected target');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');

    playerReceiptRecoveryEntity = 'media_player.kitchen_speaker';
    await resetUi(setupProjection({ receiptRecoveryEntity: 'media_player.kitchen_speaker' }));
    await page.evaluate(() => findFirstListenPlayers(document.getElementById('firstListenFindPlayersBtn')));
    await page.waitForFunction(() => (
      _firstListenUi.discovery === 'ready'
        && _firstListenUi.dispatch === 'receipt_failed'
        && _firstListenUi.selectedEntityId === 'media_player.kitchen_speaker'
        && !_firstListenUi.busy
    ));
    assert(await page.locator('#firstListenPlayerChoices input').count() === 2, 'pending-receipt transport fixture did not render discovered radios');
    const pendingAbortDiscoveryBaseline = playerRequests.length;
    const pendingAbortPlayBaseline = playRequests.length;
    abortNextPlayerDiscovery = true;
    await page.evaluate(() => findFirstListenPlayers(document.getElementById('firstListenFindPlayersBtn')));
    await page.waitForFunction(() => _firstListenUi.discovery === 'error' && !_firstListenUi.busy);
    assert(playerRequests.length === pendingAbortDiscoveryBaseline + 1, 'pending-receipt transport abort did not send exactly one discovery request');
    assert(await page.evaluate(() => _firstListenUi.players.length) === 0, 'pending-receipt transport abort retained stale player data');
    assert(await page.locator('#firstListenPlayerChoices input').count() === 0, 'pending-receipt transport abort retained stale radios');
    assert(
      await page.evaluate(() => (
        _firstListenUi.selectedEntityId === 'media_player.kitchen_speaker'
          && _firstListenUi.dispatch === 'receipt_failed'
          && _firstListenUi.attemptId === ''
      )),
      'transport abort did not preserve the genuine pending receipt only',
    );
    assert(await page.locator('#firstListenSaveAttemptBtn').isEnabled(), 'transport abort disabled genuine receipt recovery');
    assert(await page.locator('#firstListenPlayBtn').isHidden(), 'pending receipt recovery re-enabled station playback');
    assert(playRequests.length === pendingAbortPlayBaseline, 'pending receipt recovery replayed the station after transport abort');
    playerReceiptRecoveryEntity = '';

    await resetUi(setupProjection());
    failNextPlayerDiscovery = true;
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'error' && !_firstListenUi.busy);
    assert((await page.locator('#firstListenSpeakerStatus').innerText()).includes('Home Assistant'), 'unreachable discovery hid its connection failure');
    assert(await page.locator('#firstListenSpeakerStatus').getAttribute('data-tone') === 'blocked', 'unreachable discovery lost its blocked treatment');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');
    playerCandidatesAvailable = false;
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'empty' && !_firstListenUi.busy);
    assert((await page.locator('#firstListenSpeakerStatus').innerText()).includes('No ready speaker was found'), 'empty discovery hid its repair guidance');
    assert(await page.locator('#firstListenSpeakerStatus').getAttribute('data-tone') === 'degraded', 'empty discovery lost its repair treatment');
    assert(await page.locator('#firstListenPlayerChoices input').count() === 0, 'empty discovery fabricated a speaker choice');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenFindPlayersBtn');
    playerCandidatesAvailable = true;

    await resetUi(setupProjection());
    await page.locator('#firstListenFindPlayersBtn').click();
    await page.waitForFunction(() => _firstListenUi.discovery === 'ready' && !_firstListenUi.busy);
    const viewportResults = [];
    for (const [width, height] of [[320, 568], [375, 667], [430, 932], [768, 1024], [1024, 768], [1440, 900]]) {
      await page.setViewportSize({ width, height });
      const geometry = await page.evaluate(() => {
        const root = document.documentElement;
        const surface = document.getElementById('journeySurface');
        const touchTargetProbes = ['ha-preview-action', 'setup-home-preview-action', 'setup-recheck-action'].map((className) => {
          const probe = document.createElement('button');
          probe.type = 'button';
          probe.className = `btn btn-util ${className}`;
          probe.setAttribute('aria-hidden', 'true');
          Object.assign(probe.style, {
            position: 'absolute',
            visibility: 'hidden',
            pointerEvents: 'none',
          });
          surface.appendChild(probe);
          const rect = probe.getBoundingClientRect();
          const result = { className, width: rect.width, height: rect.height };
          probe.remove();
          return result;
        });
        const clipped = [...surface.querySelectorAll('button, summary, label')].filter((element) => {
          if (!element.getClientRects().length) return false;
          const rect = element.getBoundingClientRect();
          return rect.left < -1 || rect.right > root.clientWidth + 1;
        }).map((element) => element.id || element.textContent.trim().slice(0, 40));
        const smallTargets = [...surface.querySelectorAll('button, summary')].filter((element) => {
          if (!element.getClientRects().length) return false;
          const rect = element.getBoundingClientRect();
          return rect.height < 43.5;
        }).map((element) => ({
          label: element.id || element.textContent.trim().slice(0, 40),
          height: element.getBoundingClientRect().height,
        }));
        const surfaceRect = surface.getBoundingClientRect();
        const escapedRects = [...surface.querySelectorAll('*')].filter((element) => {
          if (!element.getClientRects().length || element.classList.contains('sr-only')) return false;
          const rect = element.getBoundingClientRect();
          return rect.right > surfaceRect.right + 1 || rect.left < surfaceRect.left - 1;
        }).map((element) => ({
          id: element.id,
          tag: element.tagName,
          className: typeof element.className === 'string' ? element.className : '',
          left: element.getBoundingClientRect().left,
          right: element.getBoundingClientRect().right,
          surfaceLeft: surfaceRect.left,
          surfaceRight: surfaceRect.right,
        })).slice(0, 15);
        return {
          viewport: root.clientWidth,
          documentWidth: root.scrollWidth,
          clipped,
          smallTargets,
          escapedRects,
          touchTargetProbes,
        };
      });
      viewportResults.push({ width, ...geometry });
      assert(geometry.documentWidth <= geometry.viewport + 1, `${width}px page overflowed horizontally: ${JSON.stringify(geometry)}`);
      assert(geometry.clipped.length === 0, `${width}px clipped a control: ${JSON.stringify(geometry.clipped)}`);
      assert(geometry.escapedRects.length === 0, `${width}px visible journey content escaped its surface: ${JSON.stringify(geometry.escapedRects)}`);
      if (width <= 430) {
        assert(geometry.smallTargets.length === 0, `${width}px exposed a target below 44px: ${JSON.stringify(geometry.smallTargets)}`);
        assert(
          geometry.touchTargetProbes.every((target) => target.width >= 43.5 && target.height >= 43.5),
          `${width}px stylesheet touch target fell below 44px: ${JSON.stringify(geometry.touchTargetProbes)}`,
        );
      }
    }

    await page.setViewportSize({ width: 320, height: 568 });
    const zoomGeometry = await page.evaluate(() => {
      document.documentElement.style.fontSize = '200%';
      const root = document.documentElement;
      const choice = document.querySelector('#firstListenPlayerChoices .room-choice');
      return {
        viewport: root.clientWidth,
        documentWidth: root.scrollWidth,
        longNameWidth: choice?.scrollWidth || 0,
        longNameClientWidth: choice?.clientWidth || 0,
      };
    });
    assert(
      zoomGeometry.documentWidth <= zoomGeometry.viewport + 1
        && zoomGeometry.longNameClientWidth > 0
        && zoomGeometry.longNameWidth <= zoomGeometry.longNameClientWidth + 1,
      `320px/200% first-listen geometry overflowed: ${JSON.stringify(zoomGeometry)}`,
    );

    await page.route(`${baseUrl}${ingressPrefix}/admin`, async (route) => {
      const response = await route.fetch({ url: `${baseUrl}/admin` });
      await route.fulfill({ response });
    });
    await page.goto(`${baseUrl}${ingressPrefix}/admin`, { waitUntil: 'domcontentloaded', timeout: 10000 });
    await page.waitForFunction(() => typeof renderSetup === 'function' && typeof toggleFirstListenGuide === 'function');
    await page.evaluate(() => {
      (window.__firstListenSmokeIntervals || []).forEach(({ id }) => clearInterval(id));
    });
    await resetUi(setupProjection());
    const ingressGuideBaseline = guideAudioRequests.length;
    const ingressGuideButton = page.locator('.guide-audio[data-guide="speaker"] .guide-audio-play');
    await ingressGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'playing');
    assert(guideAudioRequests.length === ingressGuideBaseline + 1, 'ingress guide did not request its audio asset');
    assert(
      new RegExp(`^${ingressPrefix}/static/audio/first_listen/speaker\\.mp3\\?v=[0-9a-f]{12}$`).test(guideAudioRequests.at(-1)),
      `ingress guide escaped its base path: ${guideAudioRequests.at(-1)}`,
    );
    await ingressGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="speaker"]')?.dataset.state === 'paused');

    assert(pageErrors.length === 0, `uncaught page errors: ${pageErrors.join(' | ')}`);
    return {
      ok: true,
      scenarios: [
        'fresh',
        'degraded-source',
        'speakers-found-long-name',
        'accepted-not-heard',
        'guide-audio-lifecycle',
        'receipt-recovery-no-replay',
        'receipt-recovery-retry',
        'receipt-recovery-reload',
        'play-response-proof-binding',
        'receipt-retry-proof-binding',
        'verify-response-proof-binding',
        'privacy-http-failure',
        'privacy-missing-ok',
        'privacy-success-contract-rejected',
        'privacy-receipt-contract-rejected',
        'preview-required-active-off',
        'preview-contract-rejected',
        'privacy-off',
        'privacy-preview-expiry',
        'privacy-enabled',
        'privacy-receipt-repair',
        'ambient-only-preview',
        'discovery-contract-rejected',
        'discovery-transport-abort-clears-stale',
        'discovery-transport-abort-preserves-receipt',
        'discovery-unreachable',
        'discovery-empty',
        'ingress-guide-audio',
        'completed-return',
        'existing-install-speaker-retest-selection',
        'existing-install',
        'configured-ai',
      ],
      viewport_results: viewportResults,
      plays: playRequests.length,
      receipt_retries: receiptRetryRequests.length,
      verifies: verifyRequests.map((entry) => entry.heard),
      previews: previewRequests.length,
      privacy_choices: privacyRequests.map((entry) => entry.enabled),
      blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
    };
  }

  return runCalmJourneySmoke();
}
