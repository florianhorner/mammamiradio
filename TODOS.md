# TODOS

## Listener UX

### Dialer revival (listener.js first-class port)

- **Source:** pre-PR#218 `static/script.js` (see `.context/research/dialer-port-blueprint.md`)
- **Scope:** ~220 LOC JS + wiring, CSS class rename, `--needle-x` token
- **Effort:** 4–6 hours
- **Gate:** design sprint (`/office-hours` on dial UX first) — do not build speculatively
- **Trigger phrase:** "bring the dial alive" / "implement the dialer"

## Infrastructure

**Priority:** P2
**Source:** /research on 2026-04-15


### Listener public API migration (full)
**Priority:** P1
**Source:** /plan-eng-review on 2026-04-25 (florianhorner/fix/radio-plan)

Listener page (`mammamiradio/static/listener.js`) polls three admin-gated endpoints: `/status`, `/api/capabilities`, `/api/listener-requests`. The fix-radio-plan PR ships a one-line stopgap (`/status` → `/public-status`) so the now-playing data works on public deploys. The other two fetches will return 401 silently on non-loopback/non-LAN clients, degrading the dediche feed and capability tier display.

**Why:** the listener page is the listener-facing product surface. Any deploy outside loopback/LAN exposure (PWA, embed, hosted listener) shows a degraded page until this is closed.

**Pros:** unblocks public deployment of the listener page. Aligns with "instant audio" leadership principle (page works immediately for any visitor).

**Cons:** requires backend additions (`/public-listener-requests` or strip-fields shim around `/api/listener-requests`; same for `/api/capabilities`). ~45 min CC. Coordination with the active UI redesign cycle.

**Context:** the UI redesign is the natural home for this — when listener.html is rewritten, the API contract for the public listener can be defined cleanly.

**Depends on / blocked by:** UI redesign cycle currently in progress. Coordinate via the redesign workspace.

**Affected files:** `mammamiradio/streamer.py`, `mammamiradio/static/listener.js` (or its replacement), tests/test_streamer.py.

### Regia.html + admin Flag Track field contract fix
**Completed:** 2026-04-27 (florianhorner/list-p1s)
Added `duration_sec` to `now_streaming` payload (models.py). Regia elapsed computed client-side from `ns.started`; duration reads `ns.duration_sec`. Flag Track key path fixed: `_st?.now?.metadata` → `_st?.now_streaming?.metadata`.

### Populate `Segment.duration_sec` from normalizer output
**Priority:** P2
**Source:** /qa on 2026-04-27 (florianhorner/list-p1s)

