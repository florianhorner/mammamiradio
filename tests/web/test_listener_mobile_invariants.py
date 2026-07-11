"""Listener mobile invariants.

Two regressions shipped to production by skipping mobile-specific CSS:

1. PR #235 (Volare Refined) renamed `.nav` → `.mmr-nav`. The pre-Volare
   `@media (max-width: 640px) { .nav-links { display: none; } }` rule was
   left for the old class name and never ported. On a 375 px phone the
   brand + 4 anchor links + the In Onda pill exceeded the viewport width,
   the body became wider than the viewport, and vertical scroll broke —
   ~80 % of sections were rendered to the right of the visible area.

2. The dedica form `<input>` and `<textarea>` shipped at `font-size: 14px`.
   iOS Safari auto-zooms any form field below 16 px on focus, which knocks
   the layout sideways and is one of the most-reported mobile UX bugs.

Both classes of bug are CSS-only and can be caught with a static parse.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "base.css"
LISTENER_CSS = REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.css"
LISTENER_JS = REPO_ROOT / "mammamiradio" / "web" / "static" / "listener.js"
LISTENER_HTML = REPO_ROOT / "mammamiradio" / "web" / "templates" / "listener.html"
UI_COPY = REPO_ROOT / "mammamiradio" / "web" / "ui_copy.py"

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# Greedy capture inside a top-level @media block. Listener CSS does not
# nest @media, so a single non-greedy outer scan with brace matching is
# overkill — we just take everything up to the next `@media` or EOF.
_MEDIA_BLOCK_RE = re.compile(
    r"@media\s*\(\s*max-width\s*:\s*(\d+)px\s*\)\s*\{(.*?)(?=@media|\Z)",
    re.DOTALL,
)


def _read_listener_css() -> str:
    return _COMMENT_RE.sub("", LISTENER_CSS.read_text(encoding="utf-8"))


def _rule_bodies_for_selector(text: str, selector: str) -> list[str]:
    rule_re = re.compile(r"([^{}]+)\{([^}]*)\}", re.DOTALL)
    bodies: list[str] = []
    for selector_block, body in rule_re.findall(text):
        selectors = [s.strip() for s in selector_block.split(",")]
        if selector in selectors:
            bodies.append(body)
    return bodies


def test_phone_breakpoint_collapses_or_hides_nav_anchor_links() -> None:
    """The <=600px @media block must hide or wrap the listener nav anchor links.

    Anchor links are the four `<a href="#…">` items inside `.mmr-nav nav`.
    On a 375 px phone they push the In Onda pill off-screen and force a
    horizontal overflow. Acceptable mitigations:
      - `.mmr-nav nav { display: none; }` (current fix), or
      - `.mmr-nav-inner { flex-wrap: wrap; }` (wrap onto a second row).
    """
    text = _read_listener_css()
    phone_blocks = [body for width, body in _MEDIA_BLOCK_RE.findall(text) if int(width) <= 600]
    assert phone_blocks, "listener.css has no @media (max-width: 600px) block — phones get desktop layout."
    combined = "\n".join(phone_blocks)
    hides_links = re.search(r"\.mmr-nav\s+nav\s*\{[^}]*display\s*:\s*none", combined)
    wraps_inner = re.search(r"\.mmr-nav-inner\s*\{[^}]*flex-wrap\s*:\s*wrap", combined)
    assert hides_links or wraps_inner, (
        "Phone breakpoint (<=600px) must either hide `.mmr-nav nav` or set "
        "`flex-wrap: wrap` on `.mmr-nav-inner`. Without one of these the "
        "header overflows the viewport and breaks vertical scroll on phones."
    )


def test_form_inputs_avoid_ios_auto_zoom() -> None:
    """All Volare-namespace input/textarea rules in listener.css must declare
    font-size >= 16px.

    iOS Safari auto-zooms any focused form field below 16 px. The dedica
    form lives on the listener page and shipped at 14 px, which broke the
    mobile UX on every iPhone tap.

    Scoped to the `.mmr-*` namespace (Volare Refined). Pre-Volare class
    selectors like `.form-input` are dead code — none of them appear in the
    rendered HTML — and are removed in #270. Catching them here would
    create a false-positive on this branch.
    """
    text = _read_listener_css()
    rule_re = re.compile(r"([^{}]+)\{([^}]*)\}", re.DOTALL)
    font_size_re = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)px")
    offenders: list[tuple[str, float]] = []
    for selector_block, body in rule_re.findall(text):
        selectors = [s.strip() for s in selector_block.split(",")]
        # Targets a form field iff at least one selector in the group is in
        # the live Volare namespace AND mentions input or textarea (either
        # as an element selector or as a class-name suffix like
        # `.mmr-dedica-form-input`).
        targets_form = any(".mmr-" in s and ("input" in s or "textarea" in s) for s in selectors)
        if not targets_form:
            continue
        # Last font-size declaration wins inside a single rule block.
        sizes = font_size_re.findall(body)
        if not sizes:
            continue
        size_px = float(sizes[-1])
        if size_px < 16:
            first_selector = selectors[0].splitlines()[0][:80]
            offenders.append((first_selector, size_px))
    assert not offenders, (
        "Listener form fields below 16 px font-size will trigger iOS auto-zoom "
        "on focus and break the mobile layout. Bump to 16 px:\n"
        + "\n".join(f"  {sel}: {size}px" for sel, size in offenders)
    )


def test_body_uses_modern_viewport_units_for_ios() -> None:
    """html/body must declare a `100svh`/`100dvh` min-height to avoid iOS
    address-bar cutoff (falling back to `100vh` is fine, but the dynamic unit
    must be present), AND must keep `overscroll-behavior-x: contain` to
    prevent horizontal rubber-band snap when content overflows.
    """
    text = _read_listener_css()
    # Order-insensitive: accept both `html, body` and `body, html`.
    html_body_re = re.compile(
        r"(?:html\s*,\s*body|body\s*,\s*html)\s*\{([^}]*)\}",
        re.DOTALL,
    )
    block = html_body_re.search(text)
    assert block, "listener.css must declare an `html, body { … }` block."
    body = block.group(1)
    has_dynamic = re.search(r"min-height\s*:\s*100(svh|dvh)", body)
    assert has_dynamic, (
        "html/body must declare `min-height: 100svh` (or 100dvh) in addition "
        "to `100vh`. Without it iOS Safari hides ~100 px of content under "
        "the collapsing address bar."
    )
    has_overscroll_guard = re.search(r"overscroll-behavior-x\s*:\s*contain", body)
    assert has_overscroll_guard, (
        "html/body must keep `overscroll-behavior-x: contain` to prevent the "
        "horizontal rubber-band snap that triggered the original `In Onda` "
        "tap regression on phones."
    )


def test_listener_uses_no_fixed_body_overlay() -> None:
    """The listener background must not be a fixed body pseudo-element.

    Real Safari viewport compositing can keep fixed pseudo-elements in a
    separate layer that hides scrolled content. Full-page screenshots flatten
    the document and miss this class of bug, so pin the CSS shape directly.
    """
    text = _read_listener_css()
    fixed_overlay = re.search(r"body::before\s*\{[^}]*position\s*:\s*fixed", text, re.DOTALL)
    assert not fixed_overlay, (
        "listener.css must not use `body::before { position: fixed; ... }` "
        "for page atmosphere. Put the glow/grain on `html, body` instead so "
        "real viewport compositing cannot cover scrolled elements."
    )


def test_mobile_now_playing_title_wraps_without_hidden_clip() -> None:
    """The sticky now-playing title must wrap on phones instead of clipping long track names."""
    text = _read_listener_css()
    base_rules = "\n".join(_rule_bodies_for_selector(text, ".mmr-stage-header .mmr-np-meta .title"))
    assert re.search(r"overflow-wrap\s*:\s*anywhere", base_rules), (
        "Now-playing titles need `overflow-wrap: anywhere` so long Italian "
        "titles or one-word track names cannot push the mobile header sideways."
    )

    phone_blocks = [body for width, body in _MEDIA_BLOCK_RE.findall(text) if int(width) <= 600]
    combined = "\n".join(phone_blocks)
    assert re.search(
        r"\.mmr-stage-header\s+\.mmr-np-meta\s+\.title\s*\{[^}]*overflow\s*:\s*visible",
        combined,
        re.DOTALL,
    ), (
        "Phone breakpoint must override the desktop hidden overflow on the "
        "now-playing title; otherwise long tracks are visibly clipped."
    )


def test_listener_anchor_targets_clear_sticky_nav() -> None:
    """Listener anchors must land below the sticky nav in real viewports."""
    text = _read_listener_css()
    scroll_margin_re = re.compile(r"scroll-margin-top\s*:\s*(?!0(?:px|rem|em|%)?\s*;)[^;]+;")
    missing = [
        selector
        for selector in (".mmr-stage", ".mmr-section", "#request-form", "#req-name", "#req-msg")
        if not any(scroll_margin_re.search(body) for body in _rule_bodies_for_selector(text, selector))
    ]
    assert not missing, (
        "Listener anchor targets need non-zero `scroll-margin-top` so sticky "
        "navigation does not hide the section when users jump or scroll to it: " + ", ".join(missing)
    )


def test_listener_page_declares_scroll_padding_for_sticky_nav() -> None:
    text = _read_listener_css()
    assert re.search(r"html\s*\{[^}]*scroll-padding-top\s*:\s*96px", text, re.DOTALL), (
        "listener.css must set `html { scroll-padding-top: 96px; }` for form focus under sticky nav."
    )


def test_listener_stopped_state_quiets_live_indicators() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    block = js[js.index("function renderStoppedState") : js.index("/* ── Toast helper")]
    for needle in (
        "_setPlaybackControls(stopped)",
        "document.querySelector('.mmr-stage-header .mmr-live')",
        "_setNowPlayingEyebrow(stopped)",
        "wave.classList.toggle('paused'",
        "_setLiveChip(",
        "if (stopped)",
        "const nowStreaming = (status && status.now_streaming) || {}",
        "renderNowPlayingStrip({ ...nowStreaming, type: 'stopped' })",
    ):
        assert needle in block, f"renderStoppedState() must drive stopped-state honesty via {needle!r}."


def test_listener_now_playing_eyebrow_reflects_stopped_state() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    assert 'id="np-eyebrow"' in html, "now-playing eyebrow must be addressable by stopped-state JS."

    js = LISTENER_JS.read_text(encoding="utf-8")
    block = js[js.index("function _setNowPlayingEyebrow") : js.index("/* ── Playback")]
    assert "$('np-eyebrow')" in block
    assert "_t('np_paused', 'Fermo')" in block
    assert "_t('np_on_air', 'Ora in onda')" in block
    assert "el.textContent = label + suffix" in block


def test_listener_live_chip_never_reinterprets_configured_frequency_as_html() -> None:
    """Configured brand.frequency is Jinja-escaped markup in the initial HTML.

    _setLiveChip() derives the frequency suffix from textContent on first poll.
    Rewriting that suffix with innerHTML would reinterpret a configured string
    like `<img onerror=...>` as markup. Use nodes/textContent only.
    """
    js = LISTENER_JS.read_text(encoding="utf-8")
    block = js[js.index("function _setLiveChip") : js.index("function _setPlayControl")]
    assert "innerHTML" not in block
    assert "replaceChildren()" in block
    assert "document.createElement('span')" in block
    assert "document.createTextNode" in block
    assert "label.lang = 'it'" in block
    assert "label.textContent = 'In Onda'" in block
    assert "_t('np_live'" not in block, "the stage flair must stay Italian, not utility-localized."


def test_listener_stopped_playback_controls_are_honestly_disabled() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    for control_id in ("nav-cta", "np-play", "hero-play"):
        opening_tag = re.search(rf'<button[^>]+id="{control_id}"[^>]*>', html)
        assert opening_tag, f"missing listener playback control {control_id}"
        assert "session_stopped" in opening_tag.group(0)
        assert " disabled" in opening_tag.group(0)
        assert "listen_paused_aria" in opening_tag.group(0)

    js = LISTENER_JS.read_text(encoding="utf-8")
    block = js[js.index("function _setPlayControl") : js.index("function _setNowPlayingEyebrow")]
    assert "_t('listen_stopped', 'Station paused')" in block
    assert "_t('listen_paused_aria', 'Station paused')" in block
    assert "el.disabled = stopped" in block
    assert "aria-pressed" in block
    assert "_setCompactPlayControl" in block
    assert "_setHeroPlayControl" in block

    copy = UI_COPY.read_text(encoding="utf-8")
    assert '"listen_stopped": "Station paused"' in copy
    assert '"listen_stopped": "Radio in pausa"' in copy


def test_listener_live_nav_cta_is_an_honest_play_pause_toggle() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    button = re.search(r'<button[^>]+id="nav-cta"[^>]*>(.*?)</button>', html, re.DOTALL)
    assert button, "listener template must render the primary nav playback control."
    opening_tag = button.group(0).split(">", 1)[0]
    assert 'aria-pressed="false"' in opening_tag
    assert "listen_now_aria" in opening_tag and "listen_paused_aria" in opening_tag
    assert "listen_now" in button.group(1) and "listen_stopped" in button.group(1)

    js = LISTENER_JS.read_text(encoding="utf-8")
    block = js[js.index("function _setPlayControl") : js.index("function _setNowPlayingEyebrow")]
    for needle in (
        "const hasIntent = !stopped && state.wantsPlay",
        "_t('listen_now', 'Listen Now')",
        "_t('listen_pause', 'Pause')",
        "_t('listen_now_aria', 'Listen now')",
        "_t('listen_pause_aria', 'Pause station')",
        "el.setAttribute('aria-pressed', hasIntent ? 'true' : 'false')",
    ):
        assert needle in block, f"nav playback control must keep its visible/action state via {needle!r}."
    for needle in (
        "function _setNavPlayControl",
        "_setPlayControl($('nav-cta'), stopped, 'nav')",
        "_setPlayControl(playBtnSmall, stopped, 'compact')",
        "_setPlayControl(heroPlay, stopped, 'hero')",
    ):
        assert needle in block, f"all listener playback controls must share {needle!r}."

    playing_block = js[js.index("function setPlayingUi") : js.index("/* ── Media Session")]
    assert "_setPlaybackControls" in playing_block, "audio events must update every play toggle immediately."


def test_listener_hero_and_compact_controls_share_honest_toggle_state() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    controls = js[js.index("function _setPlayControl") : js.index("function _setNowPlayingEyebrow")]
    for needle in (
        "el.disabled = stopped",
        "_t('listen_pause', 'Pause')",
        "_t('listen_pause_aria', 'Pause station')",
        "el.setAttribute('aria-pressed', hasIntent ? 'true' : 'false')",
        "variant === 'compact'",
        "variant === 'hero'",
    ):
        assert needle in controls

    html = LISTENER_HTML.read_text(encoding="utf-8")
    hero = re.search(r'<button[^>]+id="hero-play"[^>]*>(.*?)</button>', html, re.DOTALL)
    compact = re.search(r'<button[^>]+id="np-play"[^>]*>', html)
    assert hero and compact
    assert 'aria-pressed="false"' in hero.group(0)
    assert 'aria-pressed="false"' in compact.group(0)


def test_listener_empty_request_has_native_and_visible_validation() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    textarea = re.search(r'<textarea[^>]+id="req-msg"[^>]*>', html)
    assert textarea, "dedication message textarea must exist."
    assert re.search(r"\srequired(?:\s|>)", textarea.group(0))
    assert 'aria-describedby="request-sent"' in textarea.group(0)
    assert 'id="request-sent"' in html and 'aria-live="polite"' in html

    js = LISTENER_JS.read_text(encoding="utf-8")
    feedback = js[js.index("function _showEmptyRequestMessage") : js.index("function _resetRequestForm")]
    assert "_t(" in feedback and "'form_message_required'" in feedback
    assert "msgInput.setAttribute('aria-invalid', 'true')" in feedback
    assert "sentEl.style.display = ''" in feedback
    assert "sentEl.classList.add('is-visible')" in feedback

    submit = js[js.index("async function submitRequest") : js.index("/* ── Wire everything")]
    assert re.search(r"if \(!msg\)\s*\{\s*_showEmptyRequestMessage\(\);", submit)
    wiring = js[js.index("const reqForm = $('request-form')") : js.index("// Clip sharing button")]
    assert "reqMsg.addEventListener('invalid'" in wiring
    assert "reqMsg.addEventListener('input'" in wiring


def test_listener_request_receipt_does_not_hide_its_form_ancestor() -> None:
    """#request-sent lives inside the form, so hiding the form also hides the receipt."""
    html = LISTENER_HTML.read_text(encoding="utf-8")
    form = html[html.index('<form class="mmr-dedica-form"') : html.index("</form>")]
    assert form.index('id="request-sent"') < len(form)

    js = LISTENER_JS.read_text(encoding="utf-8")
    request_flow = js[js.index("function _setRequestFieldsHidden") : js.index("/* ── Wire everything")]
    assert "child.id !== 'request-sent'" in request_flow
    assert "_setRequestFieldsHidden(formEl, true)" in request_flow
    assert "formEl.style.display = 'none'" not in request_flow, (
        "the live-region receipt is nested inside #request-form; hiding that ancestor makes success invisible."
    )


