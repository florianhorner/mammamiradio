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
  const resumeRequests = [];
  const verifyRequests = [];
  const previewRequests = [];
  const privacyRequests = [];
  const guideAudioRequests = [];
  const ingressPrefix = '/api/hassio_ingress/first-listen-smoke';
  const RECEIPT_FAILURE_STATUS = 503;
  const PREVIEW_REQUIRED_STATUS = 409;
  let rejectNextEnable = true;
  let failNextPrivacyReceipt = false;
  let nextPrivacyChoiceFailure = '';
  let ambientOnlyPreview = false;
  let failNextGuideKey = '';
  let failNextResume = false;
  let nextResumeResponse = '';
  let nextForceResponse = '';
  let discardNextConfirmResponse = false;
  let nextVerifyResponse = '';
  let nextPreviewResponse = '';
  let privacyResponseGate = null;
  let setupResponseGate = null;
  let nextSetupStatusError = null;
  function responseGate(){let arrive,release;return{arrived:new Promise((resolve)=>{arrive=resolve;}),wait:new Promise((resolve)=>{release=resolve;}),arrive,release};}

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
    const recoveryCoverAvailable = recovery === 'cover_only' || recovery === 'on_air';
    const continuityAvailable = healthy || recoveryCoverAvailable;
    const resolvedInstallOrigin = fresh ? 'fresh' : 'existing';
    const acceptedAttemptId = durableAttemptId || (audio ? 'listener_browser-server' : '');
    const selectedEntityId = durableEntityId || '';
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
          continuity_available: continuityAvailable,
          setup_reviewed: privacy,
          accepted_attempt_id: acceptedAttemptId,
          selected_entity_id: selectedEntityId,
          heard_at: audio ? 101 : null,
          privacy_reviewed_at: privacy ? 102 : null,
          show_ai: resolvedInstallOrigin === 'existing' || (audio && privacy && continuityAvailable),
          receipt_recovery: {
            available: Boolean(receiptRecoveryEntity),
            entity_id: receiptRecoveryEntity,
          },
        },
        source_readiness: {
          rows,
          healthy,
          recovery_cover_available: recoveryCoverAvailable,
          recovery_on_air: recovery === 'on_air',
          continuity_available: continuityAvailable,
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
  const currentSourceOptions = () => {
    const rows = setupStatusProjection?.guided_setup?.source_readiness?.rows || [];
    const primary = rows.find((row) => row.kind !== 'recovery' && row.status) || {};
    const recovery = rows.find((row) => row.kind === 'recovery') || {};
    return {
      primary: primary.status || 'playable',
      recovery: recovery.status || 'cover_only',
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
    // Force Start asks for confirmation before rebuilding the station with no
    // playable runway. A real modal suspends the page and takes the driving
    // session down with it, so stand in for it here and record the asking —
    // the prompt existing at all is part of what this smoke proves.
    window.__firstListenConfirms = [];
    window.confirm = (message) => {
      window.__firstListenConfirms.push(String(message ?? ''));
      return true;
    };
    const nativeSetInterval = window.setInterval.bind(window);
    window.__firstListenSmokeIntervals = [];
    window.setInterval = (handler, delay, ...args) => {
      const id = nativeSetInterval(handler, delay, ...args);
      window.__firstListenSmokeIntervals.push({ id, delay });
      return id;
    };
    const proto = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
    if (proto && !proto.__firstListenPlayPatched) {
      proto.__firstListenPlayPatched = true;
      const nativePlay = proto.play;
      proto.play = function play() {
        if (this && this.id === 'firstListenStationAudio') {
          queueMicrotask(() => this.dispatchEvent(new Event('playing')));
          return Promise.resolve();
        }
        return nativePlay.apply(this, arguments);
      };
    }
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
    if(privacyResponseGate){const gate=privacyResponseGate;privacyResponseGate=null;gate.arrive();await gate.wait;}
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
      ...currentSourceOptions(),
    });
    await fulfillJson(route, {
      ok: true,
      enabled: body.enabled === true,
      persisted: true,
      privacy_reviewed: true,
    });
  });
  await page.route('**/api/setup/first-listen/listener-confirm', async (route) => {
    const body = bodyOf(route);
    verifyRequests.push(body);
    if (discardNextConfirmResponse) {
      discardNextConfirmResponse = false;
      await route.abort('failed');
      return;
    }
    const responseMode = nextVerifyResponse;
    nextVerifyResponse = '';
    const heard = responseMode === 'heard_mismatch' ? body.heard !== true : body.heard === true;
    const achievementFields = responseMode === 'achievement_missing'
      ? {}
      : { first_listen_achieved: responseMode === 'achievement_false' ? false : body.heard === true };
    if (responseMode === 'receipt_unavailable') {
      await fulfillJson(route, {
        ok: false,
        heard: true,
        receipt_persisted: false,
        error: { code: 'receipt_unavailable' },
      }, RECEIPT_FAILURE_STATUS);
      return;
    }
    if (responseMode === 'receipt_persisted_false') {
      await fulfillJson(route, {
        ok: false,
        heard: true,
        receipt_persisted: false,
        error: { code: 'receipt_unavailable' },
      }, RECEIPT_FAILURE_STATUS);
      return;
    }
    await fulfillJson(route, {
      ok: true,
      heard,
      ...achievementFields,
      receipt_persisted: true,
      attempt_id: `listener_browser-${verifyRequests.length}`,
    });
  });
  await page.route('**/api/resume**', async (route) => {
    resumeRequests.push(route.request().url());
    if (failNextResume) {
      failNextResume = false;
      await route.abort('failed');
      return;
    }
    if (nextResumeResponse === 'force_available') {
      nextResumeResponse = '';
      await fulfillJson(route, { ok: false, force_available: true }, RECEIPT_FAILURE_STATUS);
      return;
    }
    if (route.request().url().includes('force=true')) {
      const forceMode = nextForceResponse;
      nextForceResponse = '';
      if (forceMode === 'running') {
        await fulfillJson(route, { ok: true, recovering: false });
        return;
      }
      if (forceMode === 'failure') {
        await fulfillJson(route, { ok: false, error: 'The station is still paused.' }, RECEIPT_FAILURE_STATUS);
        return;
      }
      // Mirror the real force-recovery reply for the rebuild form.
      await fulfillJson(route, { ok: true, recovering: true, runway_source: 'none' });
      return;
    }
    await fulfillJson(route, { ok: true });
  });
  await page.route('**/api/setup/status', async (route) => {
    if(setupResponseGate){const gate=setupResponseGate;setupResponseGate=null;gate.arrive();await gate.wait;}
    if(nextSetupStatusError){const payload=nextSetupStatusError;nextSetupStatusError=null;await fulfillJson(route,payload,403);return;}
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
  assert(await page.getByRole('heading',{name:'Hear Mamma Mi Radio, right here.',level:2}).count()===1,'First Listen lost its accessible H2');
  await page.evaluate(() => {
    (window.__firstListenSmokeIntervals || []).forEach(({ id }) => clearInterval(id));
  });

  async function runCalmJourneySmoke() {
    const resetUi = async (setup, overrides = {}) => {
      setupStatusProjection = setup;
      await page.evaluate(({ projection, ui }) => {
        finalizeFirstListenCompletion();
        stopFirstListenGuide();
        stopFirstListenStationAudio();
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
        _lastSetupJson = null;
        renderSetup(projection);
        showAdminTab(
          document.body.dataset.firstListenEntry === 'required' ? 'setup' : 'motore',
          { render: false, persist: false },
        );
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
      selectedName: 'this device',
      attemptId: 'listener_browser-server',
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

    const assertRejectedVerifyContract = async (mode, label) => {
      const verifyCount = verifyRequests.length;
      await resetUi(setupProjection(), {
        selectedName: 'this device',
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
    assert(await page.locator('#firstListenPath > .first-listen-step').count() === 4, 'required First Listen path is not four stages');
    assert(await page.locator('#firstListenAiStep').evaluate((el) => el.closest('#firstListenPath') === null), 'optional AI stayed inside required progress');
    assert(await page.locator('.first-listen-step').count() === 5, 'optional AI left the First Listen surface');
    assert((await page.locator('#firstListenOptionalHeading').textContent()).trim() === 'Optional enhancement', 'optional AI lost its section label');
    assert((await page.locator('#firstListenProgressLine').innerText()).includes('Step 2 of 4'), 'required progress should open on the first interactive step');
    assert((await page.locator('#firstListenProgressLine').innerText()).includes('of 4'), 'required progress is not Step N of 4');
    assert((await page.locator('#firstListenSourceHeading').innerText()) === 'Check music can continue', 'step 1 heading drifted from music continuity');
    const sourcePreview = page.locator('#firstListenSourcePreviewDetails');
    const sourceReview = page.locator('#firstListenSourceStep > .first-listen-head > .first-listen-review');
    assert(await sourcePreview.isHidden(), 'completed source preview expanded without operator review');
    assert(await sourceReview.isVisible(), 'completed source row lost its music-readiness review action');
    await sourceReview.click();
    assert(await sourcePreview.isVisible(), 'inline source preview is missing when step 1 is reviewed');
    const sourcePreviewSummaryBox = await sourcePreview.locator('> summary').boundingBox();
    assert(sourcePreviewSummaryBox?.height >= 43.5, `source preview summary fell below 44px: ${JSON.stringify(sourcePreviewSummaryBox)}`);
    await sourceReview.click();
    assert(await sourcePreview.isHidden(), 'inline source preview stayed open after review closed');
    const speakerHelp = await page.locator('#firstListenSpeakerBody .use-copy').innerText();
    assert(
      speakerHelp.includes('through HACS')
        && speakerHelp.includes('restart Home Assistant')
        && speakerHelp.includes('Settings → Devices & Services → Add Integration')
        && speakerHelp.includes('Media → Mamma Mi Radio'),
      'step 2 lost the HA speaker prerequisite and deferral line',
    );
    assert((await page.locator('#firstListenPlayBtn').innerText()) === 'Start sound check', 'primary playback action is not Start sound check');
    assert((await page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play').innerText()) === 'Preview 16-second welcome', 'welcome preview copy drifted');
    assert(await page.locator('.program-mark img').getAttribute('src') === '/static/favicon.svg', 'standalone mark is not the canonical favicon');
    assert(await page.locator('#firstListenAiStep').getAttribute('aria-current') === null, 'optional AI received aria-current');
    assert(await page.locator('#firstListenQuickAction').count() === 0, 'legacy duplicate quick action returned');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('preload') === 'none', 'local host guide may preload unexpectedly');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('autoplay') === null, 'local host guide may autoplay');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    assert(
      await page.locator('#firstListenAiFieldset').evaluate((element) => element.disabled === true),
      'optional AI unlocked before the required journey',
    );
    assert(await page.locator('#firstListenStationAudio').count() === 1, 'hidden station audio is missing');
    assert(await page.locator('#firstListenStationAudio').getAttribute('preload') === 'none', 'station audio may preload unexpectedly');
    assert(await page.locator('#firstListenFindPlayersBtn').count() === 0, 'speaker picker returned to First Listen');
    assert(await page.locator('#firstListenPlayerChoices').count() === 0, 'speaker choices returned to First Listen');

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

    const soundGuide = page.locator('#firstListenVerifyBody > .guide-audio[data-guide="sound-check"]');
    failNextGuideKey = 'sound-check';
    await resetUi(setupProjection(), { dispatch: 'accepted' });
    const soundGuideButton = soundGuide.locator('.guide-audio-play');
    const soundGuideRequestBaseline = guideAudioRequests.length;
    await soundGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'error');
    assert(guideAudioRequests.length === soundGuideRequestBaseline + 1, 'sound-check guide error fixture did not request audio once');
    assert((await soundGuideButton.innerText()) === 'Try example again', 'failed guide did not expose retry');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, 'failed guide retained its terminal audio URL');
    await soundGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'playing');
    assert(guideAudioRequests.length === soundGuideRequestBaseline + 2, 'guide retry did not request a fresh audio response');
    await soundGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'paused');

    await resetUi(setupProjection({ primary: 'unavailable', recovery: 'cover_only' }));
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    assert((await page.locator('#firstListenSourceChip').innerText()) === 'BACKUP AUDIO AVAILABLE', 'degraded source lost its honest runtime status');
    assert((await page.locator('#firstListenSourceSummary').innerText()).includes('Backup audio'), 'degraded source did not explain what the listener gets');
    assert((await page.locator('#firstListenSourceRepair').innerText()).includes('continue'), 'degraded source blocked an otherwise usable First Listen');
    assert(await sourcePreview.evaluate((element) => element.open), 'degraded source preview did not open on the health transition');
    await sourceReview.click();
    await sourcePreview.locator('> summary').click();
    assert(!(await sourcePreview.evaluate((element) => element.open)), 'operator could not close the degraded source preview');
    await page.evaluate(() => renderFirstListenProgress());
    assert(!(await sourcePreview.evaluate((element) => element.open)), 'routine refresh reopened the source preview after the operator closed it');
    await sourceReview.click();

    const assertEarlyCompletionExit=async(beforeSave)=>{
      const ready=setupProjection({audio:true}),gate=responseGate();await resetUi(ready,audioReadyOverrides());
      if(beforeSave)privacyResponseGate=gate;else setupResponseGate=gate;
      await page.locator('#firstListenKeepOffBtn').click();await gate.arrived;
      if(!beforeSave)await page.waitForFunction(()=>_firstListenUi.showSuccess&&document.body.dataset.firstListenEntry==='completing');
      await page.evaluate(()=>openFirstListenStation());gate.release();
      await page.waitForFunction(()=>!_firstListenUi.privacySaving&&document.body.dataset.firstListenEntry==='complete');
      const state=await page.evaluate(()=>({tab:_activeTab,success:_firstListenUi.showSuccess,mount:firstListenSetupContext.parentElement?.id}));
      assert(state.tab==='scaletta'&&!state.success&&state.mount!=='firstListenPanelMount',`${beforeSave?'pre-save':'pre-status'} exit left stale success: ${JSON.stringify(state)}`);
    };await assertEarlyCompletionExit(true);await assertEarlyCompletionExit(false);

    const fallbackCompletion = setupProjection({ primary: 'unavailable', recovery: 'cover_only', audio: true });
    setupStatusProjection = fallbackCompletion;
    await resetUi(fallbackCompletion, audioReadyOverrides());
    await assertUnfinished('firstListenPrivacyStep', 'firstListenKeepOffBtn');
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.showSuccess === true && !_firstListenUi.privacySaving);
    assert(await page.locator('#firstListenSuccess').isVisible(), 'backup audio did not allow First Listen to complete');
    const successHandoff=await page.evaluate(()=>({entry:document.body.dataset.firstListenEntry,tab:_activeTab,owned:!document.getElementById('tab-setup').hidden&&document.getElementById('tab-setup').getAttribute('aria-selected')==='true',clean:['.producer-clock','.mmr-console','.mmr-deck','#setupMusicSources'].every((s)=>!document.querySelector(s)?.getClientRects().length),panel:Boolean(document.getElementById('first-listen-panel').getClientRects().length),mount:firstListenSetupContext.parentElement?.id,focus:document.activeElement?.id}));
    assert(successHandoff.entry==='completing'&&successHandoff.tab==='setup'&&successHandoff.owned&&successHandoff.clean&&successHandoff.panel&&successHandoff.mount==='firstListenPanelMount'&&successHandoff.focus==='firstListenSuccessTitle',`completion did not hold its one-time success surface: ${JSON.stringify(successHandoff)}`);
    assert((await page.locator('#firstListenSuccessCopy').innerText()).includes('Backup audio is keeping the station playing'), 'backup completion hid the continuity explanation');
    assert((await page.locator('#firstListenSuccessCopy').innerText()).includes('primary music still needs attention'), 'backup completion hid the repair follow-up');
    assert(await page.locator('#firstListenSuccessRepair').isVisible(), 'backup completion hid its primary-music repair action');
    assert((await page.locator('#firstListenSuccess .btn-trigger').innerText()) === 'Open full listener', 'success page lost Open full listener');
    const successActionStyles = await page.evaluate(() => {
      const primary = getComputedStyle(document.querySelector('#firstListenSuccess .btn-trigger'));
      const secondary = getComputedStyle(document.querySelector('#firstListenSuccess .btn-util'));
      return {
        primary: { background: primary.backgroundColor, color: primary.color },
        secondary: { background: secondary.backgroundColor, color: secondary.color },
      };
    });
    assert(
      successActionStyles.primary.background !== successActionStyles.secondary.background
        && successActionStyles.primary.color !== successActionStyles.secondary.color,
      `success primary action lost visual hierarchy: ${JSON.stringify(successActionStyles)}`,
    );
    await page.evaluate(()=>{document.getElementById('firstListenGuideAudio').src='smoke-guide.mp3';document.getElementById('firstListenStationAudio').src='smoke-station.mp3';});
    await page.getByRole('button', { name: 'Review choices' }).click();
    await page.waitForFunction(() => document.activeElement?.getAttribute('data-review-step') === 'privacy');
    const reviewHandoff=await page.evaluate(()=>({entry:document.body.dataset.firstListenEntry,tab:_activeTab,hidden:document.getElementById('tab-setup').hidden,inMotore:Boolean(firstListenSetupContext.closest('#drawer-diagnostics')),guide:document.getElementById('firstListenGuideAudio').getAttribute('src'),station:document.getElementById('firstListenStationAudio').getAttribute('src')}));
    assert(reviewHandoff.entry==='complete'&&reviewHandoff.tab==='motore'&&reviewHandoff.hidden&&reviewHandoff.inMotore&&reviewHandoff.guide===null&&reviewHandoff.station===null,`success did not finalize into Motore safely: ${JSON.stringify(reviewHandoff)}`);

    const noContinuity = setupProjection({ primary: 'unavailable', recovery: 'unavailable', audio: true });
    setupStatusProjection = noContinuity;
    await resetUi(noContinuity, audioReadyOverrides());
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
    assert(await page.locator('#firstListenSuccess').isHidden(), 'missing continuity falsely exposed the success screen');
    await assertUnfinished('firstListenSourceStep', 'firstListenRepairMusicBtn');

    await resetUi(setupProjection());
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');
    assert(await page.evaluate(() => document.activeElement?.id) === 'firstListenVerifyHeading', 'accepted playback did not focus the human sound check');
    assert(await page.locator('#firstListenPrivacyStep').getAttribute('data-state') !== 'current', 'playing event unlocked privacy before a saved Yes');

    await page.locator('#firstListenVerifyBody > .guide-audio .guide-audio-play').click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'playing');
    const proofCountWhileGuidePlays = verifyRequests.length;
    assert(
      await page.locator('#firstListenHeardBtn').isDisabled() && await page.locator('#firstListenNotYetBtn').isDisabled(),
      'guide playback left room proof actionable',
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

    await page.locator('#firstListenRetryBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && _firstListenUi.verification === 'awaiting' && !_firstListenUi.busy);
    await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');

    nextVerifyResponse = 'receipt_unavailable';
    await page.locator('#firstListenHeardBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'receipt_failed' && !_firstListenUi.busy);
    await assertUnfinished('firstListenVerifyStep', 'firstListenSaveAttemptBtn');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'receipt-only recovery is hidden');
    assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'receipt-only recovery unlocked human confirmation too soon');
    const receiptRetryBaseline = verifyRequests.length;
    const receiptRetryResumeBaseline = resumeRequests.length;
    nextVerifyResponse = 'receipt_unavailable';
    await page.locator('#firstListenSaveAttemptBtn').click();
    await page.waitForFunction(() => (
      _firstListenUi.dispatch === 'receipt_failed'
        && !_firstListenUi.receiptSaving
    ));
    assert(verifyRequests.length === receiptRetryBaseline + 1, 'failed persistence-only retry was not sent once');
    assert(resumeRequests.length === receiptRetryResumeBaseline, 'failed receipt recovery replayed the station');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'failed receipt recovery removed its only repair path');
    assert(await page.locator('#firstListenSaveAttemptBtn').isEnabled(), 'failed receipt recovery disabled its retry action');
    nextVerifyResponse = '';
    await page.locator('#firstListenSaveAttemptBtn').click();
    await page.waitForFunction(() => _firstListenUi.verification === 'heard' && !_firstListenUi.receiptSaving);
    assert(resumeRequests.length === receiptRetryResumeBaseline, 'receipt recovery replayed the station');
    assert((await page.locator('#firstListenVerifyStatus').innerText()).includes('did not play again') || (await page.locator('#firstListenVerifyStatus').innerText()).includes('this device'), 'receipt recovery lost its no-replay confirmation');
    assert(verifyRequests.at(-1)?.heard === true, 'successful persistence-only retry was not a heard confirmation');

    await resetUi(setupProjection(), { selectedName: 'this device', dispatch: 'accepted' });
    const lostConfirmResumeBaseline = resumeRequests.length;
    const lostConfirmVerifyBaseline = verifyRequests.length;
    discardNextConfirmResponse = true;
    await page.locator('#firstListenHeardBtn').click();
    await page.waitForFunction(() => !_firstListenUi.busy);
    assert(verifyRequests.length === lostConfirmVerifyBaseline + 1, 'lost-response confirmation did not send exactly one request');
    assert(resumeRequests.length === lostConfirmResumeBaseline, 'lost-response recovery sent a second playback request');
    assert(await page.evaluate(() => _firstListenUi.verification) === 'awaiting', 'lost confirmation advanced the listening proof');
    await page.locator('#firstListenHeardBtn').click();
    await page.waitForFunction(() => _firstListenUi.verification === 'heard' && !_firstListenUi.busy);

    const reloadResumeBaseline = resumeRequests.length;
    await resetUi(setupProjection(), {
      selectedName: 'this device',
      dispatch: 'receipt_failed',
      verification: 'awaiting',
      repairOpen: true,
    });
    await assertUnfinished('firstListenVerifyStep', 'firstListenSaveAttemptBtn');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'page reload lost server-owned receipt recovery');
    assert(resumeRequests.length === reloadResumeBaseline, 'reloaded receipt recovery replayed the station');

    for (const [mode, label] of [
      ['heard_mismatch', 'verification with a mismatched heard echo'],
      ['achievement_missing', 'heard verification without achievement proof'],
      ['achievement_false', 'heard verification with false achievement proof'],
    ]) {
      await assertRejectedVerifyContract(mode, label);
    }

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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

    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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
    await resetUi(setupProjection({ audio: true }), audioReadyOverrides());
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
    assert(await page.locator('#tab-setup').isHidden(), 'completed return left First Listen in the tab bar');
    assert(await page.locator('#tab-motore').getAttribute('aria-selected') === 'true', 'completed return did not land in Motore');
    assert(await page.locator('#setupGroup > summary').isVisible(), 'Motore lost its Setup disclosure after onboarding');
    assert((await page.locator('#setupGroup > summary').boundingBox()).height >= 43.5, 'Motore Setup disclosure fell below 44px');
    nextSetupStatusError={detail:{code:'active_setup_csrf_stale',title:'Reload the dashboard',message:'The security check expired.',action:'Reload /admin, then continue First Listen.'}};await page.evaluate(()=>refreshSlow());
    assert(await page.locator('#setupAccessError').isVisible(), 'completed Setup swallowed its authorization recovery');
    const setupAccessCopy=await page.locator('#setupAccessError').innerText();
    assert(setupAccessCopy.includes('continue Setup')&&!setupAccessCopy.includes('continue First Listen'),`completed recovery copy was not Setup-neutral: ${setupAccessCopy}`);
    await page.evaluate(()=>refreshSlow());assert(await page.locator('#setupAccessError').isHidden(), 'successful Setup refresh retained stale authorization recovery');
    assert(!(await page.evaluate(() => adminTabsForNav().some((tab) => tab.dataset.tab === 'setup'))), 'hidden First Listen remained in keyboard navigation');
    await page.locator('#tab-scaletta').click();
    await page.locator('#tab-scaletta').press('ArrowLeft');
    assert(await page.locator('#tab-motore').getAttribute('aria-selected') === 'true', 'keyboard navigation wrapped through hidden First Listen');
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

    const retestResumeBaseline = resumeRequests.length;
    const retestVerifyBaseline = verifyRequests.length;
    const verifyReview = page.locator('#firstListenVerifyStep > .first-listen-head > .first-listen-review');
    await verifyReview.click();
    assert(await page.locator('#firstListenRetestBtn').isVisible(), 'completed sound proof has no deliberate retest action');
    await page.locator('#firstListenRetestBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    assert(resumeRequests.length === retestResumeBaseline + 1, 'same-device retest did not send exactly one resume request');
    assert(verifyRequests.length === retestVerifyBaseline, 'same-device retest saved hearing without a Yes');
    nextVerifyResponse = 'receipt_unavailable';
    await page.locator('#firstListenHeardBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'receipt_failed' && !_firstListenUi.busy);
    await assertUnfinished('firstListenVerifyStep', 'firstListenSaveAttemptBtn');
    assert(await page.locator('#firstListenReceiptRepair').isVisible(), 'same-speaker retest receipt failure reused old durable heard proof');
    assert(await page.locator('#firstListenHeardBtn').isDisabled(), 'same-speaker retest receipt failure unlocked stale human proof');

    await resetUi(setupProjection({ fresh: false, onboardingRequired: true }));
    const noSavedResumeBaseline = resumeRequests.length;
    const noSavedSpeakerVerifyReview = page.locator('#firstListenVerifyStep > .first-listen-head > .first-listen-review');
    await noSavedSpeakerVerifyReview.click();
    assert(await page.locator('#firstListenRetestBtn').isVisible(), 'existing install without a saved speaker hid the deliberate selection action');
    assert(await page.locator('#firstListenChooseSpeakerToRetestBtn').count() === 0, 'existing install without a saved speaker exposed a dead retest action');
    await page.locator('#firstListenRetestBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    assert(resumeRequests.length === noSavedResumeBaseline + 1, 'existing-install device retest did not start playback');
    assert(await page.evaluate(() => _firstListenUi.verification) === 'awaiting', 'choosing a speaker to retest started playback automatically');

    const existingCounts = {
      resumes: resumeRequests.length,
      verifies: verifyRequests.length,
      previews: previewRequests.length,
    };
    const existingNoSources = setupProjection({ fresh: false, onboardingRequired: true, sources: false });
    setupStatusProjection = existingNoSources;
    await resetUi(existingNoSources);
    await assertUnfinished('firstListenPrivacyStep', 'firstListenKeepOffBtn');
    assert(await page.locator('#firstListenSpeakerStep').getAttribute('data-state') === 'complete', 'existing install was forced through speaker choice');
    assert(await page.locator('#firstListenVerifyStep').getAttribute('data-state') === 'complete', 'existing install was forced through audible proof');
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
    assert(resumeRequests.length === existingCounts.resumes, 'existing install replayed the station');
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

    await resetUi(setupProjection());
    failNextResume = true;
    const failedResumeBaseline = resumeRequests.length;
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'rejected' && !_firstListenUi.busy);
    assert(resumeRequests.length === failedResumeBaseline + 1, 'unreachable playback hid its connection failure');
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    nextResumeResponse = 'force_available';
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    assert(resumeRequests.length === failedResumeBaseline + 3, 'force-available resume did not retry once');
    await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');

    await resetUi(setupProjection());
    nextResumeResponse = 'force_available';
    nextForceResponse = 'running';
    const runningForceBaseline = resumeRequests.length;
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    assert(resumeRequests.length === runningForceBaseline + 2, 'running-station force start did not retry once');
    await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');

    await resetUi(setupProjection());
    nextResumeResponse = 'force_available';
    nextForceResponse = 'failure';
    const failedForceBaseline = resumeRequests.length;
    const forceFailureAudioSrc = await page.locator('#firstListenStationAudio').getAttribute('src');
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'idle' && !_firstListenUi.busy);
    assert(resumeRequests.length === failedForceBaseline + 2, 'force-start failure did not retry once');
    assert(
      (await page.locator('#firstListenSpeakerStatus').innerText()).includes('The station is still paused.'),
      'force-start failure lost the server-authored error',
    );
    assert(
      await page.locator('#firstListenStationAudio').getAttribute('src') === forceFailureAudioSrc,
      'force-start failure opened or replaced the stream',
    );
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');

    await resetUi(setupProjection());
    await page.locator('#setupAdvancedDetails > summary').click();
    assert(
      (await page.locator('#setupAdvancedDetails').innerText()).includes('media-source://mammamiradio/live'),
      'technical details lost the optional Home Assistant media source',
    );
    const technicalColumns = await page.evaluate(() => {
      const body = document.querySelector('#setupAdvancedDetails > .technical-body');
      return [...(body?.children || [])].map((child) => ({
        id: child.id || child.className,
        fullWidth: child.classList.contains('technical-group-wide'),
        start: getComputedStyle(child).gridColumnStart,
        end: getComputedStyle(child).gridColumnEnd,
      }));
    });
    for (const child of technicalColumns.filter((entry) => (
      entry.fullWidth
        || String(entry.id).includes('setupCachedContextDiagnostics')
        || String(entry.id).includes('setup-actions')
        || String(entry.id).includes('setup-snippet')
    ))) {
      assert(child.start === '1' && child.end === '-1', `technical detail child was left in an implicit grid column: ${JSON.stringify(child)}`);
    }
    await page.locator('#setupAdvancedDetails > summary').click();
    await sourceReview.click();
    assert(await sourcePreview.isVisible(), 'source preview was not exposed for responsive geometry checks');
    const viewportResults = [];
    const journeyViewports = [[320, 568], [375, 667], [430, 932], [554, 800], [720, 900], [768, 1024], [1024, 768], [1440, 900]];
    const measureJourneyGeometry = () => page.evaluate(() => {
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
          return rect.height < 43.5 || rect.width < 43.5;
        }).map((element) => ({
          label: element.id || element.textContent.trim().slice(0, 40),
          width: element.getBoundingClientRect().width,
          height: element.getBoundingClientRect().height,
        }));
        const surfaceRect = surface.getBoundingClientRect();
        const headOverlaps = [...surface.querySelectorAll('.first-listen-head')].flatMap((head) => {
          const title = head.querySelector(':scope > .first-listen-heading');
          const status = head.querySelector(':scope > .status-chip');
          const review = head.querySelector(':scope > .first-listen-review');
          const pairs = [[title, status], [title, review], [status, review]].filter(([a,b]) => a?.getClientRects().length&&b?.getClientRects().length);
          return pairs.flatMap(([left, right]) => {
            const a = left.getBoundingClientRect();
            const b = right.getBoundingClientRect();
            const overlaps = a.left < b.right - 0.5
              && a.right > b.left + 0.5
              && a.top < b.bottom - 0.5
              && a.bottom > b.top + 0.5;
            return overlaps ? [head.closest('.first-listen-step')?.id || 'unknown'] : [];
          });
        });
        const titles=[...surface.querySelectorAll('.first-listen-heading')].filter((el)=>el.getClientRects().length).map((el)=>({id:el.id,width:el.getBoundingClientRect().width}));
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
          headOverlaps,
          smallTargets,
          titles,
          escapedRects,
          touchTargetProbes,
        };
      });
    const assertJourneyGeometry = (width, geometry, label) => {
      assert(geometry.documentWidth <= geometry.viewport + 1, `${label} ${width}px page overflowed horizontally: ${JSON.stringify(geometry)}`);
      assert(geometry.clipped.length === 0, `${label} ${width}px clipped a control: ${JSON.stringify(geometry.clipped)}`);
      assert(geometry.headOverlaps.length === 0, `${label} ${width}px title/status/action overlap: ${JSON.stringify(geometry.headOverlaps)}`);
      assert(geometry.escapedRects.length === 0, `${label} ${width}px visible journey content escaped its surface: ${JSON.stringify(geometry.escapedRects)}`);
      assert(geometry.titles.every((title) => title.width >= 48), `${label} ${width}px title collapsed: ${JSON.stringify(geometry.titles)}`);
      assert(geometry.smallTargets.length === 0, `${label} ${width}px exposed a target below 44px: ${JSON.stringify(geometry.smallTargets)}`);
      assert(geometry.touchTargetProbes.every((target)=>target.width>=43.5&&target.height>=43.5),`${label} ${width}px stylesheet touch target fell below 44px: ${JSON.stringify(geometry.touchTargetProbes)}`);
    };
    for (const [width, height] of journeyViewports) {
      await page.setViewportSize({ width, height });
      const geometry = await measureJourneyGeometry();
      viewportResults.push({ state: 'active', width, ...geometry });assertJourneyGeometry(width, geometry, 'active');
    }

    await resetUi(completed);
    await assertCompleted();
    for (const [width, height] of journeyViewports) {
      await page.setViewportSize({ width, height });
      const geometry = await measureJourneyGeometry();
      viewportResults.push({ state: 'completed', width, ...geometry });assertJourneyGeometry(width, geometry, 'completed');
    }

    await resetUi(setupProjection());
    await page.setViewportSize({ width: 320, height: 568 });
    const zoomGeometry = await page.evaluate(() => {
      document.documentElement.style.fontSize = '200%';
      const root = document.documentElement;
      const play = document.getElementById('firstListenPlayBtn');
      return {
        viewport: root.clientWidth,
        documentWidth: root.scrollWidth,
        longNameWidth: play?.scrollWidth || 0,
        longNameClientWidth: play?.clientWidth || 0,
      };
    });
    assert(
      zoomGeometry.documentWidth <= zoomGeometry.viewport + 1
        && zoomGeometry.longNameClientWidth > 0
        && zoomGeometry.longNameWidth <= zoomGeometry.longNameClientWidth + 1,
      `320px/200% first-listen geometry overflowed: ${JSON.stringify(zoomGeometry)}`,
    );
    await sourceReview.click();
    const activeZoomJourneyGeometry = await measureJourneyGeometry();
    assertJourneyGeometry(320, activeZoomJourneyGeometry, 'active 200% zoom');

    await resetUi(completed);
    await page.evaluate(() => {document.documentElement.style.fontSize = '200%';});
    const completedZoomJourneyGeometry = await measureJourneyGeometry();
    assertJourneyGeometry(320, completedZoomJourneyGeometry, 'completed 200% zoom');

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
    const ingressGuideButton = page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play');
    await ingressGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing');
    assert(guideAudioRequests.length === ingressGuideBaseline + 1, 'ingress guide did not request its audio asset');
    assert(
      new RegExp(`^${ingressPrefix}/static/audio/first_listen/welcome\\.mp3\\?v=[0-9a-f]{12}$`).test(guideAudioRequests.at(-1)),
      `ingress guide escaped its base path: ${guideAudioRequests.at(-1)}`,
    );
    await ingressGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'paused');

    assert(pageErrors.length === 0, `uncaught page errors: ${pageErrors.join(' | ')}`);
    return {
      ok: true,
      scenarios: [
        'fresh',
        'degraded-source',
        'accepted-not-heard',
        'guide-audio-lifecycle',
        'receipt-recovery-no-replay',
        'receipt-recovery-retry',
        'receipt-recovery-reload',
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
        'resume-unreachable',
        'ingress-guide-audio',
        'completed-return',
        'existing-install-device-retest',
        'existing-install',
        'configured-ai',
      ],
      viewport_results: viewportResults,
      resumes: resumeRequests.length,
      verifies: verifyRequests.map((entry) => entry.heard),
      previews: previewRequests.length,
      privacy_choices: privacyRequests.map((entry) => entry.enabled),
      blocked_off_origin_requests: [...new Set(blockedOffOriginRequests)],
    };
  }

  return runCalmJourneySmoke();
}
