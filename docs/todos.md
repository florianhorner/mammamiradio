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

### Listener-visible Chaos Mode signal

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/full-chaos-mode-spec on 2026-05-16
`mammamiradio/web/templates/listener.html` — Explore a listener-facing chaos-live signal and dramatic visual treatment for chaos mode (melt / flames / glitch) in a separate design pass. The operator panel keeps the readable `CHAOS LIVE` state only.

### Document MAMMAMIRADIO_CHAOS_MODE env var

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/full-chaos-mode-spec on 2026-05-16
`CLAUDE.md` (## Environment) and `README.md` — add `MAMMAMIRADIO_CHAOS_MODE` to the `MAMMAMIRADIO_*` env-var reference lists for consistency with `MAMMAMIRADIO_SUPER_ITALIAN`. Doc-sync rule is already satisfied (architecture.md + CHANGELOG cover it); this is a reference-list consistency fix.

### Chaos control — design-system tokens and keyboard accessibility

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/full-chaos-mode-spec on 2026-05-16 (CodeRabbit findings)
`mammamiradio/web/templates/admin.html` (~lines 900-937, 923-929, 1305-1307) — Three issues parked from chaos PR review: (1) `.chaos-control` CSS uses hard-coded rgba()/px literals instead of design-system tokens from `docs/design/system.md`; replace with `--lancia2`, `--cream`, `--muted`, tokenized border-radius, gap/padding. (2) The chaos toggle `input#chaosToggle` lacks an accessible name; add `aria-label="Toggle Chaos Mode"` or associate a visible `<label>`. (3) The custom switch hides the native input and has no `focus-visible` indicator; add `.chaos-switch input:focus-visible + .chaos-slider` outline/box-shadow rule.

### Dialer revival (listener.js first-class port)

- **Source:** pre-PR#218 `static/script.js` (see `.context/research/dialer-port-blueprint.md`)
- **Scope:** ~220 LOC JS + wiring, CSS class rename, `--needle-x` token
- **Effort:** 4–6 hours
- **Gate:** design sprint (`/office-hours` on dial UX first) — do not build speculatively
- **Trigger phrase:** "bring the dial alive" / "implement the dialer"

### P2 — Super-italian-aware listener stopped-state regression guard

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P2
**Source:** scope-parked from triage of stale branch `fix/listener-polish` on 2026-05-10
`tests/web/test_ui_control_contracts.py` (`TestStoppedStateQuietsTheUI`) now asserts both stopped-state surfaces (`renderNowPlayingStrip` + `updateMediaSession` in `mammamiradio/web/static/listener.js`) route through `_t('np_paused', ...)` and do not leak `Session stopped` / `STOPPED` / hardcoded city/frequency values to lock-screen / Bluetooth / CarPlay.

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

### P2 — Group pydantic + pydantic-core in dependabot config
**Priority:** P2
**Source:** release-manager session 2026-05-13 — `main` broke when a PR bumping pydantic to 2.13.4 merged without a companion pydantic-core 2.46.4 bump. `submit-pypi` (pip-compile) failed on every subsequent deps PR (#320, #322) until #321 manually unblocked the cascade.
Add a `groups` block to `.github/dependabot.yml` so pydantic + pydantic-core ship in a single PR going forward. Same grouping logic applies to any other tightly-coupled pair (e.g. anthropic + httpx, fastapi + starlette). Prevents the "main has a broken pip-compile resolution between dependabot PRs" failure mode.

### P2 — Resolve `enable-automerge` workflow gap
**Priority:** P2
**Source:** release-manager session 2026-05-13 — every Dependabot PR (#320, #321, #322) fails its `enable-automerge` check with `GitHub Actions is not permitted to approve pull requests` (org policy). Forces a human approve + manual merge on every routine deps bump.
Two paths: (a) flip the org setting to allow GH Actions to approve PRs (security tradeoff worth weighing — if a malicious PR slips through, automerge would land it), or (b) drop the auto-approve step from `dependabot-automerge.yml` entirely and accept that humans approve all deps PRs. Path (b) is the safer default given this is a single-author repo with hands-on release management. Either decision unblocks the perma-failing check.

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

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P2
**Source:** scope-parked from florianhorner/chore/check-hist-pause-live on 2026-05-13
`POST /api/setup/provider-check` now shares one in-flight `check_provider_keys()` task across rapid callers and caches the completed result for a short debounce window. Regression: `test_setup_provider_check_shares_in_flight_probe`.



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

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (pre-existing, flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py` now resolves the rate-limit identity from `X-Forwarded-For` / `X-Real-IP` only when the direct client is inside the trusted local/HA proxy boundary; direct public callers cannot spoof forwarded headers. Regressions cover trusted HA ingress and untrusted direct clients.

### Listener-request `request_id` doubles as both public sidebar token and admin dismiss key

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`POST /api/listener-request` now creates both private `request_id` and listener-safe `public_token`; `/public-listener-requests` exposes `public_token` and no longer leaks the admin dismiss handle. Admin dismiss keeps accepting `request_id` and legacy timestamp ids.

### Coverage gaps in `_download_listener_song` failure paths

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship coverage audit on PR #325)
`tests/web/test_streamer_routes_extended.py` now covers `search_ytdlp_metadata` exceptions setting `song_error=True`; the playlist-revision mismatch drop guard was already covered by `test_download_listener_song_drops_track_on_revision_change`.

### Listener song downloads share the default ThreadPoolExecutor with producer

**Completed:** 2026-05-11 (v2.11.1)
**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py` uses a dedicated bounded `listener-dl` executor (`max_workers=2`) for `search_ytdlp_metadata`, keeping producer prefetch work off the listener download pool.

### `_download_listener_song` leaves request stuck on `asyncio.CancelledError`

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`_download_listener_song()` now catches `asyncio.CancelledError`, marks the request as errored, removes it from `pending_requests`, logs the cancellation, and re-raises. Regression: `test_download_listener_song_cancelled_marks_error_and_removes_pending`.

### Pinned-track assignment fires only when request is still queue head at download completion

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11 (flagged by pre-ship adversarial review on PR #325)
`mammamiradio/web/listener_requests.py:292` — `if state.pending_requests[0] is req` means the pin only fires if no newer request arrived during the yt-dlp download. If a second shoutout is submitted while a song is downloading, the song-request sinks to position 1, the pin is skipped, and the downloaded track plays at an unannounced time without a dedica. Phase 3 design: producer should look up the request by `request_id` and schedule accordingly rather than relying on position.

### Dedication metrics admin dashboard surface

**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-10
`mammamiradio/web/templates/admin.html:2216` — Surface dedication KPIs such as acceptance rate, median time-to-air, and miss rate in the Engine Room admin block, sourced from the planned `cache/dedication_events.jsonl` metrics log via `scripts/dedication-metrics.sh` (both also pending).

### Changelog lint: add digit-phase and Track-letter patterns

**Completed:** 2026-05-16 (florianhorner/chore/todos)
**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-11
`scripts/check-changelog-lint.sh` now rejects `\bPhase [0-9]+\b` and `\bTrack [A-Z]\b`; public changelog entries were cleaned to avoid the newly covered internal wording. Regression: `test_check_changelog_lint_rejects_digit_phase_and_track_labels`.

### Validate HA Supervisor XFF proxy behavior and harden rate-limit identity resolution

**Priority:** P2
**Source:** scope-parked from florianhorner/chore/todos on 2026-05-16
`mammamiradio/web/listener_requests.py:103` — `_client_ip_for_rate_limit` reads the leftmost valid IP from `X-Forwarded-For`, which is the client-controlled entry if a trusted-network process injects a spoofed header before the real HA Supervisor proxy appends. Exploiting this requires HA network access and only results in a 30 s rate-limit disruption for the targeted listener. Before switching to rightmost-non-trusted, verify whether HA Supervisor ingress proxy appends or overwrites the header, and add an integration test against a real HA ingress container.


### System prompt cache key — missing geography and voice fields

**Priority:** P2
**Source:** scope-parked from florianhorner/feat/host-intro-variety on 2026-05-14
`mammamiradio/hosts/scriptwriter.py:615` — `_get_system_prompt()` cache key includes `h.name`, `h.style`, `h.personality.to_dict()`, and `super_italian_mode` but omits `h.voice`, `config.sonic_brand.geography`, `config.station.name`, and `config.station.theme`. If any of those change at runtime without also changing host name/style/personality, the stale system prompt is served silently. In normal operation this is low-risk because config is loaded once at startup and hot-reload calls `importlib.reload()` which clears module globals. Becomes relevant if a settings endpoint ever allows in-memory config mutation.

## Admin UI — Design System

### Touch targets below 44px: icon buttons and programme actions

**Priority:** P2
**Source:** scope-parked from florianhorner/chore/todos on 2026-05-16
`admin.html:787, 838, 1318` — `.btn-icon` topbar icon buttons (36px), programme action buttons (26px), and checkbox (16px) miss the 44px touch target minimum; the sidebar nav link was fixed in this branch but these three element types remain.

### Ad-hoc pill systems — migrate to canonical status chips

**Priority:** P2
**Source:** scope-parked from florianhorner/chore/todos on 2026-05-16
`admin.html:574, 697, 724, 849` — `.chip`, `.now-type-pill`, `.seg-pill`, `.lr-pill` exist alongside the canonical `.status-chip` / `.status-inline` system in `base.css`; migrate these ad-hoc styles to the canonical classes so colorblind users get shape + color on all status surfaces.

### Status chip label contrast below WCAG AA

**Priority:** P3
**Source:** scope-parked from florianhorner/chore/todos on 2026-05-16
`mammamiradio/web/static/base.css` — `.status-chip` label text at 10px achieves ~3.18:1 contrast against `--surface`; WCAG AA requires 4.5:1 for text below 18px normal / 14px bold; fix by increasing chip label font-size to 12px+ or adjusting the token value. Mitigation already present: all states pair color with shape, satisfying non-text contrast (3:1 for UI components).

### Admin spacing magic numbers — tokenize to tokens.css scale

**Priority:** P3
**Source:** scope-parked from florianhorner/chore/todos on 2026-05-16
`mammamiradio/web/templates/admin.html` — spacing values `20px 18px`, `22px 28px 40px`, `14px 18px` are hardcoded instead of referencing the 4px/8px base-scale tokens in `tokens.css`; consolidate to canonical spacing variables.

### Hardcoded hex colors in admin.html — migrate to CSS variables

**Priority:** P3
**Source:** scope-parked from florianhorner/chore/todos on 2026-05-16
`mammamiradio/web/templates/admin.html` — colors `#14110F`, `#1E1610`, and raw RGB tints appear inline rather than referencing the canonical custom properties from `tokens.css`; migrate to the palette variables defined there.

## Audio Delivery

### Spoken-segment assembly hardening (deferred from fix/ha-green-hosts-drop)

**Priority:** P2
**Source:** scope-parked from florianhorner/fix/ha-green-hosts-drop on 2026-05-17
Three related hardening items from the original HA Green host-cutoff plan, deferred after the immediate root cause (Anthropic usage_limit circuit breaker) was isolated and fixed separately:

1. `mammamiradio/audio/normalizer.py:295` (`concat_files`) — Add opt-in strict mode that raises on duration shortfall instead of logging only. Intended for host-segment assembly callers; warn-only behavior preserved for generic callers.
2. `mammamiradio/audio/tts.py` (`synthesize_dialogue`) — Pre-check each intermediate line MP3 for zero-byte or sub-threshold duration before passing to `concat_files`. Currently the quality gate runs on the assembled final output only, not on intermediates.
3. `mammamiradio/audio/audio_quality.py` (`validate_segment_audio`) — Add line-count/input-duration–aware banter check so a 6-line exchange cannot pass as a 6-9s valid segment when the expected duration (based on line count × average line length) is significantly higher.

**Why deferred:** The audio quality gate already validates the final output; the current production issue on HA Green was upstream latency from Anthropic retries, not segment-length validation. These harden against a different failure class and should land once HA Green is stable post usage_limit fix.
