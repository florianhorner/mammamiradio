# Changelog

## Unreleased

### Added

- **HA Green performance smoke gate** — `make perf-smoke` now checks a live station's health, readiness, public runtime status, and first stream byte against configurable HA Green thresholds.
- **Festival Mode** — New `festival_mode` add-on option. When enabled, the AI hosts become theatrical music competition MCs: songs are introduced as fictional Italian-regional delegations, dramatic points are assigned, and drinking game triggers are called. Toggleable live from the admin panel without an add-on restart; persisted through `/data/options.json` so it survives restarts.

### Changed

- **Queue fallback starts before the health-failure window.** Active listeners now get cache/demo rescue attempts after a short bounded queue wait instead of waiting for the 30-second silence health threshold.

### Fixed

- **Norm-cache rescue no longer repeats the first cached song by filename.** Empty-queue fallback avoids the current/recent song when alternatives exist and randomizes the rescue candidate, so skip is less likely to land back on the same cached track.

## 2.12.3

### Changed

- **Italian-first is now the default.** New add-on installs default `super_italian_mode` to `true`, while the option remains available for operators who want the older code-switching style.
- **Jamendo can participate in the normal programme.** When charts and Jamendo are both configured, startup blends Jamendo tracks into the chart rotation instead of keeping Jamendo fallback-only.
- **Admin source chips enrich instead of replacing the programme.** Jamendo, chart reload, and decade buttons add tracks into the current rotation without purging the queue, skipping current playback, or clearing listener requests.

### Fixed

- **Palinsesto hides scheduler pool diagnostics and duplicate current rows.** Pool badges/wrap notes no longer appear in the operator programme, and the current segment is filtered out of history.
- **Speech/ad transition stacking is reduced.** Segments that already carry a music-tail crossfade no longer receive an extra transition sting before them.
- **Empty-queue skip is safer on HA Green.** Skip records a bridge action and forces next music before cutting when the queue is empty, and status exposes skip readiness.
- **Ad disclaimer speed is deterministic by format.** The old near-2x role spike is replaced with format-scoped pacing.

## 2.12.2

### Fixed

- **Palinsesto table no longer causes horizontal overflow on phone widths.** The six-column programme table now collapses into compact cards on phone widths and stays inside its panel on desktop.
- **Anthropic usage-limit errors now trip the provider circuit breaker.** Account quota/credit exhaustion suspends Anthropic for the existing cooldown and falls through to OpenAI immediately, instead of retrying Anthropic on every host segment while HA Green waits.

## 2.12.1

### Added

- **Chaos Mode for host banter** — Adds the `chaos_mode_active` add-on option and admin `/api/chaos` persistence path. The toggle survives add-on restarts through `/data/options.json` and can be controlled from the admin Radio tab.

### Changed

- **Listener-request public IDs are split from admin mutation IDs.** The public request feed now exposes `public_token` for listener-side tracking and keeps the admin-only `request_id` out of the public payload.
- **Banter history now separates queued tracks from heard tracks** — `played_track_log` records music when it actually starts streaming, so chaos impossible-recall prompts only reference songs listeners really heard.

### Fixed

- **Provider key checks no longer stack overlapping probes.** Rapid clicks on the setup provider check now share the active result instead of launching duplicate Anthropic/OpenAI probe sets.
- **Listener-request rate limiting respects HA ingress client headers.** Requests through trusted local proxy paths bucket by the real listener IP while direct callers cannot spoof forwarded headers.
- **Listener song-request failures leave clear state.** Search failures and shutdown cancellation now mark the request errored instead of leaving it stuck as "still downloading."

## 2.12.0

### Added

