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