def test_listener_request_outcomes_are_localized_and_failure_reset_preserves_input() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    submit = js[js.index("async function submitRequest") : js.index("/* ── Wire everything")]
    for key in (
        "form_success_song",
        "form_success_shoutout",
        "form_rate_limited",
        "form_queue_full",
        "form_declined",
        "form_network_error",
    ):
        assert re.search(rf"_t\(\s*'{key}'", submit), f"request outcome bypasses localized {key} copy"

    assert "if (r.ok && d.ok)" in submit, "an HTTP error body must never pose as a successful request."
    assert "}, isSuccess ? 15000 : 6000)" in submit
    clear_block = submit[submit.index("_resetRequestForm(formEl, sentEl);") : submit.index("} catch (e)")]
    assert "if (isSuccess)" in clear_block
    assert "msgInput.value = ''" in clear_block
    catch_block = submit[submit.index("} catch (e)") :]
    assert "msgInput.value = ''" not in catch_block, "network recovery must preserve the listener's retry text."


def test_listener_playback_is_scoped_to_explicit_play_controls() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    assert "autoStartOnce" not in js
    assert "document.addEventListener('touchstart'" not in js
    assert "document.addEventListener('click'," not in js

    wiring = js[js.index("document.addEventListener('DOMContentLoaded'") :]
    for needle in (
        "playBtn.addEventListener('click'",
        "playBtnSmall.addEventListener('click'",
        "heroPlay.addEventListener('click'",
    ):
        assert needle in wiring, f"explicit play affordance lost its playback binding: {needle}"


