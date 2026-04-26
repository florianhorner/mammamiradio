# Changelog

All notable changes to `mammamiradio` are documented here.

The current version source of truth is `pyproject.toml`.

## [Unreleased]

### Fixed

- **Listener page — Italian flag tricolor + radio cabinet rendered transparent**: PR #235 (Volare Refined) referenced `var(--flag-green)`, `var(--flag-red)`, `var(--flag-white)`, `var(--terracotta)`, `var(--terracotta-lt)`, `var(--sage)`, `var(--ink)` in listener.css and base.css but never declared the corresponding `:root` tokens. The browser silently rendered every Italian tricolor element (radio cabinet strip, hero stage seam, dedica form border, hero tricolor) and the entire vintage radio illustration body/knobs/antenna as `rgba(0,0,0,0)`. Now declared in `mammamiradio/static/tokens.css`. Added `tests/test_design_tokens.py::test_every_var_ref_resolves_to_a_defined_token` to fail CI if any CSS file references a `var(--foo)` (no fallback) that isn't declared in tokens.css — closes the regression class.
- **Listener hero stat — `0h 0m` ambiguity**: per-listener session airtime rendered "0h 0m" in italic Playfair where "0" looks like "o", giving "oh om" on first paint. Now reads `status.uptime_sec` from `/public-status` (station-wide on-air time, more meaningful than per-listener counter) and shows "In diretta" for the first minute. `stat-tracks` now uses `status.tracks_played` from the public payload instead of the queue length, matching the "Tracce in playlist" label.
- **Admin mobile layout — panel header overlap**: at narrow viewports the title (e.g. "Host personalities") wrapped onto two lines and the subtitle ("Marco, Giulia") collided with it because `.a-panel header` was a fixed flex row with `flex-shrink: 0` on the subtitle. Now stacks vertically below 768px.
- **Admin pacing slider tracks invisible on mobile**: 4px `rgba(20,17,15,0.5)` track was nearly invisible against the dark panel at narrow widths — only the gold thumb showed. Bumped to 6px and switched track base to `rgba(245,237,216,0.12)` (cream at 12%) at <=768px.
- **Programme Dur. column always empty**: `<td class="du"></td>` rendered blank for every row (past, now, upcoming). Added `fmtDur(item, typeKey)` helper that reads `duration_ms` (top-level or under `metadata`) with sensible per-type fallbacks (music 4:00, banter 0:30, ad 1:00, news 0:20) and formats as `m:ss`.
- **News flash never auto-fired**: the scheduler had `songs_since_news >= 6 AND random.random() < 0.3` — over 25 banter slots that statistically yields ~7 news flashes, but with high variance, hour-long listening sessions could see zero. Removed the 30% gate; news now fires deterministically once songs_since_news >= 6.
- **Producer wakes immediately on session resume** (P0-1): replaced 1-second `asyncio.sleep` poll with `asyncio.wait_for(state.resume_event.wait(), timeout=1.0)`. `StationState` now carries a `resume_event` asyncio.Event; both the streaming gateway and `/api/resume` set it on session resume, collapsing the worst-case resume lag from 1s to milliseconds.
- **Silence fallback never queues a silent track** (P1-2): audio quality circuit breaker now recycles the last-known-good music file (via `_get_last_music_file(state)`) or drops the segment rather than letting silent audio reach the playback queue. Prevents dead-air from corrupt/zero-length norm files reaching listeners.
- **LRU cache eviction respects playback queue** (P1): `evict_cache_lru` now accepts an optional `protected_paths: set[Path]` argument. Producer passes currently-queued norm paths so in-flight audio is never deleted mid-stream.
- **LLM prompt injection hardening** (H2/H3): `_sanitize_prompt_data` now strips six quote variants (`"`, `` ` ``, `"`, `"`, `'`, `'`) and fake role markers (`System:`, `Assistant:`, `Human:`, `User:` — case-insensitive, with optional spacing). Prevents listener-submitted metadata from breaking out of interpolated prompt strings or injecting synthetic conversation turns.
- **ICY header injection guard** (M1): station name and genre are CRLF-scrubbed before writing to ICY response headers, closing an HTTP response-splitting vector for operators who set `STATION_NAME` with embedded newlines.
- **youtube\_id format validation** (M4): `/api/playlist/add-external` now validates the `youtube_id` parameter against `[A-Za-z0-9_-]{11}` before passing it to yt-dlp, blocking path traversal and injection payloads.
- **HA addon version sync**: `ha-addon/mammamiradio/config.yaml` version bumped to `2.10.9` to match `pyproject.toml` (was stale at `2.10.8`).

### Changed

- **`StationState` carries `last_music_file`**: mirrors the module-level `_last_music_file` cache into the state object so tests can inject per-state isolation without mutating shared module globals.

### Added

- **Listener client migrated to public API (PR-F)**: `mammamiradio/static/listener.js` no longer polls admin-only `/status` + `/api/capabilities` + `/api/listener-requests`. Uses the public-API surface from PR-B exclusively: single `/public-status` fetch returns brand + capabilities + facts + ha_moments in one shape; `/public-listener-requests` for the dediche feed. Result: works on any deploy (loopback, LAN, public) without 401 risk. Wires `window.mmrApplyCaps(caps)` so `[data-cap=KEY]` elements toggle correctly per design D2. Flips body `data-state` warming→live on first successful response. Removes hardcoded "La sezione dediche si riempirà..." fallback that was overwriting the brand-voice template copy from PR-C ("Aspettiamo la prima dedica della sera…").
- **Brand themes via CSS custom properties (PR-D/5)**: `mammamiradio/static/listener.css` migrated to use `var(--brand-primary, var(--sun))`, `var(--brand-accent, var(--lancia2))`, `var(--brand-bg, var(--shadow))` — listener tokens read from per-station `[brand.theme]` injected by listener.html, with Volare Refined as fallback. Each operator's station now visibly themes (color + font) without breaking Volare's dark-canvas + Italian-warmth foundation (PR-A guardrails enforce 4.5:1 contrast on background, dark-canvas lightness, curated font allow-list).
- **OpenGraph social cards (PR-E/5)**: new `mammamiradio/og_card.py` module renders 1200×630 OG card PNGs via Pillow. Per design D-Design-2: Italian flag tricolor at top edge, brand identity dominant in italic display font, amber glow at top-left (Volare's "single amber glow" signature), gold "ORA IN ONDA" eyebrow, lower-third track band. Looks like a still-frame from the listener page, not a generic SaaS card. Honors per-station `[brand.theme]` colors. New `/og-card.png` route serves cached PNG (60s cache, evicts on track change). Falls back to logo SVG on render failure (social previews never 404). pillow>=10.0 added to dependencies.
- **Listener Jinja2 template + capability-conditional rendering (PR-C/5)**: `mammamiradio/listener.html` migrated from string-replace placeholder to a real Jinja2 template. Every brand-fiction string (station name, frequency, city, founded year, tagline, about, host display names) reads from `[brand]` block at render time. New `--brand-*` CSS custom property namespace overlays Volare Refined defaults per station theme (PR-A guardrails), with `var(--brand-primary, var(--sun))` fallback so listener.css works with or without a brand theme. Capability-conditional copy renders client-side via `[data-cap=KEY]` attributes (per design D2): JS reads `/public-status.capabilities` every poll and toggles HA / AI / PWA copy based on actual capability flags. PWA install link (was: `<a href="/static/manifest.json">`) replaced with proper `<button>` + `beforeinstallprompt` flow. New jinja2 dep (~5 LOC in pyproject.toml; FastAPI's `Jinja2Templates` integration). Empty-state copy in brand voice ("Aspettiamo la prima dedica della sera…", "Il palinsesto sta arrivando…", "Stiamo accendendo la radio…"). Cathedral standard: zero hardcoded user-facing strings.
- **Public listener API (`/public-status` + `/public-listener-requests`)**: brand-engine PR-B/5. `/public-status` now returns `brand` block (station_name, frequency, city, founded, tagline, hosts, theme tokens), `capabilities` flag set (llm, anthropic_key, openai, ha, anthropic_degraded), `uptime_sec`, `tracks_played` for cross-page parity. New `/public-listener-requests` endpoint serves filtered dediche feed (drops internal IDs, raw timestamps, error fields). Cross-page invariant tests (`tests/test_public_status_contract.py`) prevent the bug class where admin and listener disagree on shared facts (uptime, on-air state, track count).
- **Brand engine schema (`[brand]` block in `radio.toml`)**: separates brand-fiction layer (what listeners read) from operator-truth engine config (what operators control). New `BrandSection`, `BrandTheme`, `BrandHost` dataclasses in `config.py`. Theme tokens (primary/accent/background colors, display/body/mono fonts) override Volare Refined defaults per station, with constrained-variable guardrails: hex validation, background lightness ≤ 25 (dark-canvas invariant), 4.5:1 contrast against `--cream` body text, fonts from a curated allow-list of 6 (Playfair Display, Cormorant Garamond, Bodoni Moda, Lora, Outfit, JetBrains Mono for display; Outfit, Inter, Source Sans 3, IBM Plex Sans for body). `[[brand.hosts]]` maps `engine_host` (FK to `[[hosts]].name`) to a `display_name` and `description` so the listener can call them "Marco del bar" while the engine voices them via `Marco`. All validation graceful — invalid values fall back to Volare defaults and surface as `brand_warnings` in admin `/status` for the Engine Room panel. INSTANT AUDIO leadership principle preserved: bad brand config never blocks station boot.
- **`/live` mobile host control room** (admin-gated): phone-optimised operator surface at the new `/live` route. 966-line standalone HTML using the Volare Refined design tokens, viewport-fit cover for notch handling, and direct-touch buttons wired to `/api/skip`, `/api/clip`, `/api/stop`, `/api/resume`. Same auth contract as `/admin` (loopback-bypass when no password is set, basic-auth elsewhere). Three regression tests in `tests/test_streamer_routes.py` cover loopback, public-without-auth, and authenticated cases.
- **Accessibility (WCAG 2.1 AA)**: `<html lang="it">` on `admin.html`; sr-only labels on song-request form inputs in `listener.html`; `aria-hidden` on decorative tricolor band; `.sr-only` and `:focus-visible` CSS utilities in `base.css`; `outline: none` removed from form inputs in `listener.css`; `aria-pressed` synced to play button in `listener.js`.
- **Regression test suite** (`tests/test_qa_regression_guards.py`): 14 automated guards covering LRU eviction protection, prompt sanitization (quotes, role markers, control chars, truncation), ICY header injection, youtube\_id regex, HA addon version sync, `resume_event` presence, and the `_get_last_music_file` three-tier fallback chain.

- **Regia admin prototype at `/regia`** (dev preview, admin-gated): Screen 1 ON AIR of the new Concept A Time-Horizon Stack admin architecture. Persistent 16px status strip, 5-tab bar (only ON AIR wired; CODA/REVISIONE/PALINSESTO/MOTORE are placeholders), hero Now Playing with Playfair italic track name, countdown as Italian prose, banter preview as editorial pull-quote with gold quote glyph + Lancia red dropcap, 4-button trigger row (AVANTI / PAUSA / VOCE AI / SPOT), 260px read-only peek panel showing 8 upcoming items, ambient FM dial. Polls `/status` every 3s; AVANTI/VOCE AI/SPOT wired to existing `/api/skip` and `/api/trigger`. PAUSA and PANICO log warnings pending backend endpoints. `admin.html` untouched — this is a prototype at a new route, not a replacement. See `TODOS.md` § Admin UI — Regia for Phase 1 MVP follow-ups.
- **`--ai-purple` semantic token** in `mammamiradio/static/tokens.css`: `#A855F7` reserved exclusively for AI-generated segments so operators can distinguish AI content from human/music at a glance. Used in the Regia banter cards and peek-panel type dots.

### Refactored

- **Dashboard: extract inline CSS/JS into `/static/` (PR #203)**. First outside contribution by **Ashika Rai N** ([@ashika-rai-n](https://github.com/ashika-rai-n)). Moved ~575 lines of inline `<style>` and ~815 lines of inline `<script>` from `dashboard.html` into `mammamiradio/static/styles.css` and `mammamiradio/static/script.js`, with an accompanying one-line fix to `_inject_ingress_prefix` so `src="/static/..."` gets rewritten behind HA Ingress alongside `href="/static/..."`. Commit [`2028d40`](https://github.com/florianhorner/mammamiradio/commit/2028d408499cd98b15c82a39a5cd3912cdfbb1d9) landed with a `Co-authored-by` trailer preserving Ashika's authorship. The specific files were subsequently superseded by the Phase A design-system consolidation (`tokens.css` / `base.css`) and the Phase B1 `/dashboard` surface deletion, but the extraction pattern informed the Phase A approach.

### Fixed

- **Conductor setup fails on machines with broken Python 3.13**: `conductor-setup.sh` now prefers `python3.11 → python3.12 → python3.13 → python3` instead of leading with 3.13. On machines where 3.13 is installed but its `ensurepip` is broken, the setup no longer fails — it falls back to the project's target interpreter (3.11) automatically.

**Contributors:** [@ashika-rai-n](https://github.com/ashika-rai-n)

## [2.10.9] - 2026-04-20

Fixes the admin panel regression introduced in v2.10.8 and adds producer bridge metadata improvements.

### Fixed

- **Admin panel broken by v2.10.8 CSP regression** (pacing sliders, skip controls, all interactive elements): `script-src 'self'` blocked the entire inline script block in `admin.html`, and `script-src 'self' 'nonce-{x}'` (attempted intermediate fix) blocked the ~40 inline `onclick`/`oninput`/`onchange` event handlers throughout admin.html — nonces cover `<script>` elements only, not attribute event handlers. Final fix: `script-src 'self' 'unsafe-inline'`, which allows all inline code while still blocking external script sources (CDNs, attacker domains). `esc()` on all five HA fields in admin.html remains the load-bearing XSS defense.
- **Producer bridge track metadata**: Resume bridge and idle bridge segments now call `load_track_metadata(norm_path)` before humanizing the filename, so `title` and `artist` are populated from the sidecar JSON when available instead of falling back to raw filename stems.

### Security

- CSP on `/admin` now uses `script-src 'self' 'unsafe-inline'`. This blocks external script injection (the operationally relevant threat) while allowing the existing inline code that the admin panel depends on. The rationale — and why nonces alone are insufficient for this HTML structure — is documented in streamer.py.

### Tests

- Updated CSP tests to assert `'unsafe-inline'` and verify the nonce placeholder is absent from rendered HTML (`test_admin_csp_allows_inline`, `test_admin_csp_header_sent_with_unsafe_inline`).
- Added HTTP-level CSP test (`test_admin_csp_header_sent_with_unsafe_inline`): fires a real `GET /admin` via `httpx.ASGITransport` and asserts the header is actually set on the response.
- Added producer unit tests for sidecar metadata loading on resume and idle bridge paths (`tests/test_producer_unit.py`).


## [2.10.8] - 2026-04-19

Security fix: stored XSS in admin panel Engine Room (HA entity state injection + yt-dlp track title injection).

### Security

- **Stored XSS via HA entity state values** (admin.html Engine Room): Five Home Assistant-sourced fields (`mood`, `weather_arc`, `events_summary`, `pending_directive`, `last_event_label`) were rendered via `innerHTML` without HTML escaping. An attacker with write access to any HA entity feeding the admin panel could inject arbitrary HTML/JS that would execute in the authenticated admin session. Fix: all five fields now wrapped with `esc()` (DOM-based escape helper) before `innerHTML` assignment. `esc()` is applied before `.replace(/\n/g,'<br>')` on `events_summary` so the newline replacement operates on already-escaped content.
- **Stored XSS via yt-dlp track titles** (admin.html Engine Room): When a track is skipped past the repeat threshold, `ha_pending_directive` stores a raw yt-dlp track title. A maliciously named YouTube video could inject HTML/JS via this field. Fix: same `esc()` wrapper in admin.html covers this field. Raw storage in `ha_pending_directive` is intentional — the field feeds LLM prompts, and server-side HTML encoding would corrupt LLM input. Design decision documented in code comment and regression test.
- **Content-Security-Policy on `/admin`**: The `/admin` route now returns a `Content-Security-Policy` header with a per-request nonce (`script-src 'self' 'nonce-{nonce}'`) as defense-in-depth. Note: initial v2.10.8 used `script-src 'self'` without a nonce, which broke the admin panel by blocking the inline script block — fixed in the next patch.

### Tests

- `tests/test_xss_regression.py` (new): 6 regression guards covering all five HA fields, correct `esc()`-before-`.replace()` ordering, CSP presence, LLM injection phrase filtering, and the intentional raw-storage design contract for `ha_pending_directive`.


## [2.10.7] - 2026-04-18

Operator honesty II — aggregates the WS2/WS3/WS5/WS6 reliability fixes shipped on `main` since 2.10.6, plus two UI-truth fixes from the 2026-04-17 live session (queue row labels for BANTER/AD, dashboard AI pipeline pill three-state).

### Fixed

- **Source and content hygiene at ingest** (WS5): Apple Music's Italian chart occasionally surfaced podcasts, BBC comedy, and audiobook entries that played as dead-eye audio and broke the radio illusion harder than any other failure. `mammamiradio/playlist.py` now filters chart results through `_is_plausible_music_title` at ingest — conservative markers (`podcast`, `bbc comedy`, `audiobook`, `news briefing`, …) drop obvious non-music before it ever reaches the queue. The filter is narrow on purpose: oversize titles (>150 chars) and empty inputs are also rejected, but normal Italian song titles pass through untouched.
- **Rejected downloads no longer loop through `validate_download` forever** (WS5): `mammamiradio/downloader.py` now exposes `reject_cached_download` and a per-session `_REJECTED_CACHE_KEYS` denylist. When `validate_download` rejects a track (missing file, too short, corrupt), the cache file is purged and the cache key is denylisted for the remainder of the session. Producer call sites (prefetch, prewarm, main music loop) short-circuit on rejected keys via a bounded retry around `select_next_track`. Closes the `validate_download` cache-poisoning loop from the 2026-04-13 log windows `18:16:56` and `18:32:23`. The music quality gate retains its own escape valve — the 3-consecutive-rejection circuit breaker — so transient normalization artifacts don't permanently block a source track.
- **Anthropic auth flood prevented across concurrent tasks** (WS3-A): concurrent banter/ad/transition generations no longer race past the auth-block check. A module-level `asyncio.Lock` serializes the Anthropic attempt so the first 401 trips the 10-minute cooldown and sibling calls queued on the lock see the block and fall straight to OpenAI. Backoff expiry logs once (`Anthropic auth backoff expired; retrying Anthropic after cooldown`), the next call retries exactly once, and a fresh success clears the block atomically. Fixes the 8/8/22-minute 401 bursts from the 2026-04-13 log.
- **TTS voice validation at config load** (WS3-B): Every host and ad voice is now checked against the catalog for its backend (`mammamiradio/voice_catalog.py`) during `load_config()`. Invalid voice IDs — e.g. OpenAI names like `onyx` on an edge-tts host, or typos in edge voice IDs — are logged once as a WARNING and replaced with `it-IT-DiegoNeural` before the first synthesis attempt. Stops the `Invalid voice 'onyx'` flood that repeated per segment in 2026-04-13 logs (windows `15:56`, `16:09`, `18:30`, `18:40`). Runtime TTS failures are also memoized per-session so a flaky voice doesn't re-attempt for every segment. `/api/capabilities` gains a `tts_degraded` flag when any voice was substituted.
- **Queue starvation rescue via bundled demo assets** (WS2): When the playback queue has been empty for more than 30 seconds and neither a canned clip nor a pre-normalized track is available, `run_playback_loop` now falls back to a random MP3 from `mammamiradio/demo_assets/music/` before triggering forced banter. Eliminates the silent 30-second dead-air loop on fresh installs and empty-cache container starts. Bundled demo tracks in `demo_assets/music/` (named `Artist - Title.mp3`) are now also preferred over the metadata-only `DEMO_TRACKS` placeholder list at startup and when the operator explicitly selects the demo source. Source: 2026-04-13 log-resolution plan WS2.
- **`/readyz` honors stopped state** (WS2): `/readyz` now returns `503 stopped` when `session_stopped=True`, even with queue depth > 0 and startup complete. Prevents Home Assistant Supervisor and external load balancers from routing fresh listeners to a deliberately paused station. The auto-resume-on-connect path in `_audio_generator` clears `session_stopped` before audio begins, so the guard does not create a deadlock.
- **Queue rows render real titles for every segment type** (finding #8, 2026-04-17 live session): Admin queue rows used to display bare segment types like `BANTER banter` or `AD ad`. `producer.py` now populates `segment.metadata["title"]` at every construction site — LLM banter shows participating hosts (`Marco & Luca`), canned clips show `Pre-recorded banter` (even when the quality-gate rescue path rewrites `state.last_banter_script`), ad breaks show `Ad: Barella Pasta +2 more` when multiple brands appear. Station IDs, sweepers, and time checks (`Time check — Sono le 19 e 42 su Mamma Mi Radio.`) also render human labels instead of bare enum keys. News-flash and error-recovery segments pick up their own labels. `admin.html`'s queue render now also hides a label that equals the bare type, so a future producer path that forgets to set a title can't re-introduce the `BANTER banter` row.
- **Dashboard AI pipeline pill honors `anthropic_degraded`** (finding #11, 2026-04-17 live session): When the Anthropic key is configured but currently auth-suspended, `dashboard.html` showed a solid "AI" dot while every script call was failing. The pipeline pill now mirrors the three-state logic `admin.html` already had — a configured-but-suspended Anthropic renders as `AI Fallback` (triangle icon, OpenAI is generating scripts), not `AI`.

### Added

- **Release cooldown gate** (stabilization run Day 1): `.github/workflows/release-cooldown.yml` blocks any `v*` tag push if the prior published release is less than 24 hours old. Bypass: `hotfix` label on the source PR. Tunable via `MIN_COOLDOWN_HOURS`. Self-test at `tests/workflows/test_cooldown_gate.sh` covers 9 scenarios and runs on every PR via `quality.yml`. `STABILIZATION_LOG.md` records weekly fix-hours and emergency-patch counts; Day 8 Go/No-Go lives in that file.

### Tests

- `TestBanterTitle` and `TestAdTitle` in `tests/test_producer_unit.py`: 10 cases covering single/multi-host banter, canned-clip fallback, generic fallback, single/multi-brand ad summarization, empty brand list, and whitespace-only brand skipping. Guards the queue-row label contract against future regressions.


## [2.10.6] - 2026-04-17

Operator honesty pass — five UI and log fixes that stop the admin panel from lying to the operator, plus a normalizer safety guard.

### Fixed

- **Normalizer concat duration guard** (Item 1, phase 1): `concat_files` now probes input durations with `ffprobe` and logs a `WARNING` when the concatenated output is shorter than the sum of its inputs, surfacing silent truncation instead of producing a mysteriously short track. Fail-open: when ffprobe is unavailable or cannot parse, the guard stays silent rather than blocking playback.
- **Stopped state actually stops** (Item 19): Clicking Stop now freezes the dashboard animations, pauses the elapsed-time counter, and disables producer buttons until Resume. Previously the UI kept ticking as if the stream were live.
- **Three-state Anthropic status** (Item 11): Admin panel distinguishes *connected*, *not configured*, and *suspended* (401 from Anthropic) instead of flashing "connected" while every script call was failing.
- **Scheduler reason strings no longer leak to listeners** (Item 21): Up-next rows used to render raw strings like `"cooldown: 45s"` and `"banter_due_in=3"`. Those are now stripped before the row reaches the UI.
- **Raw norm-cache filenames never show as titles** (Item 20): The rescue path that replays a pre-normalized track on producer stall no longer displays `Recovered: norm_abc123.mp3`. Sidecar metadata is used when present; otherwise the filename is humanized (`norm_busted.mp3` → `Busted`).

### Tests

- `TestFFprobeDurationSecParser`: exercises the real `_ffprobe_duration_sec` parser (returncode, unparseable, OSError, timeout, empty stdout) so the concat guard's dependency is covered by the per-module ratchet.
- Rescue-path coverage: sidecar-present, malformed-sidecar, and no-sidecar paths are all asserted at the listener-facing title level.


## [2.10.5] - 2026-04-16

### Changed

- **Admin UI redesign**: Full two-column control room layout. Warm sidebar (260px, gold left border) with compact now-playing card, 5-bar animated waveform, progress bar, and 2×2 quick-controls grid (Next song / Pause / Shuffle / Banter). Right panel shows a unified "On Air" programme list — past segments dimmed, current row gold-highlighted with NOW badge and inline waveform, upcoming with "— coming up —" Playfair italic divider. Filter pills (All / Music / Banter / Ads). Pacing, Hosts, Station Log, and Engine Room collapse into accordions below. Replaces the old single-column tab layout.
- **Token cost counter regression fix**: Removed a static `<div id="apiCostEl">` that shadowed the dynamic element injected by `updateEngineRoom()`, preventing the cost display from ever rendering.
- **Stop/Resume grid fix**: Wrapped Stop and Resume buttons in a `display:contents` cell so toggling between them no longer leaves a visual gap in the 2×2 controls grid.
- **Accessibility polish**: Keyboard `:focus-visible` ring added to buttons and inputs; control buttons now enforce 44px min-height (36px chips, 32px filter pills) for touch targets; base font-size raised from 15px to 16px (WCAG floor); queue song names raised from 13px to 14px; Home Assistant slider range labels raised from 8px/18% to 9px/32% opacity.
- **Quick Action labels clarified**: Renamed to action-oriented verbs ("trim" / "force") for immediate comprehension.
- **Dead code removal**: Dropped unused `btn-skip` CSS; replaced hardcoded hover hex with `color-mix` so hover states follow the accent token.


## [2.10.4] - 2026-04-16

### Security

- **CI action SHA-pinned**: `dependabot/fetch-metadata` is now pinned to commit SHA `ffa630c65fa7e0ecfa0625b5ceda64399aea1b36` (v3). Eliminates supply chain risk from a mutable semver tag running with `contents: write` + `pull-requests: write` in `pull_request_target` context.
- **Secret scanning**: Added `.gitleaks.toml` with custom rules for Anthropic API keys (`sk-ant-…`) and Home Assistant long-lived access tokens. Extends gitleaks default ruleset with project-specific patterns and an allowlist for `.env.example`.
- **yt-dlp version floor raised**: Minimum `yt-dlp` version bumped from `>=2024.0` to `>=2026.2.21`, patching GHSA-g3gw-q23r-pgqm (RCE via `--netrc-cmd`, fixed in 2026.2.21).


## [2.10.3] - 2026-04-15

### Added

- **`POST /api/hot-reload`**: Reload `mammamiradio.scriptwriter` in-place via `importlib.reload()` without interrupting the stream. Code changes to `scriptwriter.py` take effect on the next banter generation with zero stream gap. Includes 5s debounce (429), structured error response with `stream_status: "unaffected"`, and reload timing in the response body. Requires `--workers 1`.
- **Quick Actions chips in admin UI**: Four one-tap feedback controls (Less banter / More chaos / Too many ads / Hot reload) wired to existing pacing PATCH and trigger POST endpoints. Located in the Radio tab for immediate tone adjustment during live sessions.

### Changed

- **Producer import refactor**: `producer.py` now imports `mammamiradio.scriptwriter` as a module reference (`import mammamiradio.scriptwriter as _sw`) instead of name-bound `from ... import` to ensure `importlib.reload()` takes effect at every call site.
- **`_has_script_llm` made public**: Renamed to `has_script_llm` — consistent with the module-reference import pattern and eliminates private-attribute access from producer.
- **HA addon `radio.toml` synced to root**: The HA addon now ships the same `radio.toml` as the root. The Pi-specific pacing overrides (`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`) are removed. CI validates with strict `cmp -s` and copies the root file at build time.
- **Broadcast EQ restored to 3-filter chain**: The HF harshness shelf (12kHz, -1.5dB) removed in 2.10.2 due to an ffmpeg 8.x crash concern is restored. The 3-filter chain is the correct broadcast EQ configuration.
- **Auto-resume on listener connect removed**: `_audio_generator` no longer clears `session_stopped` when a listener connects. A deliberate `/api/stop` now remains paused across restarts until explicit `/api/resume`, keeping stop/resume behavior fully operator-controlled.


## [2.10.2] - 2026-04-15

### Fixed

- **Silence after HA restart following a deliberate stop** *(critical)*: `session_stopped.flag` survived addon restarts, leaving the session permanently stopped. Listeners connecting after a restart received silence indefinitely. `_audio_generator` now clears the stopped state the moment a listener connects — a listener connecting is an unambiguous signal that someone wants music.
- **55-75 second silence on resume/idle wakeup (Pi)** *(critical)*: When no canned banter clips are available (demo_assets/banter/ is empty in the container), the resume bridge and idle-reconnect bridge had no fallback. The queue stayed empty while the first track normalized (~75s on Pi). Both bridges now fall back to the first pre-normalized `norm_*.mp3` file in `cache_dir` — zero FFmpeg wait, instant playback.
- **FFmpeg 8.x SIGABRT on Pi during normalization** *(critical)*: Three equalizer filters combined with `loudnorm` trigger an assertion crash in ffmpeg 8.1 (`calc_energy` in `psymodel.c:576`). The third equalizer (`-1.5dB HF shelf at 12kHz`) was removed. Two equalizers + loudnorm is verified safe. This crash caused normalization to silently fail on every track, leaving the queue permanently empty.
- **Stream player stalls after admin resume**: The dashboard player detected resume in the status poll but did not reconnect the audio stream, requiring a manual page reload. `_wasStopped` state tracking now triggers an automatic stream reconnect when the station resumes.

### Added

- **9 regression-prevention tests**: Guards against all four failure modes above — ffmpeg filter chain count, loudnorm presence, resume bridge with canned clip, resume bridge norm-cache fallback, resume bridge no-op on empty cache, idle bridge norm-cache fallback, auto-resume with/without flag file, and auto-resume no-op when session is already running.


## [2.10.1] - 2026-04-15

### Fixed

- **HA addon Docker images never built** *(critical)*: The CI `addon-build.yml` validate job used a byte-for-byte `cmp -s` comparison for `radio.toml`, but the HA addon intentionally carries three pacing overrides tuned for Pi/HA Green performance (`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`). The strict comparison always failed, blocking the `build` job via `needs: validate`. No images were pushed to GHCR for 2.10.0, causing the `[404] manifest unknown` error seen in HA Supervisor logs when updating. Replaced with a sed-based transform that applies the known overrides before comparing.
- **Pi pacing tuning silently discarded in Docker image**: The `build` job copied the root `radio.toml` (with higher-load default values) over the HA-specific one in the build context, causing the Docker image to ship the wrong pacing values. The HA-specific file is now used directly at build time.

### Added

- **7 regression-prevention tests** in `tests/test_addon_build_workflow.py`: guards against the `cmp -s` pattern returning, CI/Python test drift, build matrix gaps, trigger path gaps, and the radio.toml build-time overwrite. Any single test failure would have caught the 2.10.0 manifest 404 before release.


## [2.10.0] - 2026-04-14

### Added

- **Startup diagnostics**: Boot logging now prints a structured block in the first 5 seconds — resolved `config_file` path, `cache_dir`, active audio source, track count, API key presence (`anthropic`/`openai`/`ha_token` set/missing without values), and dependency status (`ffmpeg`/`ytdlp` found/missing). Operators can diagnose broken startups without grepping scattered output.
- **yt-dlp binary check**: Warns at boot when `MAMMAMIRADIO_ALLOW_YTDLP` is enabled but the `yt-dlp` binary is not installed. Previously only FFmpeg was checked; a missing yt-dlp would silently fall back to demo tracks with no explanation.

### Fixed

- **HA addon config lost on every restart** *(critical)*: `run.sh` had a shell quoting bug — an f-string containing `"double quotes"` inside a shell `"double-quoted"` string caused the Python options parser to receive mangled code. Result: `NameError: name 'true' is not defined` on every restart, `ANTHROPIC_API_KEY` never exported, all Anthropic calls falling back to OpenAI silently. 11 functional tests now cover the parser.
- **Instant startup**: Prewarm now runs as a background task instead of blocking FastAPI startup for up to 20 seconds. The app becomes available immediately on boot.
- **Normalization cache**: FFmpeg re-encoding is now skipped for tracks already normalized in a previous session. Cached at `cache_dir/norm_{track}_{bitrate}k.mp3`. On Raspberry Pi this saves 60+ seconds per restart per cached track. Cache is busted automatically if the audio bitrate config changes.
- **Pre-normalize next track**: The upcoming track is now normalized before playback begins, preventing the Pi from stalling mid-stream while FFmpeg encodes. Eliminates the queue starvation pattern on Pi-class hardware.
- **Stopped flag preserved**: Operator `/api/stop` now survives crash/restart/watchdog. The flag is only cleared via explicit `/api/resume`, not on every startup.
- **Playback gap elimination**: SQLite writes between songs are now fire-and-forget, eliminating audible gaps on Pi-class hardware.
- **Triggers not heard when queue is full**: `/api/trigger` (banter, news flash, ad) was silently ignored when the producer queue was at lookahead capacity. The queue-full gate now checks `force_next` first and falls through immediately. Regression test added.
- **Host clichés banned**: 14 overused Italian exclamations (`"che bomba"`, `"assolutamente"`, `"pazzesco"`, etc.) added to the banter system prompt banned-phrases list. Hosts were opening every segment with the same exclamation; these phrases now cause a retry.
- **Engine Room shows English**: Home Assistant context (mood, weather arc, recent events) now displays in English in the admin Engine Room panel. Italian strings are preserved internally for the scriptwriter prompt.
- **Ad sonic metadata in dashboard**: Ad segments now surface their format name and sound bed type in the "Now Playing" card during ad breaks.
- **Now-playing label fallback**: When audio normalization fails (e.g. FFmpeg SIGABRT on macOS), the dashboard shows "Preparing..." / "Waiting for first segment..." instead of raw segment type strings ("music", "banter").
- **yt-dlp temp dir cleanup**: Fragment directories under `.ytdlp_tmp/{cache_key}` are now removed after every download attempt (success or failure). Previously accumulated silently on Pi hardware.
- **Status endpoint optimization**: Golden path status (10s TTL), cache size computation (30s TTL), and directory listings are now cached to reduce Pi CPU load.
- **Admin keyboard shortcuts removed**: Global `keydown` listener (`s`/`b`/`a`/`n`) was firing commands while the user typed in the search box. Removed entirely; regression test added.
- **Download validation**: Pre-validation floor lowered from 60s to 30s so silence fallbacks (35s) aren't rejected. `ffprobe` timeout added (30s) to prevent executor thread starvation on corrupt files.
- **Demo asset protection**: Fallback canned clips are now marked non-ephemeral, preventing permanent deletion of bundled demo assets by the LRU eviction pass.


## [2.9.0] - 2026-04-13

### Added

- **Threshold reactive triggers**: New `ThresholdTrigger` type and `THRESHOLD_TRIGGERS` list in `ha_context.py`. `check_reactive_triggers` now accepts `current_states` and fires reactive banter when numeric sensor values cross a wattage threshold. First trigger: coffee machine (`> 50W` → "La caffettiera si è appena accesa!"). Cooldown-keyed separately from event triggers to avoid collision.
- **Coffee machine mood**: `classify_home_mood` now returns "Caffè in preparazione" when coffee machine power exceeds 50W at any time of day (not just morning via switch check).
- **Qualitative power formatting**: `_format_state` now translates coffee machine power to `in funzione / riscaldamento / fredda` and total household power to `casa tranquilla / normale / tutto acceso` instead of raw watts.
- **Mood prompt examples**: `_MOOD_EXAMPLES` in `scriptwriter.py` now covers all 11 moods including the 6 previously uncovered (Caffè in preparazione, La casa si sta svegliando, Stanno svegliandosi, Il robot sta pulendo, Casa vuota, Qualcuno sta facendo la doccia).
- **Deeper HA context**: 10 new entities (room-level light groups, power sensors, star projectors, terrace lights). 4 new mood classifications (Atmosfera rilassata, Lavatrice in funzione, Serata sotto le stelle, La casa si sta svegliando). Terrace lights reactive trigger.
- **Casa dashboard card**: Ambient awareness card showing HA mood, weather, and recent events on the listener dashboard. Appears only when HA is connected and has data. Fades in/out with eyebrow pulse on updates. WCAG AA compliant.
- **`ha_moments` API**: `/public-status` now includes `ha_moments` object with mood, weather, and last event (person-filtered, staleness-guarded). `/status` includes full `ha_details` for admin.
- **Tiered HA prompt references**: When a mood scene is active, hosts may reference up to 2 home details (mood counts toward cap). Weather-mood fusion instruction when both are present.
- **Numeric event passthrough**: Power sensors and other numeric-state entities now generate events correctly in `ha_enrichment.diff_states()`.
- **Multi-session arc phases**: Hosts now warm up over sessions. Four relationship phases (stranger, acquaintance, friend, old_friend) computed from session count, each with phase-aware callback and joke budgets. Milestone sessions (1, 5, 10, 25, 50, 100) inject subtle acknowledgment directives into banter prompts.
- **Song cues**: Machine-derived per-track memory. Anthem detection (played 3+ times, never skipped) and skip-bit detection (skipped 2+ times) create persistent cues. LLM can also generate per-track reaction cues during banter. Cues appear in banter prompts as "TRACK MEMORY" alongside legacy operator rules.
- **Enhanced callbacks**: `callbacks_used` from LLM responses now support structured format `{"song": "...", "context": "..."}` alongside plain strings. Context describes WHY a song was referenced, enriching cross-session memory.
- **Play history enrichment**: `skipped` and `listen_duration_s` columns added to `play_history` table, enabling cross-session anthem and skip-bit detection.
- **`[persona]` config section**: `arc_thresholds`, `anthem_threshold`, `skip_bit_threshold` configurable in `radio.toml`.

### Fixed

- **Listener song request ordering**: Background downloads now stay attached to their own pending request until that request reaches the head of the queue. Later requests can no longer overwrite `pinned_track` and play before the earlier dedication.
- **Strict external-track queueing**: `/api/playlist/add-external` now rejects requests when yt-dlp downloads are disabled instead of returning success after generating silence.
- **`/api/playlist/add-external` payload validation**: Non-object JSON payloads now return a 400 instead of raising `AttributeError`.
- **Song cue youtube_id pinning**: LLM-generated song cues now use the known track ID from playback state instead of trusting the LLM echo, preventing orphan cue rows from hallucinated IDs.
- **Cue text prompt sanitization**: Song cue text is now sanitized via `_sanitize_prompt_data` on the read path before re-injection into banter prompts, closing a cross-session prompt injection vector.
- **SQLite NULLS LAST compatibility**: Song cue ordering replaced `NULLS LAST` (requires SQLite 3.30+) with a portable `CASE` expression.
- **Listener request button**: Fixed `sendRequest()` IIFE scoping bug — button now works via `addEventListener` instead of broken inline `onclick`.
- **Clip rate limiter**: Replaced `threading.Lock` with `asyncio.Lock` for async-correct rate limiting.
- **Song request gating**: Song-request keyword detection now only activates when yt-dlp is enabled, preventing dead-end download attempts.
- **Dead code cleanup**: Removed unused `_diff_states()` and `_build_events_summary()` from `ha_context.py`, unused imports (`cast`, `threading`, `ListenerRequestCommit`).
- **Listener Casa card visibility**: The Home Assistant "Casa" ambient card now renders on `listener.html` (public `/` and `/listen`) and updates from `/public-status` `ha_moments`, not only on the admin dashboard.

## [2.8.0] - 2026-04-13

### Added

- **100-track catalog depth**: Apple Music charts fetch raised from 50 to 100 songs. Combined with local `music/` blending, playlists now hold ~7 hours of unique content before repetition.
- **Local music blending**: MP3 files in `music/` are automatically merged into the chart playlist when `MAMMAMIRADIO_ALLOW_YTDLP=true`. Parsed as `Artist - Title.mp3`; unknown artist falls back gracefully.
- **Host chemistry**: When both hosts score high on energy and chaos, they receive differentiated instructions — one runs the chaos, the other delivers surgical cuts. Prevents both hosts from sounding identically manic.
- **Echo-style transitions**: `write_transition()` now occasionally (20%) mirrors the fading song's energy in the handover phrasing instead of always pivoting away from it.
- **Banter depth**: Exchange count raised from 2-4 to 4-6 lines. Token budget doubled (600 → 1200) to prevent mid-exchange truncation.
- **Banter dedup guard**: Consecutive identical lines from LLM copy-paste errors are silently dropped.

### Fixed

- **`/readyz` always 503 with no listeners**: Producer idles when no stream client is connected, keeping queue depth at 0. Fixed: after 30s of uptime, readiness no longer requires a non-empty queue.
- **`audio_source` stuck at `"prewarm"` in `/healthz`**: First segment produced at startup labelled the source "prewarm" — this value was never replaced until the next segment played. Fixed: falls back to `playlist_source.kind` when audio_source is empty or "prewarm".
- **`allow_ytdlp` dual env-var read**: `fetch_startup_playlist()` read `MAMMAMIRADIO_ALLOW_YTDLP` directly from env instead of using the already-parsed `config.allow_ytdlp`. Fixed to use the config object as the single source of truth.
- **Clip rate-limit dict unbounded growth**: `_clip_rate` dict in `create_clip()` was never pruned. Now evicts entries older than 5 minutes on each request.

## [2.7.0] - 2026-04-12

### Added

- **WTF clip sharing**: Listeners can capture the last 30 seconds of audio into a shareable MP3 clip. Ring buffer records ~60s of stream data; `POST /api/clip` extracts a clip, `GET /clips/{id}.mp3` serves it without auth.
- **Studio bleed atmosphere**: Faint prior banter clips mixed under ~35% of music segments at -22dB, creating the "someone left a mic on" live studio feeling.
- **Studio humanity events**: One-shot events (cough, paper rustle, chair creak, pen tap) fire once per session after 15+ segments. Scarcity is the mechanic.
- **Italian ad brands**: 18 authentic Italian radio advertisers (Esselunga, Fiat, TIM, Barilla, Moment, etc.) replace the fictional brand palette. Organized by category with campaign spines.
- **Fast-talking pharma disclaimer**: Health/pharma ads end with a legally-required disclaimer at +90% TTS rate, matching real Italian radio style.
- **Cache integrity check**: On startup, purge cached files under 10KB (likely failed downloads caching silence placeholders).
- **Boot summary log**: Single INFO line at startup with resolved config, audio source, API keys, HA status, and track count.
- **Dashboard pipeline indicators**: Small status dots near "On Air" showing Anthropic/OpenAI/HA connection state.
- **Dashboard stop sticky**: Stopped station shows a clear banner with resume button instead of misleading loading states.
- **Dashboard ad metadata**: Ad format, sonic palette, and cast info shown during ad breaks.
- **Admin Engine Room tab**: Runtime stats, segment counts by type, and capability status dashboard.
- **Periodic chart refresh**: Charts playlist refreshes every 90 minutes mid-session, merging new tracks without resetting play history.

### Fixed

- **Move-to-next no longer destroys the queue**: Previously purged the entire pre-rendered audio queue on every "play next" action. Now the pinned track plays after buffered segments drain naturally.
- **Song repetition at 30-40 minutes**: Charts fetch limit raised from 20 to 50 tracks, giving ~3.5 hours of unique content.
- **Up-next sync**: Dashboard distinguishes rendered (queued) vs predicted (upcoming) segments.

### Changed

- SFX volume reduced ~12dB across all generators (bumper jingles, station ID beds, time check tones).
- Mid-bumpers between ad spots now only play ~25% of the time instead of every transition.

## [2.6.0] - 2026-04-12

### Added

- **Listener requests**: Listeners can submit song wishes and shoutouts from the dashboard or listener page. Requests appear in the admin panel with status pills (searching/found/error/shoutout) and are woven into banter by the hosts.
- **Track pinning**: Requested songs are downloaded in the background and pinned to play next, with the host announcing the dedication. Pinned tracks enter the queue naturally without interrupting the current segment.
- **External song search**: Admin search now shows both playlist matches and live web results. Clicking "Queue" on a web result downloads and pins the exact video for immediate playback.
- **Station name customisation**: Admins can set a custom station name in the Radio tab. The name persists in localStorage and syncs across open tabs.
- **IP rate limiting**: Listener requests are rate-limited to 1 per 30 seconds per IP with a 10-request queue cap. Countdown is shown to the user on 429 responses.

### Fixed

- **Prompt injection in listener requests**: Name, message, and song-track fields from public listener requests are now sanitised via `_sanitize_prompt_data()` before LLM interpolation, preventing injection attacks that could break banter JSON shape or hijack host script.
- **Stale download on playlist switch**: Background song downloads now capture the playlist revision at enqueue time and discard the result if the source changed while downloading, preventing old listener wishes from leaking into a freshly loaded playlist.
- **Type validation on public endpoint**: `/api/listener-request` now rejects non-string `name`/`message` fields with a 400 instead of raising `AttributeError` on `.strip()`.
- **`force_next` not cleared on playlist switch**: `switch_playlist()` now also resets `force_next` so a previously forced segment type cannot bleed into the new source.
- **`addExternal` button stuck loading**: The admin "↓ Queue" button now always restores via `finally`, so a thrown error can no longer leave the control permanently in a loading state.
- **Non-429 request errors silent**: The listener request form in both dashboard and listener pages now surfaces all error responses (400, 500, network failures) with visible Italian feedback instead of swallowing them in `catch`.
- **`tracks` parameter untyped in `preview_upcoming`**: Signature changed from `tracks: list` to `tracks: list[Track]`, resolving a mypy assignment error at line 142.
- **Hard-coded colour tokens in admin**: Listener request pills and external search buttons now use `var(--ok)`, `var(--sun)`, and `var(--error)` from the design system instead of one-off hex values.
- **Station name input unstyled**: The Station Name input in the Radio tab now uses `class="search-input"` so it inherits the admin dark theme instead of falling back to browser defaults.
- **Accessibility**: Request name and message inputs in `listener.html` now have `aria-label` attributes for screen reader support.
- **Download exact yt-dlp result**: `_download_ytdlp` now uses the direct `youtube.com/watch?v=ID` URL when a `youtube_id` is present on the Track, so the admin-selected video is always downloaded rather than a fresh text-search result that may return a different upload.

## [2.5.1] - 2026-04-11

### Fixed

- **Admin auth trusts private networks**: RFC1918, Tailscale CGNAT (100.64.0.0/10), and link-local IPs are now trusted for admin access. No token or password needed from your own LAN. Public IPs still require explicit auth.
- **Credential UX contradiction**: When API keys are configured (via addon or env), the admin panel now shows a "configured" indicator instead of empty password fields that imply nothing is set. AI status check uses `script_llm` flag instead of only checking Anthropic key.
- **Search error handling**: Admin panel search no longer silently fails on auth errors. Shows "No matches in playlist" instead of misleading "No matches". Placeholder clarified to "Filter playlist..." to set correct expectations.
- **Boot time to first audio**: First music segment is now pre-produced during startup, bypassing the listener idle gate. Audio is ready in the queue before any listener connects, cutting initial wait from ~2 minutes to seconds.
- **Config validation removed for non-local bind**: Binding to 0.0.0.0 no longer requires `ADMIN_PASSWORD` or `ADMIN_TOKEN` at startup. Runtime auth trusts private networks instead.
- **Flaky playlist test**: `test_no_credentials_returns_demo_tracks` no longer hits live charts when yt-dlp happens to be enabled in the test environment.

## [2.5.0] - 2026-04-11

### Added
- **Track rules system** — flag a song mid-stream with a reaction (e.g. "cringe pop classic, Aggu vibes") and future banter about that track will reference it. Rules persist in SQLite and accumulate over time. New `POST /api/track-rules` endpoint.
- **Admin UI tab split** — admin panel reorganised into "Music" tab (queue, playlist, transport) and "Radio" tab (banter triggers, last break, pacing, host personality, logs). Tab selection persists across page refreshes.
- **Flag Track button** — operator can flag the currently playing track with a reaction directly from the admin panel Now Playing card.

### Changed
- **Crossfade Option B** — host transition voice now plays over a higher music bed (50% vs 30%). The music stays audible underneath so the host sounds inside the track, not on top of a near-silent fade.
- **Station ID sting volume** — reduced from 60% to 15%. Sting is now background texture rather than a jarring hit.
- **Host chemistry** — amplified chaos and unpredictability in banter prompts: mid-conversation starts, abandoned sentences, absurdist tangents, physical studio comedy, and emotion-first reactions. Hosts feel less managed.
- **Transition lines** — musical option added: ~30% of transitions echo the song's energy (rhythm, phrasing) rather than announcing the next segment. Real radio DJ feel.

## [2.4.1] - 2026-04-11

### Added

- **Playlist search and filter**: Admin panel search bar restored. Instantly filters the visible playlist as you type, and searches the backend for matching tracks. Tracks can be queued directly from search results.
- **Drag-and-drop playlist reorder**: Playlist tracks now have a grip handle for drag-and-drop reordering. Drop a track onto another to move it. Uses the existing `/api/playlist/move` endpoint.

### Fixed

- **Search endpoint returns results**: `/api/search` now searches the current playlist by title and artist instead of returning empty results (regression from v2.3.0 Spotify removal).
- **Artist clustering prevention hardened**: `pick_next()` now uses a 4-tier progressive relaxation instead of 2 tiers, keeping artist cooldown active even when the hourly cap is relaxed. Soft weights tightened from 0.3x/0.7x to 0.05x/0.4x — same-artist clustering is near-impossible with a reasonable playlist size.
- **Addon radio.toml host personality sync**: Extended host personality descriptions (Marco's ego, Giulia's contempt) now synced to the HA addon copy.

---

## [2.4.0] - 2026-04-11

### Added

- **Volare Refined design system**: Listener and admin UIs now share a unified espresso-dark palette (`#14110F`) with golden accents. The sunset orange is preserved in typography and highlights — not the background. Typography updated to Playfair Display + Outfit + JetBrains Mono.
- **OpenAI key parity in setup status**: `OPENAI_API_KEY` is now treated as equivalent to `ANTHROPIC_API_KEY` for tier classification, health check reporting, and onboarding prompts. Running on OpenAI-only now correctly shows "Full AI Radio" instead of "Demo Radio".
- **yt-dlp in health check**: Setup status now includes a yt-dlp binary check (warn if missing, not fail — yt-dlp is preferred but optional).
- **Onboarding steps payload**: `build_setup_status` now returns an `onboarding_steps` array to drive step-by-step setup UI.
- **Canned clip on reconnect**: When the producer wakes from idle (0→1 listener), it immediately seeds a canned banter clip into the queue so reconnecting listeners hear audio within seconds instead of waiting 30–60s for generation.
- **Home context enrichment**: Four-phase HA intelligence upgrade. Phase 1: event diffing detects state changes between polls and surfaces them as temporal events ("coffee machine turned on 3 minutes ago"). Phase 2: mood classification reads aggregate state into Italian scenes (cooking, sleeping, movie night). Phase 3: weather narrative arcs evolve through the day. Phase 4: reactive impossible moments fire high-priority directives when specific events occur (coffee on → hosts smell espresso, door unlocks → "bentornato").
- **Listener launch ceremony**: Pre-launch state with animated radio warming up, welcome segment display in now-playing UI.

### Fixed

- **Ad double-bed removed**: `mix_ad_with_bed` call in the ad break pipeline was stacking a second music bed on top of the contextual bed already applied by `synthesize_ad`. Removed — ads now have one well-mixed bed, not two with loudnorm artifacts.
- **Producer resumes visibly**: When waking from idle, the producer now logs "Producer resuming (N listeners)" so the wake transition is traceable in logs.
- **Producer idle log deduplication**: The "Producer idle" log fires once per idle period, not once per second.
- **Credential write security**: `_write_env_atomic` now strips newlines from values before writing to `.env`, matching the sanitization already present in `_save_dotenv`.
- **Hub close resets listener count**: `LiveStreamHub.close()` now sets `state.listeners_active = 0` so the producer idle gate correctly reflects the empty state after shutdown.
- **listener.html CSS completeness**: Added missing `--font-mono`, `--line`, `--line-strong`, and `--warning` tokens to listener.html so all design system references resolve.
- **Flaky test eliminated**: `test_rationale_with_album` increased sample count from 100 to 500 to make the probabilistic assertion statistically reliable.

### Changed

- Station name references updated to "Mamma Mi Radio" across launcher scripts, monitor server title, and documentation. Stale SVG wireframes removed.

---

## [2.3.1] - 2026-04-11

### Added

- **Artist diversity cap**: Apple Music Italy charts now enforce a max of 2 tracks per artist, preventing any single artist (e.g. Shiva) from dominating the playlist.
- **Cache LRU eviction**: On startup and hourly while the producer is idle, the oldest cached MP3s are deleted when the cache exceeds the configured size limit (default 500 MB). Controlled via `MAMMAMIRADIO_MAX_CACHE_MB` env var. Prevents SD card overflow on Raspberry Pi.
- **API cost tracking**: `/api/status` now returns `api_cost_estimate_usd`, `cache_size_mb`, and `cache_limit_mb` so operators can monitor token spend and disk usage without SSH.
- **Listener gate**: Playback loop pauses when no listeners are connected, preventing API and CPU burn when the room is empty. Producer naturally idles as the queue stays full.
- **Ad sound beds**: Each ad voiceover is now mixed with a warm 220+330+440Hz ambient bed (-18dB) with a slow breathing LFO. Ads no longer sound like dry voice-only spots.
- **HA media_player entity**: Station now exposes `artist` and `title_only` metadata fields separately; `skipping` and `stopped` states include `metadata: {}` to prevent template errors. DOCS.md includes a copy-paste `configuration.yaml` snippet for a full `media_player` entity with play/pause/skip and album art.
- **Stable admin token**: `admin_token` is now a configurable add-on option. Set it once in the HA UI and reference it in `secrets.yaml` for the media_player integration — no more log-hunting on each restart.
- **Station name on air**: Hosts now say the station name naturally once every 3–4 banter exchanges, matching the `station_name` config option. Rename in the HA UI; hosts adapt within minutes.
- **Sharper host personalities**: Marco doubles down on bad takes and believes he's the reason people tune in. Giulia now delivers devastation with the warmth of a tax audit. Banter rules require mandatory conflict, Giulia cutting Marco off at least once per exchange, unexplained recurring bits, and song-specific reactions.

### Fixed

- `asyncio.get_event_loop()` (deprecated since Python 3.10) replaced with `asyncio.get_running_loop()` in the producer idle loop.
- yt-dlp download options now include `noprogress: True` to suppress progress-bar noise in logs.
- Error-recovery silence replaced: when segment production fails, the producer now falls back to a canned banter clip before inserting silence. Quiet patches between sections are significantly reduced.

---

## [2.3.0] - 2026-04-11

### Removed

- **Spotify integration**: Removed go-librespot, Spotipy OAuth, Spotify Connect, and all Spotify API routes. Music now comes from local files, yt-dlp chart downloads, and bundled demo tracks. The `spotipy` runtime dependency is dropped.
- **5 source files deleted**: `spotify_auth.py`, `spotify_player.py`, `go_librespot_config.py`, `go_librespot_runtime.py`, `go-librespot-config.yml`.
- **6 Spotify API routes removed**: `/spotify/auth`, `/spotify/callback`, `/api/spotify/auth-status`, `/api/spotify/disconnect`, `/api/spotify/source-options`, `/api/spotify/source/select`.
- **HA addon go-librespot**: No longer downloads or runs go-librespot binary. Docker image is smaller and starts faster (`gcompat` removed, timeout reduced to 120s).
- **Dead UI**: Removed search bar from admin panel and "Playlist link" from dashboard advanced settings (both were wired to Spotify-only backends).

### Changed

- **3-tier system**: Capabilities simplified from 5 Spotify-centric tiers to 3: Demo Radio (no LLM key), Full AI Radio (Anthropic/OpenAI key), Connected Home (LLM + Home Assistant).
- **Dashboard**: Spotify Connect card, credential forms, and source picker removed. Controls are always visible.
- **Addon config**: 3 fewer options (no spotify_client_id, spotify_client_secret, playlist_spotify_url).
- **start.sh**: Simplified to a minimal uvicorn launcher (was 137 lines managing go-librespot lifecycle).
- **Documentation**: All 9 doc files updated to remove Spotify references and reflect the 3-tier model.
- **Net reduction**: ~6,800 lines removed across 72 files.

### Fixed

- **Playlist URL loading**: `/api/playlist/load` now routes URL requests to charts instead of erroring with "Unsupported source kind: url".
- **Admin queue linking**: Fixed frontend reading `track_id` while backend sends `spotify_id`, breaking queue-to-playlist highlighting.

## [2.2.2] - 2026-04-10

### Changed

- **Listener-first routing**: `/` now serves the public listener page. Dashboard moved to `/dashboard`. Guests tap a link and hear radio, operators type `/admin`. The first thing a visitor sees is the experience, not a setup screen.
- **Readyz returns 503 when not ready**: `/readyz` now returns HTTP 503 (was 200) when the station is still starting. Adds `ready: bool` and `watchdog_status` fields for the upcoming "tuning in" animation.
- **Station name consistency**: Unified to "Mamma Mi Radio" across all files (was inconsistent "Malamie Radio" in some places).

### Fixed

- **Song repetition after ~30 min**: `played_tracks` history is now cleared on playlist switch and track reorder. A 20-track playlist no longer loops because the diversity filter's deque fills and weights flatten.
- **"Move to upcoming" queue confusion**: `move_to_next` now clears play history alongside the queue purge, preventing the moved track from being penalized by stale diversity data.
- **Pre-existing lint**: removed unused `ingress_prefix` variable in `require_admin_access`.

### Added

- **Quality gate env var escape hatch**: Set `MAMMAMIRADIO_SKIP_QUALITY_GATE=1` to bypass audio validation in emergencies.
- **Canned fallback corruption alert**: Canned banter fallback rejection upgraded to `logger.error` with "ASSET CORRUPTION" prefix for operator visibility.
- **HA addon: gcompat for go-librespot**: Alpine image now includes `gcompat` so the upstream go-librespot binary runs correctly.
- **Silence fallback duration**: Fallback silence generator now produces 35s+ audio (was 5s), passing the MUSIC quality gate minimum.
- **Audio quality gate documentation**: Added `-38dB coincidence` comment, updated `validate_segment_audio` docstring.

## [2.2.1] - 2026-04-10

### Added

- **Session stop persistence**: stopped state now survives server restarts. A `session_stopped.flag` in the cache dir is written on stop and cleared on resume, so reloading the app during a planned break keeps the station paused.
- **Spotify transfer backoff**: `SpotifyPlayer` tracks consecutive transfer failures and backs off to polling every ~5 minutes after 10 failures (was fixed 15 s), preventing log spam when no Spotify device is reachable.
- Playlist index endpoints now use a strict `_as_int_index()` helper — non-integer payloads (e.g. `"abc"`) are rejected without mutating state.

### Changed

- **Silence removal in normalizer**: `loudnorm` filter chain now appends `silenceremove` to strip trailing silence before segments enter the queue. Reduces dead air between transitions.
- `_is_addon()` no longer treats `/data/options.json` presence as an add-on signal — only Supervisor-provided env tokens are authoritative. Prevents false add-on mode in dev/test environments where that path is mounted.
- `moveNext()` admin UI function shows an optimistic queue preview while the track renders instead of leaving the queue visually stale.
- SFX generation expressions (cash register, ice clink) simplified — removed conditional time-guards that caused f-string injection of variable names into FFmpeg filter syntax.

### Fixed

- Stale test assertion for Apple Music chart track IDs (`chart_{id}` format introduced in 2.2.0 but test expected empty string).
- CI: remove unused `# noqa: N802` (pre-commit ruff v0.9.10 flags it; CI ruff v0.15.9 does not — divergence triggered `RUF100`). N802 now suppressed via `per-file-ignores` in `pyproject.toml` instead.
- CI: revert `pydantic-core` to `2.41.5` in `requirements.txt`. Dependabot PR #92 bumped it to `2.45.0` without bumping `pydantic`, breaking the lockfile (`pydantic==2.12.5` requires exactly `pydantic-core==2.41.5`).

## [2.2.0] - 2026-04-09

### Added

- **Audio quality gate**: new `audio_quality.py` module validates banter, ad, and music segments before they reach the live queue. Checks duration, silence ratio, silence span, and volume levels with per-type thresholds. Rejects corrupt yt-dlp downloads and silent placeholders before they air.
- **`AudioToolError`**: distinct exception for ffprobe/ffmpeg binary failures. Tool absence is an ops problem, not a content reject — segments pass through rather than being silently dropped when the binary is unavailable.
- **MUSIC quality threshold**: permissive gate (min 30 s, 95% silence cap) catches truncated placeholders and corrupted downloads. On reject the file is deleted and the producer retries the next track automatically.
- Runtime health transparency: `/healthz`, `/readyz`, and `/status` now expose queue-shadow integrity, task liveness, playback epoch, and active audio-source failover state.
- Deterministic Up Next explainability: preview entries now include per-segment `reason` fields and explicit source tagging (`rendered_queue` vs `predicted_from_playlist`).
- Delivery guardrail: new `scripts/check-changelog-sync.sh` hook enforces synchronized root and HA add-on changelog updates on version bumps.

### Changed

- Quality gate calls now use `loop.run_in_executor` so FFmpeg validation never blocks the async event loop — resolves potential stream freezes on HA add-on hardware.
- Music sequencing now follows playlist order deterministically in the producer, keeping playlist operations and Up Next behavior tightly coupled.
- Public/admin runtime sync now auto-corrects long-session queue-shadow drift when stale UI entries exceed real queue depth.
- Admin Host Personality UX now includes clearer axis labels, trait guidance, and one-click presets (`Balanced`, `Calm`, `Hype`).

## [2.1.0] - 2026-04-08

### Added

- **Impossible moments (zero-config)**: time-of-day, day-of-week, and listener-behavior-aware banter that works at every tier, no Spotify or Home Assistant required. The DJ knows what time it is and whether you just tuned in.
- **"Benvenuto" new listener greeting**: when someone connects to the stream, the next banter segment acknowledges them. First listener gets a special welcome. Works via TTS (no LLM) or through the LLM prompt when an API key is present.
- **Listener connection tracking**: `LiveStreamHub` now tracks active, peak, and total listener counts. Exposed on the `/status` admin API under `listeners`.
- **40+ pre-written Italian impossible lines** in `context_cues.py`: tagged by show segment (alba, mattina, pranzo, pomeriggio, sera, notte), day-of-week, and listener behavior pattern (restless_skipper, ballad_lover, energy_seeker, rides_every_song).
- **Shareware gold closer**: the 3rd demo banter clip is now a time-aware TTS line instead of a pre-recorded clip, selling differentiation over quality in the trial experience.
- **Compounding listener memory**: returning listeners are recognized across sessions. The hosts build theories, running jokes, and callbacks that persist in SQLite and feed back into banter prompts. Session 1 gets curiosity; session 5 gets inside jokes.
- **Persona feedback loop**: Claude's banter responses now include `persona_updates` (theories, jokes, callbacks) that are persisted and injected into future prompts automatically.
- **Track motif recording**: every played track is recorded in the listener persona, giving hosts material to reference past music naturally.
- **Session tracking**: listening sessions are detected (10-minute gap = new session) and counted, so banter adapts to how often the listener returns.

### Fixed

- **Persona security**: instruction-like patterns in LLM-generated persona entries are now filtered (matching the existing `ha_context` sanitizer), preventing stored prompt injection across sessions.
- **Callback sanitization**: `callbacks_used` entries from LLM responses now go through `_sanitize()` before storage.
- **Persona row seeding**: `init_db` now seeds the default persona row, preventing `increment_session` from silently no-oping on fresh databases.

## [2.0.2] - 2026-04-06

### Added

- **OpenAI fallback for script generation**: banter, ads, news flashes, and transitions try Anthropic first, then fall back to OpenAI `gpt-4o-mini` automatically. Set `OPENAI_API_KEY` in `.env` or the dashboard settings panel.
- **Golden path onboarding UI**: dashboard and listener page show clear, step-by-step guidance when Spotify isn't connected yet, including what to do and why music is silent.
- **Spotify redirect URI override**: new `MAMMAMIRADIO_SPOTIFY_REDIRECT_BASE_URL` env var lets you use a stable HTTPS domain for OAuth callbacks instead of localhost.
- **Interactive Spotify auth workaround**: when macOS hostname causes the `.local.local` mDNS bug, the app detects it and offers browser-based login instead of broken zeroconf discovery.

### Fixed

- **FFmpeg `aevalsrc` crash** (exit code 234): bare `(t>0.08)` gate expressions replaced with lavfi-safe `if(gte(t,onset),1,0)` syntax. Bumper jingles now also fall back to a simpler sine-based jingle if the complex expression still fails.
- **Spotify callback URL mismatch**: `localhost` is now canonicalized to `127.0.0.1` in OAuth redirect URIs, matching Spotify's loopback policy.

## [2.0.1] - 2026-04-06

### Fixed

- Home Assistant add-on no longer crash-loops when `/data/options.json` is unreadable or malformed. Startup now logs the parse error and continues with defaults.
- Add-on startup no longer hard-fails when `/data` is not writable. It falls back to `/tmp/mammamiradio-data` so uvicorn can still boot and ingress can connect.
- Add-on runtime paths now honor `MAMMAMIRADIO_CACHE_DIR`, `MAMMAMIRADIO_TMP_DIR`, and `MAMMAMIRADIO_GO_LIBRESPOT_CONFIG_DIR` in add-on mode, preventing hardcoded path mismatches.
- `go-librespot` config sync now uses the resolved runtime config directory instead of a hardcoded `/data/go-librespot/config.yml` target.

## [2.0.0] - 2026-04-05

This is a major release. The station now boots instantly with zero config, progressively unlocks capabilities as you add API keys, and sounds dramatically better.

### Breaking

- **Capability flags replace mode system**: the old 64-state mode field is gone. Four independent flags (`spotify_connected`, `spotify_api`, `anthropic`, `ha`) derive a tier label (Demo Radio → Your Music → Full AI Radio). Integrations reading the old mode field must switch to `GET /api/capabilities`.

### Added

- **Demo-first boot**: station starts instantly with zero config. Built-in demo playlist of classic Italian tracks plays while you set up Spotify. First 3 banter clips use pre-bundled audio, then prompt for an Anthropic API key.
- **OpenAI TTS engine for hosts**: hosts can use OpenAI's `gpt-4o-mini-tts` as an alternative to Edge TTS via `engine = "openai"` in `radio.toml`. Personality-aware delivery instructions shape each host's voice from their energy, warmth, and chaos axes.
- **Signature Ad System**: 6 ad formats (classic pitch, testimonial, duo scene, live remote, late-night whisper, institutional PSA), sonic worlds per category, role-based multi-voice casting, per-brand campaign memory with escalation rules, and brand motif generation.
- **Spotify source picker**: choose from your playlists or Liked Songs directly in the dashboard. Persisted source selection restores your last choice on restart.
- **Autoplay on Spotify Connect**: tapping the station in Spotify triggers personalized welcome banter referencing the currently playing song.
- **Listener persona system**: tracks aggregate listening patterns (skip rate, energy preference, ballad loyalty) and feeds them into banter prompts.
- **Personality sliders**: tune host energy, chaos, warmth, verbosity, and nostalgia from the dashboard.
- **Dashboard Volare theme**: warm Italian sunset palette with Playfair Display typography. Capability-driven cards, connect hero card, tier badge with pulse animation.
- **Capability flags API**: `GET /api/capabilities` returns flags, tier, guided next-step hint, and connect status.
- **New API routes**: `/api/hosts`, `/api/hosts/{name}/personality/reset`, `/api/pacing`, `/api/setup/save-keys`, `/api/spotify/auth-status`, `/api/spotify/disconnect`, `/api/spotify/source-options`, `/api/spotify/source/select`.
- CSRF protection for admin mutating endpoints when accessed over non-loopback networks.
- Conductor workspace support with lifecycle scripts for multi-agent development.
- 30+ new tests covering news flash generation, TTS prosody, crossfade audio, scheduler counters, admin CSRF, trigger endpoint, dialogue synthesis, and API client caching.

### Changed

- Bounded lists (`played_tracks`, `running_jokes`, `segment_log`, `stream_log`, `ad_history`, `recent_outcomes`) now use `deque(maxlen=N)` instead of manual truncation.
- Home Assistant context uses a reusable `httpx.AsyncClient` singleton and module-level cache.
- **Richer ad SFX**: layered tones with exponential decay envelopes and noise transients. Better bumper jingles with plucked envelopes and velocity variation. Music beds use 4 harmonic layers with per-mood tremolo and reverb.
- **Punchier ad processing**: heavier compression (8:1, -24dB), presence + air boost, 120Hz mud cut.
- **FFmpeg performance**: collapsed multi-input sine generators into single `aevalsrc` expressions (up to 8→1 inputs).
- Source switching triggers immediate cutover with queue purge and concurrent switch serialization.
- Voice differentiation via SSML prosody derived from personality axes.
- Playback throttle threshold tightened from 10ms to 5ms.
- Dashboard and admin HTML injection results cached by ingress prefix.

### Fixed

- `running_jokes` deque converted to `list()` before JSON serialization (prevented `TypeError` on `/api/status`).
- Deque fields wrapped with `list()` before slicing in scriptwriter.
- Auto-transfer no longer spams logs when no Spotify Client ID is configured.
- Banter generation gracefully falls back when no Anthropic key is set.
- Banter TTS failure skips the segment instead of crashing the producer loop.
- Spotify audio capture failures fall back to local download.
- Category-based sonic world defaults no longer share mutable references across calls.
- Duo scenes with only one role demoted to classic pitch instead of broken multi-voice audio.
- Spotify playlist fetch returned zero tracks when API items nested under `item` key.
- Listener page `_base is not defined` JS error from service worker scope.
- Producer recovery stall when go-librespot restarts mid-segment.
- `start.sh` reclaims stale ports on startup.

### Dependencies

- `docker/setup-qemu-action` 3.7.0 → 4.0.0
- `docker/login-action` 3.7.0 → 4.1.0
- `docker/setup-buildx-action` 3.12.0 → 4.0.0
- `docker/metadata-action` 5.10.0 → 6.0.0
- `docker/build-push-action` 6.19.2 → 7.0.0
- `requests` 2.33.0 → 2.33.1
- `charset-normalizer` 3.4.6 → 3.4.7

## [1.5.0] - 2026-04-04

### Added

- **Signature Ad System**: ads are now a full creative sub-format with 6 ad formats (classic pitch, testimonial, duo scene, live remote, late-night whisper, institutional PSA), sonic worlds per category, role-based speaker casting, and per-brand campaign memory with escalation rules.
- 6 new SFX types (tape stop, hotline beep, mandolin sting, ice clink, startup synth, register hit) and 8 new music beds (tarantella pop, cheap synth romance, suspicious jazz, discount techno, plus environment beds for cafe, beach, showroom, stadium, and more).
- Brand motif generation: recurring brands get a short audio jingle built from their sonic signature, prepended to each ad spot.
- Environment bed layering: ads can have a quiet ambient bed (cafe noise, highway hum) mixed under the voice before the music bed.
- Multi-voice ad support: duo scenes and testimonials cast two distinct speakers with role-based voice resolution.
- Campaign spines in `radio.toml`: each recurring brand can define a premise, escalation rule, preferred format pool, sonic signature, and spokesperson.
- `concat_files` now properly inserts silence gaps between segments (previously the `silence_ms` parameter was accepted but unused).
- Spotify source picker: choose from your playlists or Liked Songs directly in the dashboard (local/macOS mode only).
- Persisted source selection: the station restores your last chosen source on restart via `cache/playlist_source.json`.
- New API routes: `GET /api/spotify/source-options` and `POST /api/spotify/source/select` for programmatic source switching.
- CSRF protection for admin mutating endpoints when accessed over non-loopback networks.
- Personality sliders in the dashboard for tuning host energy, chaos, warmth, verbosity, and nostalgia.
- Conductor workspace support with lifecycle scripts for multi-agent development.
- Dependabot automerge workflow for dependency updates.

### Changed

- Ad generation prompt rewritten around explicit format selection, speaker role descriptions, and sonic world cues instead of one generic prompt.
- `write_ad()` now accepts a voice dict (role->AdVoice) instead of a single voice, enabling multi-speaker ads.
- `synthesize_ad()` resolves voice per-part by role, with graceful fallback to the first voice in the dict.
- Ad history now tracks format and sonic signature alongside brand and summary, enabling format rotation and campaign continuity.
- LLM ad summaries are now instructed to be in English for consistent campaign arc tracking.
- SFX type list in the LLM prompt is now generated from a single source of truth (`AVAILABLE_SFX_TYPES` in normalizer.py).
- Source switching now triggers immediate cutover: queued segments are purged and current playback is skipped so the new source starts right away.
- Concurrent source switches are serialized so rapid clicks cannot corrupt station state.
- The producer detects source changes mid-generation and discards stale segments instead of queuing them.
- In addon/Docker mode, the interactive source picker is disabled server-side; use the playlist URL field instead.

### Fixed

- Category-based sonic world defaults no longer share mutable references across calls (prevented silent state corruption).
- Duo scenes and testimonials with only one role in the LLM output are demoted to classic pitch instead of producing broken multi-voice audio.
- `_estimate_duration` helper used consistently instead of inline formula duplication.
- Spotify playlist fetch returned zero tracks when API items were nested under `item` key.
- Source picker showed 0 tracks for user playlists due to wrong count field.
- Listener page `_base is not defined` JS error from service worker scope.
- Producer recovery stall when go-librespot restarts mid-segment.

## [1.2.0] - 2026-04-02

### Added

- Go-librespot runtime ownership helpers, config-sync support, and add-on smoke coverage for startup/config path handling.
- Unauthenticated `/healthz` and `/readyz` probes, Makefile-based local quality commands, and broader regression coverage across streamer, producer, Spotify, and audio normalization paths.
- A four-step first-run onboarding flow in the dashboard, backed by `/api/setup/status`, `/api/setup/recheck`, and `/api/setup/addon-snippet`, plus a persistent station-mode banner.

### Changed

- `start.sh` now reuses the owned go-librespot process when possible, and local dev picks up `LOG_LEVEL` plus `*.toml` reloads without extra shell wiring.
- Local quality checks now run through the repo `.venv` with coverage, timeout, watch, and CI-aligned tooling defaults instead of relying on global Python utilities.
- README and Home Assistant add-on docs now mirror the same four onboarding steps and setup language shown in the product UI.

### Fixed

- Spotify auto-transfer now uses the configured go-librespot device name, and runtime ownership checks no longer rely on loose `pgrep` matching.
- Apple Silicon and stripped-PATH installs now resolve `go-librespot` correctly during setup checks, and stale Spotify auth state no longer leaves the dashboard claiming the wrong station mode.
- Setup rechecks can use cached user Spotify auth for private playlists, and the sweep-regression tests now match the runtime chirp implementation shipped in the hardening work.

## [1.1.3] - 2026-04-03

### Added

- Native Conductor workspace lifecycle hooks via `conductor.json`, including repo-owned setup, run, and archive scripts for workspace bootstrapping and cleanup.

### Changed

- Contributor docs now explain that `conductor.json` and `scripts/conductor-*.sh` are committed repo infrastructure, while `.context/` stays runtime-only.

### Fixed

- Conductor workspace setup no longer depends on a bash snippet surviving interactive `zsh` execution, so new workspaces bootstrap the project venv reliably.

## [1.1.1] - 2026-04-02

### Changed

- Local guardrails now catch addon issues before you push. Pre-commit hooks enforce conventional commit messages and version sync. `./scripts/validate-addon.sh` runs 13+ checks (version parity, config wiring, Dockerfile safety, translations, ingress rewrite safety) so CI failures are caught locally first.

### Fixed

- Version-sync hook reads from the git index instead of the working tree, so partially staged commits can no longer bypass the version check.
- Addon validation script derives the image owner from the git remote (not `gh api user`), so contributors no longer get false image-path mismatches.
- Empty version parsing now fails fast with a clear error instead of silently passing.

### For contributors

- Regression tests for the staged-version hook and the ingress rewrite validator.
- `CLAUDE.md` commit prefix docs now match the hook allowlist (added `merge:`).

## [1.1.0] - 2026-04-01

### Added

- **PWA support**: Install Radio Italì as a mobile app from your browser. Manifest, service worker, and app icons included.
- **Lock screen controls**: MediaSession API integration shows song title, artist, and Radio Italì artwork on your lock screen. Play/pause works from lock screen and notification shade.
- **Install prompt**: Chromium browsers get a native install banner; iOS shows manual "Add to Home Screen" instructions.
- **Offline resilience**: Service worker caches the app shell. If you lose connection, you see an offline status and the stream auto-reconnects with exponential backoff.
- **Static file serving**: New `/static/` route serves PWA assets (manifest, icons) with path traversal protection. `/sw.js` served at root for service worker scope.
- **HA Ingress compatibility**: Service worker uses `endsWith`/`includes` path matching so caching works correctly behind Home Assistant's ingress proxy.
- Custom logo and favicon: retro radio with Italian flag stripe, Terracotta Sera palette
- Logo in READMEs, HA addon store listing (icon.png + logo.png), apple-touch-icon, OG tags
- go-librespot bundled in HA addon Docker image for Spotify Connect support
- HA addon runbook and addon-specific documentation

### Changed

- HA addon Dockerfile builds from local source (CI copies files into build context) instead of pip installing from git
- Improved producer segment production logging and error handling
- HA addon run.sh extracts all config options including claude_model and playlist_spotify_url

### Fixed

- Re-enabled `host_network: true` — required for go-librespot mDNS (Spotify Connect discovery). Admin endpoints protected by auto-generated `ADMIN_TOKEN`
- Fixed ingress double-prefix bug: `_inject_ingress_prefix` was rewriting JS string literals (e.g. `'/api/'`, `'/status'`) that the client-side `_base` variable already handles, causing all API requests to 404 behind HA ingress
- Sanitize HA entity state values before injecting into Claude prompts (truncate, filter injection patterns, wrap in data delimiters)
- Pin all GitHub Actions in addon-build.yml to SHA hashes (supply chain hardening)
- HA addon config.yaml schema uses `password?` type for secrets (masked in UI)

### For contributors

- 6 new tests: service worker route, manifest route, 404 for missing static files, path traversal blocked, ingress prefix rewriting for static paths and sw.js.

## [1.0.0] - 2026-03-30

### Added

- **Run with Docker**: `docker compose up` and you have a radio station. No Python, no FFmpeg, works on Windows, Mac, and Linux.
- **Home Assistant add-on**: Install from the HA add-on store, configure your API key, and the radio appears in your sidebar. Hosts automatically reference your home state (lights, temperature, who's home) with zero HA configuration.
- **Dashboard works behind HA ingress**: The web UI detects its base path at runtime, so it works both at `/` and behind Home Assistant's ingress proxy.
- **Configure via environment variables**: Override station name, Spotify playlist, Claude model, and HA settings without editing `radio.toml`. Useful for Docker and add-on deployments.
- **Multi-arch Docker CI**: GitHub Actions builds amd64 + arm64 images on tag push, published to GHCR.

### Changed

- Admin tokens are now accepted only via the `X-Radio-Admin-Token` header. Query parameter auth (`?admin_token=...`) removed to prevent token leakage in browser history, server logs, and referer headers.
- API error responses no longer expose internal exception details. Generic error messages are returned to clients while full details are logged server-side.

### For contributors

- `.dockerignore` for clean Docker builds.
- `tests/test_config_env_overrides.py` covers all new env-var override paths (11 tests).
- Pinned production dependency lockfile (`requirements.txt`) with SHA-256 hashes via `pip-compile`.
- All GitHub Actions SHA-pinned to immutable commit hashes across all 3 workflow files.
- Added `.github/CODEOWNERS` requiring review for workflow file changes.
- Added gitleaks pre-commit hook for secret scanning before push.

## [0.3.0] - 2026-03-30

### Added

- Home Assistant addon: run fakeitaliradio as a supervised addon with one-click install from the HA Add-on Store. Includes Dockerfile, config schema, and GitHub Actions build pipeline for amd64/aarch64.
- HACS custom integration: media player entity that auto-discovers the addon via the Supervisor API, shows current track info, and exposes the stream URL for relay to other speakers.
- Addon-aware config: automatic detection of HA environment via `SUPERVISOR_TOKEN`, persistent paths under `/data/`, and `options.json` secret injection.
- Ingress support: dashboard and listener pages rewrite URLs for HA's Ingress proxy, with auth handled by the Supervisor.
- Spotify headless auth: addon mode disables browser-based OAuth and falls back to client credentials for public playlist access when no cached user token exists.
- Tests for addon config detection, options parsing, ingress URL rewriting, auth bypass, and Spotify addon behavior.

### Changed

- `go_librespot_config_dir` is now configurable (addon uses `/data/go-librespot`).
- Non-local bind auth check skipped in addon mode (Supervisor handles auth).
- Log path references use `config.tmp_dir` instead of hardcoded `tmp/`.

## [0.2.0] - 2026-03-30

### Added

- One-click macOS launcher: `Start Radio.command` bootstraps venv, installs deps, starts the radio, and opens the dashboard. `Stop Radio.command` to stop.
- Search and add individual Spotify tracks from the dashboard.
- Load a new Spotify playlist by URL from the dashboard.
- Inline SVG favicon, OG tags, and `theme-color` on both listener and dashboard pages.
- Sharing instructions for listeners on your network in README.
- New API routes: `/api/search`, `/api/playlist/add`, `/api/playlist/load`.
- Ruff linting and formatting config in `pyproject.toml` with auto-fixed violations across all Python files.
- Mypy type checking config (lenient mode) to catch obvious type errors without blocking on untyped legacy code.
- Pre-commit hooks for ruff lint and format (runs on every commit).
- GitHub Actions CI workflow running ruff, mypy, and pytest on push/PR to main.
- Comprehensive test suite raising coverage from 30% to 63%.
- Automated dependency update infrastructure (Dependabot + merge workflow).
- `TODOS.md` for tracking deferred work items.

### Changed

- go-librespot default path changed from hardcoded Homebrew path to bare `go-librespot` (PATH lookup).
- Home Assistant disabled by default in `radio.toml` with empty URL.
- `start.sh` uses `.venv/bin/python` instead of system `python` for reliability.
- Dashboard layout widened to 1280px max-width with 3-column grid.
- "Purge Queue" renamed to "Clear Buffer" with descriptive tooltip.
- Station name corrected to "Radio Itali" with grave accent across listener page.
- Pinned ruff (0.9.10) and mypy (1.15.0) versions in CI to prevent surprise breakage from upstream releases.
- Updated `CONTRIBUTING.md` with lint, format, and type-check commands.

### Fixed

- Playlist URL placeholder mismatch that prevented auto-populating the current playlist URL.
- Playlist input stays permanently disabled if network request fails.
- Search shows "No results" instead of cryptic error when Spotify is not configured.
- Route documentation, placeholder text, and UI hardening fixes.

## [0.1.1] - 2026-03-29

### Added

- Dedicated repo docs for architecture, operations, troubleshooting, and contributing.
- Inline module, class, and function documentation across the Python application code.

### Changed

- Expanded `README.md` and `CLAUDE.md` so setup, auth, fallback behavior, and runtime flow match the current code.
- Kept `audio.bitrate` as the canonical bitrate setting in user-facing docs.

## [0.1.0] - 2026-03-29

### Added

- Start a local AI-powered Italian radio station with an admin dashboard at `/`, a public listener page at `/listen`, and a raw MP3 stream at `/stream`.
- Alternate songs with AI-written host banter and multi-spot AI-generated ad breaks, including bumper jingles, custom ad voices, and recurring campaign callbacks.
- Expose admin controls for shuffle, skip, queue purge, track removal, reordering, and "play next" from the web UI.
- Provide public station status plus admin-only logs and debugging details for queue depth, recent playback, generated scripts, and go-librespot output.

### Changed

- Prefer real Spotify playback through go-librespot when a user connects the `mammamiradio` device, but keep the station alive with liked songs, demo tracks, local files, yt-dlp, or placeholder audio when that path is unavailable.
- Throttle stream output to the configured bitrate so the dashboard, listener, and actual audio timeline stay aligned.
- Require admin auth when binding to a non-local interface, while keeping localhost development friction low.

### For contributors

- Add pytest coverage for config validation, scheduler pacing, ad-brand selection, campaign history, and ffmpeg-backed audio helpers.
- Ship a local dev entry point in `start.sh` plus template config in `.env.example` and `radio.toml`.
