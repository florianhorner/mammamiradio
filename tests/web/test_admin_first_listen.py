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


def _ui_first_listen_error_codes() -> set[str]:
    html = _html()
    start = html.index("const FIRST_LISTEN_ERRORS={")
    end = html.index("\n};", start)
    return set(re.findall(r"^\s{2}([a-z][a-z0-9_]*):\{", html[start:end], re.MULTILINE))


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
    assert "Hear Mamma Mi Radio in one room." in html
    assert "Meet Marco and Giulia" in html
    assert "Choose one room" in html
    assert "Check the sound" in html
    assert "Choose whether the hosts can use Home details" in html
    assert "Add new conversations between songs" in html
    assert "Step 1 of 5. Next: choose one room." in html

    step = _function("firstListenSetStep", "focusCurrentFirstListenStep")
    assert "const current=state==='current'" in step
    assert "const reviewing=_firstListenUi.reviewStep===key" in step
    assert "const optionalOpen=_firstListenUi.optionalStep===key" in step
    assert "if(current)step.setAttribute('aria-current','step');else step.removeAttribute('aria-current')" in step
    assert "const anotherExpansion=Boolean(_firstListenUi.reviewStep||_firstListenUi.optionalStep)" in step
    assert "const visible=reviewing||optionalOpen||(current&&!anotherExpansion)" in step
    assert "body.hidden=!visible" in step
    assert "body.setAttribute('aria-hidden',visible?'false':'true')" in step
    assert "body.toggleAttribute('inert',!visible)" in step
    assert "review.hidden=state!=='complete'" in step
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

    mount = _function("initFirstListenPanelMount", "initTabs")
    assert "mount.append(details)" in mount

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
    assert "required&&!_adminTabUserInteracted" in resolve
    assert "showAdminTab('setup',{render:true,persist:false})" in resolve
    assert "focus(" not in resolve

    manual = _function("openSetupPanel", "firstListenEntryRequired")
    assert "showAdminTab('setup',{render:true,persist:true})" in manual
    assert "details.dataset.userPinned='true'" in manual
    assert "tab-motore" not in manual


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


def test_speaker_controls_use_active_post_routes_and_exact_media_source() -> None:
    html = _html()
    assert "apiResponse('POST','/api/setup/first-listen/players',{})" in html
    assert "apiResponse('POST','/api/setup/first-listen/play',{entity_id:entityId})" in html
    assert "apiResponse('POST','/api/setup/first-listen/receipt/retry',{entity_id:entityId})" in html
    assert "apiResponse('POST','/api/setup/first-listen/verify',{attempt_id:attemptId,heard:requestedHeard})" in html
    assert "media-source://mammamiradio/live" in html
    assert "Play in this room" in html
    assert "Yes, I hear it" in html
    assert "Not yet" in html
    assert 'type="radio"' not in html  # choices are built safely after deliberate discovery

    players = _function("renderFirstListenPlayers", "renderFirstListenShow")
    assert "input.type='radio'" in players
    assert "input.name='first-listen-player'" in players
    assert "input.checked=entityId===current" in players
    assert "firstListenPlayerChanged(select)" in players
    assert "select.value=entityId" in players
    assert "players[0]" not in players
    assert "volume_set" not in html
    assert "volume_level" not in _function("startFirstListen", "saveFirstListenAttempt")


def test_home_assistant_labels_are_added_with_text_content() -> None:
    player_block = _function("renderFirstListenPlayers", "renderFirstListenShow")
    preview_block = _function("renderHomeContextPreview", "findFirstListenPlayers")

    assert "const friendlyName=firstListenPlayerLabel(player)" in player_block
    assert "option.textContent=friendlyName" in player_block
    assert "copy.textContent=friendlyName" in player_block
    assert "select.replaceChildren()" in player_block
    assert ".innerHTML" not in player_block
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
    assert "Your existing speaker choice still stands. Review your Home details next." in progress

    html = _html()
    render_start = html.index("function renderFirstListen(setup")
    setup = html[render_start : html.index("function renderSetup", render_start)]
    assert "Your existing station stays as it is." in setup
    assert "You do not need to replay the speaker." in setup
    assert "/api/setup/first-listen/play',{entity_id" not in setup + progress

    choice = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "const celebrate=!projection.privacyReviewed&&projection.heard&&!priorInstall&&!reviewingPrivacy" in choice

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

    preview = _function("renderHomeContextPreview", "findFirstListenPlayers")
    assert "payload.status==='review_retry'" in preview
    assert "payload.enabled===true?'Home context is on" in preview