def test_listener_playback_pending_and_retries_are_cancellable_and_deduplicated() -> None:
    js = LISTENER_JS.read_text(encoding="utf-8")
    state = js[js.index("const state = {") : js.index("/* ── DOM refs")]
    assert "playPending: false" in state
    assert "retryTimer: null" in state

    playback = js[js.index("function _clearPlaybackRetry") : js.index("/* ── Media Session")]
    for needle in (
        "state.retryTimer !== null",
        "clearTimeout(state.retryTimer)",
        "if (!state.wantsPlay || state.retryTimer !== null || _stationIsStopped()) return",
        "if (!state.wantsPlay || _stationIsStopped()) return",
        "state.isPlaying || state.playPending",
        "state.wantsPlay = false",
        "state.playPending = false",
        "_clearPlaybackRetry()",
    ):
        assert needle in playback

    events = js[js.index("// Audio element event wiring") : js.index("// Request form")]
    assert "setTimeout(startStream" not in events
    assert events.count("_scheduleStreamRetry(") == 2
    pause = events[events.index("audio.addEventListener('pause'") : events.index("audio.addEventListener('ended'")]
    for needle in (
        "if (!audio.ended && !audio.error)",
        "state.wantsPlay = false",
        "state.playPending = false",
        "_clearPlaybackRetry()",
        "setPlayingUi(false)",
    ):
        assert needle in pause, f"external pause handling lost {needle!r}."