- **Jamendo client ID option** — `jamendo_client_id` is now a first-class add-on option. Set it in the add-on configuration to enable CC-licensed music from Jamendo. Leave empty to disable.
- **Secret-safe provider check endpoint** — admin-only `POST /api/setup/provider-check` actively probes the live Anthropic key, OpenAI chat key, and OpenAI TTS key with tiny requests, returning only configured/ok/status/error-category fields. This helps distinguish "the add-on has a bad key" from "local `.env` has a different key" without exposing secrets.
- **Full imaging architecture** — music-to-voice and voice-to-music boundaries now get short branded transition stings, sweepers pick up motif underlays, and banter/news can sit over ducked talk beds for a more continuous station feel. Enabled by default and configurable through the new `[imaging]` block in the add-on `radio.toml`; FFmpeg-generated stings and beds are used automatically when no bundled imaging assets are present.
- **Super Italian Mode toggle** — new `super_italian_mode` addon option (default `false`). Off: listener UI in English with Italian station-feel words intact (`Stasera in onda`, `Palinsesto`, `Mi`, tricolor); AI hosts code-switch with Italian sprinkles. On: listener UI flips to full Italian; hosts lean fully into Italian idioms and address listeners as `amici miei`. Admin UI stays English regardless. Toggle is also exposed in the admin Engine Room and persists via `/data/options.json` so it survives addon container updates.

### Fixed

- **Anthropic model and audio-FX guardrails**: add-on model choices no longer offer retired/invalid Claude 4.5 dated IDs; `claude_model` now offers the existing Haiku default plus current Sonnet/Opus options. Anthropic 404/model-not-found errors now trip a 10-minute provider backoff and fall through to OpenAI once instead of spamming each generation. Synthetic ad beds/foley now clamp generated ffmpeg filter parameters into valid ranges (`aphaser.delay <= 5`, `tremolo.f >= 0.1`), fixing the previously failing `luxury_spa`, `mysterious`, and `cafe` paths.
- **Admin control room reads as espresso warm-brown again.** v2.11.0 shipped with the admin Engine Room washed out to taupe after PR #298 raised four shared `tokens.css` values to make listener cards visible. Tokens reverted to Pi-baseline; listener cards keep the brighter values via inline overrides on `.mmr-stage`, `.mmr-np-bar`, `.btn-ghost`, `.mmr-schedule`, `.mmr-dedica`, `.mmr-about-card`.

## 2.11.1

### Added

- **Listener-request identity fields** — Each request now carries `request_id`, `status`, and a reserved `evict_after` field. The rate-limit key moved to a hashed form so no raw IP is stored. `GET /public-listener-requests` exposes `request_id` and `status` for upcoming sidebar UIs; dismiss accepts both the legacy timestamp id and the new `request_id`.

### Changed

- **Listener song downloads use a bounded executor** — `search_ytdlp_metadata` runs in a separate 2-thread pool so listener download tasks cannot contend with the producer on Pi hardware.

### Fixed

- Rate-limit dict pruned before queue-cap check to prevent unbounded growth under sustained rejection waves.
- Trackless shoutout dismiss no longer clears unrelated pinned tracks set by a sibling song request.

## 2.11.0

The big one for the addon: Italian-trending music as the default Jamendo source, the listener page reads correctly at rest on every viewport we test on, the admin panel is fully in Italian, and the source tree is reshaped around seven subpackages.

### Added

- **Jamendo `country` and `order` filters — Italian-trending music as the default Jamendo source.** Two new fields in `[playlist]` (`jamendo_country`, `jamendo_order`) plus matching `JAMENDO_COUNTRY` / `JAMENDO_ORDER` env-var overrides, validated at config load. The addon's default radio.toml now ships `country = "ITA"` + `order = "popularity_week"`, so the Jamendo source surfaces Italian-trending tracks instead of any-country pop. Same engine + different `country=` is the foundation for future country-specific radio "skins".
- **`--ai-purple` semantic token** in `tokens.css`: `#A855F7` reserved for AI-generated segments so operators can distinguish AI content from human/music at a glance.
- **Accessibility (WCAG 2.1 AA)**: `<html lang="it">` on `admin.html`; sr-only labels on song-request inputs in `listener.html`; `aria-hidden` on decorative tricolor; `.sr-only` and `:focus-visible` utilities in `base.css`; `aria-pressed` synced to play button.
- **Content-based asset fingerprinting** for `/static/*.css` and `/static/*.js`: visual fixes invalidate stale browser URLs even without an addon-version bump.
- **Docker CI smoke test** in `addon-build.yml`: a 40-second live test runs against the freshly built amd64 image — hits `/healthz`, asserts `status != 'failing'` and `queue_empty_elapsed_s <= 30`. Catches "server starts but can't produce audio" without a Pi runner.

### Changed

