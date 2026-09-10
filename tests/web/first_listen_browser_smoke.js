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
  const rememberGuideAudioRequest = (url) => {
    const requestPath = url.slice(originOf(url).length);
    if (!/\/static\/audio\/(?:first_listen|voice_examples|home_moments)\/[^/?]+\.mp3(?:\?|$)/.test(requestPath)) return;
    guideAudioRequests.push(requestPath);
  };
  function assertGuideRequests(baseline,key,prefix='') {
    const paths=guideAudioRequests.slice(baseline);
    const folder=key==='free-voices'?'voice_examples':'first_listen';
    const pattern=new RegExp(`^${prefix}/static/audio/${folder}/${key}\\.mp3\\?v=[0-9a-f]{12}$`);
    // WebKit can probe bytes 0-1 before requesting the remaining MP3 bytes.
    assert(paths.length>0, `${key} did not request its local audio asset`);
    assert(new Set(paths).size===1&&paths.every(path=>pattern.test(path)), `${key} used unexpected audio URLs: ${JSON.stringify(paths)}`);
  }
  const ingressPrefix = '/api/hassio_ingress/first-listen-smoke';
  const RECEIPT_FAILURE_STATUS = 503;
  const PREVIEW_REQUIRED_STATUS = 409;
  let rejectNextEnable = true;
  let failNextPrivacyReceipt = false;
  let nextPrivacyChoiceFailure = '';
  let ambientOnlyPreview = false;
  let failNextGuideKey = '';
  let guideResponseGate = null;
  let failNextResume = false;
  let nextResumeResponse = '';
  let resumeResponseGate = null;
  let nextForceResponse = '';
  let discardNextConfirmResponse = false;
  let nextVerifyResponse = '';
  let nextPreviewResponse = '';
  let privacyResponseGate = null;
  let setupResponseGate = null;
  let nextSetupStatusError = null;
  let nextSetupFailure = '';
  let timeoutSetupGate = null;
  let failCapabilities = false;
  let smokeStage = 'bootstrap';
  function responseGate(){let arrive,release;return{arrived:new Promise((resolve)=>{arrive=resolve;}),wait:new Promise((resolve)=>{release=resolve;}),arrive,release};}
  const initialCapabilitiesGate = responseGate();
  let capabilitiesResponseGate = initialCapabilitiesGate;

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
    window.__firstListenOpenedWindows = [];
    window.__firstListenStationMedia = {
      activeSrc: '',
      events: [],
      playing: false,
      streamRequests: [],
    };
    window.confirm = (message) => {
      window.__firstListenConfirms.push(String(message ?? ''));
      return true;
    };
    window.open = (...args) => {
      window.__firstListenOpenedWindows.push(args.map((value) => String(value ?? '')));
      return null;
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
      const nativePause = proto.pause;
      const nativeLoad = proto.load;
      const pausedDescriptor = Object.getOwnPropertyDescriptor(proto, 'paused');
      const stationState = () => window.__firstListenStationMedia;
      proto.play = function play() {
        if (this && this.id === 'firstListenStationAudio') {
          const state = stationState();
          const src = this.getAttribute('src') || '';
          const wasPlaying = state.playing;
          state.events.push({ type: 'play', src });
          if (src && state.activeSrc !== src) {
            state.activeSrc = src;
            state.streamRequests.push(src);
          }
          state.playing = Boolean(src);
          if (!wasPlaying) queueMicrotask(() => this.dispatchEvent(new Event('playing')));
          return Promise.resolve();
        }
        return nativePlay.apply(this, arguments);
      };
      proto.pause = function pause() {
        if (this && this.id === 'firstListenStationAudio') {
          const state = stationState();
          state.events.push({ type: 'pause', src: this.getAttribute('src') || '' });
          state.playing = false;
        }
        return nativePause.apply(this, arguments);
      };
      proto.load = function load() {
        if (this && this.id === 'firstListenStationAudio') {
          stationState().events.push({ type: 'load', src: this.getAttribute('src') || '' });
        }
        return nativeLoad.apply(this, arguments);
      };
      if (pausedDescriptor?.get && pausedDescriptor.configurable) {
        Object.defineProperty(proto, 'paused', {
          ...pausedDescriptor,
          get() {
            if (this && this.id === 'firstListenStationAudio') return !stationState().playing;
            return pausedDescriptor.get.call(this);
          },
        });
      }
      const nativeRemoveAttribute = Element.prototype.removeAttribute;
      Element.prototype.removeAttribute = function removeAttribute(name) {
        if (this && this.id === 'firstListenStationAudio' && String(name).toLowerCase() === 'src') {
          const state = stationState();
          state.events.push({ type: 'remove-src', src: this.getAttribute('src') || '' });
          state.activeSrc = '';
          state.playing = false;
        }
        return nativeRemoveAttribute.apply(this, arguments);
      };
    }
  });
  page.on('request', (request) => rememberGuideAudioRequest(request.url()));
  await page.route('**/*', async (route) => {
    const requestOrigin = originOf(route.request().url());
    if (!requestOrigin || requestOrigin === baseOrigin) {
      await route.fallback();
      return;
    }
    blockedOffOriginRequests.push(route.request().url());
    await route.fulfill({ status: 204, contentType: 'text/plain', body: '' });
  });
  await page.route(/\/static\/audio\/(?:first_listen|voice_examples|home_moments)\/[^/?]+\.mp3(?:\?|$)/, async (route) => {
    const requestAddress = route.request().url();
    const requestPath = requestAddress.slice(originOf(requestAddress).length);
    const filename = requestPath.split('?', 1)[0].split('/').at(-1) || '';
    const guideKey = filename.replace(/\.mp3$/i, '');
    rememberGuideAudioRequest(requestAddress);
    if (guideResponseGate) {
      const gate = guideResponseGate;
      guideResponseGate = null;
      gate.arrive();
      await gate.wait;
      await route.abort('failed');
      return;
    }
    if (failNextGuideKey === guideKey) {
      await route.abort('failed');
      return;
    }
    if (requestPath.startsWith(`${ingressPrefix}/static/audio/`)) {
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
    if(resumeResponseGate&&(!resumeResponseGate.force||route.request().url().includes('force=true'))){const gate=resumeResponseGate;resumeResponseGate=null;gate.arrive();await gate.wait;}
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
    const failure=nextSetupFailure;nextSetupFailure='';
    if(failure==='timeout'){const gate=timeoutSetupGate;await gate.wait;await route.abort().catch(()=>{});return;}
    if(failure==='network'){await route.abort('failed');return;}
    if(failure==='http-json'){await fulfillJson(route,{},503);return;}
    if(failure==='invalid-json'||failure==='http'){await route.fulfill({status:failure==='http'?503:200,contentType:'text/html',body:'Unavailable'});return;}
    if(failure==='empty'||failure==='malformed'){await fulfillJson(route,failure==='empty'?{}:{guided_setup:{first_listen:{audio_complete:'yes'}}});return;}
    if(failure==='render-error'){await fulfillJson(route,{...setupStatusProjection,available_modes:{}});return;}

    if(setupResponseGate){const gate=setupResponseGate;setupResponseGate=null;gate.arrive();await gate.wait;}
    if(nextSetupStatusError){const payload=nextSetupStatusError;nextSetupStatusError=null;await fulfillJson(route,payload,403);return;}
    await fulfillJson(route, setupStatusProjection);
  });
  await page.route('**/api/capabilities', async (route) => {
    if(failCapabilities){await route.abort('failed');return;}
    if(capabilitiesResponseGate){const gate=capabilitiesResponseGate;capabilitiesResponseGate=null;gate.arrive();await gate.wait;}
    await fulfillJson(route, { capabilities: {}, golden_path: {} });
  });
  await page.route(`${baseUrl}/admin`, async (route) => {
    const response = await route.fetch();
    const html = await response.text();
    const bodyTag = /(<\/head>\s*<body\b)([^>]*)(>)/i;
    const entryAttribute = /\bdata-first-listen-entry="(?:pending|required|complete)"/g;
    const body = html.match(bodyTag);
    assert(body && (body[2].match(entryAttribute) || []).length === 1, 'admin bootstrap fixture drifted');
    // Match the fresh API fixture before initTabs reads the server bootstrap.
    // The host instance may already contain the operator's completed receipts.
    await route.fulfill({ response, body: html.replace(bodyTag, (_, open, attributes, close) => (
      open + attributes.replace(entryAttribute, 'data-first-listen-entry="required"') + close
    )) });
  });

  // Fault injection owns network responses; a PWA cache must not bypass them.
  // Native-media acceptance exercises the unchanged service worker separately.
  await page.addInitScript(()=>{
    if('serviceWorker' in navigator)navigator.serviceWorker.register=async()=>({});
    window.__stationMediaHandlers={};
    if('mediaSession' in navigator){
      const install=navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
      navigator.mediaSession.setActionHandler=(action,handler)=>{window.__stationMediaHandlers[action]=handler;install(action,handler);};
    }
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
  await initialCapabilitiesGate.arrived;
  await page.evaluate((projection) => {
    _lastSetupJson = null;
    renderSetup(projection);
  }, setupProjection({ primary: 'unavailable' }));
  assert(await page.locator('#firstListenSourcePreview [data-source-kind="live_charts"]').count() === 0, 'pending capabilities fabricated chart availability');
  assert(await page.locator('#firstListenRepairMusicBtn').isDisabled(), 'music repair stayed actionable before capabilities resolved');
  initialCapabilitiesGate.release();
  await page.waitForFunction(() => _capsState === 'ready');
  assert(await page.locator('#firstListenSourcePreview [data-source-kind="live_charts"]').count() === 0, 'capability resolution did not repair chart guidance');
  assert((await page.locator('#firstListenRepairMusicBtn').innerText()) === 'Open music source setup', 'resolved add-on repair did not name the available setup path');
  await page.locator('#firstListenSourcePreviewDetails').evaluate((element) => { element.open = false; });
  assert(await page.getByRole('heading',{name:'Hear Mamma Mi Radio, right here.',level:2}).count()===1,'First Listen lost its accessible H2');
  await page.evaluate(() => {
    (window.__firstListenSmokeIntervals || []).forEach(({ id }) => clearInterval(id));
  });

  async function runCalmJourneySmoke() {
    smokeStage = 'journey-start';
    const resetUi = async (setup, overrides = {}) => {
      setupStatusProjection = setup;
      await page.evaluate(({ projection, ui }) => {
        cancelFirstListenHandoff();
        finalizeFirstListenCompletion();
        stopFirstListenGuide();
        stopFirstListenStationAudio();
        firstListenStationAudio().removeAttribute('src');
        firstListenStationAudio().load();
        _firstListenPlayback.phase='idle';
        renderFirstListenPlayer();
        document.getElementById('firstListenDestinations').open=false;
        document.querySelector('.success-saved').open=false;
        document.getElementById('firstListenPlayer').scrollTop=0;
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
          successAnnounced: false,
          restarting: false,
          connectionStage: 'invite',
          keySaving: false,
          connectionChecking: false,
          keySaveUnconfirmed: false,
          acknowledgedKeys: [],
          ...ui,
        });
        _lastSetupJson = null;
        renderSetup(projection);
        if (firstListenProjection().privacyUnlocked && !firstListenProjection().privacyReviewed) showFirstListenConnection('home');
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
      const number=['firstListenSpeakerStep','firstListenVerifyStep','firstListenPrivacyStep'].indexOf(expectedId)+1;
      assert((await page.locator('#firstListenProgressLine').innerText()).startsWith(`Step ${number} of 3.`), 'human step label drifted from progress');
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

    const stationMediaSnapshot = () => page.evaluate(() => {
      const state = window.__firstListenStationMedia;
      const station = document.getElementById('firstListenStationAudio');
      return {
        activeSrc: state.activeSrc,
        events: state.events.map((event) => ({ ...event })),
        playing: state.playing,
        src: station?.getAttribute('src') || null,
        streamRequests: [...state.streamRequests],
      };
    });

    const openHomeChoice = async () => {
      await page.evaluate(() => showFirstListenConnection('home'));
    };

    const startAudibleFirstListen = async ({ home = false } = {}) => {
      await resetUi(setupProjection());
      const before = await stationMediaSnapshot();
      await page.locator('#firstListenPlayBtn').click();
      await page.waitForFunction(() => (
        _firstListenUi.dispatch === 'accepted'
          && !_firstListenUi.busy
          && window.__firstListenStationMedia.playing
      ));
      await page.locator('#firstListenHeardBtn').click();
      await page.waitForFunction(() => _firstListenUi.verification === 'heard' && !_firstListenUi.busy);
      if(home){
        await page.locator('#firstListenMakeYoursBtn').click();
        await page.locator('#firstListenConnectionNext').click();
        await page.locator('#firstListenConnectionNext').click();
      }
      const started = await stationMediaSnapshot();
      assert(started.src?.endsWith('/stream?first_listen=1'), `First Listen opened the wrong stream: ${started.src}`);
      assert(started.activeSrc === started.src, 'instrumented station connection does not own the audio element source');
      assert(started.streamRequests.length === before.streamRequests.length + 1, 'sound check did not open exactly one station stream');
      assert(started.playing, 'sound confirmation paused the station');
      return {
        eventIndex: started.events.length,
        requestCount: started.streamRequests.length,
        src: started.src,
      };
    };

    const assertStationPreserved = async (checkpoint, label, { playing = true } = {}) => {
      const state = await stationMediaSnapshot();
      const destructive = state.events.slice(checkpoint.eventIndex).filter(({ type }) => (
        type === 'remove-src' || type === 'load' || type === 'pause'
      ));
      assert(state.src === checkpoint.src, `${label} replaced or cleared the station source`);
      assert(state.activeSrc === (checkpoint.src||''), `${label} released the station connection`);
      assert(state.streamRequests.length === checkpoint.requestCount, `${label} opened a second station stream`);
      assert(destructive.length === 0, `${label} tore down the station: ${JSON.stringify(destructive)}`);
      assert(state.playing === playing, `${label} left station playing=${state.playing}`);
      return state;
    };

    const prepareOwnedStation = async ({ showSuccess = false, sourceOptions = {} } = {}) => {
      await resetUi(setupProjection({ audio: true, ...sourceOptions }), audioReadyOverrides());
      const before = await stationMediaSnapshot();
      await page.evaluate(async ({ success }) => {
        if (success) {
          document.body.dataset.firstListenEntry = 'completing';
          _firstListenUi.showSuccess = true;
          syncFirstListenSetupMount();
          updateFirstListenSuccess();
        }
        const station = document.getElementById('firstListenStationAudio');
        _firstListenPlayback.intent=true;
        await ensureFirstListenMix(_firstListenPlayback.epoch);
        station.src = `${_base}${FIRST_LISTEN_STREAM}`;
        await station.play();
      }, { success: showSuccess });
      await page.waitForFunction(() => window.__firstListenStationMedia.playing);
      const started = await stationMediaSnapshot();
      assert(started.streamRequests.length === before.streamRequests.length + 1, 'owned-station fixture did not open one stream');
      return {
        eventIndex: started.events.length,
        requestCount: started.streamRequests.length,
        src: started.src,
      };
    };
    assert(await page.evaluate(()=>!__stationMediaHandlers.play&&!__stationMediaHandlers.pause),'welcome-only playback claimed station headset controls');
    await page.evaluate(()=>{
      if(!('mediaSession' in navigator))return;
      const install=navigator.mediaSession.setActionHandler;
      try{
        navigator.mediaSession.setActionHandler=(action,handler)=>{if(action==='stop')throw new Error('unsupported action');install(action,handler);};
        _firstListenPlayback.phase='paused';renderFirstListenPlayer();
        _firstListenPlayback.phase='idle';renderFirstListenPlayer();
      }finally{navigator.mediaSession.setActionHandler=install;}
    });
    assert(await page.evaluate(()=>!__stationMediaHandlers.play&&!__stationMediaHandlers.pause),'partial MediaSession support leaked station controls into welcome');

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

    await resetUi(setupProjection({sources:false}));
    await page.evaluate(()=>_firstListenUi.projection=null);nextSetupFailure='network';
    await page.evaluate(()=>refreshSlow());
    assert(await page.evaluate(()=>!firstListenProjection().legacy&&journeySurface.dataset.currentStep==='1'&&firstListenPlayBtn.disabled&&firstListenPrivacyBody.inert&&!setupAccessError.hidden),'first polling failure fabricated saved progress');
    for(const failure of ['network','timeout','invalid-json','http','http-json','empty','malformed','render-error']){
      setupStatusProjection=setupProjection({audio:true});
      await resetUi(setupStatusProjection,audioReadyOverrides());
      await page.evaluate(()=>refreshSlow());
      await page.locator('#firstListenKeepOffBtn').focus();
      const before=await page.evaluate(()=>JSON.stringify({projection:_firstListenUi.projection,progress:firstListenProgressLine.textContent,focus:document.activeElement?.id}));
      nextSetupFailure=failure;
      if(failure==='timeout')timeoutSetupGate=responseGate();
      try{
        await page.evaluate(async(failure)=>{
          const original=window.setTimeout;
          window.setTimeout=(fn,ms,...args)=>original(fn,failure==='timeout'&&ms===SLOW_POLL_DEADLINE_MS?50:ms,...args);
          try{await refreshSlow();}finally{window.setTimeout=original;}
        },failure);
      }finally{timeoutSetupGate?.release();timeoutSetupGate=null;}
      assert(await page.locator('#setupAccessError').isVisible(), `${failure} polling failure was silent`);
      if(failure==='render-error'){nextSetupFailure=failure;await page.evaluate(()=>refreshSlow());assert(await page.locator('#setupAccessError').isVisible(),'repeated malformed setup response hid its error');}
      assert((await page.locator('#setupAccessError').innerText()).includes('reload this page'), `${failure} polling failure lost concrete recovery action`);
      assert((await page.locator('#firstListenSourceChip').innerText()) === 'COULDN’T CHECK MUSIC', `${failure} polling failure kept stale readiness`);
      assert((await page.locator('#firstListenSourceSummary').textContent()).includes('last music details'), `${failure} still promised music from stale source details`);
      assert(before===await page.evaluate(()=>JSON.stringify({projection:_firstListenUi.projection,progress:firstListenProgressLine.textContent,focus:document.activeElement?.id})), `${failure} polling failure changed saved progress or focus`);
      failCapabilities=true;
      try{await page.evaluate(()=>refreshSlow());}finally{failCapabilities=false;}
      assert(await page.locator('#setupAccessError').isHidden(), `${failure} successful polling kept stale error`);
      assert((await page.locator('#firstListenSourceChip').innerText()) === 'MUSIC IS READY', `${failure} unchanged setup response failed to restore readiness`);
      assert(!(await page.locator('#firstListenSourceSummary').textContent()).includes('last music details'), `${failure} recovery kept stale source copy`);
    }

    for(const kind of ['preview','privacy']){
      await resetUi(setupProjection({audio:true,privacy:kind==='preview',privacyEnabled:kind==='preview'}),audioReadyOverrides());
      await page.evaluate(async(kind)=>{
        const fetchOriginal=window.fetch,timerOriginal=window.setTimeout;
        window.setTimeout=(fn,ms,...args)=>timerOriginal(fn,ms===FIRST_LISTEN_TIMEOUTS[kind]?25:ms,...args);
        window.fetch=(url,options)=>String(url).includes(kind==='preview'?'home-context-preview':'home-context-choice')?new Promise((resolve,reject)=>options.signal?.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')))):fetchOriginal(url,options);
        try{if(kind==='preview')await loadHomeContextPreview();else await chooseFirstListenPrivacy(false);}finally{window.fetch=fetchOriginal;window.setTimeout=timerOriginal;}
      },kind);
      assert(await page.evaluate(()=>!_firstListenUi.privacySaving&&_firstListenUi.privacyPreview!=='previewing'&&!_firstListenUi.showSuccess),'hung privacy request froze the journey');
      assert(await page.locator('#firstListenPrivacyStatus').getAttribute('data-tone')==='blocked','privacy deadline lost its recovery message');
      if(kind==='privacy')await assertUnfinished('firstListenPrivacyStep','firstListenPreviewBtn');
      else assert(await page.evaluate(()=>firstListenProjection().privacyEnabled)&&!(await page.locator('#firstListenPrivacyStatus').innerText()).includes('stays off'),'failed preview contradicted the active sharing choice');
    }

    for(const reviewing of [false,true])for(const responseFailure of ['headers','body','invalid'])for(const pendingReceipt of [false,true])for(const enabled of [true,false]){
      await resetUi(setupProjection({audio:true,privacy:reviewing&&!pendingReceipt,privacyEnabled:!enabled}),{...audioReadyOverrides(),privacyReceiptChoice:pendingReceipt?!enabled:null});
      setupStatusProjection=setupProjection({audio:true,privacy:true,privacyEnabled:enabled});
      await page.evaluate(async({enabled,projection,responseFailure,reviewing})=>{
        const fetchOriginal=window.fetch,timerOriginal=window.setTimeout;
        _firstListenUi.reviewStep=reviewing?'privacy':'';_firstListenUi.privacyPreviewValid=true;
        window.setTimeout=(fn,ms,...args)=>timerOriginal.call(window,fn,ms===FIRST_LISTEN_TIMEOUTS.privacy?25:ms,...args);
        window.fetch=(url,options)=>{
          if(!String(url).includes('home-context-choice'))return fetchOriginal(url,options);
          renderSetup(projection);
          const lostResponse=new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));
          return responseFailure==='headers'?lostResponse:Promise.resolve({ok:true,status:200,json:()=>responseFailure==='invalid'?Promise.reject(new SyntaxError('Invalid JSON')):lostResponse});
        };
        try{await chooseFirstListenPrivacy(enabled);}finally{window.fetch=fetchOriginal;window.setTimeout=timerOriginal;}
      },{enabled,projection:setupStatusProjection,responseFailure,reviewing});
      assert(await page.locator('#firstListenPrivacyStatus').isVisible(),'fresh lost save hid its recovery action');
      assert((await page.locator('#firstListenPrivacyStatus').innerText()).includes('may already have reached'),'lost save response falsely claimed privacy stayed off');
      assert(await page.evaluate(()=>firstListenProjection().privacyEnabled)===enabled,'lost privacy response hid the saved server choice');
      assert(await page.evaluate(()=>_firstListenUi.privacyReceiptChoice===null&&!_firstListenUi.privacyPreviewValid),'lost response retained an earlier privacy receipt or consent preview');
      await page.evaluate(()=>refreshSlow());
      assert(await page.evaluate(()=>_firstListenUi.privacyChoice)===enabled,'identical poll did not reconcile privacy after a timed-out save');
      assert((await page.locator('#firstListenPrivacyChip').innerText()).includes(enabled?'HOME CONTEXT ON':'STAYS OFF'),'privacy summary contradicted saved server state');
    }

    const initialJourneyProjection = setupProjection();
    initialJourneyProjection.guided_setup.source_readiness.advanced = {
      kind: 'custom_rotation', label: 'Custom rotation', status: 'configured_unchecked',
    };
    await resetUi(initialJourneyProjection);
    assert(await page.locator('#tab-setup').getAttribute('aria-selected') === 'true', 'fresh install did not land on First Listen');
    assert(await page.locator('#first-listen-panel').isVisible(), 'First Listen is not the primary setup surface');
    assert(await page.locator('#journeySurface').isVisible(), 'calm First Listen surface is missing');
    assert(await page.locator('#firstListenPath > .first-listen-step').count() === 3, 'required First Listen path is not three stages');
    assert(await page.locator('#firstListenAiStep').evaluate((el) => el.closest('#firstListenPath') === null), 'optional AI stayed inside required progress');
    assert(await page.locator('.first-listen-step').count() === 4, 'optional AI left the First Listen surface');
    assert((await page.locator('#firstListenOptionalHeading').textContent()).trim() === 'Your ongoing show', 'optional AI lost its section label');
    assert((await page.locator('#firstListenProgressLine').innerText()).includes('Step 1 of 3'), 'required progress should open on the first interactive step');
    assert((await page.locator('#firstListenProgressLine').innerText()).includes('of 3'), 'required progress is not Step N of 3');
    assert((await page.locator('#firstListenSourceChip').innerText()) === 'MUSIC IS READY', 'readiness heading drifted from music continuity');
    assert(await page.locator('#firstListenSourceStep .first-listen-number, #firstListenSourceStep[aria-current="step"]').count() === 0, 'readiness received numbered human progress');
    const sourcePreview = page.locator('#firstListenSourcePreviewDetails');
    const sourceReview = sourcePreview.locator('> summary');
    assert(await sourcePreview.isVisible(), 'native music readiness disclosure is missing');
    assert(!(await sourcePreview.evaluate((element) => element.open)), 'healthy source preview expanded itself without being asked');
    const sourceStepCopy = await page.locator('#firstListenSourceSummary').textContent();
    assert(sourceStepCopy.includes('Music will continue'), `readiness overpromised primary music readiness: ${sourceStepCopy}`);
    const sourcePreviewSummaryBox = await sourcePreview.locator('> summary').boundingBox();
    assert(sourcePreviewSummaryBox?.height >= 43.5, `source preview summary fell below 44px: ${JSON.stringify(sourcePreviewSummaryBox)}`);
    await sourcePreview.locator('> summary').click();
    assert(await page.locator('#firstListenSourcePreview .first-listen-source-row:visible').count() === 1, 'healthy readiness made optional sources look required');
    const otherSources = sourcePreview.locator('.first-listen-other-sources');
    await otherSources.locator('> summary').click();
    assert(await otherSources.getByRole('button', { name: 'Open music source setup' }).isEnabled(), 'optional music sources have no available setup action');
    await page.evaluate(() => renderFirstListenSources(_firstListenUi.projection.guided_setup.source_readiness));
    assert(await otherSources.evaluate(element => element.open), 'music polling closed the other-sources disclosure');
    assert(await page.locator('#firstListenHomeAssistantGuide > summary').evaluate(element => getComputedStyle(element,'::before').content.includes('›')), 'expandable help lost its disclosure arrow');
    const sourcePreviewCopy = (await page.locator('#firstListenSourcePreview').innerText()).replace(/\s+/g, ' ');
    assert(
      sourcePreviewCopy.includes('Live charts') && !sourcePreviewCopy.includes('Recovery cover')
        && !sourcePreviewCopy.includes('Checking what can play'),
      `source preview did not render the known sources: ${sourcePreviewCopy}`,
    );
    assert(
      !/proves transport|Configured · not checked|Candidates only|Cover only|Not bundled/.test(sourcePreviewCopy),
      `required journey leaked machine readiness vocabulary: ${sourcePreviewCopy}`,
    );
    assert(
      sourcePreviewCopy.includes('Optional. Open music source setup to turn it on.')
        && sourcePreviewCopy.includes('No local songs were found. Add MP3 files to the station’s music folder.')
        && !sourcePreviewCopy.includes('Bundled demo music')
        && !sourcePreviewCopy.includes('Add your own, or use live charts.'),
      `source preview lost setup-specific guidance: ${sourcePreviewCopy}`,
    );
    const plainStatusLabels = await page.locator('#firstListenSourcePreview .status-chip').evaluateAll((chips) => chips.map((chip) => ({
      ariaLabel: chip.getAttribute('aria-label'),
      source: chip.closest('.first-listen-source-row')?.querySelector('.first-listen-source-name')?.textContent?.trim(),
      text: chip.textContent?.trim(),
      title: chip.getAttribute('title'),
    })));
    assert(
      plainStatusLabels.length === 3
        && plainStatusLabels.every(({ ariaLabel, source, text, title }) => ariaLabel === `${source}: ${text}` && title === text),
      `plain source chips exposed internal states: ${JSON.stringify(plainStatusLabels)}`,
    );
    assert(await page.locator('#firstListenSpeakerChip').evaluate(element => {
      const style = getComputedStyle(element);
      return element.tagName === 'SPAN' && style.borderTopWidth === '0px' && style.backgroundColor === 'rgba(0, 0, 0, 0)';
    }), 'noninteractive step status still looks like a button');
    await sourceReview.click();
    assert(await page.locator('#firstListenSourceBody').isHidden(), 'music details stayed open after disclosure closed');
    const speakerHelp = await page.locator('#firstListenSpeakerBody .use-copy').innerText();
    assert(
      speakerHelp.includes('Hearing it here is enough to finish setup') && !speakerHelp.includes('HACS'),
      'step 1 lost its plain-language local completion path',
    );
    const homeAssistantGuide = page.locator('#firstListenHomeAssistantGuide');
    const homeAssistantGuideSummary = homeAssistantGuide.locator('> summary');
    assert(!(await homeAssistantGuide.evaluate((element) => element.open)), 'optional Home Assistant guide opened before the operator asked');
    const homeAssistantGuideSummaryBox = await homeAssistantGuideSummary.boundingBox();
    assert(homeAssistantGuideSummaryBox?.height >= 43.5, `Home Assistant guide summary fell below 44px: ${JSON.stringify(homeAssistantGuideSummaryBox)}`);
    const homeAssistantGuideCopy = (await homeAssistantGuide.textContent()).replace(/\s+/g, ' ');
    assert(
      homeAssistantGuideCopy.includes('Home Assistant Community Store (HACS)')
        && homeAssistantGuideCopy.includes('optional Mamma Mi Radio connection')
        && homeAssistantGuideCopy.includes('Custom repositories')
        && homeAssistantGuideCopy.includes('choose Integration as the category')
        && homeAssistantGuideCopy.includes('Restart Home Assistant')
        && homeAssistantGuideCopy.includes('Settings → Devices & Services → Add Integration → Mamma Mi Radio')
        && homeAssistantGuideCopy.includes('Media → Mamma Mi Radio → Mamma Mi Radio Live'),
      'optional Home Assistant guide lost an actionable installation or playback step',
    );
    assert(
      (await homeAssistantGuide.locator('a').getAttribute('href')) === 'https://github.com/florianhorner/mammamiradio/blob/main/docs/integrations/ha-integration.md#install-the-hacs-integration-for-ha-native-playback',
      'optional Home Assistant guide lost its canonical setup link',
    );
    await homeAssistantGuideSummary.click();
    assert(await homeAssistantGuide.evaluate((element) => element.open), 'optional Home Assistant guide did not open on request');
    await homeAssistantGuideSummary.click();
    assert(!(await homeAssistantGuide.evaluate((element) => element.open)), 'optional Home Assistant guide did not close on request');
    assert((await page.locator('#firstListenPlayBtn').innerText()) === 'Play my station', 'primary playback action is not Play my station');
    const welcomeManifest=await page.evaluate(async()=>{const pack=await (await fetch(_base+'/static/audio/spoken_assets.json')).json();return pack.assets.find(asset=>asset.path==='first_listen/welcome.mp3');});
    assert((await page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play').innerText()) === 'Take your seat', 'welcome preview copy drifted');
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

    await page.locator('.first-listen-station-controls').click();await page.locator('#tab-rotazione').click();
    await page.evaluate(()=>{_st.jamendo={enabled:false,state:'needs_config',client_id_configured:true,client_id_source:'bundled',shared_access_available:true,noncommercial_acknowledged:false};renderJamendoStatus(_st.jamendo)});
    await page.locator('#jamendoSourceActions button').click();
    await page.waitForFunction(() => document.body.dataset.firstListenSetupView === 'music-sources' && document.activeElement?.id === 'jamendoSetupHeading');assert(await page.locator('#setupMusicSources').isVisible() && await page.locator('#journeySurface').isHidden(), 'Jamendo setup did not replace the required journey');
    const jamendoClear=page.locator('#jamendoClearClientId');assert(await jamendoClear.count()===1&&await jamendoClear.isHidden(), 'Clear remained visible for bundled access');
    await page.locator('.first-listen-station-controls').click();await page.locator('#tab-setup').click();
    assert(await page.locator('#journeySurface').isVisible() && await page.locator('#setupMusicSources').isHidden(), 'Setup tab did not restore First Listen');

    const welcomeGuide = page.locator('.guide-audio[data-guide="welcome"]');
    const welcomeGuideButton = welcomeGuide.locator('.guide-audio-play');
    const welcomeRequestBaseline = guideAudioRequests.length;
    assert(await welcomeGuideButton.innerText() === 'Take your seat', 'the first action stopped inviting the listener');
    const welcomeProof = {resume:resumeRequests.length,verify:verifyRequests.length,privacy:privacyRequests.length};
    await welcomeGuideButton.focus();
    await welcomeGuideButton.press('Enter');
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing');
    assertGuideRequests(welcomeRequestBaseline,'welcome');
    assert((await welcomeGuideButton.innerText()) === 'Pause welcome', 'welcome guide did not expose pause after playback started');
    await welcomeGuideButton.press('Enter');
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'paused');
    assert((await welcomeGuideButton.innerText()) === 'Continue welcome', 'welcome guide did not expose resume after pause');
    await welcomeGuideButton.press('Enter');
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing');
    await page.locator('#firstListenGuideAudio').evaluate((audio) => audio.dispatchEvent(new Event('ended')));
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'ended');
    assert((await welcomeGuideButton.innerText()) === 'Hear it again', 'ended guide did not expose replay');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, 'ended guide retained its audio URL');
    assert(await page.locator('.invitation-next').isVisible(), 'welcome ended without naming the next act');
    assert(await welcomeGuideButton.evaluate(button => document.activeElement === button), 'welcome ending moved keyboard focus');
    assert(await page.locator('.invitation-stage').isHidden(), 'the ended welcome still displaced the station controls');
    assert(resumeRequests.length===welcomeProof.resume&&verifyRequests.length===welcomeProof.verify&&privacyRequests.length===welcomeProof.privacy, 'welcome playback advanced station or consent proof');
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(()=>_firstListenUi.dispatch==='accepted'&&!_firstListenUi.busy);
    assert(await page.locator('.invitation-next').isHidden(), 'the welcome still asked to start an already-playing station');



    const soundGuide = page.locator('#firstListenVerifyBody .guide-audio[data-guide="sound-check"]');
    failNextGuideKey = 'sound-check';
    await resetUi(setupProjection(), { dispatch: 'accepted' });
    const soundGuideButton = soundGuide.locator('.guide-audio-play');
    const soundGuideRequestBaseline = guideAudioRequests.length;
    await soundGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'error');
    assertGuideRequests(soundGuideRequestBaseline,'sound-check');
    assert((await soundGuideButton.innerText()) === 'Try explanation again', 'failed guide did not expose retry');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, 'failed guide retained its terminal audio URL');
    failNextGuideKey = '';
    const soundGuideRetryBaseline = guideAudioRequests.length;
    await soundGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'playing');
    assertGuideRequests(soundGuideRetryBaseline,'sound-check');
    await page.evaluate(()=>{stopFirstListenGuide();renderFirstListenProgress();});
    await page.waitForFunction(()=>!_firstListenUi.guideKey&&!firstListenGuideLocksRoomProof(),null,{timeout:1000});
    await soundGuideButton.click();
    await page.waitForFunction(()=>document.querySelector('[data-guide="sound-check"]').dataset.state==='playing');

    await soundGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="sound-check"]')?.dataset.state === 'paused');
    await welcomeGuideButton.click();
    await page.waitForFunction(() => document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing');
    assert(await soundGuide.getAttribute('data-state') === 'idle', 'switching clips did not reset the previous guide');
    assert(await soundGuideButton.innerText() === 'Hear why we ask', 'switching clips lost the previous idle label');
    assert((await page.locator('#firstListenGuideAudio').getAttribute('src')).includes('/welcome.mp3?'), 'switching clips did not load the new source');

    await resetUi(setupProjection({ primary: 'unavailable', recovery: 'cover_only' }));
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    assert((await page.locator('#firstListenSourceChip').innerText()) === 'MUSIC NEEDS ATTENTION', 'degraded source lost its honest runtime status');
    assert((await page.locator('#firstListenSourceSummary').innerText()).includes('Backup audio'), 'degraded source did not explain what the listener gets');
    assert((await page.locator('#firstListenSourceRepair').innerText()).includes('continue'), 'degraded source blocked an otherwise usable First Listen');
    assert(await sourcePreview.evaluate((element) => element.open), 'degraded source preview did not open on the health transition');
    const degradedPreviewCopy = (await page.locator('#firstListenSourcePreview').innerText()).replace(/\s+/g, ' ');
    assert(
      !/proves transport|Configured · not checked|Candidates only|Cover only|Not bundled|Transport cover only|No bundled library/.test(degradedPreviewCopy),
      `degraded source preview leaked machine readiness vocabulary: ${degradedPreviewCopy}`,
    );
    assert(
      degradedPreviewCopy.includes('Backup audio only. It keeps the station on while real music is found.')
        && !degradedPreviewCopy.includes('Live charts') && degradedPreviewCopy.includes('Add MP3 files'),
      `degraded source preview lost its plain-language way out: ${degradedPreviewCopy}`,
    );
    await sourceReview.click();
    assert(!(await sourcePreview.evaluate((element) => element.open)), 'operator could not close the degraded source preview');
    await page.evaluate(() => renderFirstListenProgress());
    assert(!(await sourcePreview.evaluate((element) => element.open)), 'routine refresh reopened the source preview after the operator closed it');
    const announcementChanges=await page.evaluate(()=>{
      const observer=new MutationObserver(()=>{});
      observer.observe(document.getElementById('firstListenSourceChip'),{childList:true,characterData:true,subtree:true});
      renderFirstListenProgress();
      const changes=observer.takeRecords().length;observer.disconnect();return changes;
    });
    assert(announcementChanges===0, 'unchanged readiness repeated its live announcement');
    await sourceReview.click();

    await resetUi(setupProjection({ primary: 'unavailable', recovery: 'on_air' }));
    assert((await page.locator('#firstListenSourceChip').innerText()) === 'MUSIC NEEDS ATTENTION', 'recovery audio was mistaken for a healthy primary source');
    const recoveryPreviewCopy = (await page.locator('#firstListenSourcePreview').innerText()).replace(/\s+/g, ' ');
    assert(
      recoveryPreviewCopy.includes('Backup audio is playing, so the station stays on.')
        && recoveryPreviewCopy.includes('open music source setup')
        && !recoveryPreviewCopy.includes('open music source tools')
        && !recoveryPreviewCopy.includes('proves transport'),
      `recovery-on-air preview still speaks machine: ${recoveryPreviewCopy}`,
    );
    const sourceFallbackProjection = setupProjection({ recovery: 'not_bundled' });
    sourceFallbackProjection.guided_setup.source_readiness.rows.find((row) => row.kind === 'jamendo').status = 'candidates_only';
    await resetUi(sourceFallbackProjection);
    const candidatesRow = page.locator('#firstListenSourcePreview [data-source-kind="jamendo"]');
    const notBundledRecoveryRow = page.locator('#firstListenSourcePreview [data-source-kind="recovery"]');
    const sourceFallbackMatrixCopy = {
      candidatesDetail: await candidatesRow.locator('.first-listen-source-detail').textContent(),
      candidatesLabel: await candidatesRow.locator('.status-chip').textContent(),
      recoveryRows: await notBundledRecoveryRow.count(),
    };
    assert(
      sourceFallbackMatrixCopy.candidatesDetail === 'Songs found. Still listening to check they play cleanly.'
        && sourceFallbackMatrixCopy.candidatesLabel === 'Almost ready'
        && sourceFallbackMatrixCopy.recoveryRows === 0,
      `source fallback matrix lost its plain-language copy: ${JSON.stringify(sourceFallbackMatrixCopy)}`,
    );

    const originalRejectEnable=rejectNextEnable;rejectNextEnable=false;
    for(const [pendingSave,enabled] of [[true,false],[true,true],[false,false]]){
      smokeStage=`completion-poll-${pendingSave}-${enabled}`;
      const checkpoint=await prepareOwnedStation();
      const gate=responseGate();
      if(pendingSave){
        if(enabled){await page.locator('#firstListenPreviewBtn').click();await page.waitForFunction(()=>_firstListenUi.privacyPreviewValid);}
        privacyResponseGate=gate;
        await page.locator(enabled?'#firstListenEnableContextBtn':'#firstListenKeepOffBtn').click();await gate.arrived;
        const requests=privacyRequests.length;
        await page.evaluate(enabled=>chooseFirstListenPrivacy(enabled),enabled);
        assert(privacyRequests.length===requests,'pending privacy choice submitted twice');
        const resumes=resumeRequests.length;
        await page.locator('[data-review-step="verify"]').click();
        assert(await page.locator('#firstListenRetestBtn').isDisabled(),'pending privacy save allowed a retest');
        await page.evaluate(async()=>{retestFirstListenSpeaker();await startFirstListen();});
        assert(resumeRequests.length===resumes&&await page.evaluate(()=>!_firstListenUi.retestPending&&firstListenProjection().heard),'pending privacy save invalidated hearing proof');
      }else await page.locator('#tab-setup').focus();
      const focus=await page.evaluate(()=>document.activeElement?.id);
      setupStatusProjection=setupProjection({audio:true,privacy:true,privacyEnabled:enabled});
      await page.evaluate(()=>refreshSlow());
      assert(await page.evaluate(()=>_activeTab==='setup'),'completion polling navigated away from the show');
      assert(await page.evaluate(()=>document.activeElement?.id)===focus,`completion polling moved keyboard focus (pending save: ${pendingSave})`);
      await assertStationPreserved(checkpoint,'completion polling');
      if(pendingSave){
        gate.release();
        await page.waitForFunction(()=>_firstListenUi.showSuccess&&!_firstListenUi.privacySaving);
        await assertStationPreserved(checkpoint,'privacy response after completion polling');
      }
    }
    smokeStage='privacy-save-loses-continuity';
    const pendingContinuity=await prepareOwnedStation(), continuityGate=responseGate();
    privacyResponseGate=continuityGate;
    await page.locator('#firstListenKeepOffBtn').click();await continuityGate.arrived;
    setupStatusProjection=setupProjection({audio:true,privacy:true,primary:'unavailable',recovery:'unavailable'});
    await page.evaluate(()=>refreshSlow());
    await assertStationPreserved(pendingContinuity,'continuity lost during privacy save');
    continuityGate.release();
    await page.waitForFunction(()=>!_firstListenUi.privacySaving);
    assert(await page.evaluate(()=>firstListenProjection().privacyReviewed&&!_firstListenUi.showSuccess),'lost continuity discarded privacy or celebrated');
    assert(await page.locator('#firstListenRepairMusicBtn').isVisible(),'lost continuity hid music repair');
    await assertStationPreserved(pendingContinuity,'privacy receipt with unavailable continuity');
    rejectNextEnable=originalRejectEnable;

    for(const completedPoll of [false,true]){smokeStage=`repair-completion-${completedPoll}`;
      const repairStation=await prepareOwnedStation({sourceOptions:{primary:'unavailable',recovery:'cover_only'}});
      const gate=responseGate();privacyResponseGate=gate;
      await page.locator('#firstListenKeepOffBtn').click();await gate.arrived;
      await page.locator('#firstListenRepairMusicBtn').click();
      await page.waitForFunction(()=>document.activeElement?.id==='jamendoSetupHeading');
      await page.locator('#jamendoEnabled').focus();
      if(completedPoll){setupStatusProjection=setupProjection({audio:true,privacy:true,primary:'unavailable'});await page.evaluate(()=>refreshSlow());}
      assert(await page.locator('#setupMusicSources').isVisible(),'completion poll swallowed the music repair view');
      assert(await page.evaluate(()=>document.activeElement?.id)==='jamendoEnabled','completion poll moved focus in music repair');
      gate.release();await page.waitForFunction(()=>!_firstListenUi.privacySaving);
      assert(await page.locator('#setupMusicSources').isVisible()&&await page.locator('#firstListenSuccess').isHidden(),'late privacy response swallowed the music repair view');
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      assert(await page.evaluate(()=>document.activeElement?.id)==='jamendoEnabled','late privacy response moved focus in music repair');
      await assertStationPreserved(repairStation,'music repair completion');
    }

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
    await assertUnfinished('firstListenPrivacyStep', 'firstListenPreviewBtn');
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.showSuccess === true && !_firstListenUi.privacySaving);
    assert(await page.locator('#firstListenSuccess').isVisible(), 'backup audio did not allow First Listen to complete');
    const successHandoff=await page.evaluate(()=>({entry:document.body.dataset.firstListenEntry,tab:_activeTab,owned:!document.getElementById('tab-setup').hidden&&document.getElementById('tab-setup').getAttribute('aria-selected')==='true',clean:['.producer-clock','.mmr-console','.mmr-deck','#setupMusicSources'].every((s)=>!document.querySelector(s)?.getClientRects().length),panel:Boolean(document.getElementById('first-listen-panel').getClientRects().length),mount:firstListenSetupContext.parentElement?.id,focus:document.activeElement?.id}));
    assert(successHandoff.entry==='completing'&&successHandoff.tab==='setup'&&successHandoff.owned&&successHandoff.clean&&successHandoff.panel&&successHandoff.mount==='firstListenPanelMount'&&successHandoff.focus==='firstListenSuccessTitle',`completion did not hold its one-time success surface: ${JSON.stringify(successHandoff)}`);
    assert((await page.locator('#firstListenSuccessCopy').innerText()).includes('Backup audio is keeping the station playing'), 'backup completion hid the continuity explanation');
    assert((await page.locator('#firstListenSuccessCopy').innerText()).includes('primary music still needs attention'), 'backup completion hid the repair follow-up');
    assert(await page.locator('#firstListenSuccessRepair').isVisible(), 'backup completion hid its primary-music repair action');
    assert((await page.locator('#firstListenSuccess .btn-trigger').innerText()) === 'Listen to the station', 'success page lost its listener destination');
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
    await page.locator('.success-saved > summary').click();
    await page.getByRole('button', { name: 'Review choices' }).click();
    await page.waitForFunction(() => document.activeElement?.getAttribute('data-review-step') === 'privacy');
    const reviewHandoff=await page.evaluate(()=>({entry:document.body.dataset.firstListenEntry,tab:_activeTab,hidden:document.getElementById('tab-setup').hidden,inMotore:Boolean(firstListenSetupContext.closest('#drawer-diagnostics')),guide:document.getElementById('firstListenGuideAudio').getAttribute('src'),station:document.getElementById('firstListenStationAudio').getAttribute('src')}));
    assert(reviewHandoff.entry==='complete'&&reviewHandoff.tab==='motore'&&reviewHandoff.hidden&&reviewHandoff.inMotore&&reviewHandoff.guide===null&&reviewHandoff.station==='smoke-station.mp3',`success did not finalize into Motore safely: ${JSON.stringify(reviewHandoff)}`);

    const noContinuity = setupProjection({ primary: 'unavailable', recovery: 'unavailable', audio: true });
    setupStatusProjection = noContinuity;
    await resetUi(noContinuity, audioReadyOverrides());
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
    assert(await page.locator('#firstListenSuccess').isHidden(), 'missing continuity falsely exposed the success screen');
    assert(await page.locator('#firstListenPath [aria-current="step"]').count() === 0, 'music repair reset saved human progress');
    assert((await page.locator('#firstListenProgressLine').innerText()).includes('choices are saved. Music still needs attention.'), 'music repair falsely claimed First Listen complete');
    assert(await page.locator('#firstListenRepairMusicBtn').isVisible(), 'music repair lost its action after all three choices were saved');
    const addOnRecoveryUnavailable = await page.locator('#firstListenSourcePreview [data-source-kind="live_charts"], #firstListenSourcePreview [data-source-kind="recovery"]').count();
    assert(
      addOnRecoveryUnavailable === 0 && (await page.locator('#firstListenRepairMusicBtn').innerText()) === 'Open music source setup',
      `add-on unavailable source exposed the wrong repair path: ${addOnRecoveryUnavailable}`,
    );
    await page.locator('#firstListenRepairMusicBtn').click();
    await page.waitForFunction(() => (
      document.body.dataset.firstListenSetupView === 'music-sources'
        && document.activeElement?.id === 'jamendoSetupHeading'
    ));
    assert(
      await page.locator('#setupMusicSources').isVisible() && await page.locator('#journeySurface').isHidden(),
      'add-on repair action did not open an available music-source setup path',
    );
    await page.locator('.first-listen-station-controls').click();
    await page.locator('#tab-setup').click();
    assert(
      await page.locator('#journeySurface').isVisible() && await page.locator('#setupMusicSources').isHidden(),
      'Setup tab did not restore First Listen after source repair',
    );
    await page.evaluate(() => {
      _caps = null;
      _capsState = 'error';
      renderFirstListenProgress();
    });
    const erroredChartDetail = await page.locator('#firstListenSourcePreview [data-source-kind="live_charts"]').count();
    assert(
      erroredChartDetail === 0,
      `capability error fabricated chart availability: ${erroredChartDetail}`,
    );
    assert((await page.locator('#firstListenRepairMusicBtn').innerText()) === 'Open music source setup', 'capability error exposed unavailable chart tools');
    await page.evaluate(() => {
      _caps = { capabilities: { charts_reload: true } };
      _capsState = 'ready';
      updateSourceControls(_st, _caps);
      renderFirstListenProgress();
    });
    assert((await page.locator('#firstListenRepairMusicBtn').innerText()) === 'Open music source tools', 'charts-capable repair lost its library-tools path');
    const chartsRecoveryUnavailable = await page.locator('#firstListenSourcePreview [data-source-kind="live_charts"] .first-listen-source-detail').textContent();
    assert(
      chartsRecoveryUnavailable.includes('Open music source tools') && !chartsRecoveryUnavailable.includes('Open music source setup'),
      `charts-capable unavailable source lost its repair path: ${chartsRecoveryUnavailable}`,
    );
    await page.locator('#firstListenRepairMusicBtn').click();
    await page.waitForFunction(() => (
      _activeTab === 'rotazione'
        && document.getElementById('libraryTools')?.open
        && document.activeElement?.id === 'sourceChartsBtn'
    ));
    assert(await page.locator('#sourceChartsBtn').isVisible(), 'charts-capable repair focused a hidden source control');
    await page.locator('#tab-setup').click();
    await resetUi(setupProjection({ primary: 'unavailable', recovery: 'on_air' }));
    const chartsRecoveryCopy = await page.locator('#firstListenSourcePreview [data-source-kind="recovery"] .first-listen-source-detail').textContent();
    assert(
      chartsRecoveryCopy.includes('Real music still needs a source — open music source tools.')
        && !chartsRecoveryCopy.includes('open music source setup'),
      `charts-capable recovery guidance lost its available repair path: ${chartsRecoveryCopy}`,
    );
    await page.evaluate(() => {
      _caps = { capabilities: {} };
      _capsState = 'ready';
      updateSourceControls(_st, _caps);
      renderFirstListenProgress();
    });

    smokeStage = 'listener-music-options';
    const includedProjection=setupProjection({primary:'not_configured'});
    Object.assign(includedProjection.guided_setup.source_readiness,{healthy:true,current_rotation:{kind:'starter'},advanced:{kind:'custom',label:'Starter crate',status:'playable'}});
    await resetUi(includedProjection);
    const listenerKinds=await page.locator('#firstListenSourcePreview [data-source-kind]').evaluateAll(rows=>rows.map(row=>row.dataset.sourceKind));
    assert(JSON.stringify(listenerKinds)===JSON.stringify(['custom','jamendo','local_music']),'included music exposed impossible or obsolete options');
    assert((await page.locator('#firstListenSourcePreview').textContent()).includes('Included music'),'starter music lost its listener name');
    assert(await page.locator('#firstListenSources [data-source-kind]').count()===6,'listener filtering removed technical source diagnostics');
    for(const capability of [true,false]){
      await page.evaluate(charts=>{_caps={capabilities:{charts_reload:charts}};renderFirstListenProgress();},capability);
      assert(await page.locator('#firstListenSourcePreview [data-source-kind="live_charts"]').count()===(capability?1:0),'chart options ignored actual capability');
    }
    for(const status of ['unavailable','playable']){
      includedProjection.guided_setup.source_readiness.rows.find(row=>row.kind==='jamendo').status=status;
      await resetUi(includedProjection);
      assert((await page.locator('#firstListenSourcePreview [data-source-kind="jamendo"] .status-chip').textContent())===(status==='playable'?'Ready':'Unavailable'),'a failed configured music source was hidden or labelled optional');
    }
    Object.assign(includedProjection.guided_setup.source_readiness.rows.find(row=>row.kind==='local'),{status:'unavailable',candidates:0,playable:0,on_air:false});
    await resetUi(includedProjection);
    for(const scan of [{complete:true,in_progress:false,error:'',files_found:0,active:0},{complete:false,in_progress:false,error:'Scan failed',files_found:0,active:0},{complete:true,in_progress:false,error:'',files_found:2,active:0},{complete:true,in_progress:true,error:'',files_found:0,active:0},{complete:true,in_progress:false,error:''},null]){
      await page.evaluate(scan=>{_st.local_library=scan;renderFirstListenProgress();},scan);
      const empty=scan?.complete===true&&scan.in_progress===false&&!scan.error&&scan.files_found===0&&scan.active===0;
      assert((await page.locator('#firstListenSourcePreview [data-source-kind="local_music"] .status-chip').textContent())===(empty?'Optional':'Unavailable'),'empty-library wording concealed a failed or unknown scan');
    }
    Object.assign(includedProjection.guided_setup.source_readiness.rows.find(row=>row.kind==='local'),{status:'not_configured',attempted:true,failure:'Check music folders'});
    await resetUi(includedProjection);
    await page.evaluate(()=>{_st.local_library={complete:true,in_progress:false,error:'',files_found:0,active:0};renderFirstListenProgress();});
    assert((await page.locator('#firstListenSourcePreview [data-source-kind="local_music"] .status-chip').textContent())==='Unavailable','a missing music folder was labelled optional');
    includedProjection.guided_setup.source_readiness.rows.find(row=>row.kind==='recovery').status='on_air';
    await resetUi(includedProjection);
    assert(await page.locator('#firstListenSourcePreview [data-source-kind="recovery"]').count()===1&&await page.locator('.first-listen-other-sources [data-source-kind="recovery"]').count()===0,'playing backup audio appeared as an addable music option');
    assert(!(await page.locator('#firstListenSourcePreview [data-source-kind="recovery"]').textContent()).includes('still needs a source'),'playing backup contradicted available music');

    await resetUi(setupProjection());
    await assertUnfinished('firstListenSpeakerStep', 'firstListenPlayBtn');
    await page.locator('#firstListenPlayBtn').click();
    await page.waitForFunction(() => _firstListenUi.dispatch === 'accepted' && !_firstListenUi.busy);
    await assertUnfinished('firstListenVerifyStep', 'firstListenHeardBtn');
    assert(await page.evaluate(() => document.activeElement?.id) === 'firstListenVerifyHeading', 'accepted playback did not focus the human sound check');
    assert(await page.locator('#firstListenPrivacyStep').getAttribute('data-state') !== 'current', 'playing event unlocked privacy before a saved Yes');

    await page.locator('#firstListenSoundHelp .guide-audio-play').click();
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
    assert(verifyRequests.at(-1)?.heard === false, 'No sound yet was not recorded explicitly');
    assert(await page.locator('#firstListenRepair').isVisible(), 'No sound yet did not expose contextual recovery');
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
    await assertUnfinished('firstListenPrivacyStep', 'firstListenPreviewBtn');
    const previewBaseline = previewRequests.length;
    const privacyBaseline = privacyRequests.length;
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyChoice === false && !_firstListenUi.privacySaving);
    assert(previewRequests.length === previewBaseline, 'private path requested a Home preview');
    assert(privacyRequests.length === privacyBaseline + 1 && privacyRequests.at(-1).enabled === false, 'private path did not save an explicit false');
    assert(await page.locator('#firstListenSuccess').isVisible(), 'fresh private completion did not reach the success moment');
    assert(await page.locator('#journeySurface').isHidden(), 'success moment left the journey competing on screen');
    assert((await page.locator('#firstListenSuccessPrivacy').textContent()) === 'Home stays private', 'success receipt lost the private choice');
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
    assert((await page.locator('#firstListenSuccessPrivacy').textContent()) === 'Home context is on', 'success receipt lost the enabled choice');

    smokeStage = 'continuous-private-achievement';
    const privateStation = await startAudibleFirstListen();
    await openHomeChoice();
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.showSuccess && !_firstListenUi.privacySaving);
    await assertStationPreserved(privateStation, 'private achievement transition');
    assert(await page.locator('#firstListenSuccess').isVisible(), 'audible private path did not keep achievement inside First Listen');

    const successGuide = page.locator('#firstListenSuccess .guide-audio[data-guide="success"]');
    assert(await page.evaluate(()=>_firstListenUi.successAnnounced),'fresh completion did not announce itself');
    await page.waitForFunction(() => (
      document.querySelector('#firstListenSuccess .guide-audio[data-guide="success"]')?.dataset.state === 'playing'
        && window.__firstListenStationMedia.playing
    ));
    await assertStationPreserved(privateStation, 'success narration duck');
    const celebrationRequests=guideAudioRequests.length;
    await page.evaluate(()=>{updateFirstListenSuccess();renderFirstListenProgress();updateFirstListenSuccess();});
    assert(guideAudioRequests.length===celebrationRequests,'polling replayed the celebration');
    await page.locator('#firstListenGuideAudio').evaluate((audio) => audio.dispatchEvent(new Event('ended')));
    await page.waitForFunction(() => (
      window.__firstListenStationMedia.playing
        && document.querySelector('#firstListenSuccess .guide-audio[data-guide="success"]')?.dataset.state === 'ended'
    ));
    const afterSuccessGuide = await assertStationPreserved(privateStation, 'success narration resume');
    assert(await page.locator('#firstListenSuccess').isVisible(), 'success narration dismissed the achievement');
    for(const [width,height,zoom] of [[320,568,false],[375,812,false],[768,1024,false],[1440,900,false],[320,568,true]]){
      await page.setViewportSize({width,height});
      await page.evaluate(zoom=>{document.documentElement.style.fontSize=zoom?'200%':'';},zoom);
      const geometry=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>innerWidth+1,targets:[...document.querySelectorAll('#firstListenSuccess button,#firstListenSuccess summary')].filter(e=>e.getClientRects().length).map(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}))}));
      assert(!geometry.overflow&&geometry.targets.every(r=>r.width>=44&&r.height>=44),`finale geometry failed at ${width}/${zoom}: ${JSON.stringify(geometry)}`);
    }
    await page.evaluate(()=>document.documentElement.style.fontSize='');await page.setViewportSize({width:1280,height:900});
    const reviewExit = { ...privateStation, eventIndex: afterSuccessGuide.events.length };
    await page.locator('.success-saved > summary').click();
    await page.getByRole('button', { name: 'Review choices' }).click();
    await assertStationPreserved(reviewExit, 'Review choices exit');

    smokeStage='paused-completion';
    await startAudibleFirstListen();
    await page.locator('#firstListenPlayerToggle').click();
    const pausedCelebration=guideAudioRequests.length;
    await openHomeChoice();
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(()=>_firstListenUi.showSuccess&&!_firstListenUi.privacySaving);
    assert(guideAudioRequests.length===pausedCelebration,'paused completion started a recording');
    assert(!(await stationMediaSnapshot()).playing,'paused completion resumed the station');
    assert(!(await page.locator('#firstListenSuccessCopy').innerText()).includes('keeps playing'),'paused completion claimed playback');

    smokeStage = 'failed-privacy-keeps-stream';
    const failedPrivacyStation = await startAudibleFirstListen();
    failNextPrivacyReceipt = true;
    await openHomeChoice();
    await page.locator('#firstListenKeepOffBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyReceiptChoice === false && !_firstListenUi.privacySaving);
    await assertStationPreserved(failedPrivacyStation, 'failed privacy persistence');
    assert(await page.locator('#firstListenSuccess').isHidden(), 'failed privacy persistence exposed achievement');

    smokeStage = 'continuous-enabled-achievement';
    const enabledStation = await startAudibleFirstListen();
    await openHomeChoice();
    await page.locator('#firstListenPreviewBtn').click();
    await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
    rejectNextEnable = false;
    await page.locator('#firstListenEnableContextBtn').click();
    await page.waitForFunction(() => _firstListenUi.showSuccess && !_firstListenUi.privacySaving);
    const enabledSuccess = await assertStationPreserved(enabledStation, 'enabled achievement transition');
    assert((await page.locator('#firstListenSuccessPrivacy').textContent()) === 'Home context is on', 'audible enabled path lost its choice');
    const stationControlsExit = { ...enabledStation, eventIndex: enabledSuccess.events.length };
    await page.locator('#firstListenSuccess').getByRole('button', { name: 'Open station controls' }).click();
    await assertStationPreserved(stationControlsExit, 'Station controls exit');

    smokeStage='guided-connection';
    const connectionStation=await startAudibleFirstListen({home:false});
    await assertUnfinished('firstListenPrivacyStep','firstListenKeepListeningBtn');
    assert(await page.locator('#firstListenConnectionInvite .household-scene').count()===4,'home moments are missing from the invitation');
    assert(await page.locator('#firstListenConnectionInvite .household-example-play').count()===4,'home moments are not playable');
    assert(await page.evaluate(()=>typeof toggleHouseholdExample==='function'),'household examples lost their play helper');
    assert(
      await page.locator('[data-explainer-scenario="quiet"] .day-one-chip').count()===1,
      'day-one moment lost its visible chip',
    );
    await page.locator('[data-household-example="quiet"]').click();
    await page.waitForFunction(()=>_firstListenUi.guideKey==='quiet'&&!firstListenGuideAudio().paused);
    const quietDebug=await page.evaluate(()=>({
      src:firstListenGuideAudio().getAttribute('src'),
      currentSrc:firstListenGuideAudio().currentSrc,
    }));
    assert(
      /\/static\/audio\/home_moments\/quiet\.mp3\?v=02fc7d83734a/.test(quietDebug.src||quietDebug.currentSrc||''),
      `day-one moment loaded the wrong source: ${JSON.stringify(quietDebug)}`,
    );
    await page.locator('[data-household-example="quiet"]').click();
    await page.waitForFunction(()=>firstListenGuideAudio().paused);
    await page.locator('[data-household-example="laundry"]').click();
    await page.waitForFunction(()=>_firstListenUi.guideKey==='laundry'&&!firstListenGuideAudio().paused);
    const laundryDebug=await page.evaluate(()=>({
      src:firstListenGuideAudio().getAttribute('src'),
      currentSrc:firstListenGuideAudio().currentSrc,
    }));
    assert(
      /\/static\/audio\/home_moments\/laundry\.mp3\?v=e7607b0c0566/.test(laundryDebug.src||laundryDebug.currentSrc||''),
      `laundry example loaded the wrong source: ${JSON.stringify(laundryDebug)}`,
    );
    await page.locator('[data-household-example="laundry"]').click();
    await page.waitForFunction(()=>firstListenGuideAudio().paused);
    assert((await page.locator('#firstListenPrivacyHeading').innerText())==='Make it yours','privacy step lost its polished heading');
    assert(await page.locator('[data-guide="free-voices"]').isHidden(),'free audition appeared before voice choice');
    assert(await page.locator('#firstListenSetupDoneBtn').count()===1,'Done with setup is missing');
    assert(await page.locator('#firstListenSetupReturnBtn').count()===1,'Back to setup is missing');
    await page.locator('#firstListenMakeYoursBtn').click();
    assert(await page.locator('[data-guide="ai"] .guide-audio-play').isVisible(),'writing pane hid its recorded voice exchange');
    assert(await page.locator('#firstListenAiStep').getAttribute('aria-current')===null,'connection added a fourth numbered step');
    await page.locator('#firstListenConnectionNext').click();
    assert(await page.locator('[data-guide="free-voices"]').isVisible(),'voice pane hid the free audition');
    const freeBaseline=guideAudioRequests.length;
    await page.locator('[data-guide="free-voices"] .guide-audio-play').click();
    await page.waitForFunction(()=>_firstListenUi.guideKey==='free-voices'&&!firstListenGuideAudio().paused);
    const freeDebug=await page.evaluate(()=>({
      src:firstListenGuideAudio().getAttribute('src'),
      currentSrc:firstListenGuideAudio().currentSrc,
    }));
    assert(
      /\/static\/audio\/voice_examples\/free-voices\.mp3\?v=[0-9a-f]{12}/.test(freeDebug.src||freeDebug.currentSrc||''),
      `free-voices loaded the wrong source: ${JSON.stringify(freeDebug)} requests=${JSON.stringify(guideAudioRequests.slice(freeBaseline))}`,
    );
    if(guideAudioRequests.slice(freeBaseline).length)assertGuideRequests(freeBaseline,'free-voices');
    await page.locator('#firstListenConnectionNext').click();
    await page.waitForFunction(()=>!_firstListenUi.guideKey);
    assert(await page.locator('#firstListenHomeChoice').isVisible(),'voices did not continue to Home');
    await assertStationPreserved(connectionStation,'guided connection');

    smokeStage='live-station-controls';
    const graph=await page.evaluate(()=>{window.__transportOwner={audio:firstListenStationAudio(),source:_firstListenPlayback.source,context:_firstListenPlayback.context};return true;});
    assert(graph,'media owner capture failed');
    for(const [endpoint,handler] of [['/api/skip','doSkip'],['/api/track/ban-now-playing','doBanNowPlaying']]){
      for(const mode of ['success','failure','paused','late-pause']){
        if(!(await stationMediaSnapshot()).playing)await page.locator('#firstListenPlayerToggle').click();
        await page.waitForFunction(()=>window.__firstListenStationMedia.playing);
        if(mode==='paused')await page.locator('#firstListenPlayerToggle').click();
        const baseline=await stationMediaSnapshot(),gate=responseGate();
        const routeHandler=async route=>{gate.arrive();await gate.wait;await fulfillJson(route,{ok:mode!=='failure'},mode==='failure'?503:200);};
        await page.route('**'+endpoint,routeHandler);
        const pending=page.evaluate(name=>window[name](document.getElementById('skipBtn')),handler);
        await gate.arrived;
        if(mode==='late-pause')await page.locator('#firstListenPlayerToggle').click();
        gate.release();await pending;
        if(mode==='success')await page.waitForFunction(()=>window.__firstListenStationMedia.playing);
        const after=await stationMediaSnapshot();
        assert(after.streamRequests.length===baseline.streamRequests.length+(mode==='success'?1:0),`${handler}/${mode} used stale audio or reopened unexpectedly`);
        if(mode==='paused'||mode==='late-pause')assert(!after.playing,`${handler}/${mode} overrode Pause`);
        assert(await page.evaluate(()=>firstListenStationAudio()===__transportOwner.audio&&_firstListenPlayback.source===__transportOwner.source&&_firstListenPlayback.context===__transportOwner.context),'transport replaced the audio owner');
        await page.unroute('**'+endpoint,routeHandler);
      }
    }

    smokeStage='stopped-continue-force-success';
    await page.evaluate(()=>updateStopState(true));
    nextResumeResponse='force_available';nextForceResponse='running';
    const stoppedProof=await page.evaluate(()=>({verification:_firstListenUi.verification,choice:_firstListenUi.privacyChoice}));
    await page.locator('#firstListenPlayerToggle').click();
    await page.waitForFunction(()=>window.__firstListenStationMedia.playing);
    assert((await stationMediaSnapshot()).src.endsWith('first_listen=live'),'stopped Continue did not rejoin the running station');
    assert(JSON.stringify(await page.evaluate(()=>({verification:_firstListenUi.verification,choice:_firstListenUi.privacyChoice})))===JSON.stringify(stoppedProof),'stopped Continue reset saved proof');

    smokeStage = 'explicit-admin-tab-exit';
    const adminTabExit = await prepareOwnedStation();
    await page.evaluate(() => document.getElementById('tab-rotazione').click());
    await assertStationPreserved(adminTabExit, 'admin tab exit');

    smokeStage = 'explicit-music-source-exit';
    const musicToolsExit = await prepareOwnedStation({
      sourceOptions: { primary: 'unavailable', recovery: 'unavailable' },
    });
    await page.locator('#firstListenRepairMusicBtn').click();
    await assertStationPreserved(musicToolsExit, 'music-source tools exit');

    // Source repair keeps the station; only the decorative guide stops.
    smokeStage = 'music-source-exit-during-guide';
    const guideExit = await prepareOwnedStation({
      sourceOptions: { primary: 'unavailable', recovery: 'unavailable' },
    });
    await page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play').click();
    await page.waitForFunction(() => (
      document.querySelector('.guide-audio[data-guide="welcome"]')?.dataset.state === 'playing'
        && window.__firstListenStationMedia.playing
    ));
    const guideParked = await stationMediaSnapshot();
    assert(guideParked.src === guideExit.src, 'guide narration replaced the station source');
    assert(guideParked.streamRequests.length === guideExit.requestCount, 'guide narration opened a second station stream');
    // Re-baseline past the pause the guide itself took, so the assertions below
    // see the exit teardown alone.
    const guideExitCheckpoint = { ...guideExit, eventIndex: guideParked.events.length };
    await page.locator('#firstListenRepairMusicBtn').click();
    await assertStationPreserved(guideExitCheckpoint, 'music-source exit during guide playback');
    const guideExitEvents = (await stationMediaSnapshot()).events.slice(guideExitCheckpoint.eventIndex);
    assert(
      guideExitEvents.filter((event) => event.type === 'play').length === 0,
      `music-source exit resumed the station before releasing it: ${JSON.stringify(guideExitEvents)}`,
    );

    for (const exit of ['admin-tab', 'music-source']) {
      smokeStage = `${exit}-during-guide-load`;
      const station = await prepareOwnedStation({
        sourceOptions: { primary: 'unavailable', recovery: 'unavailable' },
      });
      const gate = responseGate();
      guideResponseGate = gate;
      try {
        // Keep the real media play() pending until navigation aborts it.
        await page.evaluate(() => {
          const button = document.querySelector('.guide-audio[data-guide="welcome"] .guide-audio-play');
          window.__pendingGuideAttempt = toggleFirstListenGuide('welcome', button);
        });
        await gate.arrived;
        assert(await welcomeGuide.getAttribute('data-state') === 'loading', `${exit} fixture did not hold the guide load`);
        const parked = await assertStationPreserved(station, smokeStage);
        const checkpoint = { ...station, eventIndex: parked.events.length };
        if (exit === 'admin-tab') {
          await page.evaluate(() => document.getElementById('tab-rotazione').click());
        } else {
          await page.locator('#firstListenRepairMusicBtn').click();
        }
        await page.evaluate(() => window.__pendingGuideAttempt);
        assert(await welcomeGuide.getAttribute('data-state') === 'idle', `${exit} interrupted load stamped a false guide error`);
        assert(await welcomeGuideButton.innerText() === 'Take your seat', `${exit} interrupted load overwrote the idle label`);
        assert(await welcomeGuideButton.getAttribute('aria-pressed') === 'false', `${exit} interrupted load left the button pressed`);
        assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, `${exit} interrupted load retained its source`);
        await assertStationPreserved(checkpoint, smokeStage);
        if (exit === 'music-source') {
          const events = (await stationMediaSnapshot()).events.slice(checkpoint.eventIndex);
          assert(!events.some(({ type }) => type === 'play'), 'pending guide music-source exit restarted the station');
        }
      } finally {
        gate.release();
      }
    }

    smokeStage = 'same-guide-pause-during-load';
    const loadingGuideStation = await prepareOwnedStation();
    const loadingGuideGate = responseGate();
    guideResponseGate = loadingGuideGate;
    try {
      await page.evaluate(() => {
        const button = document.querySelector('.guide-audio[data-guide="welcome"] .guide-audio-play');
        window.__pendingGuideAttempt = toggleFirstListenGuide('welcome', button);
      });
      await loadingGuideGate.arrived;
      assert(await welcomeGuide.getAttribute('data-state') === 'loading', 'same-guide fixture did not hold the guide load');
      await assertStationPreserved(loadingGuideStation, smokeStage);
      await welcomeGuideButton.click();
      await page.evaluate(() => window.__pendingGuideAttempt);
      assert(await welcomeGuide.getAttribute('data-state') === 'paused', 'same-guide pause during load stamped a false guide error');
      assert(await welcomeGuideButton.innerText() === 'Continue welcome', 'same-guide pause during load lost its continue label');
      assert(await welcomeGuideButton.getAttribute('aria-pressed') === 'false', 'same-guide pause during load left the button pressed');
      assert(await page.locator('#firstListenGuideAudio').getAttribute('src') !== null, 'same-guide pause during load reset its source');
      await assertStationPreserved(loadingGuideStation, smokeStage);
    } finally {
      loadingGuideGate.release();
    }

    smokeStage = 'guide-playback-rejection';
    const rejectedGuideStation = await prepareOwnedStation();
    await page.evaluate(async () => {
      const audio = document.getElementById('firstListenGuideAudio');
      const nativePlay = audio.play;
      audio.play = () => Promise.reject(new DOMException('Playback denied', 'NotAllowedError'));
      try {
        const button = document.querySelector('.guide-audio[data-guide="welcome"] .guide-audio-play');
        await toggleFirstListenGuide('welcome', button);
      } finally {
        audio.play = nativePlay;
      }
    });
    assert(await welcomeGuide.getAttribute('data-state') === 'error', 'genuine playback rejection lost its error state');
    assert(await welcomeGuideButton.innerText() === 'Try welcome again', 'genuine playback rejection lost its retry label');
    assert(await page.locator('#firstListenGuideAudio').getAttribute('src') === null, 'genuine playback rejection retained its source');
    await assertStationPreserved(rejectedGuideStation, smokeStage);

    smokeStage = 'explicit-listener-exit';
    const listenerExit = await prepareOwnedStation({ showSuccess: true });
    const openedWindowBaseline = await page.evaluate(() => window.__firstListenOpenedWindows.length);
    await page.getByRole('button', { name: 'Listen to the station', exact: true }).click();
    await page.waitForFunction(()=>_firstListenHandoff.ready&&location.pathname.endsWith('/listen'));
    let listenerFrame = page.frameLocator('#firstListenListenerFrame');
    assert(await listenerFrame.locator('.mmr-stage').isVisible(),'handoff did not reuse the listener page');
    assert(await listenerFrame.locator('#radio-audio').getAttribute('src')===null,'listener opened its own audio during handoff');
    assert(await page.locator('#firstListenPlayerToggle').isHidden(),'handoff showed duplicate playback controls');
    await assertStationPreserved(listenerExit,'existing listener handoff');
    assert(await page.evaluate(()=>window.__firstListenOpenedWindows.length)===openedWindowBaseline,'handoff opened a duplicate window');
    await listenerFrame.getByRole('button',{name:'Pause station',exact:true}).first().click();
    await page.waitForFunction(()=>!_firstListenPlayback.intent&&!window.__firstListenStationMedia.playing);
    await listenerFrame.getByRole('button',{name:'Listen now',exact:true}).first().click();
    await page.waitForFunction(()=>_firstListenPlayback.intent&&window.__firstListenStationMedia.playing);
    const resumedHandoff=await stationMediaSnapshot();
    assert(resumedHandoff.streamRequests.length===listenerExit.requestCount+1&&resumedHandoff.src.endsWith('first_listen=live'),'listener resume did not rejoin live');
    const resumedCheckpoint={src:resumedHandoff.src,requestCount:resumedHandoff.streamRequests.length,eventIndex:resumedHandoff.events.length};
    await page.evaluate(()=>history.back());
    await page.waitForFunction(()=>!_firstListenHandoff.frame&&location.pathname.endsWith('/admin'));
    assert(await page.locator('#firstListenPlayerToggle').isVisible(),'Back lost the local controls');
    await assertStationPreserved(resumedCheckpoint,'Back to Admin');
    await page.evaluate(()=>history.forward());
    await page.waitForFunction(()=>_firstListenHandoff.ready);
    listenerFrame=page.frameLocator('#firstListenListenerFrame');
    await listenerFrame.locator('a[href="#dediche"]').first().click();
    await page.waitForFunction(()=>_firstListenHandoff.frame.contentWindow.location.hash==='#dediche');
    assert(await listenerFrame.locator('#dediche').isVisible(),'listener anchor navigation broke the reused page');
    await page.evaluate(()=>history.back());
    await page.waitForFunction(()=>_firstListenHandoff.ready&&_firstListenHandoff.frame.contentWindow.location.hash!=='#dediche');
    await page.evaluate(()=>openFirstListenStation());
    await assertStationPreserved(resumedCheckpoint,'repeated listener return');
    await page.evaluate(()=>history.back());
    await page.waitForFunction(()=>_firstListenHandoff.ready);
    await page.evaluate(()=>{
      if('mediaSession' in navigator)navigator.mediaSession.metadata=new MediaMetadata({title:'Last listener track'});
      _firstListenHandoff.binding.notify=()=>{throw new Error('lost listener controls');};
      renderFirstListenPlayer();
    });
    assert(await page.locator('#firstListenListenerRetry').isVisible(),'disconnected listener hid its recovery action');
    assert((await page.locator('#firstListenListenerRecoveryStatus').innerText()).includes('lost its controls'),'disconnected listener hid its explanation');
    assert(await page.evaluate(()=>!('mediaSession' in navigator)||navigator.mediaSession.metadata===null),'detached listener left stale track metadata');
    await page.locator('#firstListenListenerRetry').click();
    await page.waitForFunction(()=>_firstListenHandoff.ready);
    await page.evaluate(()=>openFirstListenStation());

    smokeStage='listener-paused-handoff';
    await prepareOwnedStation({showSuccess:true});
    await page.locator('#firstListenPlayerToggle').click();
    const pausedBefore=await stationMediaSnapshot();
    await page.locator('#firstListenListenerBtn').click();
    await page.waitForFunction(()=>_firstListenHandoff.ready);
    await assertStationPreserved({src:pausedBefore.src,requestCount:pausedBefore.streamRequests.length,eventIndex:pausedBefore.events.length},'paused handoff',{playing:false});
    assert(await page.frameLocator('#firstListenListenerFrame').getByRole('button',{name:'Listen now',exact:true}).first().isVisible(),'paused handoff did not offer explicit play');
    await page.evaluate(()=>openFirstListenStation());

    smokeStage='listener-load-failures';
    for(const kind of ['error-page','missing-script','late-ready']){
      smokeStage=`listener-load-${kind}`;
      const checkpoint=await prepareOwnedStation({showSuccess:true});
      const loading=responseGate();let finished;const settled=new Promise(resolve=>{finished=resolve;});
      const gate=async route=>{
        loading.arrive();
        try{if(kind==='late-ready'){await loading.wait;await route.abort();}
        else await route.fulfill({status:503,contentType:'text/html',body:'Unavailable'});}
        finally{finished();}
      };
      const pattern=kind==='error-page'?'**/listen':'**/static/listener.js*';
      // Repeated iframe/history visits can reuse the parsed script without a
      // request. A distinct asset URL makes each injected failure observable.
      const documentGate=async route=>{
        const response=await route.fetch();
        await route.fulfill({response,body:(await response.text()).replace('/static/listener.js?v=',`/static/listener.js?h4-fault=${kind}&v=`)});
      };
      if(kind!=='error-page')await page.route('**/listen',documentGate);
      await page.route(pattern,gate);
      const loadRequest=page.waitForRequest(pattern,{timeout:12000});
      await page.locator('#firstListenListenerBtn').click();
      assert(await page.locator('#firstListenPlayerToggle').isVisible(),`${kind} hid controls before ready`);
      await loadRequest;await loading.arrived;
      if(kind==='late-ready'){
        await page.evaluate(()=>{window.__lateListenerBridge=mmrConnectListener(_firstListenHandoff.frame.contentWindow);});
        await page.locator('#firstListenListenerBtn').click();loading.release();
        await page.evaluate(()=>{window.__lateListenerCallback=false;__lateListenerBridge.subscribe(()=>{window.__lateListenerCallback=true;});__lateListenerBridge.play();__lateListenerBridge.pause();});
        assert(await page.evaluate(()=>!__lateListenerCallback),'cancelled listener retained its playback authority');
      }else{
        await page.waitForFunction(()=>!_firstListenHandoff.frame,null,{timeout:12000});
        assert((await page.locator('#firstListenHandoffStatus').innerText()).includes('could not open'),`${kind} did not offer recovery`);
      }
      await settled;await page.unroute(pattern,gate);
      if(kind!=='error-page')await page.unroute('**/listen',documentGate);
      assert(await page.evaluate(()=>!_firstListenHandoff.ready&&!_firstListenHandoff.binding),'late readiness reopened a cancelled listener');
      await assertStationPreserved(checkpoint,kind);
    }

    smokeStage='restart-first-listen';
    for(const [origin,enabled,playing] of [['fresh',false,true],['existing',true,true],['legacy',false,false]]){
      await prepareOwnedStation();
      if(!playing)await page.locator('#firstListenPlayerToggle').click();
      const saved=setupProjection({audio:true,privacy:true,privacyEnabled:enabled});
      if(origin==='legacy')delete saved.guided_setup.first_listen;
      else saved.guided_setup.first_listen.install_origin=origin;
      setupStatusProjection=saved;
      await page.evaluate(saved=>{_lastSetupJson='';renderSetup(saved);openFirstListenStation();showAdminTab('motore');},saved);
      const before=await stationMediaSnapshot(),writes=[resumeRequests.length,verifyRequests.length,privacyRequests.length,previewRequests.length];
      await page.locator('#firstListenRestartBtn').click();
      assert(await page.evaluate(()=>document.activeElement.id==='firstListenShowTitle'),'restart lost welcome focus');
      for(let poll=0;poll<2;poll++)await page.evaluate(saved=>{_lastSetupJson='';renderSetup(saved);},saved);
      assert((await page.locator('#firstListenProgressLine').innerText()).startsWith('Step 1 of 3'),`${origin}: old receipts advanced restart`);
      assert(await page.evaluate(()=>!firstListenProjection().privacyReviewed),`${origin}: restart reused its old privacy review`);
      assert(await page.evaluate(()=>firstListenProjection().privacyEnabled)===enabled,`${origin}: restart changed effective privacy`);
      assert(JSON.stringify(writes)===JSON.stringify([resumeRequests.length,verifyRequests.length,privacyRequests.length,previewRequests.length]),'restart made a settings or playback request');
      await assertStationPreserved({src:before.src,requestCount:before.streamRequests.length,eventIndex:before.events.length},'restart',{playing});
      await page.locator('#firstListenPlayBtn').click();
      await page.waitForFunction(()=>_firstListenUi.dispatch==='accepted'&&!_firstListenUi.busy);
      await page.locator('#firstListenNotYetBtn').click();
      await page.waitForFunction(()=>_firstListenUi.verification==='not_yet'&&!_firstListenUi.busy);
      await page.evaluate(saved=>{_lastSetupJson='';renderSetup(saved);},saved);
      assert(await page.evaluate(()=>!firstListenProjection().heard&&!firstListenProjection().privacyUnlocked),'old confirmation unlocked replay after No sound yet');
      await page.locator('#firstListenRetryBtn').click();
      await page.waitForFunction(()=>_firstListenUi.dispatch==='accepted'&&!_firstListenUi.busy);
      await page.locator('#firstListenHeardBtn').click();
      await page.waitForFunction(()=>_firstListenUi.verification==='heard'&&!_firstListenUi.busy);
      assert(await page.evaluate(()=>!firstListenProjection().privacyReviewed),'restart skipped its privacy choice');
      await openHomeChoice();
      await page.locator('#firstListenKeepOffBtn').click();
      await page.waitForFunction(()=>_firstListenUi.showSuccess&&!_firstListenUi.privacySaving);
      assert(await page.evaluate(()=>!_firstListenUi.restarting&&_firstListenUi.successAnnounced),'restarted flow did not reach its finale');
    }

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

    for (const enabled of [false, true]) {
      const beforeChoice = privacyRequests.length;
      await resetUi(setupProjection({ audio: true, privacyEnabled: enabled, privacyChoiceExplicit: true }));
      assert((await page.locator('#firstListenPrivacyChip').innerText()).toLowerCase() === 'review', 'an initial configured privacy choice was labelled a failed save');
      assert((await page.locator('#firstListenKeepOffBtn').innerText()) === (enabled ? 'Switch to private' : 'Keep Home private'), 'initial privacy action implies an earlier save attempt');
      if (enabled) {
        assert((await page.locator('#firstListenPrivacySummary').innerText()).includes('Home context is on'), 'reloaded active choice lost live privacy truth');
        await page.locator('#firstListenPreviewBtn').click();
        await page.waitForFunction(() => _firstListenUi.privacyPreviewValid === true);
        assert(await page.getByRole('button', { name: 'Save shared choice', exact: true }).isEnabled(), 'configured sharing could not save its first review');
      }
      assert(privacyRequests.length === beforeChoice, 'configured privacy submitted consent without a click');
      await page.locator(enabled ? '#firstListenEnableContextBtn' : '#firstListenKeepOffBtn').click();
      await page.waitForFunction(choice => _firstListenUi.privacyChoice === choice && !_firstListenUi.privacySaving, enabled);
      assert(privacyRequests.length === beforeChoice + 1, 'configured privacy did not save exactly once');
      assert(await page.locator('#firstListenSuccess').isVisible(), 'configured privacy did not complete after acknowledgement');
    }

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
    assert(await page.locator('.first-listen-step[data-state="complete"] > .first-listen-head > .first-listen-review:visible').count() === 3, 'completed choices are not visibly revisitable');
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
    assert((await page.locator('#firstListenSourceChip').innerText()) === 'STILL CHECKING', 'unknown sources lost their checking status');
    if(!(await sourcePreview.evaluate((element) => element.open)))await sourceReview.click();
    assert(
      (await page.locator('#firstListenSourcePreview').innerText()).includes('Checking what can play'),
      'unknown sources lost the honest what-plays-next placeholder',
    );
    await sourceReview.click();
    await assertUnfinished('firstListenPrivacyStep', 'firstListenPreviewBtn');
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
    assert(['ai service connected','writing service responded','saved · checking','not connected'].includes(aiChipText.toLowerCase()), `configured AI provider was not summarized safely: ${aiChipText}`);
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
        const smallTargets = [...surface.querySelectorAll('button, summary, a')].filter((element) => {
          if (!element.checkVisibility({checkVisibilityCSS:true})) return false;
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
        const brokenStatusWords=[...surface.querySelectorAll('#firstListenSourceStep .status-chip')].filter(chip=>chip.checkVisibility()).flatMap(chip=>{
          const text=chip.firstChild;
          if(text?.nodeType!==Node.TEXT_NODE)return[];
          return [...text.textContent.matchAll(/\S+/g)].filter(word=>{
            const range=document.createRange();range.setStart(text,word.index);range.setEnd(text,word.index+word[0].length);
            return range.getClientRects().length>1;
          }).map(word=>word[0]);
        });
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
          brokenStatusWords,
        };
      });
    const assertJourneyGeometry = (width, geometry, label) => {
      assert(geometry.documentWidth <= geometry.viewport + 1, `${label} ${width}px page overflowed horizontally: ${JSON.stringify(geometry)}`);
      assert(geometry.clipped.length === 0, `${label} ${width}px clipped a control: ${JSON.stringify(geometry.clipped)}`);
      assert(geometry.headOverlaps.length === 0, `${label} ${width}px title/status/action overlap: ${JSON.stringify(geometry.headOverlaps)}`);
      assert(geometry.escapedRects.length === 0, `${label} ${width}px visible journey content escaped its surface: ${JSON.stringify(geometry.escapedRects)}`);
      assert(geometry.titles.every((title) => title.width >= 48), `${label} ${width}px title collapsed: ${JSON.stringify(geometry.titles)}`);
      assert(geometry.smallTargets.length === 0, `${label} ${width}px exposed a target below 44px: ${JSON.stringify(geometry.smallTargets)}`);
      assert(geometry.brokenStatusWords.length===0,`${label} ${width}px split a music status word: ${geometry.brokenStatusWords.join(', ')}`);
      assert(geometry.touchTargetProbes.every((target)=>target.width>=43.5&&target.height>=43.5),`${label} ${width}px stylesheet touch target fell below 44px: ${JSON.stringify(geometry.touchTargetProbes)}`);
    };
    for (const [width, height] of journeyViewports) {
      await page.setViewportSize({ width, height });
      const geometry = await measureJourneyGeometry();
      viewportResults.push({ state: 'active', width, ...geometry });assertJourneyGeometry(width, geometry, 'active');
    }
    if(!(await sourcePreview.evaluate((element) => element.open)))await sourceReview.click();
    assert(await sourcePreview.isVisible(), 'source preview was not exposed for responsive geometry checks');
    await page.locator('#firstListenHomeAssistantGuide').evaluate(e=>e.open=true);
    for (const [width, height] of journeyViewports) {
      await page.setViewportSize({ width, height });
      const geometry = await measureJourneyGeometry();
      viewportResults.push({ state: 'source-review', width, ...geometry });assertJourneyGeometry(width, geometry, 'source review');
    }

    await page.locator('#firstListenHomeAssistantGuide').evaluate(e=>e.open=false);
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
    const activeZoomJourneyGeometry = await measureJourneyGeometry();
    assertJourneyGeometry(320, activeZoomJourneyGeometry, 'active 200% zoom');
    if(!(await sourcePreview.evaluate((element) => element.open)))await sourceReview.click();
    const sourceReviewZoomJourneyGeometry = await measureJourneyGeometry();
    assertJourneyGeometry(320, sourceReviewZoomJourneyGeometry, 'source review 200% zoom');

    await resetUi(completed);
    await page.evaluate(() => {document.documentElement.style.fontSize = '200%';});
    const completedZoomJourneyGeometry = await measureJourneyGeometry();
    assertJourneyGeometry(320, completedZoomJourneyGeometry, 'completed 200% zoom');

    smokeStage = 'explicit-pause-cancels-delayed-start';
    await page.setViewportSize({width:1280,height:900});
    for(const force of [false,true]){
      await resetUi(setupProjection());
      const gate=responseGate();gate.force=force;resumeResponseGate=gate;
      if(force)nextResumeResponse='force_available';
      const before=await stationMediaSnapshot();
      await page.locator('#firstListenPlayBtn').click();await gate.arrived;
      await page.locator('#firstListenPlayerToggle').click();
      const reply=page.waitForResponse(response=>response.url().includes('/api/resume')&&response.url().includes('force=true')===force);
      gate.release();await reply;
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      assert(!(await stationMediaSnapshot()).playing,'a delayed start ignored Pause');
      assert((await stationMediaSnapshot()).streamRequests.length===before.streamRequests.length,'a canceled start opened a stream');
    }
    smokeStage = 'persistent-player-ducking';
    for(const mode of ['late-before-continue','late-after-continue','rejected']){
      await resetUi(setupProjection());
      await page.evaluate(mode=>{
        const audio=firstListenStationAudio(),play=audio.play;
        audio.play=function(){audio.play=play;return mode==='rejected'?Promise.reject(new Error('play rejected')):new Promise(resolve=>{window.__releaseStationPlay=()=>play.call(audio).then(resolve);});};
      },mode);
      await page.locator('#firstListenPlayBtn').click();
      await page.waitForFunction(()=>firstListenStationAudio().getAttribute('src'));
      if(mode==='rejected')await page.waitForFunction(()=>!_firstListenUi.busy);
      else{
        await page.locator('#firstListenPlayerToggle').click();
        if(mode==='late-before-continue'){
          await page.evaluate(()=>__releaseStationPlay());
          assert(!(await stationMediaSnapshot()).playing,'late play ignored paused intent');
        }
      }
      const continueGate=responseGate();resumeResponseGate=continueGate;
      await page.locator('#firstListenPlayerToggle').click();await continueGate.arrived;
      await page.evaluate(()=>firstListenStationAudio().dispatchEvent(new Event('playing')));
      assert(await page.evaluate(()=>_firstListenUi.dispatch==='starting'),'stale playing event accepted a pending attempt');
      continueGate.release();
      await page.waitForFunction(()=>_firstListenUi.dispatch==='accepted'&&!_firstListenUi.busy);
      await page.evaluate(()=>firstListenStationAudio().dispatchEvent(new Event('pause')));
      assert(await page.evaluate(()=>_firstListenPlayback.phase==='playing'),'stale pause event interrupted current playback');
      await assertUnfinished('firstListenVerifyStep','firstListenHeardBtn');
      if(mode==='late-after-continue'){
        await page.locator('#firstListenHeardBtn').click();
        await page.waitForFunction(()=>_firstListenUi.verification==='heard'&&!_firstListenUi.busy);
        await page.evaluate(()=>__releaseStationPlay());
        assert(await page.evaluate(()=>_firstListenUi.verification==='heard'),'stale play reset the new hearing receipt');
      }
    }
    const continuous=await startAudibleFirstListen();
    const proofBefore=await page.evaluate(()=>({heard:_firstListenUi.verification,choice:_firstListenUi.privacyChoice}));
    await page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play').click();
    await page.waitForFunction(()=>_firstListenPlayback.gain?.gain.value<0.2);
    await assertStationPreserved(continuous,'welcome duck');
    await page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play').click();
    await page.waitForFunction(()=>_firstListenPlayback.gain?.gain.value>0.99);
    await assertStationPreserved(continuous,'manual guide pause');
    await page.locator('#firstListenDestinations > summary').click();
    await assertStationPreserved(continuous,'destination help');
    assert(await page.locator('#firstListenCopyStream').isHidden(),'local-only preview offered an unreachable stream address');
    await page.locator('#firstListenPlayerToggle').click();
    assert(!(await stationMediaSnapshot()).playing,'Pause music did not pause');
    const paused=await stationMediaSnapshot();
    await page.locator('#firstListenPlayerToggle').click();
    await page.waitForFunction(()=>window.__firstListenStationMedia.playing);
    const continued=await stationMediaSnapshot();
    assert(continued.src.endsWith('first_listen=live')&&continued.streamRequests.length===paused.streamRequests.length+1,'Continue music resumed buffered history');
    assert(JSON.stringify(await page.evaluate(()=>({heard:_firstListenUi.verification,choice:_firstListenUi.privacyChoice})))===JSON.stringify(proofBefore),'Pause/Continue changed saved progress');

    smokeStage='persistent-player-geometry';
    for(const [width,height,zoom] of [[320,568,false],[375,812,false],[1440,900,false],[320,568,true]]){
      await page.setViewportSize({width,height});await resetUi(setupProjection());
      if(zoom)await page.evaluate(()=>document.documentElement.style.fontSize='200%');
      await page.locator('#firstListenPlayBtn').click();
      await page.waitForFunction(()=>_firstListenUi.dispatch==='accepted'&&!_firstListenUi.busy);
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const clearance=await page.evaluate(()=>({actions:firstListenVerifyActions.getBoundingClientRect().bottom,player:firstListenPlayer.getBoundingClientRect().top}));
      assert(clearance.actions<=clearance.player,`confirmation choices overlap the player at ${width}/${zoom}: ${JSON.stringify(clearance)}`);
      await page.evaluate(()=>undoableToast('Banned and skipped — won’t come back',()=>{},30000));
      const notice=await page.locator('#undoStack').boundingBox();
      assert(notice.y+notice.height<=clearance.player,`undo notice covers player at ${width}/${zoom}`);
      await page.locator('#firstListenDestinations > summary').click();
      const geometry=await page.locator('#firstListenPlayer').evaluate(p=>({width:p.clientWidth,scroll:p.scrollWidth,targets:[...p.querySelectorAll('button,summary,a')].filter(e=>e.getClientRects().length).map(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}))}));
      assert(geometry.scroll<=geometry.width+1&&geometry.targets.every(r=>r.width>=44&&r.height>=44),`player geometry failed at ${width}/${zoom}: ${JSON.stringify(geometry)}`);
      await page.locator('#firstListenPlayer a').scrollIntoViewIfNeeded();
      await page.locator('#firstListenDestinations > summary').click();
      await page.locator('#firstListenPlayerToggle').click();
      assert(!(await stationMediaSnapshot()).playing,'expanded player prevented explicit Pause');
    }
    await page.setViewportSize({width:1280,height:900});

    smokeStage = 'audio-graph-failure-matrix';
    await page.evaluate(()=>{
      window.__realFirstListenAudio={Context:window.AudioContext,WebkitContext:window.webkitAudioContext,playback:{..._firstListenPlayback},timeout:window.setTimeout};
    });
    for(const mode of ['unsupported','constructor','gain','gain-connect','resume','timeout','source','bypass','broken-bypass','normal']){
      await resetUi(setupProjection());
      await page.evaluate(mode=>{
        const p=_firstListenPlayback;
        Object.assign(p,{context:null,source:null,gain:null,mixTask:null,mixReady:false,mixUnavailable:false,unrecoverable:false});
        const trace=window.__mixTrace={mode,contexts:0,bindings:0,routes:[],release:null};
        window.setTimeout=(fn,ms,...args)=>window.__realFirstListenAudio.timeout.call(window,fn,mode==='timeout'&&ms===8000?10:ms,...args);
        class Context extends EventTarget{
          constructor(){super();trace.contexts++;if(mode==='constructor')throw new Error('constructor');this.state='running';this.currentTime=0;this.destination={destination:true};}
          createGain(){
            if(mode==='gain')throw new Error('gain');
            return{gain:{value:1,cancelScheduledValues(){},setValueAtTime(v){this.value=v;},linearRampToValueAtTime(v){this.value=v;}},connect(){if(mode==='gain-connect')throw new Error('gain-connect');}};
          }
          resume(){if(mode==='resume')return Promise.reject(new Error('resume'));if(mode==='timeout')return new Promise(resolve=>{trace.release=resolve;});this.state='running';this.dispatchEvent(new Event('statechange'));return Promise.resolve();}
          createMediaElementSource(){
            if(mode==='source')throw new Error('source');trace.bindings++;
            return{disconnect(){trace.routes=[];},connect(target){if(['bypass','broken-bypass'].includes(mode)&&!target.destination)throw new Error('connect');if(mode==='broken-bypass')throw new Error('bypass');trace.routes.push(target.destination?'direct':'gain');}};
          }
        }
        window.AudioContext=mode==='unsupported'?undefined:Context;
        window.webkitAudioContext=undefined;
      },mode);
      await page.locator('#firstListenPlayBtn').click();
      await page.waitForFunction(()=>!_firstListenUi.busy);
      const snapshot=await page.evaluate(()=>({playing:__firstListenStationMedia.playing,bindings:__mixTrace.bindings,mixed:_firstListenPlayback.mixReady,source:Boolean(_firstListenPlayback.source),phase:_firstListenPlayback.phase,dispatch:_firstListenUi.dispatch,status:document.getElementById('firstListenSpeakerStatus').innerText}));
      assert(snapshot.playing===(mode!=='broken-bypass'),`${mode}: audio fallback lied about playback: ${JSON.stringify(snapshot)}`);
      assert(snapshot.bindings<=1,`${mode}: station was bound twice`);
      if(mode==='broken-bypass'){
        assert((await page.locator('#firstListenPlayerToggle').innerText())==='Reload player','failed bound output lacks recovery');
      }else if(mode==='normal'){
        await page.evaluate(()=>openFirstListenListener());
        await page.waitForFunction(()=>_firstListenHandoff.ready);
        const attached=page.frameLocator('#firstListenListenerFrame');
        await page.evaluate(()=>{_firstListenPlayback.context.state='suspended';_firstListenPlayback.context.dispatchEvent(new Event('statechange'));});
        assert((await page.locator('#firstListenPlayerToggle').innerText())==='Continue music','suspended output lacks Continue');
        await attached.getByRole('button',{name:'Listen now',exact:true}).first().click();
        await page.waitForFunction(()=>_firstListenPlayback.phase==='playing');
        await page.evaluate(()=>{_firstListenPlayback.context.state='closed';_firstListenPlayback.context.dispatchEvent(new Event('statechange'));});
        assert((await page.locator('#firstListenPlayerToggle').innerText())==='Reload player','closed output lacks Reload');
        assert(await attached.getByRole('button',{name:'Reload player',exact:true}).first().isVisible(),'attached listener hid closed-context recovery');
        await page.evaluate(()=>openFirstListenStation());
      }else{
        await page.locator('.guide-audio[data-guide="welcome"] .guide-audio-play').click();
        await page.waitForFunction(()=>document.querySelector('.guide-audio[data-guide="welcome"]').dataset.state==='error');
        assert(await page.locator('#firstListenGuideAudio').evaluate(audio=>audio.paused),'unsupported mixing overlapped full-volume audio');
        assert((await stationMediaSnapshot()).playing,'unsupported mixing stopped native music');
        if(mode==='timeout'){
          await page.evaluate(()=>__mixTrace.release());
          assert(await page.evaluate(()=>!_firstListenPlayback.source),'late context resume bound a canceled graph');
        }
        if(mode==='bypass')assert(await page.evaluate(()=>JSON.stringify(__mixTrace.routes)==='["direct"]'),'bypass did not retain exactly one direct route');
      }
    }
    await page.evaluate(()=>{
      stopFirstListenStationAudio();
      window.AudioContext=__realFirstListenAudio.Context;window.webkitAudioContext=__realFirstListenAudio.WebkitContext;window.setTimeout=__realFirstListenAudio.timeout;
      Object.assign(_firstListenPlayback,__realFirstListenAudio.playback,{intent:false,phase:'idle'});
    });

    smokeStage = 'ingress-guide-audio';
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
    assertGuideRequests(ingressGuideBaseline,'welcome',ingressPrefix);
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
        'guide-clip-switching',
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
        'continuous-private-achievement',
        'continuous-enabled-achievement',
        'failed-privacy-keeps-stream',
        'navigation-preserves-stream',
        'live-station-controls',
        'guided-connection',
        'stopped-continue-force-success',
        'listener-page-handoff',
        'listener-paused-handoff',
        'listener-load-failures',
        'paused-completion',
        'restart-first-listen',
        'audio-graph-failure-matrix',
        'persistent-player-geometry',
        'explicit-pause-cancels-delayed-start',
        'music-source-exit-during-guide',
        'admin-tab-during-guide-load',
        'music-source-during-guide-load',
        'same-guide-pause-during-load',
        'guide-playback-rejection',
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

  try {
    return await runCalmJourneySmoke();
  } catch (error) {
    throw new Error(`${error instanceof Error ? error.message : String(error)} [stage=${smokeStage}]`);
  }
}