def test_listener_decorative_italian_declares_element_language() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    required_fragments = (
        '<a href="#stasera" class="active" lang="it">Stasera</a>',
        '<a href="#palinsesto" lang="it">Palinsesto</a>',
        '<span lang="it">In Onda</span>',
        '<h1 class="mmr-h1" lang="it">',
        '<p class="mmr-lede" lang="it">',
        '<h2 lang="it">Stasera in <em>onda</em></h2>',
        '<h2 lang="it">Dediche &amp; <em>Saluti</em></h2>',
        '<div class="eyebrow" lang="it">Manda al DJ',
        '<h2 lang="it">La <em>stazione</em></h2>',
        '<span lang="it">{{ brand.tagline or "La notte è italiana" }}</span>',
        '<span lang="it">Stasera · {{ brand.hosts[-1].display_name }}</span>',
        '<span lang="it">Dediche aperte</span>',
        '<span lang="it">Manda al DJ</span>',
    )
    missing = [fragment for fragment in required_fragments if fragment not in html]
    assert not missing, "decorative Italian needs per-element lang=it markers:\n" + "\n".join(missing)

    js = LISTENER_JS.read_text(encoding="utf-8")
    assert '<div class="sig" lang="it">${sig}</div>' in js


def test_listener_mixed_language_blocks_use_narrow_overrides() -> None:
    html = LISTENER_HTML.read_text(encoding="utf-8")
    assert '<div class="track" lang="it">' not in html
    assert '<div class="mmr-about-card" lang="it"><div class="eyebrow">Codice</div>' not in html
    assert '<span lang="en">Open source</span> <span lang="it">su</span> GitHub.' in html
    assert '<span data-cap="llm" lang="en" hidden>AI scriptwriter.</span>' in html
    assert '<span lang="en">Made with espresso.</span>' in html
    assert '<p class="fine" lang="it">' not in html