`producer.py` creates `Segment` objects but never calls `normalizer._ffprobe_duration_sec()` after normalization, so `Segment.duration_sec` stays at the default `0.0`. The `now_streaming` payload includes `duration_sec` (added in PR #257) and `regia.html` reads it, but the progress bar stays at 0% because the value is always zero.

**Fix:** In `producer.py`, after calling `normalize()` to get `norm_path`, call `_ffprobe_duration_sec(norm_path)` and assign the result to `segment.duration_sec`. ~3 lines.

**Affected files:** `mammamiradio/producer.py` (all `Segment(...)` creation sites for music/banter/ad types), `mammamiradio/normalizer.py` (export `_ffprobe_duration_sec` or add a public wrapper).

### Host name selector hardening
**Priority:** P3
**Source:** /plan-eng-review on 2026-04-25 (florianhorner/fix/radio-plan)

`admin.html:1539-1574` uses `esc()` for HTML escaping before interpolating host names into onclick handlers and data attributes. The CSS attribute selectors at lines 1569, 1574 use template literals with raw (un-escaped) names: `` `[data-h="${n}"]` ``. Names with special CSS characters (quotes, brackets, escaped chars) cause silent no-match — UI fails closed (no XSS, just brittle).

**Why:** rejected as a real bug in the radio-plan review, but the brittleness will surface eventually as someone names a host with a special character.

**Pros:** robustness improvement.

**Cons:** very low priority; no current user impact.

**Context:** wrap the selector access in `CSS.escape()` or normalize host names to alphanumeric IDs internally and only show the display name in UI text.

**Depends on / blocked by:** none.

**Affected files:** `mammamiradio/admin.html` (or its replacement).

### Docker container smoke test in CI
After `addon-build.yml` builds the image, run a 30s smoke test:
- `docker run` → wait 10s → `curl -f /health`
- Check logs contain no `Queue empty` warning in first 20s
Catches "server starts but can't produce audio" — the exact production failure class.
**Files:** `.github/workflows/addon-build.yml`

## Admin UI — Regia (Time-Horizon Stack)

**Source:** `/research` + `/design-shotgun` on 2026-04-21 → approved direction is Concept A modified (Variant E2 warmth iteration).
**IA reference:** `.context/attachments/Radio Control-Room IA  Architecture Comparison, Recommendation & MVP Build Order.md`
**Design reference:** `~/.gstack/projects/florianhorner-mammamiradio/designs/admin-regia-concepts-20260421/variant-E2.png` + `approved.json`
**Prototype shipped (this branch):** `mammamiradio/regia.html` served at `/regia` (admin-gated). Screen 1 ON AIR + 260px read-only Peek Panel + persistent status strip + tab bar (tabs 2–5 are inert placeholders).

### P1 — Wire Screen 1 ON AIR to real behavior
- **Pause button** — no backend endpoint today. Either add `/api/pause` (session-level pause distinct from `/api/session/stop`) or remove the button until a pause semantic is agreed. Today it logs `[regia] pause not yet wired`.
- **Panic Overlay** — full-screen SILENCE NOW + FORCE FALLBACK modal per IA doc. Needs a backend trigger (likely `force_next = SILENCE` segment type + emergency fallback push). Currently the Panico button only logs a warning.
- **AI Approval badge** — surface needs a backend concept. Today `/status` exposes `last_banter_script` (already generated, already approved). For the approval workflow we need a pending-segment queue with APPROVE/REJECT state. Placeholder card hidden in prototype.

### P1 — Build Screen 2 QUEUE
Phase 1 MVP per IA doc. Reuse the color-coded item pattern from the peek panel. Drag-to-reorder, break-structure card (next break slot with segment-type mini-timeline), inline search against existing `/api/search` endpoint, skip/remove controls.

### P2 — Build Screen 3 REVIEW, Screen 4 PROGRAMME, Screen 5 MOTORE
Phase 2 per IA doc. Screen 3 is AI content approval (banter + ad preview with audio + APPROVE/REJECT/EDIT). Screen 4 is format-clock + pacing. Screen 5 is the current admin Engine Room — API cost counter, capability flags, model info, logs.

### P2 — Swap `/admin` to point at the new stack
Once Screens 1+2 are solid, move the current 1744-line `admin.html` behind `/admin/legacy` and make `/admin` land on `/regia`. Do not touch `admin.html` until the new architecture covers all the current admin features (hosts config, pacing sliders, listener requests, playlist management).

### P3 — Italianize remaining UI copy
The prototype uses Italian labels (CODA, REVISIONE, PALINSESTO, MOTORE, PANICO). Once Screens 2–5 are built, audit all existing admin.html strings and normalize to the same voice.

### P2 — Italianize admin.html panel contents (Approach B)
PR #248 (Approach A) italianized the admin shell: sidebar nav, h2 titles, eyebrows, top status panel. Panel **contents** are still English — visible to the operator and creating mixed-language whiplash. Scope:
- Top-bar `Queue banter` CTA (`admin.html:1118`)
- Trigger card titles + descriptions: `Queue banter / Force ad break / News flash / Chaos incoming` (`admin.html:1156-1172`)
- Quick-action chips: `Trim banter / Trim ads / Hot reload / Purge queue / Flag track` (`admin.html:1179-1183`)
- Conduttori host UI: preset names `BALANCED / CALM / HYPE`, slider labels `ENERGY / CHAOS / WARMTH / VERBOSITY / NOSTALGIA`, axis arrays `AX_LOW`/`AX_HIGH` (`admin.html:1944-1951`), host-block template (`admin.html:2013`)
- Search placeholder + button (`admin.html:1265`)
- Engine room status table (`admin.html:2172-2175`) and onboarding step checklist (`admin.html:1310, 1335`)
- Filter chips + table column headers (JS-rendered — find the renderer)
- Toast strings (`admin.html:1405`)
- `75 tracks` → `75 tracce` next to `Musica & Coda`
- `ON AIR` pill → `IN ONDA` to match listener
**Effort:** ~30-40 string changes, half in JS template strings. **Risk:** low (label-only). **Source:** /qa report `.gstack/qa-reports/qa-report-admin-2026-04-27.md`.

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
