"""Static browser-contract guards for the quiet Moment Picker.

The listener client has no bundled JavaScript unit-test runtime. These guards
pin the non-negotiable browser boundary: server-owned choices, ingress-safe
audio URLs, an earned share action, and no accidental editor affordances.
"""

from __future__ import annotations

from pathlib import Path

from mammamiradio.web.ui_copy import COPY

ROOT = Path(__file__).resolve().parents[2]
LISTENER_HTML = ROOT / "mammamiradio" / "web" / "templates" / "listener.html"
LISTENER_JS = ROOT / "mammamiradio" / "web" / "static" / "listener.js"
LISTENER_CSS = ROOT / "mammamiradio" / "web" / "static" / "listener.css"
SERVICE_WORKER = ROOT / "mammamiradio" / "web" / "static" / "sw.js"
STREAMER = ROOT / "mammamiradio" / "web" / "streamer.py"


def _picker_markup() -> str:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    start = html.index('<dialog class="mmr-moment-picker"')
    end = html.index("</dialog>", start) + len("</dialog>")
    return html[start:end]


def test_picker_is_a_native_dialog_without_editor_controls() -> None:
    markup = _picker_markup()

    assert 'id="moment-picker"' in markup
    assert 'aria-modal="true"' in markup
    assert 'id="moment-picker-audio" preload="metadata"' in markup
    assert 'id="moment-picker-context-toggle"' in markup
    assert 'id="moment-picker-share"' in markup
    audio_start = markup.index('<audio id="moment-picker-audio"')
    audio_tag = markup[audio_start : markup.index(">", audio_start)]
    assert " controls" not in audio_tag, "native audio transport must not expose a scrubber"
    assert "<input" not in markup, "the picker must not grow waveform/range editor controls"


def test_picker_uses_server_frozen_fields_and_no_audio_music_fallback() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")

    assert "fetch(_base + '/api/clip/capture'" in js
    assert "fetch(_base + '/api/clip/commit'" in js
    assert "fetch(_base + '/api/clip'," in js
    assert "momentAudio.src = _base + capture.audio_path;" in js
    assert "JSON.stringify({ capture_id: capture.capture_id, choice_id: choice.choice_id })" in js
    assert "typeof choice.choice_id === 'string'" in js
    assert "choice_id: choice.choice_id" in js
    capture = js[js.index("async function _requestMomentCapture") : js.index("function _momentShareUrl")]
    no_audio_gate = capture.index("data && data.reason === 'no_audio'")
    fallback_call = capture.index("_requestLegacyMusicShare(token, controller)")
    legacy_post = capture.index("fetch(_base + '/api/clip',")
    assert no_audio_gate < fallback_call < legacy_post


def test_picker_requires_metadata_seek_and_audible_progress_before_share() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    start = js.index("async function _startMomentAudition")
    end = js.index("function _onMomentTimeUpdate", start)
    audition = js[start:end]

    assert "_waitForMomentMetadata(token)" in audition
    assert "_seekMomentAudio(start, token)" in audition
    assert audition.index("_waitForMomentMetadata(token)") < audition.index("_seekMomentAudio(start, token)")
    assert audition.index("_seekMomentAudio(start, token)") < audition.index("await momentAudio.play()")
    assert "'loadedmetadata'" in js
    assert "'seeked'" in js
    seek = js[js.index("function _seekMomentAudio") : js.index("async function _startMomentAudition")]
    assert "timeoutMs = 5000" in seek
    assert "addEventListener('error', onError" in seek
    assert "addEventListener('abort', onAbort" in seek
    assert "setTimeout(() => fail('audio seek expired'), timeoutMs)" in seek
    assert "addEventListener('timeupdate', _onMomentTimeUpdate)" in js
    assert "current >= start + 0.5" in js
    assert "momentPicker.heard = true" in js
    assert "_setMomentShareAvailability(true)" in js


def test_native_share_cancellation_reuses_committed_url_and_locks_consumed_choices() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")

    assert "if (err && err.name === 'AbortError') return 'cancelled';" in js
    assert "momentPicker.committedResult = data;" in js
    commit = js[js.index("async function _commitMomentChoice") : js.index("async function doShare")]
    assert commit.index("if (momentPicker.committedResult)") < commit.index("fetch(_base + '/api/clip/commit'")
    share = js[
        js.index("async function _shareFrozenMomentResult") : js.index("function _restoreMomentAfterCommitFailure")
    ]
    assert "momentPicker.committedResult === result" in js
    assert "_lockCommittedMomentControls();" in share
    assert "_setMomentShareAvailability(true);" in share
    assert "momentListenBtn.disabled = false" not in share
    assert "momentContextToggle.disabled = false" not in share
    assert "moment_share_cancelled" in js
    audio_error = js[
        js.index("momentAudio.addEventListener('error', () => {") : js.index("// Service-worker registration")
    ]
    assert "momentPicker.committedResult || momentPicker.commitInFlight" in audio_error
    assert "_lockCommittedMomentControls();" in audio_error
    assert "momentDialog.dataset.state = 'committed';" in audio_error
    assert "_setMomentShareAvailability(true);" in audio_error


