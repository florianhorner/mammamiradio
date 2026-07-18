"""UI contract guards for the Home Assistant first-listen golden path."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ADMIN_HTML = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "templates" / "admin.html"
STREAMER_PY = Path(__file__).resolve().parents[2] / "mammamiradio" / "web" / "streamer.py"


def _html() -> str:
    return ADMIN_HTML.read_text(encoding="utf-8")


def _function(name: str, next_name: str) -> str:
    html = _html()
    start = html.index(f"function {name}")
    end = html.index(f"function {next_name}", start)
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
    assert 'id="firstListenPath"' in html
    assert "Marco and Giulia are ready to go on air" in html
    assert "Your opening is on deck" in html
    assert "Choose where the station goes live" in html
    assert "Is the station in the room?" in html
    assert "Should the station notice home?" in html
    assert "Make the hosts more spontaneous" in html

    step = _function("firstListenSetStep", "focusCurrentFirstListenStep")
    assert "const current=state==='current'" in step
    assert "const reviewable=id==='firstListenSourceStep'&&state==='complete'" in step
    assert "const visible=current||reviewable" in step
    assert "body.hidden=!visible" in step
    assert "body.setAttribute('aria-hidden',visible?'false':'true')" in step
    assert "body.toggleAttribute('inert',!visible)" in step
    assert html.count('class="first-listen-body" hidden aria-hidden="true" inert') == 3
    assert 'id="firstListenAiBody" hidden aria-hidden="true" inert' in html


def test_unfinished_fresh_install_owns_a_top_level_first_surface() -> None:
    html = _html()
    assert 'data-tab="setup"' in html
    assert 'id="tab-setup"' in html
    assert 'aria-controls="first-listen-panel"' in html
    assert 'id="first-listen-panel" data-panel="setup"' in html
    assert 'id="firstListenPanelMount"' in html
    assert 'id="firstListenQuickAction" hidden' in html
    assert 'id="firstListenQuickFindPlayersBtn"' in html
    assert html.index('data-tab="setup"') < html.index('data-tab="scaletta"')

    mount = _function("initFirstListenPanelMount", "initTabs")
    assert "mount.append(details)" in mount

    required = _function("firstListenEntryRequired", "resolveFirstListenLanding")
    assert "first.bootstrap_ready===true" in required
    assert "first.fresh_install===true" in required
    assert "origin==='unknown'" in required
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
    assert "The opening is ready. Recovery cover carries the station afterward" in html
    assert "Recovery cover is available afterward, but no primary music source is ready." in html
    assert "Recovery audio follows the opening while music needs attention." in html
    assert "Recovery cover is available, but primary music still needs attention." in html
    assert "source.transport_audible" not in html
    assert "authored opening is ready" in html


def test_speaker_controls_use_active_post_routes_and_exact_media_source() -> None:
    html = _html()
    assert "api('POST','/api/setup/first-listen/players',{})" in html
    assert "api('POST','/api/setup/first-listen/play',{entity_id:entityId})" in html
    assert "api('POST','/api/setup/first-listen/receipt/retry',{entity_id:entityId})" in html
    assert "api('POST','/api/setup/first-listen/verify',{attempt_id:attemptId,heard:Boolean(heard)})" in html
    assert "media-source://mammamiradio/live" in html
    assert "Start Mamma Mi Radio" in html
    assert "Yes — that’s Mamma Mi Radio" in html
    assert "Not yet" in html


def test_home_assistant_labels_are_added_with_text_content() -> None:
    player_block = _function("renderFirstListenPlayers", "renderFirstListenProgress")
    preview_block = _function("renderHomeContextPreview", "findFirstListenPlayers")

    assert "option.textContent=firstListenPlayerLabel(player)" in player_block
    assert "select.replaceChildren()" in player_block
    assert ".innerHTML" not in player_block
    for assignment in (
        "title.textContent=label",
        "meta.textContent=",
        "id.textContent=entityId",
        "mute.textContent=",
    ):
        assert assignment in preview_block
    assert ".innerHTML" not in preview_block


def test_privacy_preview_is_explicit_and_precedes_optional_ai() -> None:
    html = _html()
    privacy = html.index('id="firstListenPrivacyStep"')
    ai = html.index('id="firstListenAiStep"')
    assert privacy < ai
    assert "You see it before the hosts do." in html
    assert "sends it to an AI provider" in html
    assert "api('POST','/api/setup/home-context-preview',{})" in html
    assert "api('PATCH','/api/setup/home-context-choice',{enabled:Boolean(enabled)})" in html
    assert "Keep private and continue" in html
    assert "Let future hosts use this" in html


def test_existing_install_opens_privacy_without_replaying_first_audio() -> None:
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")

    assert "const priorInstall=projection.legacy||projection.first.install_origin==='existing'" in progress
    assert "const listenAccepted=projection.accepted||priorInstall" in progress
    assert "const listenComplete=projection.heard||priorInstall" in progress
    assert "listenAccepted?'complete':'current'" in progress
    assert "listenComplete?'complete':'current'" in progress
    assert "privacyMilestone?'complete':listenComplete?'current'" in progress
    assert "sourceKnown?'complete':priorInstall?'available':'current'" in progress
    assert "priorInstall?'complete':!sourceKnown?'locked'" in progress
    assert "firstListenSetStep('firstListenAiStep',privacyMilestone?'current':'locked')" in progress
    assert "aiFieldset.disabled=!privacyMilestone||!projection.showAi" in progress
    assert "First Listen does not make you replay it." in progress
    assert "Review Home context next." in progress

    setup = _function("renderFirstListen", "renderSetup")
    assert "Your existing station stays as it is." in setup
    assert "No speaker replay is required." in setup

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
    assert "code==='privacy_receipt_unavailable'||code==='preview_required'" in choice
    assert "focusPreview=code==='preview_required'" in choice
    assert "if(focusPreview)document.getElementById('firstListenPreviewBtn')?.focus({preventScroll:true})" in choice
    assert "resp?.persisted===false" not in choice

    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "!_firstListenUi.privacyPreviewValid" in progress
    assert "keepOffBtn.disabled=!projection.privacyUnlocked" in progress


def test_no_ai_key_is_needed_for_first_audio() -> None:
    html = _html()
    assert "No AI key." in html
    assert "Marco and Giulia already open the station" in html
    assert "newly generated conversations" in html
    assert 'id="firstListenAiBody" hidden aria-hidden="true" inert' in html
    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "aiFieldset.disabled=!privacyMilestone||!projection.showAi" in progress
    assert "filter(e=>e.key==='llm_keys')" in progress
    start_block = _function("startFirstListen", "verifyFirstListen")
    assert "setupAnthropicKey" not in start_block
    assert "setupOpenaiKey" not in start_block


def test_not_yet_has_warm_bounded_repair_and_no_volume_mutation() -> None:
    html = _html()
    repair_start = html.index('id="firstListenRepair"')
    repair_end = html.index("</div>", repair_start)
    repair = html[repair_start:repair_end]

    assert "Give the speaker a few seconds" in repair
    assert "check mute and volume" in repair
    assert "Confirm the selected speaker" in repair
    assert "media-source://mammamiradio/live" in repair
    assert "HACS integration" in repair
    assert 'id="firstListenRetryBtn"' in repair
    assert "Retry on same speaker" in repair
    assert 'id="firstListenChooseAnotherBtn"' in repair
    assert "Choose another speaker" in repair
    assert "Restart Home Assistant only after a new integration install" in repair
    choose_another = _function("chooseAnotherFirstListenSpeaker", "startFirstListen")
    assert "_firstListenUi.selectionDirty=true" in choose_another
    assert "_firstListenUi.selectedEntityId=''" in choose_another
    assert "_firstListenUi.attemptId=''" in choose_another
    assert "_firstListenUi.dispatch='ready'" in choose_another
    assert "_firstListenUi.verification='awaiting'" in choose_another
    projection = _function("firstListenProjection", "firstListenSourceStatus")
    assert "serverProofMatches=!_firstListenUi.selectionDirty" in projection
    assert "volume_set" not in html
    assert "volume_level" not in _function("startFirstListen", "verifyFirstListen")


def test_unsaved_accepted_attempt_is_recovered_without_replaying() -> None:
    html = _html()
    panel_start = html.index('id="firstListenReceiptRepair"')
    panel_end = html.index("</div>", panel_start)
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

    assert "Home Assistant accepted the show." in panel
    assert "Only this listening check still needs saving." in panel
    assert "does not send another playback request" in panel
    assert 'id="firstListenSaveAttemptBtn"' in panel
    assert "Save this listening check" in panel
    assert 'id="firstListenVerifyActions"' in html
    assert "if(accepted&&resp?.receipt_persisted===false)" in start
    assert "_firstListenUi.attemptId=''" in start
    assert "_firstListenUi.dispatch='receipt_failed'" in start
    assert "verifyActions.hidden=_firstListenUi.dispatch==='receipt_failed'" in progress
    assert ".first-listen-actions[hidden]{display:none}" in html
    assert "_firstListenUi.receiptSaving=true" in save
    assert "_firstListenUi.receiptSaving=false" in save
    assert "api('POST','/api/setup/first-listen/receipt/retry',{entity_id:entityId})" in save
    assert "/api/setup/first-listen/play" not in save
    assert "resp?.receipt_persisted!==true||!attemptId" in save
    assert "_firstListenUi.dispatch='accepted'" in save
    assert "code==='receipt_recovery_missing'" in save
    assert "_firstListenUi.dispatch='ready'" in save
    assert "focusTarget='firstListenPlayBtn'" in save
    assert "receiptRepair.hidden=_firstListenUi.dispatch!=='receipt_failed'" in progress
    assert "_firstListenUi.dispatch!=='receipt_failed'" in progress
    assert "_firstListenUi.receiptSaving" in progress
    save_disabled = next(line for line in progress.splitlines() if "saveAttemptBtn.disabled=" in line)
    assert "_firstListenUi.players" not in save_disabled

    assert "recovery.available!==true" in recovery_entity
    assert "entityId.startsWith('media_player.')" in recovery_entity
    assert "!entityId||_firstListenUi.selectionDirty" in hydrate
    assert "_firstListenUi.attemptId=''" in hydrate
    assert "_firstListenUi.selectedEntityId=entityId" in hydrate
    assert "_firstListenUi.dispatch='receipt_failed'" in hydrate
    assert "startFirstListen(" not in hydrate
    assert "pendingRecoveryEntityId" in players
    assert "current===pendingRecovery" in players
    assert "Accepted speaker —" in players
    assert "hydrateFirstListenReceiptRecovery(resp)" in discovery
    assert "renderFirstListenPlayers(players,saved,pendingRecovery)" in discovery
    assert "const recoveredEntity=hydrateFirstListenReceiptRecovery(first)" in render
    assert "if(!recoveredEntity&&attempt" in render
    assert "if(!recoveredEntity&&projectionMatchesSelection" in render
    assert "pendingReceiptProjected" in projection
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
        "firstListenQuickFindPlayersBtn",
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
    ):
        assert html.count(f'id="{control_id}"') == 1

    progress = _function("renderFirstListenProgress", "shouldShowHomeContextPreview")
    assert "quickAction.hidden=!quickDiscovery" in progress
    assert "firstListenSetActionTone(playBtn,_firstListenUi.players.length>0&&!listenAccepted)" in progress
    assert "select.replaceChildren" not in progress
    assert "playerSelect').innerHTML" not in progress
    assert "document.activeElement" not in progress
    assert "focus(" not in progress
    assert "if(el.textContent!==message)el.textContent=message" in html


def test_first_listen_controls_have_accessible_busy_and_mobile_contracts() -> None:
    html = _html()
    assert ".first-listen-action{min-width:44px;min-height:44px}" in html
    assert ".first-listen-select{min-width:0;min-height:44px" in html
    assert "@media(max-width:720px)" in html
    assert ".first-listen-fields{grid-template-columns:1fr}" in html
    assert ".first-listen-actions{flex-direction:column;align-items:stretch}" in html
    assert ".first-listen-summary{min-width:0" in html
    assert ".first-listen-status{min-width:0" in html
    assert ".first-listen-source-name{min-width:0" in html
    assert ".ha-preview-title{min-width:0" in html
    assert "overflow-wrap:anywhere" in html
    assert 'aria-live="polite" aria-atomic="true"' in html
    assert "setAttribute('aria-busy','true')" in html
    assert "prefers-reduced-motion: reduce" in html


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


def test_post_hacs_timing_is_local_and_ends_only_on_heard_confirmation() -> None:
    html = _html()
    assert "mammamiradio:first-listen-ready" in html
    assert "mammamiradio:first-listen-heard" in html
    assert "mammamiradio:first-listen-post-hacs" in html
    assert "if(resp?.media_source_ready===true)startFirstListenClock()" in html
    verify = _function("verifyFirstListen", "loadHomeContextPreview")
    assert "if(heard){" in verify
    assert "completeFirstListenClock();" in verify
    assert "No telemetry leaves this browser" in html