def test_listener_station_identity_prefers_and_repairs_from_server_payload() -> None:
    """Server identity/brand wins; localStorage is a repaired last-resort cache."""
    js = LISTENER_JS.read_text(encoding="utf-8")
    resolver = js[js.index("function stationNameFromStatus") : js.index("function syncStationName")]
    assert resolver.index("status.identity") < resolver.index("status.brand")
    assert resolver.index("stationNameFromStatus(state.status)") < resolver.index("localStorage.getItem('stationName')")

    sync = js[js.index("function syncStationName") : js.index("/* ── State")]
    assert "stationNameFromStatus(status)" in sync
    assert "localStorage.setItem('stationName', serverName)" in sync

    fetch = js[js.index("async function fetchStatus") : js.index("async function fetchRequests")]
    assert "syncStationName(status)" in fetch, "every successful status update must overwrite stale cache data."


def test_listener_schedule_type_pills_do_not_override_timing_state() -> None:
    text = _read_listener_css()
    pill_body = "\n".join(_rule_bodies_for_selector(text, ".mmr-schedule .pill"))
    assert "box-shadow" in pill_body and "--pill-type" in pill_body

    for selector in (
        ".mmr-schedule .pill.pill-music",
        ".mmr-schedule .pill.pill-banter",
        ".mmr-schedule .pill.pill-ad",
        ".mmr-schedule .pill.pill-news",
        ".mmr-schedule .pill.pill-idle",
    ):
        body = "\n".join(_rule_bodies_for_selector(text, selector))
        assert "--pill-type" in body, f"{selector} must expose type as an accent token."
        assert not re.search(r"(?<!-)color\s*:", body), f"{selector} must not override current/next text color."
        assert "background" not in body, f"{selector} must not override current/next background."

    for selector in (".mmr-schedule .pill.pill-current", ".mmr-schedule .pill.pill-next"):
        body = "\n".join(_rule_bodies_for_selector(text, selector))
        assert "color:" in body and "background:" in body, f"{selector} owns timing emphasis."