def test_commit_stages_a_frozen_url_for_a_fresh_share_click() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    legacy = js[js.index("async function _requestLegacyMusicShare") : js.index("function _momentShareUrl")]
    commit = js[js.index("async function _commitMomentChoice") : js.index("async function doShare")]
    staging = js[
        js.index("function _stageCommittedMomentForShare") : js.index("async function _shareFrozenMomentResult")
    ]

    assert "_stageCommittedMomentForShare();" in legacy
    assert "_shareFrozenMomentResult(data, token)" not in legacy
    assert "_stageCommittedMomentForShare();" in commit
    assert "_shareFrozenMomentResult(data, token)" not in commit
    assert "_shareFrozenMomentResult(momentPicker.committedResult, token)" in commit
    assert "momentDialog.dataset.state = 'committed';" in staging
    assert "_setMomentShareAvailability(true);" in staging


def test_manual_copy_prompt_never_reports_unverified_share_success() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    share = js[js.index("async function _shareCommittedMoment") : js.index("function _lockCommittedMomentControls")]

    assert "const promptResult = window.prompt" in share
    assert "promptResult === null ? 'cancelled' : 'manual'" in share
    assert "return 'shared';" not in share[share.index("const promptResult = window.prompt") :]
    assert "shareResult === 'cancelled' || shareResult === 'manual'" in js


def test_active_moment_owns_media_session_and_collapsed_choice_focus() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    registration = js[js.index("if ('mediaSession' in navigator)") : js.index("/* ── Rendering ──")]
    collapse_start = js.index("function _collapseMomentChoices")
    collapse = js[collapse_start : js.index("function _renderMomentChoice", collapse_start)]
    selection = js[js.index("function _selectMomentChoice") : js.index("function _waitForMomentEvent")]

    assert "setActionHandler('play', _mediaSessionPlay)" in registration
    assert "setActionHandler('pause', _mediaSessionPause)" in registration
    assert "setActionHandler('stop', _mediaSessionStop)" in registration
    assert "momentPicker.playback === 'paused' || momentPicker.playback === 'ended'" in js
    assert "momentChoicesEl.contains(document.activeElement)" in collapse
    assert "if (restoreFocus) momentContextToggle.focus();" in collapse
    assert selection.count("_collapseMomentChoices();") == 2


def test_picker_scopes_commit_and_share_completion_to_current_generation() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    commit = js[js.index("async function _commitMomentChoice") : js.index("async function doShare")]

    assert "const token = momentPicker.generation;" in commit
    assert "const captureId = capture.capture_id;" in commit
    assert commit.count("_momentCommitIsCurrent(token, captureId)") >= 2
    assert "_momentShareIsCurrent(token, result)" in js
    assert "if (!momentPicker.open) return;" not in commit


def test_stale_successful_capture_is_released_without_aborting_create() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    request = js[js.index("async function _requestMomentCapture") : js.index("async function _requestLegacyMusicShare")]
    create_start = request.index("const res = await fetch(_base + '/api/clip/capture'")
    create_end = request.index("const data = await res.json()", create_start)
    create_fetch = request[create_start:create_end]
    stale_start = request.index("if (!_momentIsCurrent(token)) {")
    stale_end = request.index("if (!res.ok", stale_start)
    stale_completion = request[stale_start:stale_end]

    assert "signal:" not in create_fetch, "create must finish so a stale successful ID can be learned"
    assert "data.ok === true" in stale_completion
    assert "typeof data.capture_id === 'string'" in stale_completion
    assert "_releaseMomentCapture(data.capture_id);" in stale_completion
    assert stale_completion.index("_releaseMomentCapture(data.capture_id);") < stale_completion.index("return;")