- **`mammamiradio/` subpackaged into seven subpackages** (`core`, `audio`, `playlist`, `hosts`, `home`, `scheduling`, `web`). Public addon entrypoint `mammamiradio.main:app` unchanged. **Migration note** for any out-of-tree script that imports modules directly: flat paths like `mammamiradio.config`, `mammamiradio.streamer`, `mammamiradio.playlist`, etc. no longer resolve; rewrite to subpackage paths (`mammamiradio.core.config`, `mammamiradio.web.streamer`, …).
- **Repo root reduced to four top-level files; everything else moved under `docs/`.** Cleaner top-level navigation for operators reading the source.
- **Admin panel fully Italianized**: trigger card titles, quick-action chips, filter pills, preset names, slider axis labels, search placeholder/button, engine room headings, setup subheadings, toast strings, and `ON AIR` → `IN ONDA` are now Italian. Eliminates the mixed-language whiplash that remained after the panel shell was italianized but content strings stayed in English.
- **Service worker switched to network-first** for `/listen`, CSS, JS, and `sw.js` itself. Was cache-first; UI fixes were getting stuck behind stale caches and the only escape was a hard-refresh + version bump. Now visual fixes reach a returning listener on the next request.
- **Design system refresh**: `tokens.css` / `base.css` / `waveform.js` extracted; `admin.html` migrated to canonical base.css components; `listener.html` rewritten to a five-band radio-station composition; `/dashboard` surface deleted, redirects to `/admin`.
- **Ad creative system extracted** into `ad_creative.py` (closes #161).
- **Dashboard CSS/JS extraction** (PR #203) by [@ashika-rai-n](https://github.com/ashika-rai-n).
- **`docs/architecture.md`** updated to describe Jamendo's new country+order filter behavior and the soft-migration path.

### Fixed

- **CI no longer silently swallows pytest failures.** The coverage ratchet previously only failed when both `returncode != 0` AND no module rows were parsed, which let red tests ride green CI since PR #279. Hard-exit on any non-zero pytest returncode + a dedicated `pytest tests/` step before the coverage ratchet on every PR.
- **Charts source no longer impersonates local files when the charts API returns empty.** When charts returns zero, the chart loader returns empty too instead of mutating in local MP3s under a `kind="charts"` label. Operator dashboard and persisted source kind now tell the truth.
- **Local `music/` is a real startup source.** When `yt-dlp` is disabled and Jamendo isn't configured but MP3s exist in `music/`, they load as a first-class source instead of falling through to demo assets with a misleading warning.
- **Charts `source_id` numerical drift** (`apple_music_it_top_50` → `apple_music_it_top_100`): the URL fetches up to 100 tracks; the persisted label now matches. Transparent migration on read.
- Stale test-assertion path in `tests/scheduling/test_producer_unit.py:573`. The CI swallow had been hiding this failure.
- **Listener cards visible at rest**: surface tokens lifted hard against the espresso body bg so the Schedule, Dedica, and About cards register as panels at a glance, not page bg with a hairline border.
- **Listener page sections silently hidden on Safari and Chrome**: the fixed-position `body::before` glow overlay could be promoted into a compositor layer that occluded scrolled real-viewport content. Removed the fixed overlay; the glow and grain stay in the normal page background. Anchor scroll margins added so sticky navigation cannot hide a target section after a hash jump.
- **Listener now-playing strip never shows "Session stopped"**: idle state used to leak the internal segment label into title and artist slots and broadcast it to the lock screen / Bluetooth / CarPlay via Media Session metadata. Now renders "In pausa" everywhere, with no artist sub-line.
- **Listener page on Safari < 16.2**: `.status-chip` and `.status-dot` use `color-mix()` for their tinted background; older Safari can't parse it and was rendering with no chip background. Added a literal-rgba fallback line above the `color-mix()` declaration.
- **Service worker `/listen` precache restored**: a freshly-installed PWA can now open `/listen` cold-cache offline.
- **Service worker catch-all branch for same-origin GETs**: brand assets (`logo.svg`, future webfonts, future static images) get network-with-cache-fallback handling instead of silently bypassing the cache.
- **Listener mobile** — header overflowed phone viewport, broke vertical scroll, snapped on `In Onda` tap. The pre-Volare phone breakpoint targeted a class name that PR #235 had renamed; never ported. Three layered fixes: phone-breakpoint nav hide, `100svh` for iOS Safari address-bar collapse, `overscroll-behavior-x: contain` to disable horizontal rubber-band. Form inputs bumped to 16 px so iOS Safari stops auto-zooming on focus.
- **Listener brand wordmark — golden "Mi" accent restored** (regression from the Volare class rename).
- **Mobile tap latency and tap-highlight flash on every interactive control**: `-webkit-tap-highlight-color: transparent` on the universal reset and `touch-action: manipulation` on interactive elements. Removes the iOS Safari grey/blue tap-highlight rectangle and the 300 ms double-tap-to-zoom delay. Pinch-zoom on the page itself preserved.
- **Admin brand wordmark — golden "Mi" accent restored.**
- **Admin form fields** no longer trigger iOS Safari auto-zoom on focus (search box and key fields bumped from 13 px to 16 px).
- **Safari banter and news segments cut off after 6–9 seconds**: Safari honoured the Xing/Info VBR duration header embedded by ffmpeg's loudnorm filter and fired `ended` at the declared duration. Two-layer fix: `‑write_xing 0` added to ffmpeg output args; stream-time stripper hardened to handle "free format" frames.
- **Jamendo source-strict downloads**: Jamendo tracks fetch from `direct_url` only — avoids deterministic failures where yt-dlp treated the Jamendo track ID as a YouTube video ID. Cache keys are source-aware so Jamendo and YouTube tracks with the same slug never collide.
- **Producer wakes immediately on session resume**: 1-second `asyncio.sleep` poll replaced with `asyncio.wait_for(resume_event.wait(), timeout=1.0)`. Resume lag drops from worst-case 1s to milliseconds.
- **Silence fallback never queues a silent track**: audio quality circuit breaker recycles the last-known-good music file or drops the segment rather than letting silent audio reach the queue.
- **LRU cache eviction respects the playback queue**: currently-queued norm paths are never deleted mid-stream.
- **LLM prompt injection hardening**: `_sanitize_prompt_data` strips six quote variants and fake role markers (`System:`, `Assistant:`, `Human:`, `User:`, case-insensitive).
- **ICY header injection guard**: station name and genre are CRLF-scrubbed before writing to ICY response headers.
- **`youtube_id` format validation**: `/api/playlist/add-external` validates against `[A-Za-z0-9_-]{11}` before passing to yt-dlp.
- **HA addon version sync**: `ha-addon/mammamiradio/config.yaml` version kept in sync with `pyproject.toml`.
- **Listener brand cleanup**: removed `Napoli` from the hero eyebrow and about-section note. The station fiction is "from Windor to Vergen" via `[sonic_brand].geography`; `Napoli` was leftover seed config.
- **Browser tab title** shortened to just the station name (frequency and city remain in `og:description` for share previews).
- **Host stat typography**: the "I conduttori" stat scaled down so it reads as a labeled stat, not a hero number.
- **Shellcheck warnings resolved** in `ha-addon/mammamiradio/rootfs/run.sh` and `scripts/validate-addon.sh`.

### Removed

- **`/regia` route + `regia.html` template** — the Regia design language already shipped on `/admin` (admin panel title is "Mamma Mi Radio — Regia"); the standalone `/regia` URL served an obsolete prototype duplicate and is gone. Operators land on `/admin` for the control room.
- **Dead `[sonic_brand]` config keys** `short_sting` and `sweeper_probability` — never read by production code. Older operator `radio.toml` files carrying the legacy keys still load cleanly (graceful `pop()`).
- **Dead onboarding/taste-crate copy** in `mammamiradio/playlist/track_rationale.py` and dead taste-mirror helpers in `mammamiradio/hosts/context_cues.py`.
- **567 lines of dead pre-Volare CSS from `listener.css`** — selectors confirmed to have zero matches in the rendered HTML before removal.
- **Dead `probe` parameter from `build_setup_status`** — Spotify-era keyword argument never read or passed.

### Dependencies

- `openai` 2.32.0 → 2.36.0 (script generation; includes `prompt_cache_retention` enum value fix).
- `pydantic-settings` 2.13.1 → 2.14.1.
- Routine: `certifi` 2026.2.25 → 2026.4.22, `click` 8.3.2 → 8.3.3, `idna` 3.11 → 3.13.

**Contributors:** [@ashika-rai-n](https://github.com/ashika-rai-n)

## 2.10.10

Brand engine, listener redesign, mobile host control room, and security hardening.

### Added

- **Brand engine (`[brand]` block in `radio.toml`)**: per-station identity layer (name, frequency, city, hosts, theme tokens — colors and curated fonts) separated from operator engine config. Theme overrides Volare Refined defaults with contrast and font-allowlist guards; bad brand config never blocks station boot.
- **Public listener API** (`/public-status` + `/public-listener-requests`): listener page works on any deploy without 401 risk. `listener.js` no longer polls admin-gated `/status`.
- **OpenGraph social cards** (`/og-card.png`) rendered via Pillow with brand colors, station identity, and current track. Falls back to logo SVG on render failure.
- **Listener template migrated to Jinja2** with capability-conditional rendering: PWA, HA, and AI copy toggle based on `[data-cap=KEY]` attributes reading actual capability flags. PWA install replaced with proper `beforeinstallprompt` flow.
- **`/live` mobile host control room** (admin-gated): phone-optimised operator surface for skip / clip / stop / resume.
- **Accessibility (WCAG 2.1 AA)**: `<html lang="it">` on admin; sr-only labels on song-request inputs; aria-hidden on decorative tricolor; focus-visible utilities; aria-pressed sync on play button.
- **Regression test suite** (`tests/test_qa_regression_guards.py`): 14 automated guards covering LRU eviction protection, prompt sanitization, ICY header injection, youtube_id regex, addon version sync, resume_event presence, and the three-tier last-music-file fallback chain.
- **`--ai-purple` semantic token** for AI-generated segments (used in Regia banter cards and peek-panel type dots).
- **Song-to-host "exclaim" transition style**: hosts open with a short Italian musical exclamation — *Bravo!*, *Magnifico!*, *Che canzone!* — before pivoting to speech (10% probability when song cues are present).

### Fixed

- **Listener tricolor + radio cabinet rendered transparent**: a CSS refactor referenced color tokens (`--flag-green`, `--flag-red`, `--flag-white`, `--terracotta`, `--sage`, `--ink`) that were never declared, so Italian flag elements and the vintage radio illustration silently rendered with `rgba(0,0,0,0)`. Tokens now declared in `tokens.css` with warm copper-brown cabinet (`#6B3E2D`) and tan highlights (`#B47850`); a new test guards every `var(--*)` reference resolves to a defined token.
- **Programme Dur. column always empty**: `<td class="du"></td>` rendered blank for every row. New `fmtDur(item, typeKey)` helper reads `duration_ms` (top-level or under metadata) with sensible per-type fallbacks (music 4:00, banter 0:30, ad 1:00, news 0:20).
- **News flash auto-fires reliably**: removed the `random.random() < 0.3` gate; news now fires deterministically once `songs_since_news >= 6` (over hour-long sessions, the random gate sometimes never fired).
- **Listener cards visible at rest**: bumped `.mmr-about-card` from `--surface` to `--surface-strong`; the four About cards now register against the page bg.
- **Regia progress bar always showed 0%**: `Segment.duration_sec` was never populated in `producer.py`. Now probed via `_ffprobe_duration_sec` at the prewarm path and main convergence point.
- **Listener now-playing strip falls through "0h 0m"**: now reads `status.uptime_sec` from `/public-status` (station-wide on-air time) and shows "In diretta" for the first minute.
- **Admin mobile layout — panel header overlap**: title and subtitle stacked vertically below 768px so they don't collide.
- **Conductor setup fails on machines with broken Python 3.13**: `conductor-setup.sh` prefers `python3.11 → 3.12 → 3.13 → python3` instead of leading with 3.13.

### Refactored

- **Dashboard inline CSS/JS extracted into `/static/`** by [@ashika-rai-n](https://github.com/ashika-rai-n).

**Contributors:** [@ashika-rai-n](https://github.com/ashika-rai-n)

## 2.10.9

Fixes the admin panel regression introduced in v2.10.8 and adds producer bridge metadata improvements.

### Fixed

- **Admin panel broken by v2.10.8 CSP regression**: `script-src 'self'` blocked the entire inline script block in `admin.html`, and a `nonce`-based intermediate attempt blocked the ~40 inline event handlers. Final fix: `script-src 'self' 'unsafe-inline'`, which allows all inline code while still blocking external script sources. The `esc()` wrappers from 2.10.8 remain the load-bearing XSS defense.
- **Producer bridge track metadata**: Resume bridge and idle bridge segments now call `load_track_metadata()` before humanizing the filename, so `title` and `artist` are populated from the sidecar JSON when available instead of falling back to raw filename stems.

### Security

- CSP on `/admin` now uses `script-src 'self' 'unsafe-inline'`, blocking external script injection (the operationally relevant threat) while allowing the inline code the admin panel depends on.


## 2.10.8

Security fix: stored XSS in admin panel Engine Room via HA entity state injection and yt-dlp track title injection.

### Security

- **Stored XSS via HA entity state values**: Five Home Assistant-sourced fields (`mood`, `weather_arc`, `events_summary`, `pending_directive`, `last_event_label`) were rendered via `innerHTML` without escaping. All five are now wrapped with `esc()` before assignment.
- **Stored XSS via yt-dlp track titles**: Maliciously named YouTube videos could inject HTML/JS via `ha_pending_directive`. Same `esc()` wrapper in `admin.html` covers this field. Raw storage is preserved for LLM prompt quality; HTML encoding only happens at the render site.
- **Content-Security-Policy on `/admin`**: The `/admin` route now sets a `Content-Security-Policy` header as defense-in-depth.


## 2.10.7

Reliability and UI-truth fixes across playback fallback, AI fallback, queue labeling, and content hygiene.

### Fixed

- Anthropic auth flood no longer fires under concurrent load: attempt lock serializes the 401 cooldown check across sibling banter/ad/transition calls. First 401 trips the 10-minute backoff; concurrent callers see the block and use the OpenAI fallback.
- TTS voice validation at config load: invalid voice IDs (e.g. `onyx` on an edge-tts host, typos in edge voice IDs) are now logged once and replaced with `it-IT-DiegoNeural` before any synthesis attempt. Runtime TTS failures are memoized per-session so a flaky voice doesn't re-attempt per segment. `/api/capabilities` gains a `tts_degraded` flag when any voice was substituted.
- Queue starvation rescue: when the queue is empty for 30s and no canned clip or norm-cache is available, playback falls back to a random MP3 from `demo_assets/music/` instead of looping silently. Bundled demo tracks in `demo_assets/music/` (named `Artist - Title.mp3`) are preferred over placeholder tones at startup and when demo source is explicitly selected.
- `/readyz` honors stopped state: a stopped session now returns `503 stopped` even when the queue is populated, so HA Supervisor no longer routes listeners to a deliberately paused station.
- Chart ingest filters non-music entries: Apple Music's Italian chart occasionally surfaces podcasts, BBC comedy, and audiobooks that played as dead-eye audio and broke the radio illusion. Narrow content filter drops obvious non-music before it reaches the queue.
- Rejected downloads purge + denylist: `validate_download` failures now purge the cache file and add the cache key to a per-session denylist. Producer, prefetch, and prewarm short-circuit on denylisted keys so the same broken track cannot loop forever.
- Queue rows no longer render bare segment types for BANTER, AD, STATION ID, SWEEPER, or TIME CHECK. BANTER rows now show the participating hosts (`Marco & Luca`), canned clips show `Pre-recorded banter`, AD breaks show `Ad: Barella Pasta +2 more`, station IDs show `Station ID`, sweepers show `Station sweeper`, and time checks show the spoken time. News-flash and error-recovery segments pick up their own labels. Admin queue render also hardened to hide a label that equals the bare type, so a future producer path that forgets to set a title can't re-introduce the `BANTER banter` row.
- Dashboard "AI" pipeline pill no longer lies when Anthropic is auth-suspended. Dashboard now mirrors the three-state logic `admin.html` already had: a configured-but-suspended Anthropic shows `AI Fallback` instead of `AI`.


## 2.10.6

UI truth and playback safety fixes, plus a normalizer concat duration guard.

### Fixed

- Normalizer `concat_files` now probes input durations with `ffprobe` and logs a WARNING when the concatenated output is shorter than expected. Fail-open when ffprobe is unavailable.
- Stopped state actually stops: Stop freezes dashboard animations, pauses the elapsed-time counter, and disables producer buttons.
- Admin panel distinguishes *connected*, *not configured*, and *suspended* Anthropic states instead of flashing "connected" while 401s are failing every call.
- Scheduler reason strings (`cooldown: 45s`, `banter_due_in=3`) no longer leak to listener-facing up-next rows.
- Norm-cache rescue path no longer shows raw filenames as titles. Sidecar metadata used when present; otherwise humanized (`norm_busted.mp3` → `Busted`).


## 2.10.5

### Changed

- Admin UI redesign: two-column control room layout with warm sidebar, compact now-playing card, waveform/progress, 2×2 quick-controls grid (Next / Pause / Shuffle / Banter), unified "On Air" programme list with NOW badge, and filter pills (All / Music / Banter / Ads). Pacing, Hosts, Station Log, Engine Room collapsed into accordions.
- Token cost counter regression fix: static element no longer shadows the dynamic Engine Room cost display.
- Stop/Resume 2×2 grid fix: no more visual gap when toggling Stop↔Resume.
- Accessibility polish: keyboard `:focus-visible` ring on buttons/inputs, 44px touch-target floor on controls (36px chips, 32px pills), base font-size raised to 16px (WCAG), queue title raised to 14px, HA slider labels raised to 9px/32% opacity.
- Quick Action labels renamed to action-oriented verbs (trim / force).
- Dead `btn-skip` CSS removed; hardcoded hover hex replaced with `color-mix` on the accent token.


## 2.10.4

### Security

- CI action SHA-pinned: `dependabot/fetch-metadata` now pinned to commit SHA (supply chain hardening).
- Added `.gitleaks.toml` for secret scanning (Anthropic API keys, HA tokens).
- Raised `yt-dlp` minimum version to `>=2026.2.21` (patches GHSA-g3gw-q23r-pgqm).


## 2.10.3

### Added
- `POST /api/hot-reload`: reloads `mammamiradio.scriptwriter` in place without interrupting the stream.
- Quick Actions chips in admin UI: one-tap controls for Less banter / More chaos / Too many ads / Hot reload.

### Changed
- Producer now imports `mammamiradio.scriptwriter` as a module reference so hot reload applies at every call site.
- `_has_script_llm` renamed to `has_script_llm` for the new module-reference import pattern.
- HA add-on `radio.toml` now ships byte-for-byte identical to the root `radio.toml`. The Pi-specific pacing overrides (`songs_between_banter=3`, `ad_spots_per_break=1`, `lookahead_segments=2`) are removed; CI, the local validator, and `tests/test_addon_radio_sync.py` all enforce strict `cmp -s`.
- Broadcast EQ restored to the 3-filter chain.
- Auto-resume on listener connect removed. A deliberate `/api/stop` stays paused across restarts until explicit `/api/resume`.


## 2.10.2

### Fixed
- **Critical**: Silence after HA restart following a deliberate stop. The `session_stopped.flag` survived restarts — any listener connecting after a restart heard nothing until a manual admin resume. Fixed: listener connecting now auto-clears the stopped state.
- **Critical**: 55-75 second silence on resume and idle wakeup on Pi. No canned banter clips ship in the container, so the bridge had no audio to play. Both resume and idle bridges now fall back to the first pre-normalized track in cache, available immediately without FFmpeg.
- **Critical**: FFmpeg 8.1 SIGABRT during normalization on Pi aarch64. Three equalizer filters + loudnorm trigger a `calc_energy` assertion crash (`psymodel.c:576`). Third equalizer removed. Every track was silently failing to normalize, leaving the queue permanently empty.
- Stream player no longer requires a page reload after admin resume. Auto-reconnects within 300ms of the status flip.


## 2.10.1

### Fixed
- **Critical**: Docker images for 2.10.0 were never built. The CI validate job used a strict byte-comparison of `radio.toml` files, but the HA add-on intentionally carries Pi/HA Green pacing overrides. Validate always failed, blocking image builds — HA Supervisor got `[404] manifest unknown` on every update attempt.
- **Pi pacing tuning discarded**: The build step was copying the root `radio.toml` (higher CPU load defaults) over the HA-specific one, shipping the wrong pacing values baked into the image.