def test_listener_now_playing_allows_two_line_clamp() -> None:
    text = _read_listener_css()
    title_block = _rule_bodies_for_selector(text, ".mmr-stage-header .mmr-np-meta .title")
    assert title_block, "now-playing title rule must exist."
    combined = "\n".join(title_block)
    assert "-webkit-line-clamp:2" in combined.replace(" ", ""), (
        "Primary now-playing title must clamp to two lines on narrow layouts."
    )


def test_listener_phone_radio_illustration_stays_in_bounds() -> None:
    text = _read_listener_css()
    phone_blocks = [body for width, body in _MEDIA_BLOCK_RE.findall(text) if int(width) <= 600]
    combined = "\n".join(phone_blocks)
    assert _rule_bodies_for_selector(combined, ".mmr-stage > .mmr-hero-art"), (
        "Phone CSS must target `.mmr-stage > .mmr-hero-art`; the base child-selector rule "
        "otherwise wins over a bare `.mmr-hero-art` override."
    )
    assert not _rule_bodies_for_selector(combined, ".mmr-hero-art"), (
        "Phone CSS must not use a bare `.mmr-hero-art` override for the stage art; "
        "it is lower-specificity than the base `.mmr-stage > .mmr-hero-art` rule."
    )
    assert re.search(r"\.mmr-radio\s*\{[^}]*max-width\s*:\s*320px", combined), (
        "Phone CSS must cap `.mmr-radio` width so the illustration does not clip."
    )
    waves = "\n".join(_rule_bodies_for_selector(combined, ".mmr-waves"))
    assert "right:0" in waves.replace(" ", ""), (
        "Phone CSS must pull `.mmr-waves` back inside the radio bounds; the base rule uses `right: -4%`."
    )
    assert "width:14%" in waves.replace(" ", "") or "max-width:48px" in waves.replace(" ", ""), (
        "Phone CSS must narrow `.mmr-waves` so the wave arcs do not clip on 375px screens."
    )
    assert re.search(r"\.mmr-knob-cap\s*\{[^}]*display\s*:\s*none", combined), (
        "Phone CSS should hide nonessential knob labels below 600px."
    )