def test_picker_releases_only_ready_uncommitted_capture_after_audio_detaches() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    releasable = js[js.index("function _releasableMomentCaptureId") : js.index("function _releaseMomentCapture")]
    release = js[js.index("function _releaseMomentCapture") : js.index("function _setMomentStatus")]
    close = js[js.index("function _finishMomentPickerClose") : js.index("function _closeMomentPicker")]
    supersede = js[
        js.index("async function _requestMomentCapture") : js.index("async function _requestLegacyMusicShare")
    ]

    assert "momentPicker.capture && momentPicker.capture.capture_id" in releasable
    assert "momentPicker.committedResult || momentPicker.commitInFlight" in releasable
    assert "'/api/clip/capture/' + encodeURIComponent(captureId)" in release
    assert "method: 'DELETE'" in release
    assert ".catch(() => {})" in release
    for transition in (close, supersede):
        snapshot = transition.index("const releaseCaptureId = _releasableMomentCaptureId();")
        detach = transition.index("momentAudio.removeAttribute('src')")
        release_call = transition.index("_releaseMomentCapture(releaseCaptureId);")
        clear = transition.index("momentPicker.capture = null;")
        assert snapshot < detach < release_call < clear


def test_picker_releases_a_malformed_server_capture_before_rendering_error() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    ready = js[js.index("function _renderMomentReady") : js.index("function _selectMomentChoice")]

    assert "const captureId = typeof capture.capture_id === 'string'" in ready
    assert ready.index("_releaseMomentCapture(captureId);") < ready.index("_renderMomentError(")


def test_picker_owns_one_audio_focus_and_restores_live_intent() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    audition = js[js.index("async function _startMomentAudition") : js.index("function _onMomentTimeUpdate")]

    assert audition.index("_suspendRadioForMoment();") < audition.index("await momentAudio.play()")
    assert "momentPicker.radioResumeIntent = resumeIntent;" in js
    assert "if (shouldResume) startStream();" in js
    assert "if (momentPicker.radioPauseOwned)" in js
    assert "_resumeRadioAfterMoment();" in audition


def test_share_title_and_provenance_stay_capture_frozen() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    share = js[js.index("async function _shareCommittedMoment") : js.index("function _lockCommittedMomentControls")]
    preparing = js[js.index("function _renderMomentPreparing") : js.index("function _renderMomentError")]
    error = js[js.index("function _renderMomentError") : js.index("function _renderMomentReady")]

    assert "typeof data.track_title === 'string'" in share
    assert "document.getElementById('np-track')" not in share
    assert "_clearMomentProvenance();" in preparing
    assert "_clearMomentProvenance();" in error


def test_picker_closes_on_escape_and_restores_launcher_focus() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")

    assert "momentDialog.addEventListener('cancel'" in js
    assert "event.preventDefault();" in js
    assert "_closeMomentPicker();" in js
    assert "momentPicker.lastFocus" in js
    assert "focusTarget && focusTarget.isConnected" in js
    assert "focusTarget.focus()" in js


def test_picker_keeps_quiet_visual_contract_and_mobile_sheet() -> None:
    css = LISTENER_CSS.read_text(encoding="utf-8")
    markup = _picker_markup()

    assert 'class="mmr-moment-picker__progress-fill" id="moment-picker-progress-fill"' in markup
    assert ".mmr-moment-picker__progress-fill" in css
    assert ".mmr-moment-picker__provenance" in css
    assert "border-left: 2px solid var(--sun2)" in css
    assert ".mmr-moment-picker__share" in css and "background: var(--ok)" in css
    assert ".mmr-moment-picker[open]" in css
    assert "inset: auto 0 0" in css
    assert "safe-area-inset-bottom" in css


def test_service_worker_never_caches_temporary_capture_audio() -> None:
    worker = SERVICE_WORKER.read_text(encoding="utf-8")

    assert "radio-itali-v8" in worker
    capture_bypass = worker.index("path.includes('/captures/')")
    assert capture_bypass < worker.index("const isFreshAsset")
    assert capture_bypass < worker.index("Catch-all for any other same-origin GET")


def test_playback_feeds_capture_and_keepsake_ledgers_once_per_chunk() -> None:
    source = STREAMER.read_text(encoding="utf-8")
    start = source.index("# Feed only non-music into the generic share/capture ring.")
    end = source.index("pacing = pacer.after_send", start)
    hot_path = source[start:end]

    assert hot_path.count("_append_clip_chunk(app, chunk)") == 1
    assert "clip_buf.append(chunk)" not in hot_path
    assert "clip_segment_chunks += 1" in hot_path
    assert '_seg_record["chunks"] = clip_segment_chunks' in hot_path


def test_moment_picker_copy_is_complete_in_both_listener_languages() -> None:
    keys = (
        "moment_title",
        "moment_listen_title",
        "moment_listen",
        "moment_pause",
        "moment_continue",
        "moment_listen_again",
        "moment_change_context",
        "moment_listen_first",
        "moment_share",
        "moment_cancel",
        "moment_preparing",
        "moment_share_cancelled",
        "moment_rate_limited",
    )
    for language in ("en", "it"):
        for key in keys:
            assert COPY[language].get(key), f"missing {key} in {language}"
        assert "{s}" in COPY[language]["moment_rate_limited"]
