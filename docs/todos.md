# TODOS

## Architecture

### "Skins" — country as multi-tenancy axis

- **Source:** /office-hours seed conversation 2026-05-03 + PR #283 (Jamendo country/order filters)
- **Frame:** mammamiradio engine is country-agnostic; the brand is country-specific. Same engine + different `country=` + different brand assets (logo, hosts' accent, ad brands, station name, ad voiceovers, sweepers) = `mammamiradio`/ITA, `fakefranziradio`/DEU, `radio-saudade`/BRA, etc. PR #283 made `country=` a real filter; the full skins layer makes the rest of the brand swap with it.
- **Scope:** per-skin radio.toml overlays (or a `[skins.de]` table), per-skin host personas keyed by language, per-skin `[[ads.brands]]` lists, per-skin sonic_brand sweepers, a `STATION_SKIN=de` env var that loads the right overlay at boot. Probably a deployment-mode rather than runtime swap.
- **Open questions:** does each skin run as a separate addon/Docker image, or one image with a runtime skin selector? How does the `fakeitaliradio` MemPalace wing relate (existing thinking?)? What happens to listener-saved preferences across skins? Does the addon name change per skin, or just the `STATION_NAME`?
- **Effort estimate:** weeks if done right, since it touches brand, hosts, ads, sweepers, identity. Worth a real /office-hours pass before any code lands.
- **Gate:** /office-hours on the skins architecture before any implementation. PR #283's CHANGELOG flagged country as the foundation; implementation waits.
- **Trigger phrase:** "design the skins layer" / "country as multi-tenancy" / "spawn fakefranziradio"

## Listener UX

### Dialer revival (listener.js first-class port)

- **Source:** pre-PR#218 `static/script.js` (see `.context/research/dialer-port-blueprint.md`)
- **Scope:** ~220 LOC JS + wiring, CSS class rename, `--needle-x` token
- **Effort:** 4–6 hours
- **Gate:** design sprint (`/office-hours` on dial UX first) — do not build speculatively
- **Trigger phrase:** "bring the dial alive" / "implement the dialer"

### P2 — Super-italian-aware listener stopped-state regression guard

**Priority:** P2
**Source:** scope-parked from triage of stale branch `fix/listener-polish` on 2026-05-10
`tests/web/test_ui_control_contracts.py` (`TestStoppedStateQuietsTheUI`) — write a fresh regression test asserting the stopped-state surfaces (`renderNowPlayingStrip` + `updateMediaSession` in `mammamiradio/web/static/listener.js`) route through the super-italian copy bag (`_t('np_paused', ...)`) and never leak `Session stopped` / `STOPPED` / hardcoded city/frequency values to lock-screen / Bluetooth / CarPlay. The original guard on `fix/listener-polish` predates super-italian (PR #310) and asserts the literal `'In pausa'`, which would silently break the i18n toggle if cherry-picked as-is. Write against current code, do not resurrect the stale branch's version.

## Infrastructure

**Priority:** P2
**Source:** /research on 2026-04-15


### Listener public API migration (full)
**Completed:** 2026-04-28 (florianhorner/show-p1-tasks)
`listener.js` already uses `/public-status` + `/public-listener-requests` exclusively. Capabilities are embedded in the `/public-status` response under `status.capabilities`. No admin-gated endpoints remain in listener.js. Migration was done as part of the fix-radio-plan / PR-F listener rewrite.

### Regia.html + admin Flag Track field contract fix
**Completed:** 2026-04-27 (florianhorner/list-p1s)
Added `duration_sec` to `now_streaming` payload (models.py). Regia elapsed computed client-side from `ns.started`; duration reads `ns.duration_sec`. Flag Track key path fixed: `_st?.now?.metadata` → `_st?.now_streaming?.metadata`.

### Populate `Segment.duration_sec` from normalizer output
**Completed:** 2026-04-27 (florianhorner/p1-todo-review)
Imported `_ffprobe_duration_sec` in producer.py. Added probe in prewarm path (before `queue.put`) and at the main convergence point (`if segment:` block before `_queue_segment`). Covers all segment types in one place. Test timing mock added to `test_drain_guard_inserts_canned_clip_on_queue_drain`.

### Host name selector hardening
**Completed:** 2026-05-08 (v2.11.0 — #284)
The two `` `[data-h="${n}"]` `` template literals in `updHost()` and `applyHostPreset()` now wrap `n` with `CSS.escape()`. Host names containing CSS special characters (quotes, brackets, dots) no longer cause silent no-match — the host block is found and the personality sliders work for operators with unconventional host names. **Affected files:** `mammamiradio/web/templates/admin.html`.

### Docker container smoke test in CI
**Completed:** 2026-05-08 (v2.11.0 — #284)
After both amd64 and aarch64 images build, the new `smoke` job in `addon-build.yml` pulls the amd64 image and runs a 40-second live test: hits `/healthz`, asserts `status != 'failing'` and `queue_empty_elapsed_s <= 30`. Catches "server starts but can't produce audio" — the exact production failure class — without needing a Pi runner. **Files:** `.github/workflows/addon-build.yml`.

### Add-on/upstream release consolidation
**Priority:** P2
**Source:** 2026-05-04 listener viewport debugging

Static/listener fixes can land in upstream `main` and `CHANGELOG.md [Unreleased]`
while HA Green users still run the previous add-on version until
`ha-addon/mammamiradio/config.yaml` is bumped and GHCR images are rebuilt.
Consolidate the release flow so user-visible HA fixes cannot remain upstream-only
without an explicit add-on delivery decision.

**Affected files:** release scripts, `addon-build.yml`, `pyproject.toml`,
`ha-addon/mammamiradio/config.yaml`, both changelogs.

## Admin UI — Regia (Time-Horizon Stack)

**Source:** `/research` + `/design-shotgun` on 2026-04-21 → approved direction is Concept A modified (Variant E2 warmth iteration).
**IA reference:** `.context/attachments/Radio Control-Room IA  Architecture Comparison, Recommendation & MVP Build Order.md`
**Design reference:** `~/.gstack/projects/florianhorner-mammamiradio/designs/admin-regia-concepts-20260421/variant-E2.png` + `approved.json`
**Status (2026-05-03):** the Regia *design language* landed on `/admin` directly — admin panel title is "Mamma Mi Radio — Regia". The standalone `/regia` route + `regia.html` prototype was removed; the IA work now drives admin.html iteratively.

### P1 — Wire Screen 1 ON AIR + Build Screen 2 QUEUE
**Completed:** 2026-04-28 (florianhorner/show-p1-tasks)
- Panic overlay: replaced browser `confirm()` with in-page CSS modal (no `alert` API, Esc to dismiss, Annulla/Taglia Ora buttons). Calls existing `/api/panic`. Stream stays live.
- Pause button: dropped. ICY stream can't pause without dropping listeners — pause = stop from the listener's perspective. Removed from scope permanently.
- AI Approval badge: deferred to P2. Requires pending-segment queue with APPROVE/REJECT state in backend — separate PR scope.
- Screen 2 QUEUE: tab switching wired (all 5 tabs, JS `switchTab(n)`). Full queue list rendered from `queued_segments` (reusing `.peek-item` CSS). Break-structure card (next non-music in `upcoming[]`). Skip current + Purge all controls. Inline search against `/api/search`. Remove-from-queue via new `POST /api/queue/remove` endpoint (drain+rebuild asyncio.Queue). Drag-to-reorder deferred to P2 (asyncio.Queue is not random-access).

### P2 — AI Approval badge (Regia Screen 1)
Requires a pending-segment queue in `StationState` with APPROVE/REJECT state. Producer writes to pending queue before approved segments reach the asyncio.Queue. New endpoints: `GET /api/pending-segments`, `POST /api/approve-segment`, `POST /api/reject-segment`. Wire the badge into `admin.html` when the backend is built.

### P2 — Drag-to-reorder (Regia Screen 2)
`asyncio.Queue` is not random-access. To support reorder: drain + rebuild under concurrency, or replace with a custom queue structure that supports `insert`. Deferred because the drain+rebuild approach has a contention window and needs careful testing.

### P2 — Build Screen 3 REVIEW, Screen 4 PROGRAMME, Screen 5 MOTORE
Phase 2 per IA doc. Screen 3 is AI content approval (banter + ad preview with audio + APPROVE/REJECT/EDIT). Screen 4 is format-clock + pacing. Screen 5 is the current admin Engine Room — API cost counter, capability flags, model info, logs.

### P3 — Italianize remaining UI copy
The prototype uses Italian labels (CODA, REVISIONE, PALINSESTO, MOTORE, PANICO). Once Screens 2–5 are built, audit all existing admin.html strings and normalize to the same voice.

### P2 — Italianize admin.html panel contents (Approach B)
**Completed:** 2026-05-08 (v2.11.0 — #284)
All admin panel contents now read in Italian: trigger card titles (`Aggiungi banter / Forza pubblicità / Notizia flash / Caos in arrivo`), quick-action chips (`Taglia banter / Taglia pubblicità / Ricarica live / Svuota coda / Segnala traccia`), filter pills (`Tutto / Musica / Pubblicità`), Conduttori preset names (`EQUILIBRATO / CALMO`) and slider axis labels (`Energia / Caos / Calore / Verbosità`), search placeholder + button (`Cerca musica / Cerca`), engine room headings, onboarding subheadings, toast strings, and the `ON AIR → IN ONDA` pill. API endpoint strings, JS variable names, CSS class names, and `data-` identifiers are unchanged. Eliminates the mixed-language whiplash that remained after PR #248 (Approach A) italianized only the panel shell.

## Process & Discipline

**Source:** /office-hours + /plan-eng-review on 2026-05-03 — CICD freeze reflection.

### P1 — Resume stabilization-log measurement protocol
`docs/stabilization-log.md` row for 2026-W16 still says `_tbd Sun 2026-04-19_`. Day 8 Go/No-Go decision was due 2026-04-24 and has not been written. The cooldown-alone experiment has been operating unmeasured for ~17 days. Qualitative read says cooldown is working (no v-tag panic-cycle in commits since 2026-04-17) but cannot be claimed without filling in the log. **Source:** research surfaced during 2026-05-03 /office-hours.

### P2 — Watch for scope-creep pattern recurrence
Re-audit creep frequency at PR #292 (10 PRs after the 2026-05-03 audit which measured 2/10). If the rate rises >4/10, reactivate the scope-guard mechanism path; design preserved at `~/.gstack/projects/florianhorner-mammamiradio/florianhorner-cicd-freeze-reflection-design-20260503.md`. **Source:** scope-discipline rule landed 2026-05-03; audit revisit gate.

### P3 — Generalize "designated observer role" pattern
Release-manager (lyon) is one instance; document the meta-rule for when to designate a new role across parallel Conductor worktrees. Already partially captured in agent memory at `feedback_designated_observer_pattern.md`. **Source:** /office-hours session insight 2026-05-03.

### P3 — Codify "audit-before-build" as a pre-build gate
The 3-agent PR audit on 2026-05-03 took ~45s and flipped a 2-3 day build into a 5-line CLAUDE.md rule. Worth codifying as a standard step when a proposed mechanism's frequency justification is unmeasured. **Source:** /plan-eng-review reversed the /office-hours recommendation based on agent-swarm data.

### P3 — Bump verify-claims pin once `gh-workflows` v1.2 ships
**Priority:** P3
**Source:** scope-parked from `florianhorner/commit-standards-bootstrap` on 2026-05-08
`.github/workflows/verify-claims-call.yml` pins `florianhorner/gh-workflows/.github/workflows/verify-claims.yml@v1.1`. Upstream PR `florianhorner/gh-workflows#3` fixes the `parseProofLines` self-reference-tag bug that caused PR #302's `runtime: proof/...txt` line to fail validation; once the fix lands and is tagged `v1.2`, bump the pin here so future PRs can use file-path and gist-URL artifacts directly instead of routing every artifact through a CI run URL.

## Scriptwriter Anthropic state

**Source:** /simplify scope-park on `fix/anthropic-model-and-audio-fx-guardrails` 2026-05-10.

### P2 — Collapse Anthropic block state into a typed value object
`mammamiradio/hosts/scriptwriter.py:51` — `_anthropic_auth_blocked_key`, `_anthropic_auth_blocked_until`, and `_anthropic_blocked_reason` are three module-globals that must always reset together. A small dataclass with a `.clear()` method would prevent future drift across the four reset/write sites (module init, `reset_provider_backoff`, success-path clear, `_trip_anthropic_circuit_and_fallback`).

### P2 — Use SDK-typed exceptions in Anthropic error classification
`mammamiradio/hosts/scriptwriter.py:233,242` — `_is_anthropic_auth_error` and `_is_anthropic_nonretryable_provider_error` sniff `type(exc).__name__` and `str(exc)` substrings. The Anthropic SDK exposes typed exceptions (`anthropic.AuthenticationError`, `anthropic.NotFoundError`). Switch to `isinstance` checks (with `hasattr` guard for SDK-version safety) and keep string match as a fallback.

## Completed

### Mark addon as experimental in HACS
**Completed:** 2026-04-21
Added `stage: experimental` to `ha-addon/mammamiradio/config.yaml` + runbook note. HACS will show the orange Experimental badge next to the addon in the store. Revisit at v1.0 cut.

### Silence health gate + /healthz/readyz 503 + norm-cache rescue + force-resume
**Completed:** 2026-04-16 (florianhorner/list-p1-items-v1)
Playback loop tracks `queue_empty_since`, tries canned clips on timeout, then rescues from `cache/norm_*.mp3` at 30s, then sets `state.force_next = BANTER` at 60s. `/healthz` and `/readyz` both return 503 when queue-empty > 30s with active listeners so HA Supervisor auto-restarts. Tests cover all three scenarios including post-restart `session_stopped.flag` clearing.

### aarch64 CI smoke job with static ffmpeg 8.x
**Completed:** 2026-04-16 (florianhorner/list-p1-items-v1)
`.github/workflows/pi-smoke.yml` runs on `ubuntu-24.04-arm` with John Van Sickle's static ffmpeg 8.x build. Runs `test_normalizer_unit.py`, `test_normalizer_extended.py`, and `test_normalizer_real_ffmpeg.py`.

### scripts/pre-release-check.sh
**Completed:** v2.10.4 (#191)
Pre-version-bump sanity script, wired as `make pre-release`.

### 3-scenario invariant test rule in CLAUDE.md
**Completed:** v2.10.2 (2026-04-15)
Added "Audio delivery test coverage rule" section to CLAUDE.md under Review Discipline. Three scenarios required for all audio-delivery PRs: Normal, Empty fallback (no canned clips/norm cache), Post-restart (flag files, session_stopped state).

### CI guard for pre-release-check.sh
**Completed:** 2026-04-27 (#256)
Version sync check conditional on `pyproject.toml`/`ha-addon/config.yaml` diff; release invariants (FFmpeg eq count, canned-clip mock, session_stopped test) run unconditionally on every PR. Both wired into `quality.yml`.


## Admin endpoints — config-write race protection

### Debounce or throttle `POST /api/setup/provider-check`

**Priority:** P2
**Source:** scope-parked from florianhorner/chore/check-hist-pause-live on 2026-05-13
`mammamiradio/core/provider_checks.py` — each call fires up to three live HTTP probes (Anthropic + OpenAI chat + OpenAI TTS) with a 12 s timeout each. The endpoint has admin auth but no rate limiting; a rapid-click operator could launch overlapping probe sets. Add a short debounce or a per-server in-flight lock before the next caller triggers a second outbound probe while the first is still awaiting a timeout.



### Apply `_super_italian_lock` pattern to `/api/credentials`

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/translation-immersion on 2026-05-08
`mammamiradio/web/streamer.py` — `/api/super-italian` serializes config-attr + `os.environ` + `.env` + `/data/options.json` writes under `_super_italian_lock` to avoid same-process race during the `await executor` window. `/api/credentials` and `/api/setup/save-keys` do the same read-modify-write of `.env` without serialization. Low blast radius (admin-only, single operator), but the pattern should be unified once a third caller appears.

### Unify `_save_addon_options` + `_save_super_italian_addon_options`

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/translation-immersion on 2026-05-08
`mammamiradio/web/streamer.py:1260` and `:1580` — two helpers share the read-modify-write skeleton for `/data/options.json`. They differ in value type (str vs bool) and key handling (key_map vs direct). Acceptable as-is at 2 callers; refactor to a single `_save_addon_options(updates: dict[str, Any])` accepting a typed patch when a third caller lands.

### Time-aware scheduler refactor (gates ETA buckets)

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-10
`mammamiradio/scheduling/scheduler.py:42` — Refactor banter triggering from `pacing.songs_between_banter` counts to time-aware scheduling so honest ETA display, urgent dedication preemption, and future `dedication-becomes-track` behavior can be implemented coherently.

### Moderation static-name blocklist (v2.11.1 hardening)

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-10
`mammamiradio/web/listener_requests.py:184` — Add a config-driven static blocked-name list in `radio.toml` `[moderation.blocked_names]` (likely a new `mammamiradio/hosts/moderation.py`) as a second-line safeguard at the listener-request validation gate against LLM false-positive approvals involving real people.

### Listener-request rate limiter buckets all `request.client is None` traffic into one slot

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (pre-existing, flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py:190` — `ip = request.client.host if request.client else "unknown"` means every caller without a `request.client` (and every caller through HA Supervisor ingress, where `request.client` is the loopback) shares one rate-limit bucket. Real listener IPs need to be read from `X-Forwarded-For` / `X-Real-IP` behind a trusted proxy boundary. Trust-boundary decision required — not a one-line patch.

### Listener-request `request_id` doubles as both public sidebar token and admin dismiss key

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py:129` — the public `/public-listener-requests` feed exposes `request_id`, which is also the canonical id `/api/listener-requests/dismiss` accepts. Dismiss is admin-auth gated, so this only matters under stolen admin credentials, but the design conflates the listener's "track my own card" token with the admin mutation handle. Phase 3 should consider splitting into `public_token` + `request_id`.

### Coverage gaps in `_download_listener_song` failure paths

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship coverage audit on PR #325)
`mammamiradio/web/listener_requests.py:281-282` and `:271-274` — no tests cover (a) `search_ytdlp_metadata` raising an exception (should set `song_error=True`, not silently drop) or (b) the `playlist_revision` mismatch guard that drops a downloaded track when the source switched mid-download. Add `test_download_listener_song_search_exception_marks_error` and `test_download_listener_song_revision_mismatch_drops_track` in `tests/web/test_streamer_routes_extended.py`.

### Listener song downloads share the default ThreadPoolExecutor with producer

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py:266` — `loop.run_in_executor(None, search_ytdlp_metadata, ...)` uses the default executor (min(32, cpu+4) threads on Pi 4 = 8). Multiple concurrent listener song downloads can exhaust the executor and starve the producer's audio prefetch tasks, causing a buffer underrun and breaking the illusion. Fix: use a separate bounded executor (`max_workers=2`) for listener downloads so producer tasks are never blocked.

### `_download_listener_song` leaves request stuck on `asyncio.CancelledError`

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py:296` — the `except Exception` handler does not catch `asyncio.CancelledError` (BaseException subclass in Python 3.8+). On app shutdown mid-download, the request stays stuck as `song_found=False, song_error=False` ("still downloading") for ~2 banter cycles until scriptwriter.py's `banter_cycles_missed` counter expires it. Explicit: re-raise `CancelledError` after setting `song_error=True` and removing from `pending_requests`.

### Pinned-track assignment fires only when request is still queue head at download completion

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py:292` — `if state.pending_requests[0] is req` means the pin only fires if no newer request arrived during the yt-dlp download. If a second shoutout is submitted while a song is downloading, the song-request sinks to position 1, the pin is skipped, and the downloaded track plays at an unannounced time without a dedica. Phase 3 design: producer should look up the request by `request_id` and schedule accordingly rather than relying on position.

### Dedication metrics admin dashboard surface

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-10
`mammamiradio/web/templates/admin.html:2216` — Surface dedication KPIs such as acceptance rate, median time-to-air, and miss rate in the Engine Room admin block, sourced from the planned `cache/dedication_events.jsonl` metrics log via `scripts/dedication-metrics.sh` (both also pending).

### Changelog lint: add digit-phase and Track-letter patterns

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11
`scripts/check-changelog-lint.sh` — Current `PATTERNS` array catches `Phase A` (letter-suffix) but not `Phase 1` (digit-suffix) or `Track B` (workstream labels). Add `\bPhase [0-9]+\b` and `\bTrack [A-Z]\b` so the CI gate enforces the full policy from CLAUDE.md's Changelog editorial boundary section.


### System prompt cache key — missing geography and voice fields

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/host-intro-variety on 2026-05-14
`mammamiradio/hosts/scriptwriter.py:615` — `_get_system_prompt()` cache key includes `h.name`, `h.style`, `h.personality.to_dict()`, and `super_italian_mode` but omits `h.voice`, `config.sonic_brand.geography`, `config.station.name`, and `config.station.theme`. If any of those change at runtime without also changing host name/style/personality, the stale system prompt is served silently. In normal operation this is low-risk because config is loaded once at startup and hot-reload calls `importlib.reload()` which clears module globals. Becomes relevant if a settings endpoint ever allows in-memory config mutation.
