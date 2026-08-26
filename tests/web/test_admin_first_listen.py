"""UI contract guards for the Home Assistant first-listen golden path."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"
FIRST_LISTEN_CSS = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "static" / "first-listen.css"
STREAMER_PY = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "streamer.py"


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def _css() -> str:
    return FIRST_LISTEN_CSS.read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    html = _html()
    start = html.index(f"function {name}(")
    end = html.index(f"function {next_name}(", start)
    return html[start:end]


def _server_setup_error_codes() -> set[str]:
    tree = ast.parse(STREAMER_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_SETUP_ERRORS"
            and node.value is not None
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError("_SETUP_ERRORS not found")


def _first_listen_errors_block() -> str:
    html = _html()
    start = html.index("const FIRST_LISTEN_ERRORS={")
    return html[start : html.index("\n};", start)]


def _ui_first_listen_error_codes() -> set[str]:
    return set(re.findall(r"^\s{2}([a-z][a-z0-9_]*):\{", _first_listen_errors_block(), re.MULTILINE))


def test_first_listen_is_one_vertical_progressive_path_before_advanced_details() -> None:
    html = _html()
    stages = [
        'id="firstListenSourceStep"',
        'id="firstListenSpeakerStep"',
        'id="firstListenVerifyStep"',
        'id="firstListenPrivacyStep"',
        'id="firstListenAiStep"',
        'id="setupAdvancedDetails"',
    ]

    positions = [html.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert html.count('class="first-listen-step"') == 5
    assert 'id="firstListenPath"' in html
    path = html[html.index('id="firstListenPath"') : html.index('id="firstListenOptional"')]
    assert path.count('class="first-listen-step"') == 4
    assert 'id="firstListenAiStep"' not in path
    assert "Optional enhancement" in html
    assert (
        html.index('id="firstListenPath"')
        < html.index('id="firstListenOptional"')
        < html.index('id="firstListenAiStep"')
    )
    assert "Hear Mamma Mi Radio right here." in html
    assert '<header class="mmr-panel-head sr-only">' in html
    assert "Check music can continue" in html
    assert "What plays after the welcome" in html
    assert 'id="firstListenSourcePreview"' in html
    assert "Play it on this device" in html
    assert "Start sound check" in html
    assert "Check the sound" in html
    assert "Choose whether the hosts can use Home details" in html
    assert "Add new conversations between songs" in html
    assert "Hearing it here is enough to finish setup." in html
    assert 'id="firstListenHomeAssistantGuide"' in html
    assert "Home Assistant Community Store (HACS)" in html
    assert "optional Mamma Mi Radio connection" in html
    assert "Custom repositories" in html
    assert "choose Integration as the category" in html
    assert "github.com/florianhorner/mammamiradio/blob/main/docs/integrations/ha-integration.md" in html
    assert "Restart Home Assistant" in html
    assert "Settings → Devices &amp; Services → Add Integration → Mamma Mi Radio" in html
    assert "Media → Mamma Mi Radio → Mamma Mi Radio Live" in html
    assert "Step 1 of 4. Next: play it on this device." in html

    step = _function("firstListenSetStep", "focusCurrentFirstListenStep")
    assert "const current=state==='current'" in step
    assert "const reviewing=_firstListenUi.reviewStep===key" in step
    assert "const optionalOpen=_firstListenUi.optionalStep===key" in step
    assert (
        "if(current&&key!=='ai')step.setAttribute('aria-current','step');else step.removeAttribute('aria-current')"
        in step
    )
    assert "const anotherExpansion=Boolean(_firstListenUi.reviewStep||_firstListenUi.optionalStep)" in step
    assert "const visible=reviewing||optionalOpen||(current&&!anotherExpansion)" in step
    assert "body.hidden=!visible" in step
    assert "body.setAttribute('aria-hidden',visible?'false':'true')" in step
    assert "body.toggleAttribute('inert',!visible)" in step
    assert "review.hidden=state!=='complete'" in step
    assert "FIRST_LISTEN_REVIEW_LABELS[key]" in step
    assert "review.setAttribute('aria-expanded',reviewing?'true':'false')" in step
    collapsed_bodies = re.findall(r'<div class="first-listen-body"[^>]*\bhidden\b[^>]*\binert\b', html)
    assert len(collapsed_bodies) == 4
    assert 'id="firstListenAiBody"' in collapsed_bodies[-1]

    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    for stage in (
        "firstListenSourceStep",
        "firstListenSpeakerStep",
        "firstListenVerifyStep",
        "firstListenPrivacyStep",
        "firstListenAiStep",
    ):
        assert progress.count(f"firstListenSetStep('{stage}'") == 1
    assert "firstListenSetStep('firstListenAiStep',configuredKeys.length?'complete':'optional')" in progress
    assert "firstListenAiStep',configuredKeys.length?'current'" not in progress


def test_completed_first_listen_hides_setup_tab_and_routes_repair_to_motore() -> None:
    html = _html()
    for state in ("pending", "complete"):
        assert f'body[data-first-listen-entry="{state}"] #tab-setup' in html
        assert f'body[data-first-listen-entry="{state}"] #first-listen-panel' in html
    sections = (
        ("showAdminTab", "syncFirstListenSetupMount",
         ("!['required','completing'].includes", "if(name!=='setup')finalizeFirstListenCompletion()")),
        ("initTabs", "initProgrammeActions", ("adminTabsForNav()", "if(initTab==='setup')initTab='scaletta'")),
        ("renderSetup", "setupRecheck", ("firstListenEntry==='required'", "firstListenTabAlert")),
        ("finalizeFirstListenCompletion", "showAdminTab",
         ("firstListenEntry!=='completing'", "stopFirstListenGuide()", "stopFirstListenStationAudio()",
          "firstListenEntry='complete'", "syncFirstListenSetupMount()")),
        ("resolveFirstListenLanding", "renderGuidedSetupStrip",
         ("_activeTab==='setup'&&_firstListenUi.showSuccess", "previous==='completing'",
          "preserveSuccess?'completing':required?'required':'complete'")),
    )  # fmt: skip
    for start, end, needles in sections:
        body = _function(start, end)
        assert all(needle in body for needle in needles)


def test_unfinished_fresh_install_owns_a_top_level_first_surface() -> None:
    html = _html()
    assert 'data-tab="setup"' in html
    assert 'id="tab-setup"' in html
    assert 'aria-controls="first-listen-panel"' in html
    assert 'id="first-listen-panel" data-panel="setup"' in html
    assert "data-calm-journey" not in html
    assert 'id="firstListenPanelMount"' in html
    assert 'id="journeySurface"' in html
    assert 'id="firstListenQuickAction"' not in html
    assert 'id="firstListenQuickFindPlayersBtn"' not in html
    assert 'id="firstListenQuickCopy"' not in html
    assert html.index('data-tab="setup"') < html.index('data-tab="scaletta"')

    mount = _function("syncFirstListenSetupMount", "initTabs")
    for needle in ("entry==='required'", "firstListenSetupContext", "target.appendChild(context)",
                   "entry==='completing'", "const hide=!guided"):  # fmt: skip
        assert needle in mount
    assert 'id="firstListenSetupContext"' in html
    assert 'class="first-listen-panel first-listen-context"' in html
    css = _css()
    assert "#first-listen-panel .setup-group > summary" in css
    assert ".first-listen-panel .setup-group > summary" not in css

    origin_gate = _function("firstListenInstallNeedsOnboarding", "firstListenEntryRequired")
    assert "first?.install_origin||'unknown'" in origin_gate
    assert ".trim().toLowerCase()" in origin_gate
    assert "first?.fresh_install===true" in origin_gate
    assert "origin==='fresh'||origin==='unknown'" in origin_gate

    required = _function("firstListenEntryRequired", "resolveFirstListenLanding")
    assert "first.bootstrap_ready===true" in required
    assert "firstListenInstallNeedsOnboarding(first)" in required
    assert "first.audio_complete&&first.privacy_complete" in required
    assert "onboarding_steps" not in required

    resolve = _function("resolveFirstListenLanding", "renderGuidedSetupStrip")
    assert "if(_firstListenLandingResolved)return" in resolve
    assert "syncFirstListenSetupMount()" in resolve
    assert "previous==='required'&&!required&&_activeTab==='setup'" in resolve
    assert "required&&!_adminTabUserInteracted" in resolve
    assert "showAdminTab('setup',{render:true,persist:false})" in resolve
    assert "focus(" not in resolve

    manual = _function("openSetupPanel", "openMusicSourceTools")
    assert "firstListenRequired?'setup':'motore'" in manual
    assert "details.dataset.userPinned='true'" in manual


def test_required_source_truth_rows_and_recovery_boundary_are_explicit() -> None:
    html = _html()
    for label in (
        "Live charts",
        "Jamendo",
        "Local music",
        "Bundled demo music",
        "Recovery cover",
    ):
        assert f"label:'{label}'" in html

    assert "source.recovery_on_air===true" in html
    assert "source.recovery_cover_available===true" in html
    assert "The welcome is ready. Backup music will keep the station playing." in html
    assert "Backup music will play after the welcome." in html
    assert "Backup music is ready after the welcome." in html
    assert "The welcome is ready, but music cannot continue afterward yet." in html
    assert "source.transport_audible" not in html
    cue = _function("firstListenListeningCue", "renderFirstListenProgress")
    assert "if(healthy)" in cue
    assert "if(recoveryOnAir)" in cue
    assert "if(recoveryAvailable)" in cue
    assert "then the first song" in cue
    assert "then backup music" in cue
    assert "Backup music is ready" in cue
    assert "opening may end before music" in cue
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "firstListenListeningCue({healthy,recoveryOnAir,recoveryAvailable})" in progress
    render_start = html.index("function renderFirstListen(setup")
    render = html[render_start : html.index("function setupProviderLabels", render_start)]
    assert "firstListenListeningCue(state)" in render
    assert html.index('id="setupAdvancedDetails"') < html.index('id="firstListenSources"')
    assert html.index('id="firstListenSourcePreview"') < html.index('id="setupAdvancedDetails"')


def test_speaker_controls_use_active_post_routes_and_exact_media_source() -> None:
    html = _html()
    assert "apiResponse('POST','/api/setup/first-listen/players'" not in html
    assert "apiResponse('POST','/api/setup/first-listen/play'" not in html
    assert "Find my speakers" not in html
    assert "Play in this room" not in html
    assert 'id="firstListenPlayerChoices"' not in html
    assert 'id="firstListenPlayerSelect"' not in html
    assert 'id="firstListenFindPlayersBtn"' not in html
    assert (
        "apiResponse('POST','/api/setup/first-listen/listener-confirm',"
        "{heard:requestedHeard},FIRST_LISTEN_TIMEOUTS.verify)" in html
    )
    assert "media-source://mammamiradio/live" in html
    assert "Start sound check" in html
    assert "Yes, I hear it" in html
    assert "Not yet" in html
    audio_tag = re.search(r'<audio id="firstListenStationAudio"[^>]*>', html)
    assert audio_tag is not None
    assert 'preload="none"' in audio_tag.group(0)
    assert "playsinline" in audio_tag.group(0)
    assert "autoplay" not in audio_tag.group(0)
    assert html.count('id="firstListenStationAudio"') == 1
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "audio.src=`${_base}${FIRST_LISTEN_STREAM}`" in start
    assert "FIRST_LISTEN_STREAM='/stream?first_listen=1'" in html
    assert "audio.play()" in start
    assert "addEventListener('playing',firstListenStationPlaying)" in html
    assert "volume_set" not in html
    assert "volume_level" not in start


def test_home_assistant_labels_are_added_with_text_content() -> None:
    preview_block = _function("renderHomeContextPreview", "retestFirstListenSpeaker")
    for assignment in (
        "title.textContent=label",
        "meta.textContent=",
        "id.textContent=entityId",
        "mute.textContent=",
    ):
        assert assignment in preview_block
    assert "if(targetId!=='haContextPreview')" in preview_block
    assert ".innerHTML" not in preview_block


def test_privacy_preview_is_explicit_and_precedes_optional_ai() -> None:
    html = _html()
    privacy = html.index('id="firstListenPrivacyStep"')
    ai = html.index('id="firstListenAiStep"')
    assert privacy < ai
    assert "Example only. This is not your Home data" in html
    assert "We won’t request Home details unless you ask for a preview." in html
    assert "Keep Home private and we request nothing." in html
    assert "preview exactly what the hosts would receive, then decide." in html
    assert "apiResponse('POST','/api/setup/home-context-preview',{})" in html
    assert "apiResponse('PATCH','/api/setup/home-context-choice',{enabled:requestedEnabled})" in html
    assert "Keep Home private" in html
    assert "See what the hosts would receive" in html
    assert "Let Marco and Giulia use these details" in html

    keep_private = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "loadHomeContextPreview" not in keep_private
    assert "requestedEnabled&&!_firstListenUi.privacyPreviewValid" in keep_private
    preview = _function("loadHomeContextPreview", "loadCachedHomeContextDiagnostics")
    assert "_firstListenUi.privacyPreviewValid=false" in preview
    assert "_firstListenUi.privacyPreviewValid=true" in preview


def test_existing_install_opens_privacy_without_replaying_first_audio() -> None:
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")

    assert "const priorInstall=projection.legacy||projection.first.install_origin==='existing'" in progress
    assert "const existingProof=priorInstall&&!_firstListenUi.selectionDirty&&!projection.proofPending" in progress
    assert "const listenAccepted=projection.accepted||existingProof||receiptRepairRequired" in progress
    assert "const listenComplete=projection.heard||existingProof" in progress
    assert "const existingPrivacyReview=existingProof&&!projection.privacyReviewed" in progress
    assert "listenAccepted?'complete':sourceMilestone?'current':'upcoming'" in progress
    assert "listenComplete?'complete':receiptRepairRequired||listenAccepted?'current':'upcoming'" in progress
    assert "privacyMilestone?'complete':existingPrivacyReview||listenComplete?'current':'upcoming'" in progress
    assert "configuredKeys.length?'complete':'optional'" in progress
    assert "aiFieldset.disabled=(!privacyMilestone&&projection.haAccess)||!projection.showAi" in progress
    assert "You do not need to play the station again." in progress
    assert "Your existing listening check still stands. Review your Home details next." in progress

    html = _html()
    render_start = html.index("function renderFirstListen(setup")
    setup = html[render_start : html.index("function renderSetup", render_start)]
    assert "Your existing station stays as it is." in setup
    assert "You do not need to replay the station." in setup
    assert "/api/setup/first-listen/play',{entity_id" not in setup + progress

    choice = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "const continuityAvailable=firstListenSourceState(projection).continuityAvailable" in choice
    assert (
        "const celebrate=!projection.privacyReviewed&&projection.heard&&!priorInstall&&!reviewingPrivacy"
        "&&continuityAvailable" in choice
    )

    preview_gate = _function("shouldShowHomeContextPreview", "previewRows")
    assert "first.install_origin==='existing'" in preview_gate
    assert "!Object.keys(first).length" in preview_gate


def test_first_listen_distinguishes_configured_unchecked_sources() -> None:
    source_status = _function("firstListenSourceStatus", "firstListenSourceRows")
    assert "configured_unchecked:{state:'idle',label:'Configured · not checked'}" in source_status
    assert "unavailable:{state:'blocked',label:'Unavailable'}" in source_status


def test_privacy_choice_invalidates_the_client_preview_proof() -> None:
    choice = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "_firstListenUi.privacyPreviewValid=false" in choice
    assert "_firstListenUi.privacyPreview='untouched'" in choice
    assert "if(receiptRepairAccepted)" in choice
    assert "else if(code==='preview_required')" in choice
    assert "_firstListenUi.privacyReceiptChoice=requestedEnabled" in choice
    assert "_firstListenUi.privacyReceiptChoice=previewRequiredActiveOff?false:null" in choice
    assert "status:'review_retry'" in choice
    assert "focusTarget=_firstListenUi.privacyReceiptChoice?'firstListenPreviewBtn':'firstListenKeepOffBtn'" in choice
    assert "_firstListenUi.privacyReceiptChoice=null" in choice
    assert "if(focusTarget)document.getElementById(focusTarget)?.focus()" in choice
    assert "resp?.persisted===false" not in choice

    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "!_firstListenUi.privacyPreviewValid" in progress
    assert "keepOffBtn.disabled=!projection.privacyUnlocked" in progress
    assert "const pendingPrivacyChoice=_firstListenUi.privacyReceiptChoice!==null" in progress
    assert "projection.privacy.choice_explicit===true" in progress
    assert "Home context is on, but First Listen did not save this step." in progress
    assert "Home context stays off, but First Listen did not save this step." in progress
    assert "Show your Home details again, then save the choice." in progress
    assert "Save the private choice again." in progress

    preview = _function("renderHomeContextPreview", "retestFirstListenSpeaker")
    assert "payload.status==='review_retry'" in preview
    assert "payload.enabled===true?'Home context is on" in preview


def test_first_listen_actions_fail_closed_on_http_or_payload_errors() -> None:
    cases = (
        (
            "startFirstListen",
            "saveFirstListenAttempt",
            "apiResponse('POST','/api/resume',undefined,FIRST_LISTEN_TIMEOUTS.play)",
            "audio.src=`${_base}${FIRST_LISTEN_STREAM}`",
        ),
        (
            "saveFirstListenAttempt",
            "verifyFirstListen",
            "apiResponse('POST','/api/setup/first-listen/listener-confirm',{heard:true},FIRST_LISTEN_TIMEOUTS.receipt)",
            "_firstListenUi.attemptId=attemptId",
        ),
        (
            "verifyFirstListen",
            "loadHomeContextPreview",
            "apiResponse('POST','/api/setup/first-listen/listener-confirm',"
            "{heard:requestedHeard},FIRST_LISTEN_TIMEOUTS.verify)",
            "_firstListenUi.verification=requestedHeard?'heard':'not_yet'",
        ),
        (
            "loadHomeContextPreview",
            "loadCachedHomeContextDiagnostics",
            "apiResponse('POST','/api/setup/home-context-preview',{})",
            "_firstListenUi.privacyPreviewValid=true",
        ),
        (
            "chooseFirstListenPrivacy",
            "renderHomeContextPreviewGate",
            "apiResponse('PATCH','/api/setup/home-context-choice',{enabled:requestedEnabled})",
            "_firstListenUi.privacyChoice=requestedEnabled",
        ),
    )

    for name, next_name, request, state_advance in cases:
        block = _function(name, next_name)
        assert request in block
        if name != "startFirstListen":
            assert "const resp=payload&&typeof payload==='object'?payload:{}" in block
        guard_text = {
            "startFirstListen": "}else if(!response.ok){",
            "loadHomeContextPreview": "if(!detachedPreview){",
            "chooseFirstListenPrivacy": "if(!choiceSaved){",
        }.get(name, "if(!response.ok||resp?.ok!==true")
        guard = block.index(guard_text)
        assert guard < block.index(state_advance)

    privacy = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "api('PATCH','/api/setup/home-context-choice'" not in privacy
    assert "if(!choiceSaved){" in privacy
    assert privacy.index("if(!choiceSaved){") < privacy.index("_firstListenUi.showSuccess=showCelebration")


def test_first_listen_side_effect_responses_are_bound_to_the_exact_request() -> None:
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "audio.src=`${_base}${FIRST_LISTEN_STREAM}`" in start
    assert "audio.play()" in start
    assert "_firstListenUi.dispatch='starting'" in start
    playing = _function("firstListenStationPlaying", "initFirstListenStationAudio")
    assert "_firstListenUi.dispatch='accepted'" in playing
    assert "firstListenVerifyHeading" in playing

    save = _function("saveFirstListenAttempt", "verifyFirstListen")
    retry_guard = "if(!response.ok||resp?.ok!==true||resp?.heard!==true||resp?.first_listen_achieved!==true){"
    assert retry_guard in save
    assert "resp?.receipt_persisted!==true||!attemptId" in save
    assert save.index(retry_guard) < save.index("_firstListenUi.attemptId=attemptId")

    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    assert "const requestedHeard=Boolean(heard)" in verify
    assert "{heard:requestedHeard}" in verify
    verification_binding = (
        "const verificationMatches=resp?.heard===requestedHeard&&(!requestedHeard||resp?.first_listen_achieved===true)"
    )
    assert verification_binding in verify
    assert "if(!response.ok||resp?.ok!==true||!verificationMatches){" in verify
    assert verify.index("if(!response.ok||resp?.ok!==true||!verificationMatches){") < verify.index(
        "_firstListenUi.verification=requestedHeard?'heard':'not_yet'"
    )

    privacy = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "const requestedEnabled=Boolean(enabled)" in privacy
    assert "{enabled:requestedEnabled}" in privacy
    assert (
        "const receiptRepairAccepted=response.ok===false&&response.status===503&&resp?.ok===false"
        "&&resp?.persisted===true&&enabledMatches&&code==='privacy_receipt_unavailable'"
    ) in privacy
    assert (
        "const choiceSaved=response.ok===true&&resp?.ok===true&&resp?.persisted===true"
        "&&resp?.privacy_reviewed===true&&enabledMatches"
    ) in privacy
    assert "const enabledMatches=typeof resp?.enabled==='boolean'&&resp.enabled===requestedEnabled" in privacy
    assert "_firstListenUi.privacyReceiptChoice=requestedEnabled" in privacy
    assert "_firstListenUi.privacyChoice=requestedEnabled" in privacy
    assert "receiptRepairAccepted?'degraded':'blocked'" in privacy
    assert "code==='privacy_receipt_unavailable'&&!receiptRepairAccepted" in privacy
    assert privacy.index("if(!choiceSaved){") < privacy.index("_firstListenUi.privacyChoice=requestedEnabled")


def test_discovery_and_privacy_preview_require_canonical_detached_envelopes() -> None:
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "audio.src=`${_base}${FIRST_LISTEN_STREAM}`" in start
    assert "startFirstListenClock();" in start
    assert start.index("startFirstListenClock();") < start.index("audio.play()")
    playing = _function("firstListenStationPlaying", "initFirstListenStationAudio")
    assert playing.index("_firstListenUi.dispatch='accepted'") < playing.index("renderFirstListenProgress()")

    preview = _function("loadHomeContextPreview", "loadCachedHomeContextDiagnostics")
    assert (
        "const detachedPreview=response.ok===true&&resp?.ok===true&&resp?.fresh===true"
        "&&Array.isArray(resp?.sent_now)&&resp.sent_now.length===0"
        "&&['useful','ambient_only','empty'].includes(resp?.context_value)"
    ) in preview
    assert "if(!detachedPreview){" in preview
    assert "renderHomeContextPreview(null)" in preview
    assert "{error:{code:'preview_unavailable'}}" in preview
    assert preview.index("if(!detachedPreview){") < preview.index("_firstListenUi.privacyPreviewValid=true")

    privacy = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert (
        "const previewRequiredActiveOff=response.ok===false&&response.status===409&&resp?.ok===false"
        "&&resp?.persisted===true&&resp?.enabled===false&&code==='preview_required'"
    ) in privacy
    assert "_firstListenUi.privacyReceiptChoice=previewRequiredActiveOff?false:null" in privacy
    assert "resp?.enabled===true&&code==='preview_required'" not in privacy


def test_no_ai_key_is_needed_for_first_audio() -> None:
    html = _html()
    assert "No AI key needed." in html
    assert "Welcome and live radio" in html
    assert "New host conversations" in html
    assert "The station already works without AI." in html
    ai_body = re.search(r'<div class="first-listen-body"[^>]*id="firstListenAiBody"[^>]*>', html)
    assert ai_body is not None
    assert "hidden" in ai_body.group(0)
    assert 'aria-hidden="true"' in ai_body.group(0)
    assert "inert" in ai_body.group(0)
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "aiFieldset.disabled=(!privacyMilestone&&projection.haAccess)||!projection.showAi" in progress
    assert "filter(e=>e.key==='llm_keys')" in progress
    assert "firstListenSetStep('firstListenAiStep',configuredKeys.length?'complete':'optional')" in progress
    assert "AI service connected" in progress
    start_block = _function("startFirstListen", "verifyFirstListen")
    assert "setupAnthropicKey" not in start_block
    assert "setupOpenaiKey" not in start_block

    values = _function("firstListenKeyValues", "updateFirstListenKeySaveState")
    update = _function("updateFirstListenKeySaveState", "openFirstListenKeyEditor")
    save = _function("setupSaveKeys", "copySetupSnippet")
    assert "setupAnthropicKey" in values
    assert "setupOpenaiKey" in values
    assert "save.disabled=!firstListenKeyValues().some(Boolean)" in update
    assert "if(anthropic) payload.ANTHROPIC_API_KEY=anthropic" in save
    assert "if(openai) payload.OPENAI_API_KEY=openai" in save
    assert "if(!Object.keys(payload).length)" in save
    assert "Saved keys stay hidden." in html
    assert "Leave a field empty to keep its current value." in html
    assert html.count('placeholder="Leave blank to keep saved key"') == 4


def test_not_yet_has_warm_bounded_repair_and_no_volume_mutation() -> None:
    html = _html()
    repair_start = html.index('id="firstListenRepair"')
    repair_end = html.index('id="firstListenRetestWrap"', repair_start)
    repair = html[repair_start:repair_end]

    assert "Get this device playing." in repair
    assert "Wait a few seconds" in repair
    assert "check mute and volume" in repair
    assert "Confirm the sound is coming from this tab, not another app." in repair
    assert "Technical help is below the journey." in repair
    assert 'id="firstListenRetryBtn"' in repair
    assert "Try this device again" in repair
    assert 'id="firstListenChooseAnotherBtn"' not in html
    assert "Choose another speaker" not in html
    retest = _function("retestFirstListenSpeaker", "startFirstListen")
    assert "_firstListenUi.selectionDirty=true" in retest
    assert "_firstListenUi.retestPending=true" in retest
    assert "_firstListenUi.attemptId=''" in retest
    assert "_firstListenUi.dispatch='ready'" in retest
    assert "_firstListenUi.verification='awaiting'" in retest
    projection = _function("firstListenProjection", "firstListenSourceStatus")
    assert "serverProofMatches=!_firstListenUi.selectionDirty" in projection
    assert "volume_set" not in html
    assert "volume_level" not in _function("startFirstListen", "verifyFirstListen")


def test_unsaved_accepted_attempt_is_recovered_without_replaying() -> None:
    html = _html()
    panel_start = html.index('id="firstListenReceiptRepair"')
    panel_end = html.index('id="firstListenRepair"', panel_start)
    panel = html[panel_start:panel_end]
    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    save = _function("saveFirstListenAttempt", "verifyFirstListen")
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    render_start = html.index("function renderFirstListen(setup")
    render = html[render_start : html.index("function renderSetup", render_start)]
    projection = _function("firstListenProjection", "firstListenSourceStatus")

    assert "The station already started on this device." in panel
    assert "Restore the missing sound check without playing it again." in panel
    assert "This will not replay the station." in panel
    assert 'id="firstListenSaveAttemptBtn"' in panel
    assert "Restore sound check" in panel
    assert 'id="firstListenVerifyActions"' in html
    assert "receipt_unavailable" in verify
    assert "_firstListenUi.dispatch='receipt_failed'" in verify
    assert "_firstListenUi.repairOpen=true" in verify
    assert "const receiptRepairRequired=_firstListenUi.dispatch==='receipt_failed'" in progress
    assert "receiptRepairRequired||listenAccepted?'current'" in progress
    assert "verifyActions.hidden=receiptRepairRequired" in progress
    assert ".first-listen-panel [hidden]," in _css()
    assert "_firstListenUi.repairOpen=true" in save
    assert "_firstListenUi.receiptSaving=true" in save
    assert "_firstListenUi.receiptSaving=false" in save
    assert (
        "apiResponse('POST','/api/setup/first-listen/listener-confirm',"
        "{heard:true},FIRST_LISTEN_TIMEOUTS.receipt)" in save
    )
    assert "/api/setup/first-listen/play" not in save
    assert "resp?.receipt_persisted!==true||!attemptId" in save
    assert "_firstListenUi.dispatch='accepted'" in save
    assert "receiptRepair.hidden=!receiptRepairRequired" in progress
    assert "repair.hidden=!_firstListenUi.repairOpen||receiptRepairRequired" in progress
    assert "_firstListenUi.receiptSaving" in progress
    save_disabled = next(line for line in progress.splitlines() if "saveAttemptBtn.disabled=" in line)
    assert "_firstListenUi.players" not in save_disabled
    assert "hydrateFirstListenReceiptRecovery(first)" not in render
    assert "const durableHeard=Boolean(first.audio_complete||first.heard_at" in render
    assert "const serverAttempt=String(first.accepted_attempt_id||verification.attempt_id||'')" in projection
    assert (
        "const localAttemptMatches=!_firstListenUi.attemptId||!serverAttempt||"
        "_firstListenUi.attemptId===serverAttempt" in projection
    )
    assert "const localProofPending=Boolean(" in projection
    assert "_firstListenUi.dispatch==='receipt_failed'||" in projection
    assert "(_firstListenUi.attemptId&&_firstListenUi.verification!=='heard')" in projection
    assert "!_firstListenUi.selectionDirty&&!proofPending&&localAttemptMatches" in projection
    assert "pendingReceiptProjected" in projection
    assert "!localProofPending&&!pendingReceiptProjected&&serverProofMatches" in projection
    assert "!pendingReceiptProjected&&serverProofMatches" in projection
    assert "/api/setup/first-listen/play" not in render
    assert "/api/setup/first-listen/players" not in html


def test_fixed_error_copy_covers_all_public_first_listen_failures() -> None:
    html = _html()
    # Network exceptions use one fixed client-only safe-state message. Every
    # server reason must otherwise have exactly one explicit UI mapping.
    assert _ui_first_listen_error_codes() == _server_setup_error_codes() | {"persistence_failed"}
    assert "stale_attempt:" not in html

    error_block = _function("firstListenErrorCopy", "firstListenErrorMessage")
    assert "response?.error?.code" in error_block
    assert "response?.error?.message" not in error_block


def test_every_first_listen_error_states_a_failure_and_a_way_out() -> None:
    """Require each error to name the problem and recovery action (principle #5).

    The key-parity test cannot detect empty fields. This check leaves the
    wording flexible.
    """
    entries = re.findall(r"^\s{2}([a-z][a-z0-9_]*):\{(.*)\},$", _first_listen_errors_block(), re.MULTILINE)
    assert len(entries) == len(_ui_first_listen_error_codes())

    for code, body in entries:
        for field in ("title", "message", "action"):
            match = re.search(rf"{field}:'([^']*)'", body)
            assert match and match.group(1).strip(), f"{code}.{field} must contain text"

    media_source = re.search(r"action:'([^']*)'", dict(entries)["media_source_missing"])
    assert media_source is not None and "Mamma Mi Radio" in media_source.group(1)


def test_interactive_subtrees_are_static_and_status_polling_only_patches_them() -> None:
    html = _html()
    for control_id in (
        "firstListenPlayBtn",
        "firstListenHeardBtn",
        "firstListenNotYetBtn",
        "firstListenRetryBtn",
        "firstListenSaveAttemptBtn",
        "firstListenPreviewBtn",
        "firstListenKeepOffBtn",
        "firstListenEnableContextBtn",
        "firstListenRetestBtn",
        "firstListenStationAudio",
    ):
        assert html.count(f'id="{control_id}"') == 1

    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "quickAction" not in progress
    assert "quickCopy" not in progress
    assert "firstListenSetActionTone(playBtn,!listenAccepted)" in progress
    assert "select.replaceChildren" not in progress
    assert "playerSelect').innerHTML" not in progress
    assert "document.activeElement" not in progress
    assert "focus(" not in progress
    assert "if(el.textContent!==message)el.textContent=message" in html


def test_first_listen_controls_have_accessible_busy_and_mobile_contracts() -> None:
    html = _html()
    css = _css()
    assert '<link rel="stylesheet" href="/static/first-listen.css">' in html
    assert ".first-listen-panel .first-listen-review {" in css
    assert "@media (max-width: 430px)" in css
    assert "@media (max-width: 720px)" in css
    assert "width: min(42.5rem, 100%)" in css
    assert 'aria-live="polite" aria-atomic="true"' in html
    assert "setAttribute('aria-busy','true')" in html
    assert "prefers-reduced-motion: reduce" in css


def test_first_listen_uses_truthful_action_copy() -> None:
    html = _html()
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    for copy in ("Preview 16-second welcome", "Start sound check", "Review music readiness", "Review playback",
                 "Review sound check", "Review privacy choice", "Review AI setup"):  # fmt: skip
        assert copy in html
    for copy in ("Start sound check again", "firstListenSetChip('firstListenSpeakerChip','working','Start here')",
                 "Tap Start sound check to hear it on this device."):  # fmt: skip
        assert copy in progress
    assert "Play the station" not in html


def test_first_listen_program_mark_reuses_canonical_favicon() -> None:
    html = _html()
    mark = html[html.index('class="program-mark"') : html.index("program-label")]
    assert all(needle in mark for needle in ('src="/static/favicon.svg"', 'alt=""', "aria-hidden"))
    assert ">Mi<" not in mark
    wordmark = html[html.index('<h1 class="wm">') : html.index("</h1>")]
    assert 'class="mi">Mi</span>' in wordmark


def test_completed_rows_review_inline_without_stealing_current_step() -> None:
    html = _html()
    assert html.count('onclick="toggleFirstListenReview(') == 4
    for key, body, label in (
        ("source", "firstListenSourceBody", "Review music readiness"),
        ("speaker", "firstListenSpeakerBody", "Review playback"),
        ("verify", "firstListenVerifyBody", "Review sound check"),
        ("privacy", "firstListenPrivacyBody", "Review privacy choice"),
    ):
        assert f'data-review-step="{key}"' in html
        assert f'aria-controls="{body}"' in html
        assert label in html
    assert 'aria-expanded="false"' in html

    toggle = _function("toggleFirstListenReview", "toggleFirstListenOptional")
    assert "_firstListenUi.reviewStep=key" in toggle
    assert "_firstListenUi.optionalStep=''" in toggle
    assert "_firstListenUi.reviewTrigger=trigger||null" in toggle
    assert "renderFirstListenProgress()" in toggle
    assert "firstListenSetStep" not in toggle

    close = _function("closeFirstListenExpansion", "toggleFirstListenReview")
    assert "const trigger=_firstListenUi.reviewTrigger" in close
    assert "_firstListenUi.reviewStep=''" in close
    assert "_firstListenUi.optionalStep=''" in close
    assert "if(!focusCurrentFirstListenStep()&&trigger?.isConnected)trigger.focus()" in close
    assert "event.key==='Escape'" in html


def test_guide_audio_is_click_only_local_and_cannot_advance_first_listen() -> None:
    html = _html()
    audio_tag = re.search(r'<audio id="firstListenGuideAudio"[^>]*>', html)
    assert audio_tag is not None
    assert 'preload="none"' in audio_tag.group(0)
    assert "autoplay" not in audio_tag.group(0)
    assert html.count('id="firstListenGuideAudio"') == 1
    assert html.count('class="guide-audio-play"') == 7
    for key in (
        "welcome",
        "sound-check",
        "not-yet",
        "receipt-recovery",
        "privacy",
        "ai",
        "success",
    ):
        assert f'data-guide-key="{key}"' in html
    assert 'data-guide-key="speaker"' not in html

    guides_start = html.index("const FIRST_LISTEN_GUIDES={")
    guides_end = html.index("function initFirstListenTechnicalDetails", guides_start)
    guide_code = html[guides_start:guides_end]
    assert "/static/audio/first_listen/${guide.file}" in guide_code
    # Playback is raced against a load deadline, so the clip still starts from a
    # click and nothing else, but a stalled one cannot wait forever.
    assert "await Promise.race([" in guide_code
    assert "audio.play()," in guide_code
    assert "function resetFirstListenGuideSource(audio)" in guide_code
    assert "audio.removeAttribute('src')" in guide_code
    assert "if(!audio.getAttribute('src'))" in guide_code
    assert "audio.src=`${_base}/static/audio/first_listen/${guide.file}?v=${guide.version}`" in guide_code
    assert "audio.load()" in guide_code
    toggle = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    # A failed load/play must not strand a station audio paused for this guide --
    # see test_every_guide_narration_exit_can_resume_the_station. Anchored on
    # the trailing ';' so an explanatory comment mentioning the same function
    # name cannot satisfy this on its own -- a prior version of this fix was
    # UNTESTED by exactly that gap (mutation-verified against admin.html).
    assert "stopFirstListenGuide();" in toggle.split("}catch(error){", 1)[1]
    init = _function("initFirstListenGuideAudio", "initFirstListenTechnicalDetails")
    error_handler = init.split("audio.addEventListener('error',()=>{", 1)[1].split("});", 1)[0]
    assert "stopFirstListenGuide();" in error_handler
    assert "document.addEventListener('visibilitychange'" in guide_code
    assert "if(!document.hidden)return" in guide_code
    assert "stopFirstListenGuide()" in guide_code
    assert "renderFirstListenProgress()" in guide_code
    for forbidden in (
        "/api/setup/first-listen/play",
        "/api/setup/first-listen/verify",
        "/api/setup/first-listen/listener-confirm",
        "/api/setup/first-listen/receipt/retry",
        "/api/setup/home-context-choice",
        "startFirstListen(",
        "verifyFirstListen(",
        "saveFirstListenAttempt(",
        "chooseFirstListenPrivacy",
        "_firstListenUi.attemptId=",
        "_firstListenUi.dispatch=",
        "_firstListenUi.verification=",
        "_firstListenUi.privacyChoice=",
        "_firstListenUi.showSuccess=",
    ):
        assert forbidden not in guide_code

    lock = _function("firstListenGuideLocksRoomProof", "firstListenGuideIdleLabel")
    assert "dataset.state||''" in lock
    # Only a clip that is actually playing may hold the room proof. Locking on
    # 'loading' let a clip that never arrived disable "Yes, I hear it" and
    # "Not yet" with no way back short of a reload.
    assert "return state==='playing'" in lock
    assert "loading" not in lock
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "const browserGuidePlaying=firstListenGuideLocksRoomProof()" in progress
    assert "playBtn.disabled=" in progress and "speakerBusy||browserGuidePlaying" in progress
    assert "retryBtn.disabled=" in progress and "speakerBusy||browserGuidePlaying" in progress
    assert "heardBtn.disabled=" in progress and "listenComplete||browserGuidePlaying" in progress
    assert "notYetBtn.disabled=" in progress and "listenComplete||browserGuidePlaying" in progress

    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    assert "firstListenGuideLocksRoomProof())return" in verify


def test_existing_install_retest_requires_a_fresh_human_confirmation() -> None:
    html = _html()
    state = html[html.index("const _firstListenUi={") : html.index("const FIRST_LISTEN_GUIDES={")]
    projection = _function("firstListenProjection", "firstListenSourceStatus")
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    retest = _function("retestFirstListenSpeaker", "startFirstListen")
    start = _function("startFirstListen", "saveFirstListenAttempt")
    # Not asserted on here; the call still pins that saveFirstListenAttempt
    # exists in admin.html, because _function raises when the symbol is absent.
    _function("saveFirstListenAttempt", "verifyFirstListen")
    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    render_start = html.index("function renderFirstListen(setup")
    render = html[render_start : html.index("function setupProviderLabels", render_start)]

    assert "retestPending:false" in state
    assert "const serverProofPending=Boolean(serverAttempt" in projection
    assert "const proofPending=_firstListenUi.retestPending||serverProofPending" in projection
    assert "const localProofPending=Boolean(\n    proofPending||" in projection
    assert "const serverProofMatches=!_firstListenUi.selectionDirty&&!proofPending" in projection
    assert "const privacyUnlocked=heard||(existingAccess&&!_firstListenUi.selectionDirty&&!proofPending)" in projection
    assert "proofPending" in projection.split("return{", 1)[1]
    assert "const existingProof=priorInstall&&!_firstListenUi.selectionDirty&&!projection.proofPending" in progress
    assert "_firstListenUi.retestPending=true" in retest
    assert "_firstListenUi.retestPending=false" not in start
    assert "if(!response.ok||resp?.ok!==true||!verificationMatches){" in verify
    assert verify.index("if(!response.ok||resp?.ok!==true||!verificationMatches){") < verify.index(
        "_firstListenUi.retestPending=false"
    )
    heard_branch = verify.split("if(requestedHeard){", 1)[1].split("}else{", 1)[0]
    assert "_firstListenUi.retestPending=false" in heard_branch
    assert "_firstListenUi.retestPending=false" not in verify.split("}else{", 1)[1]
    assert "durableHeard&&!_firstListenUi.retestPending" in render


def test_existing_install_without_a_saved_speaker_routes_to_selection() -> None:
    html = _html()
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    retest = _function("retestFirstListenSpeaker", "startFirstListen")

    assert 'id="firstListenRetestBtn"' in html
    assert 'id="firstListenChooseSpeakerToRetestBtn"' not in html
    assert "Choose a speaker to test" not in html
    assert "if(retestBtn)retestBtn.hidden=false" in progress
    assert "startFirstListen(el)" in retest
    assert "chooseAnotherFirstListenSpeaker" not in retest


def test_fresh_completion_uses_a_separate_success_surface() -> None:
    html = _html()
    assert 'id="firstListenSuccess" aria-labelledby="firstListenSuccessTitle" hidden aria-hidden="true" inert' in html
    assert "Bravo! Your first broadcast is complete." in html
    assert "Your station is on air." in html
    assert "Open full listener" in html
    assert 'onclick="openFirstListenListener()"' in html
    success_actions = html[html.index('class="success-actions"') : html.index('id="firstListenSuccessRepair"')]
    assert success_actions.index("Open full listener") < success_actions.index("Station controls")
    assert "Review choices" in html
    assert "Play the celebration" in html
    assert "Open your station" not in html

    update = _function("updateFirstListenSuccess", "reviewFirstListenChoices")
    assert "const show=Boolean(_firstListenUi.showSuccess)" in update
    assert "journey.toggleAttribute('inert',show)" in update
    assert "success.toggleAttribute('inert',!show)" in update
    assert "success.classList.add('is-celebrating')" in update
    scroll = "success.scrollIntoView({block:'start'})"
    focus = "firstListenSuccessTitle')?.focus({preventScroll:true})"
    assert scroll in update
    assert focus in update
    assert update.index(scroll) < update.index(focus)

    choice = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "const priorInstall=projection.legacy||projection.first.install_origin==='existing'" in choice
    assert "const reviewingPrivacy=_firstListenUi.reviewStep==='privacy'" in choice
    assert "const celebrate=!projection.privacyReviewed&&projection.heard&&!priorInstall&&!reviewingPrivacy" in choice
    assert "_firstListenUi.showSuccess=showCelebration" in choice


def test_setup_alert_uses_canonical_onboarding_requirement_not_optional_ai_todos() -> None:
    setup = _function("renderSetup", "setupRecheck")
    decision_start = setup.index("renderFirstListen(setup,modeLabel,stationLabel)")
    decision_end = setup.index("const alertDot", decision_start)
    decision = setup[decision_start:decision_end]

    assert "const needsAction=setup.onboarding_required===true" in decision
    assert "onboarding_steps" not in decision
    assert "firstListenTabAlert" in setup
    assert "motoreTabAlert" not in setup


def test_existing_setup_inventory_stays_under_advanced_details() -> None:
    html = _html()
    advanced = html.index('id="setupAdvancedDetails"')
    assert "<summary>Technical details and help</summary>" in html
    assert '<div class="technical-body" aria-hidden="true" inert>' in html
    for target in (
        'id="setupIdentity"',
        'id="setupSteps"',
        'id="setupEssentials"',
        'id="setupChecks"',
        'id="setupSnippetWrap"',
    ):
        assert html.index(target) > advanced
    assert "/api/homeassistant/context-candidates" in html
    assert "Cached Home context diagnostics" in html

    init = _function("initFirstListenTechnicalDetails", "toast")
    assert "details.addEventListener('toggle',sync)" in init
    assert "body.setAttribute('aria-hidden',details.open?'false':'true')" in init
    assert "body.toggleAttribute('inert',!details.open)" in init


def test_source_repair_and_sound_lanes_keep_the_existing_first_listen_path_actionable() -> None:
    html = _html()
    assert 'id="firstListenSourceActions"' in html
    assert 'onclick="openMusicSourceTools()"' in html
    assert 'id="firstListenSuccessRepair"' in html
    assert 'id="setupConversationsHeading"' in html
    assert 'id="setupVoiceQualityHeading"' in html
    assert "Choose the clarity, warmth, and presence" in html
    assert "These providers shape how the hosts sound" in html
    assert "They do not add new conversations" not in html

    strip = _function("renderGuidedSetupStrip", "shouldShowHomeContextPreview")
    assert "primary.focus" in strip
    assert "openSetupPanel('source')" in strip

    source_health = _function("firstListenSourceHealthy", "firstListenSourceState")
    assert "source.healthy===true" in source_health
    assert "item.kind!=='recovery'" in source_health
    source_state = _function("firstListenSourceState", "firstListenSourcesHtml")
    assert "firstListenSourceHealthy(source,normalized)" in source_state
    assert "continuity_available===true" in source_state
    sources = _function("renderFirstListenSources", "firstListenPlayerLabel")
    assert "firstListenSourcePreview" in sources
    assert "firstListenSourcePreviewDetails" in sources
    assert "firstListenSourceHealthy(source)" in sources
    assert "previousHealth!=='degraded'" in sources
    assert "previewDetails.dataset.sourceHealth=sourceHealth" in sources
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "const sourceComplete=sourceMilestone&&continuityAvailable" in progress
    assert "sourceKnown&&!continuityAvailable?'repair the music source'" in progress


def test_post_hacs_timing_is_local_and_ends_only_on_heard_confirmation() -> None:
    html = _html()
    assert "mammamiradio:first-listen-ready" in html
    assert "mammamiradio:first-listen-heard" in html
    assert "mammamiradio:first-listen-post-hacs" in html
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "startFirstListenClock();" in start
    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    assert "if(requestedHeard){" in verify
    assert "completeFirstListenClock();" in verify
    assert "No telemetry leaves this browser" in html


def test_step_status_chips_become_visible_once_they_carry_real_state() -> None:
    """The chips ship hidden so nothing stale paints before the first render.

    Every honest signal the journey has — "Review not saved", "Save check",
    "Music missing" — is written through firstListenSetChip, so if that helper
    does not clear `hidden`, the operator is told nothing while the smoke suite
    still reads the text (innerText falls back to textContent on an unrendered
    node). Keep the two halves bound together here.
    """
    html = _html()
    css = _css()

    for chip_id in (
        "firstListenSpeakerChip",
        "firstListenVerifyChip",
        "firstListenPrivacyChip",
        "firstListenAiChip",
    ):
        assert f'id="{chip_id}"' in html, f"{chip_id} disappeared from the journey"

    assert ".first-listen-panel [hidden]" in css
    set_chip = _function("firstListenSetChip", "firstListenSetActionTone")
    assert "el.removeAttribute('hidden')" in set_chip


def test_every_first_listen_mutation_carries_a_deadline_and_a_way_out() -> None:
    """A hung request must not be able to freeze the journey.

    apiResponse only arms an AbortController when a 4th argument is passed, and
    renderFirstListen refuses to hydrate from the server while `busy` is set. So
    a mutation without a deadline latches the whole flow until a reload, with
    nothing on screen explaining why every control went dead.
    """
    html = _html()

    assert "const FIRST_LISTEN_TIMEOUTS={" in html
    assert "function firstListenTimedOut(error){return Boolean(error&&error.name==='AbortError')}" in html

    for route, budget in (("/api/resume", "FIRST_LISTEN_TIMEOUTS.play"),):
        call = re.search(rf"apiResponse\('POST','{re.escape(route)}',[^;]*?\);", html)
        assert call is not None, f"{route} is no longer called through apiResponse"
        assert budget in call.group(0), f"{route} is dispatched with no deadline"

    listener_confirm_calls = [
        match.group(0)
        for match in re.finditer(
            r"apiResponse\('POST','/api/setup/first-listen/listener-confirm',[^;]*?\);",
            html,
        )
    ]
    assert listener_confirm_calls
    for confirm_call in listener_confirm_calls:
        assert "FIRST_LISTEN_TIMEOUTS." in confirm_call, f"{confirm_call} is dispatched with no deadline"

    # Every timeout tells the operator what to do next, never just that it failed.
    for owner, nxt, latch in (
        ("startFirstListen", "saveFirstListenAttempt", "_firstListenUi.busy=false"),
        ("saveFirstListenAttempt", "verifyFirstListen", "_firstListenUi.receiptSaving=false"),
        ("verifyFirstListen", "loadHomeContextPreview", "_firstListenUi.busy=false"),
    ):
        body = _function(owner, nxt)
        assert "firstListenTimedOut(error)" in body, f"{owner} cannot tell a timeout from a failure"
        assert "taking longer than usual" in body, f"{owner} names no way out on a timeout"
        assert "}finally{" in body and latch in body, f"{owner} can leave its latch set"


def test_a_slow_or_missing_guide_clip_cannot_block_setup() -> None:
    """Narration decorates the journey. It is never allowed to gate it.

    A clip whose bytes never arrive settles neither play() nor an error event,
    so without a deadline the container stays on 'loading' forever. That state
    used to hold the room-proof lock, which disabled "Yes, I hear it" and
    "Not yet" with no message and no exit short of a page reload.
    """
    html = _html()

    assert "const FIRST_LISTEN_GUIDE_LOAD_MS=" in html
    toggle = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    assert "await Promise.race([" in toggle
    assert "guide_audio_timeout" in toggle
    assert "FIRST_LISTEN_GUIDE_LOAD_MS" in toggle
    # The deadline is always cleared, so a clip that plays leaves no pending timer.
    assert "}finally{" in toggle and "clearTimeout(deadline)" in toggle
    # A timed-out clip lands in the error state with its retry affordance.
    assert "container.dataset.state='error'" in toggle
    assert "button.textContent='Try example again'" in toggle

    # The lock itself must not depend on a state a stalled clip can reach.
    lock = _function("firstListenGuideLocksRoomProof", "firstListenGuideIdleLabel")
    assert "return state==='playing'" in lock
    assert "loading" not in lock


def test_a_superseded_guide_clip_never_tidies_up_the_shared_player() -> None:
    """All remaining clips share one <audio>, so attempts must not touch each other.

    Clicking a second guide calls stopFirstListenGuide, whose load() rejects the
    first pending play() with AbortError. Without a ticket, that older attempt's
    catch ran resetFirstListenGuideSource on the shared element and killed the
    clip the second click had just started, leaving both buttons on the error
    label when nothing had actually failed.
    """
    html = _html()

    assert html.count('<audio id="firstListenGuideAudio"') == 1
    assert "let _firstListenGuideAttempt=0;" in html

    toggle = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    assert "const attempt=++_firstListenGuideAttempt;" in toggle
    # The ticket is taken before the await, and both outcomes check it.
    assert toggle.index("const attempt=++_firstListenGuideAttempt;") < toggle.index("await Promise.race([")
    assert toggle.count("if(attempt!==_firstListenGuideAttempt)return;") == 2
    success_guard = toggle.index("if(attempt!==_firstListenGuideAttempt)return;")
    assert success_guard < toggle.index("container.dataset.state='playing'")
    assert toggle.index("}catch(error){") < toggle.rindex("if(attempt!==_firstListenGuideAttempt)return;")
    last_guard = toggle.rindex("if(attempt!==_firstListenGuideAttempt)return;")
    assert last_guard < toggle.index("stopFirstListenGuide()", last_guard)


def test_leaving_first_listen_releases_the_station_audio_element() -> None:
    """The hidden station element must be stopped on every way out of the step.

    `hidden` is display:none and does NOT pause media. Without a teardown the
    admin tab keeps its own hub subscription while `openListener()` opens a
    second one in a new tab, and the operator hears two unsynchronized copies
    of the same live stream at the moment onboarding is supposed to land.
    """
    html = _html()
    assert "stopFirstListenStationAudio" in html

    open_listener = _function("openListener", "jamendoFailureHint")
    assert "stopFirstListenStationAudio()" in open_listener
    assert open_listener.index("stopFirstListenStationAudio()") < open_listener.index("window.open(")

    finalize = _function("finalizeFirstListenCompletion", "showAdminTab")
    assert "stopFirstListenStationAudio()" in finalize
    assert finalize.index("stopFirstListenStationAudio()") < finalize.index("syncFirstListenSetupMount()")

    open_listener_exit = _function("openFirstListenListener", "openFirstListenStation")
    assert open_listener_exit.index("showAdminTab('scaletta'") < open_listener_exit.index("openListener()")

    open_station = _function("openFirstListenStation", "startFirstListenClock")
    assert "showAdminTab('scaletta'" in open_station

    # Every other tab switch (direct clicks, "Repair music source", any future
    # caller of showAdminTab) is covered separately -- see
    # test_leaving_the_setup_tab_by_any_route_stops_the_station_audio.
    # Guide narration pauses rather than tearing down -- see
    # test_guide_narration_pauses_the_station_instead_of_tearing_it_down.


def test_first_listen_force_start_is_confirmed_not_silent() -> None:
    """Onboarding must not silently rebuild the station with no runway.

    Force Start sets force_recovery_active and resumes with nothing playable.
    The producer desk gates the identical call behind a confirm; First Listen
    is the one surface that should be MORE careful, not less.
    """
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "/api/resume?force=true" in start
    assert "window.confirm(FORCE_RESUME_CONFIRM_COPY)" in start
    assert start.index("window.confirm(FORCE_RESUME_CONFIRM_COPY)") < start.index("/api/resume?force=true")
    # A failed force must not fall through to loading the stream anyway.
    assert "forceResponse.ok" in start


def test_first_listen_renders_the_servers_own_authored_error_copy() -> None:
    """A dependency raising HTTPException(detail={...}) serializes as {"detail": {...}}.

    The client's usual accessor reads {"ok":false,"error":{...}}, so without a
    detail branch the untrusted-host 403 loses its authored next step and the
    operator sees the generic fallback instead of "Reopen /admin using this
    machine's local IP".
    """
    copy_fn = _function("firstListenErrorCopy", "firstListenErrorMessage")
    assert "response?.detail" in copy_fn
    assert "authored.title" in copy_fn
    assert "authored.action" in copy_fn


def test_first_listen_step_copy_names_this_device_not_a_speaker_or_room() -> None:
    """Leadership principle #5: every word on screen is product copy.

    The step no longer offers a speaker or a room, so it must not ask for one.
    """
    html = _html()
    for stale in (
        "Choose one room",
        "One speaker in your home",
        "Confirm that you heard the speaker first",
        "Tell us when you hear the station in the room",
        "Choose one room, then play the opening there",
    ):
        assert stale not in html, f"stale speaker/room copy still rendered: {stale!r}"


def test_first_listen_force_start_decline_never_calls_undefined_setup() -> None:
    """Regression: the decline path called renderFirstListen(_setup), an

    identifier defined nowhere in the page. The ReferenceError was swallowed
    by startFirstListen's own catch block, which overwrote the authored
    "give the tape decks a few seconds" message with a generic error and
    the wrong dispatch state.
    """
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "_setup" not in start
    assert "renderFirstListen(" not in start


def test_first_listen_non_ok_resume_fails_closed_before_opening_the_stream() -> None:
    """A resume failure other than the force-available 503 must not fall through

    to playback. apiResponse does not throw for 401/403/500, so without an
    explicit return the stream opens anyway and the server's authored recovery
    copy is replaced by whatever the audio element reports.
    """
    start = _function("startFirstListen", "saveFirstListenAttempt")
    guard = """      if(document.body.dataset.stopped==='true'){
        _firstListenUi.dispatch='rejected';
        firstListenSetStatus('firstListenSpeakerStatus',detail,'blocked');
        return;
      }"""
    # The return is the whole guard. Asserting the branch as one block keeps a
    # later edit from deleting it while the surrounding lines still match.
    assert guard in start
    assert start.index(guard) < start.index("audio.src=`${_base}${FIRST_LISTEN_STREAM}`")

    # /api/resume reports failures as {ok:false, error:"<sentence>"}.
    # firstListenErrorMessage only reads {detail:{...}} or {error:{code}}, so
    # using it here silently replaces the server's wording with the generic
    # fallback. transportFailureCopy is the reader for this endpoint.
    assert "transportFailureCopy(payload,FIRST_LISTEN_RESUME_FAILURE_COPY)" in start
    assert "firstListenErrorMessage(payload)" not in start

    # A running station whose marker write failed still has audio on air.
    # Failing closed there would cost real playback over a bookkeeping error.
    assert "firstListenSetStatus('firstListenSpeakerStatus',detail,'degraded');" in start


def test_first_listen_force_start_accepts_running_station_ok_without_recovering() -> None:
    """Force Start succeeds on ok:true even when recovering is false.

    While the confirm dialog is open, another tab (or Resume) may already have
    put the station on air. The server then answers {"ok": true, "recovering":
    false} — a start, not a rebuild. Requiring recovering===true shows a false
    failure and drops First Listen back to idle while audio is playing (#1022).
    """
    start = _function("startFirstListen", "saveFirstListenAttempt")
    assert "forcePayload?.ok===true" in start
    assert "forcePayload?.recovering===true" not in start
    # Genuine force failures must keep the server's sentence, not a fixed string.
    assert "transportFailureCopy(forcePayload," in start
    assert start.index("transportFailureCopy(forcePayload,") < start.index("audio.src=`${_base}${FIRST_LISTEN_STREAM}`")


def test_guide_narration_pauses_the_station_instead_of_tearing_it_down() -> None:
    """A hard teardown left steps 4-5 (no Play control) with no way back to

    audio once a guide clip was played. Pausing is reversible; a full
    pause/removeAttribute('src')/load() teardown is not.
    """
    toggle_guide = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    assert "stopFirstListenStationAudio()" not in toggle_guide
    assert "stationAudio.pause()" in toggle_guide
    assert "stationPausedForGuide=true" in toggle_guide

    stop_guide = _function("stopFirstListenGuide", "toggleFirstListenGuide")
    assert "stationPausedForGuide" in stop_guide
    assert "stationAudio.play()" in stop_guide

    stop_station = _function("stopFirstListenStationAudio", "firstListenStationPlaying")
    assert "stationPausedForGuide=false" in stop_station


def test_guide_resume_playing_event_does_not_replay_a_confirmed_sound_check() -> None:
    """stopFirstListenGuide() resumes the station element with play(), firing

    the same 'playing' listener a genuine start does. Regression: it
    unconditionally reset verification to 'awaiting' and stole focus back to
    the sound-check heading, knocking an already-confirmed listener back from
    the privacy or success step on every guide clip played afterward.
    """
    playing = _function("firstListenStationPlaying", "initFirstListenStationAudio")
    # The whole body is gated, not individual effects. A resume must not touch
    # dispatch or repairOpen either: both repair panels host their own guide
    # clip, so clearing repairOpen when that clip ends hides the only control
    # that can still persist a failed sound check.
    assert "if(_firstListenUi.dispatch!=='starting')return;" in playing
    early_return = playing.index("if(_firstListenUi.dispatch!=='starting')return;")
    for effect in (
        "_firstListenUi.selectionDirty=false;",
        "_firstListenUi.dispatch='accepted';",
        "_firstListenUi.verification='awaiting';",
        "_firstListenUi.repairOpen=false;",
        "firstListenVerifyHeading",
    ):
        assert effect in playing, effect
        # Presence alone does not prevent the regression: a copy of any effect
        # placed above the gate satisfies a plain `in` check while the ungated
        # write still wins at runtime.
        assert playing.count(effect) == 1, effect
        assert early_return < playing.index(effect), effect

    # The gate only holds because the guide-resume path reaches play() without
    # claiming a fresh start. If that ever sets dispatch='starting', this
    # function can no longer tell a resume from a real start.
    stop_guide = _function("stopFirstListenGuide", "toggleFirstListenGuide")
    assert "stationAudio.play()" in stop_guide
    assert "dispatch='starting'" not in stop_guide


def test_leaving_the_setup_tab_by_any_route_stops_the_station_audio() -> None:
    """The hidden station element must release its hub subscription on every

    tab switch away from Setup, not only the two exits that call it
    explicitly (openListener, openFirstListenStation) -- direct tab-bar
    clicks and the "Repair music source" shortcut both route through
    showAdminTab and left it playing as a phantom listener.
    """
    show_tab = _function("showAdminTab", "syncFirstListenSetupMount")
    assert "stopFirstListenStationAudio()" in show_tab
    assert show_tab.index("stopFirstListenGuide()") < show_tab.index("stopFirstListenStationAudio()")


def test_active_setup_csrf_stale_403_is_structured_not_a_bare_string() -> None:
    """The two CSRF-failure 403s must carry the same {code,title,message,action}

    shape as the untrusted-host 403, or firstListenErrorCopy falls back to
    the generic message and the authored "reload the dashboard" fix never
    reaches the screen.
    """
    src = STREAMER_PY.read_text(encoding="utf-8")
    assert src.count("detail=_active_setup_csrf_stale_detail()") == 2
    assert '"code": "active_setup_csrf_stale"' in src
    assert '"action": "Reload /admin, then continue First Listen."' in src


def test_every_guide_narration_exit_can_resume_the_station() -> None:
    """The 'ended' and 'error' events on the guide <audio> element, and the

    play()/timeout catch inside toggleFirstListenGuide, must route through
    stopFirstListenGuide() rather than hand-roll the same teardown -- letting
    a 12-second clip simply finish is the ordinary case, not an edge case,
    and a hand-rolled exit silently strands the station paused with no
    recovery at steps 4-5 (no Play control there).
    """
    init_guide_audio = _function("initFirstListenGuideAudio", "initFirstListenTechnicalDetails")
    # Anchored on ';' -- see the comment on the sibling assertion in
    # test_guide_audio_is_click_only_local_and_cannot_advance_first_listen for
    # why a bare-name check is not enough.
    ended_handler = init_guide_audio.split("addEventListener('ended',()=>{", 1)[1]
    assert "stopFirstListenGuide();" in ended_handler.split("});", 1)[0]
    error_handler = init_guide_audio.split("addEventListener('error',()=>{", 1)[1]
    assert "stopFirstListenGuide();" in error_handler.split("});", 1)[0]

    toggle_guide = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    assert "}catch(error){" in toggle_guide
    catch_body = toggle_guide[toggle_guide.index("}catch(error){") :]
    assert "stopFirstListenGuide();" in catch_body


def test_guide_playback_stall_is_recovered_by_a_watchdog() -> None:
    """A live browser reproduced a real stall: sound-check.mp3 (9.336s) froze

    at 8.595s and never fired 'ended'. Nothing else in the page notices a
    started clip that silently stops delivering timeupdate/ended -- the app
    UI kept reading "Pause example" and the station stayed paused forever,
    because every recovery path this session built (see
    test_every_guide_narration_exit_can_resume_the_station) depends on that
    event firing at all. This pins the second-layer safety net: a timer keyed
    to the clip's own duration that forces the same teardown regardless.
    """
    toggle = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    assert "audio.duration>0" in toggle
    assert "_firstListenGuideWatchdog=setTimeout(" in toggle
    watchdog_body = toggle.split("_firstListenGuideWatchdog=setTimeout(()=>{", 1)[1].split("},audio.duration", 1)[0]
    # Must not fire on a superseded attempt, a clip that legitimately ended,
    # or a clip the operator deliberately paused mid-playback (that reads as
    # dataset.state=='paused', not 'playing' -- see the manual pause-toggle
    # branch earlier in this same function).
    assert "attempt!==_firstListenGuideAttempt" in watchdog_body
    assert "audio.ended" in watchdog_body
    assert "dataset.state!=='playing'" in watchdog_body
    assert "stopFirstListenGuide();" in watchdog_body

    init_guide_audio = _function("initFirstListenGuideAudio", "initFirstListenTechnicalDetails")
    assert "let _firstListenGuideWatchdog=null;" not in init_guide_audio  # declared once, near the attempt ticket

    stop_guide = _function("stopFirstListenGuide", "toggleFirstListenGuide")
    assert "clearTimeout(_firstListenGuideWatchdog)" in stop_guide
