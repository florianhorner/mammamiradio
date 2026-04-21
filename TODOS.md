# TODOS

## Before next release (blocks version bump)

### Mark addon as experimental in HACS

- **What:** Add `stage: experimental` to `ha-addon/mammamiradio/config.yaml` so the HACS addon store shows the orange "Experimental" badge (like Govee2MQTT Bridge does today — see `.context/attachments/image.png`).
- **Why:** Signal pre-1.0 status to new installers. Current HACS listing shows "Mamma Mi Radio" with no qualifier next to Govee2MQTT which has the badge.
- **Scope:** one-line YAML addition. No code changes.
- **Docs to update in same commit:** `HA_ADDON_RUNBOOK.md` (mention the stage flag in the release checklist), `CHANGELOG.md` (note the flag at next version bump).
- **Ship gate:** land before the next `v*` tag push.

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

## Completed

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