def test_first_listen_actions_fail_closed_on_http_or_payload_errors() -> None:
    cases = (
        (
            "findFirstListenPlayers",
            "firstListenPlayerChanged",
            "apiResponse('POST','/api/setup/first-listen/players',{})",
            "_firstListenUi.players=players",
        ),
        (
            "startFirstListen",
            "saveFirstListenAttempt",
            "apiResponse('POST','/api/setup/first-listen/play',{entity_id:entityId})",
            "_firstListenUi.dispatch='accepted'",
        ),
        (
            "saveFirstListenAttempt",
            "verifyFirstListen",
            "apiResponse('POST','/api/setup/first-listen/receipt/retry',{entity_id:entityId})",
            "_firstListenUi.attemptId=attemptId",
        ),
        (
            "verifyFirstListen",
            "loadHomeContextPreview",
            "apiResponse('POST','/api/setup/first-listen/verify',{attempt_id:attemptId,heard:requestedHeard})",
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
        assert "const resp=payload&&typeof payload==='object'?payload:{}" in block
        guard_text = {
            "findFirstListenPlayers": "if(!discoveryReady){",
            "loadHomeContextPreview": "if(!detachedPreview){",
            "chooseFirstListenPrivacy": "if(!choiceSaved){",
        }.get(name, "if(!response.ok||resp?.ok!==true")
        guard = block.index(guard_text)
        assert guard < block.index(state_advance)

    privacy = _function("chooseFirstListenPrivacy", "renderHomeContextPreviewGate")
    assert "api('PATCH','/api/setup/home-context-choice'" not in privacy
    assert "if(!choiceSaved){" in privacy
    assert privacy.index("if(!choiceSaved){") < privacy.index("_firstListenUi.showSuccess=celebrate")


def test_first_listen_side_effect_responses_are_bound_to_the_exact_request() -> None:
    start = _function("startFirstListen", "saveFirstListenAttempt")
    partial_play = (
        "const acceptedWithoutReceipt=response.ok===false&&response.status===503&&resp?.ok===false"
        "&&resp?.accepted===true&&resp?.receipt_persisted===false&&resp?.entity_id===entityId"
        "&&resp?.error?.code==='receipt_unavailable'"
    )
    assert partial_play in start
    assert "if(!response.ok||resp?.ok!==true||resp?.accepted!==true||resp?.entity_id!==entityId){" in start
    assert "resp?.receipt_persisted!==true||!attemptId" in start
    assert "resp?.error?.code==='receipt_unavailable'?{error:{code:'invalid_request'}}:resp" in start
    assert start.index(partial_play) < start.index("_firstListenUi.dispatch='receipt_failed'")
    assert start.index("resp?.entity_id!==entityId") < start.index("_firstListenUi.dispatch='accepted'")

    save = _function("saveFirstListenAttempt", "verifyFirstListen")
    retry_guard = "if(!response.ok||resp?.ok!==true||resp?.accepted!==true||resp?.entity_id!==entityId){"
    assert retry_guard in save
    assert "resp?.receipt_persisted!==true||!attemptId" in save
    assert save.index(retry_guard) < save.index("_firstListenUi.attemptId=attemptId")

    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    assert "const requestedHeard=Boolean(heard)" in verify
    assert "{attempt_id:attemptId,heard:requestedHeard}" in verify
    verification_binding = (
        "const verificationMatches=resp?.attempt_id===attemptId&&resp?.heard===requestedHeard"
        "&&(!requestedHeard||resp?.first_listen_achieved===true)"
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
    discovery = _function("findFirstListenPlayers", "firstListenPlayerChanged")
    assert (
        "const discoveryReady=response.ok===true&&resp?.ok===true&&resp?.media_source_ready===true"
        "&&resp?.media_source_uri===FIRST_LISTEN_MEDIA_SOURCE"
    ) in discovery
    assert "if(!discoveryReady){" in discovery
    assert "{error:{code:'media_source_missing'}}" in discovery
    assert discovery.index("if(!discoveryReady){") < discovery.index("_firstListenUi.players=players")
    assert discovery.index("if(!discoveryReady){") < discovery.index("startFirstListenClock();")
    transport_catch = discovery.split("}catch(error){", 1)[1]
    assert "_firstListenUi.players=[]" in transport_catch
    assert "const projectedRecovery=firstListenReceiptRecoveryEntity(firstListenProjection().first)" in transport_catch
    assert "_firstListenUi.dispatch==='receipt_failed'?_firstListenUi.selectedEntityId:''" in transport_catch
    assert "renderFirstListenPlayers([], '', pendingRecovery)" in transport_catch

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

    assert "Get the room playing." in repair
    assert "Wait a few seconds" in repair
    assert "check mute and volume" in repair
    assert "Check that the saved speaker is the room you meant." in repair
    assert "Technical help is below the journey." in repair
    assert 'id="firstListenRetryBtn"' in repair
    assert "Try this room again" in repair
    assert 'id="firstListenChooseAnotherBtn"' in repair
    assert "Choose another speaker" in repair
    choose_another = _function("chooseAnotherFirstListenSpeaker", "startFirstListen")
    assert "_firstListenUi.selectionDirty=true" in choose_another
    assert "_firstListenUi.retestPending=true" in choose_another
    assert "_firstListenUi.selectedEntityId=''" in choose_another
    assert "_firstListenUi.attemptId=''" in choose_another
    assert "_firstListenUi.dispatch='ready'" in choose_another
    assert "_firstListenUi.verification='awaiting'" in choose_another
    assert "document.querySelectorAll('#firstListenPlayerChoices input')" in choose_another
    assert "input.checked=false" in choose_another
    projection = _function("firstListenProjection", "firstListenSourceStatus")
    assert "serverProofMatches=!_firstListenUi.selectionDirty" in projection
    assert "volume_set" not in html
    assert "volume_level" not in _function("startFirstListen", "verifyFirstListen")


def test_unsaved_accepted_attempt_is_recovered_without_replaying() -> None:
    html = _html()
    panel_start = html.index('id="firstListenReceiptRepair"')
    panel_end = html.index('id="firstListenRepair"', panel_start)
    panel = html[panel_start:panel_end]
    start = _function("startFirstListen", "saveFirstListenAttempt")
    save = _function("saveFirstListenAttempt", "verifyFirstListen")
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    recovery_entity = _function("firstListenReceiptRecoveryEntity", "hydrateFirstListenReceiptRecovery")
    hydrate = _function("hydrateFirstListenReceiptRecovery", "renderFirstListenPlayers")
    players = _function("renderFirstListenPlayers", "renderFirstListenProgress")
    discovery = _function("findFirstListenPlayers", "firstListenPlayerChanged")
    render_start = html.index("function renderFirstListen(setup")
    render = html[render_start : html.index("function renderSetup", render_start)]
    projection = _function("firstListenProjection", "firstListenSourceStatus")

    assert "Home Assistant already sent the station." in panel
    assert "Restore the missing sound check without playing it again." in panel
    assert "This will not replay the room speaker." in panel
    assert 'id="firstListenSaveAttemptBtn"' in panel
    assert "Restore sound check" in panel
    assert 'id="firstListenVerifyActions"' in html
    assert "const acceptedWithoutReceipt=" in start
    assert "if(acceptedWithoutReceipt)" in start
    assert "_firstListenUi.attemptId=''" in start
    assert "_firstListenUi.dispatch='receipt_failed'" in start
    assert "_firstListenUi.repairOpen=true" in start
    assert "const receiptRepairRequired=_firstListenUi.dispatch==='receipt_failed'" in progress
    assert "receiptRepairRequired||listenAccepted?'current'" in progress
    assert "verifyActions.hidden=receiptRepairRequired" in progress
    assert ".first-listen-panel [hidden]," in _css()
    assert "_firstListenUi.repairOpen=true" in save
    assert "_firstListenUi.receiptSaving=true" in save
    assert "_firstListenUi.receiptSaving=false" in save
    assert "apiResponse('POST','/api/setup/first-listen/receipt/retry',{entity_id:entityId})" in save
    assert "/api/setup/first-listen/play" not in save
    assert "resp?.receipt_persisted!==true||!attemptId" in save
    assert "_firstListenUi.dispatch='accepted'" in save
    assert "code==='receipt_recovery_missing'" in save
    assert "_firstListenUi.dispatch='ready'" in save
    assert "focusTarget='firstListenPlayBtn'" in save
    assert "receiptRepair.hidden=!receiptRepairRequired" in progress
    assert "repair.hidden=!_firstListenUi.repairOpen||receiptRepairRequired" in progress
    assert "_firstListenUi.receiptSaving" in progress
    save_disabled = next(line for line in progress.splitlines() if "saveAttemptBtn.disabled=" in line)
    assert "_firstListenUi.players" not in save_disabled

    assert "recovery.available!==true" in recovery_entity
    assert "entityId.startsWith('media_player.')" in recovery_entity
    assert "!entityId||_firstListenUi.selectionDirty" in hydrate
    assert "_firstListenUi.attemptId=''" in hydrate
    assert "_firstListenUi.selectedEntityId=entityId" in hydrate
    assert "_firstListenUi.dispatch='receipt_failed'" in hydrate
    assert "_firstListenUi.repairOpen=true" in hydrate
    assert "startFirstListen(" not in hydrate
    assert "pendingRecoveryEntityId" in players
    assert "current===pendingRecovery" in players
    assert "Previously selected speaker" in players
    assert "hydrateFirstListenReceiptRecovery(resp)" in discovery
    assert "renderFirstListenPlayers(players,saved,pendingRecovery)" in discovery
    assert "const recoveredEntity=hydrateFirstListenReceiptRecovery(first)" in render
    assert "if(!recoveredEntity&&attempt" in render
    assert "if(!recoveredEntity&&!_firstListenUi.retestPending&&projectionMatchesSelection" in render
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
    assert "/api/setup/first-listen/play" not in hydrate + render


def test_fixed_error_copy_covers_all_public_first_listen_failures() -> None:
    html = _html()
    # Network exceptions use one fixed client-only safe-state message. Every
    # server reason must otherwise have exactly one explicit UI mapping.
    assert _ui_first_listen_error_codes() == _server_setup_error_codes() | {"persistence_failed"}
    assert "stale_attempt:" not in html

    error_block = _function("firstListenErrorCopy", "firstListenErrorMessage")
    assert "response?.error?.code" in error_block
    assert "response?.error?.message" not in error_block


def test_interactive_subtrees_are_static_and_status_polling_only_patches_them() -> None:
    html = _html()
    for control_id in (
        "firstListenFindPlayersBtn",
        "firstListenPlayerChoices",
        "firstListenPlayerSelect",
        "firstListenPlayBtn",
        "firstListenHeardBtn",
        "firstListenNotYetBtn",
        "firstListenRetryBtn",
        "firstListenChooseAnotherBtn",
        "firstListenSaveAttemptBtn",
        "firstListenPreviewBtn",
        "firstListenKeepOffBtn",
        "firstListenEnableContextBtn",
        "firstListenRetestBtn",
        "firstListenChooseSpeakerToRetestBtn",
    ):
        assert html.count(f'id="{control_id}"') == 1

    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "quickAction" not in progress
    assert "quickCopy" not in progress
    assert "firstListenSetActionTone(playBtn,_firstListenUi.players.length>0&&!listenAccepted)" in progress
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
    assert "width: min(42.5rem, 100%)" in css
    assert 'aria-live="polite" aria-atomic="true"' in html
    assert "setAttribute('aria-busy','true')" in html
    assert "prefers-reduced-motion: reduce" in css


def test_completed_rows_review_inline_without_stealing_current_step() -> None:
    html = _html()
    assert html.count('onclick="toggleFirstListenReview(') == 4
    for key, body in (
        ("source", "firstListenSourceBody"),
        ("speaker", "firstListenSpeakerBody"),
        ("verify", "firstListenVerifyBody"),
        ("privacy", "firstListenPrivacyBody"),
    ):
        assert f'data-review-step="{key}"' in html
        assert f'aria-controls="{body}"' in html
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
    assert html.count('class="guide-audio-play"') == 8
    for key in (
        "welcome",
        "speaker",
        "sound-check",
        "not-yet",
        "receipt-recovery",
        "privacy",
        "ai",
        "success",
    ):
        assert f'data-guide-key="{key}"' in html

    guides_start = html.index("const FIRST_LISTEN_GUIDES={")
    guides_end = html.index("function initFirstListenTechnicalDetails", guides_start)
    guide_code = html[guides_start:guides_end]
    assert "/static/audio/first_listen/${guide.file}" in guide_code
    assert "await audio.play()" in guide_code
    assert "function resetFirstListenGuideSource(audio)" in guide_code
    assert "audio.removeAttribute('src')" in guide_code
    assert "if(!audio.getAttribute('src'))" in guide_code
    assert "audio.src=`${_base}/static/audio/first_listen/${guide.file}?v=${guide.version}`" in guide_code
    assert "audio.load()" in guide_code
    toggle = _function("toggleFirstListenGuide", "initFirstListenGuideAudio")
    assert "resetFirstListenGuideSource(audio)" in toggle.split("}catch(error){", 1)[1]
    init = _function("initFirstListenGuideAudio", "initFirstListenTechnicalDetails")
    error_handler = init.split("audio.addEventListener('error',()=>{", 1)[1]
    assert "resetFirstListenGuideSource(audio)" in error_handler
    assert "document.addEventListener('visibilitychange'" in guide_code
    assert "if(!document.hidden)return" in guide_code
    assert "stopFirstListenGuide()" in guide_code
    assert "renderFirstListenProgress()" in guide_code
    for forbidden in (
        "/api/setup/first-listen/play",
        "/api/setup/first-listen/verify",
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
    assert "state==='loading'||state==='playing'" in lock
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
    save = _function("saveFirstListenAttempt", "verifyFirstListen")
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
    assert "_firstListenUi.retestPending=false" not in save
    assert "if(!response.ok||resp?.ok!==true||!verificationMatches){" in verify
    assert verify.index("if(!response.ok||resp?.ok!==true||!verificationMatches){") < verify.index(
        "_firstListenUi.retestPending=false"
    )
    heard_branch = verify.split("if(requestedHeard){", 1)[1].split("}else{", 1)[0]
    assert "_firstListenUi.retestPending=false" in heard_branch
    assert "_firstListenUi.retestPending=false" not in verify.split("}else{", 1)[1]
    assert "!_firstListenUi.retestPending&&projectionMatchesSelection" in render


def test_existing_install_without_a_saved_speaker_routes_to_selection() -> None:
    html = _html()
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    retest = _function("retestFirstListenSpeaker", "startFirstListen")

    assert 'id="firstListenRetestBtn"' in html
    assert 'id="firstListenChooseSpeakerToRetestBtn"' in html
    assert "Choose a speaker to test" in html
    assert "const canRetestSavedSpeaker=Boolean(_firstListenUi.selectedEntityId)" in progress
    assert "if(retestBtn)retestBtn.hidden=!canRetestSavedSpeaker" in progress
    assert "if(chooseSpeakerToRetestBtn)chooseSpeakerToRetestBtn.hidden=canRetestSavedSpeaker" in progress
    assert "if(!_firstListenUi.selectedEntityId){chooseAnotherFirstListenSpeaker();return;}" in retest


def test_fresh_completion_uses_a_separate_success_surface() -> None:
    html = _html()
    assert 'id="firstListenSuccess" aria-labelledby="firstListenSuccessTitle" hidden aria-hidden="true" inert' in html
    assert "Bravo! Your first broadcast is complete." in html
    assert "Your station is on air." in html
    assert "Open your station" in html
    assert "Review choices" in html
    assert "Play the celebration" in html

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
    assert "_firstListenUi.showSuccess=celebrate" in choice


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


def test_post_hacs_timing_is_local_and_ends_only_on_heard_confirmation() -> None:
    html = _html()
    assert "mammamiradio:first-listen-ready" in html
    assert "mammamiradio:first-listen-heard" in html
    assert "mammamiradio:first-listen-post-hacs" in html
    discovery = _function("findFirstListenPlayers", "firstListenPlayerChanged")
    assert "if(!discoveryReady){" in discovery
    assert "startFirstListenClock();" in discovery
    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    assert "if(requestedHeard){" in verify
    assert "completeFirstListenClock();" in verify
    assert "No telemetry leaves this browser" in html