def test_listener_template_bakes_initial_stopped_state() -> None:
    """First server paint must reflect session_stopped.

    The runtime JS (renderStoppedState) keeps the live/stopped indicators honest
    after the first /public-status poll, but the initial HTML paints before that.
    Without baking the state a stopped station flashes as live ("In Onda", pulsing
    dot, animated wave) for one poll cycle — a #1/#5 leadership-principle breach.
    """
    html = LISTENER_HTML.read_text(encoding="utf-8")
    assert 'data-stopped="true"' in html, (
        "body must bake data-stopped when session_stopped — it drives the "
        "`body[data-stopped] .mmr-wave span` pause in listener.css."
    )
    assert html.count("session_stopped") >= 3, (
        "the body, nav pill, and hero live indicator must each branch on session_stopped."
    )
    assert "is-stopped" in html, "live indicators must carry is-stopped at render when stopped."


def test_listener_has_main_landmark_and_skip_link() -> None:
    """The listener page must expose a <main> landmark and a skip link so keyboard
    and screen-reader users can bypass the nav (WCAG 2.4.1)."""
    html = LISTENER_HTML.read_text(encoding="utf-8")
    assert '<main id="content"' in html, "primary content must be wrapped in a <main> landmark."
    assert 'class="skip-link"' in html and 'href="#content"' in html, (
        "a skip-to-content link must target the <main> landmark."
    )


def test_listener_lang_reflects_copy_register() -> None:
    """<html lang> must follow the active copy register (it/en); a static lang=it
    makes screen readers read English copy with Italian phonemes (WCAG 3.1.1)."""
    html = LISTENER_HTML.read_text(encoding="utf-8")
    assert 'lang="{{ page_lang }}"' in html, "<html lang> must be driven by page_lang, not hardcoded to it."


def _read_base_css() -> str:
    return _COMMENT_RE.sub("", BASE_CSS.read_text(encoding="utf-8"))


def test_base_css_pins_text_size_adjust() -> None:
    """base.css (shared by listener + admin) must pin `text-size-adjust: 100%`.

    Without it, mobile Edge/Safari apply text autosizing ("font boosting") and
    inflate type inside text blocks. That bloats `white-space: nowrap` elements
    (admin np-title, tab bar, log labels) past their containers, widens the
    layout viewport beyond device-width, and makes `@media (max-width: 768px)`
    evaluate against the inflated width — so the mobile breakpoint never fires
    and the desktop layout renders at phone width with horizontal scroll. This
    shipped to a real device (Edge) and was invisible to headless Chromium,
    which disables boosting. Both prefixed and unprefixed declarations required.
    """
    css = _read_base_css()
    assert re.search(r"-webkit-text-size-adjust\s*:\s*100%", css), (
        "base.css must declare `-webkit-text-size-adjust: 100%` to stop mobile font boosting."
    )
    assert re.search(r"(?<!-)text-size-adjust\s*:\s*100%", css), (
        "base.css must declare the unprefixed `text-size-adjust: 100%`."
    )


def test_base_css_contains_page_level_horizontal_overflow() -> None:
    """base.css must clip page-level horizontal overflow on html/body.

    A single too-wide element otherwise widens the layout viewport and scrolls
    the whole page sideways on mobile (the reported /admin breakage). `clip`
    is preferred over `hidden` because `hidden` creates a scroll container that
    breaks `position: sticky` headers; either satisfies the contract.
    """
    css = _read_base_css()
    bodies = _rule_bodies_for_selector(css, "html") + _rule_bodies_for_selector(css, "html, body")
    bodies += _rule_bodies_for_selector(css, "body")
    has_guard = any(re.search(r"overflow-x\s*:\s*(clip|hidden)", body) for body in bodies)
    assert has_guard, (
        "base.css must set `overflow-x: clip` (or hidden) on html/body so no "
        "element can create page-wide horizontal scroll on mobile."
    )
