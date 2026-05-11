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
`mammamiradio/scheduling/scheduler.py:42` — banter triggers on `pacing.songs_between_banter` count, not time. Track B v2.11 dropped ETA buckets because of this. A time-aware scheduler would unlock honest ETA display, "preempt next break" for hot dedications, and the v2.12 `dedication-becomes-track` feature. Multi-week scope; deserves its own /office-hours.

### Moderation static-name blocklist (v2.11.1 hardening)
**Priority:** P2
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-10
`mammamiradio/hosts/moderation.py` — current LLM-only moderation has no second-line defense for real-person false-positives. Static blocklist of political figures, brand owners, and a few high-profile celebrities catches the LLM mishaps. Pulls from a config-driven list in `radio.toml` `[moderation.blocked_names]`. ~50 LOC. Should land in v2.11.1 or earlier if any false-positive observed during the v2.11.0 baseline window.

### Dedication metrics admin dashboard surface
**Priority:** P3
**Source:** scope-parked from florianhorner/feat/track-b-sidebar on 2026-05-10
v2.11.0 lands with `cache_dir/dedication_events.jsonl` and a `scripts/dedication-metrics.sh` reader. Long-term the admin Engine Room should surface live counters (acceptance rate, median time-to-air, miss rate). Bundles with the existing token-cost-counter UI surface.
