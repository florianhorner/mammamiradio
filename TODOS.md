# TODOS

## Release Quality

**Priority:** P1
**Source:** /research on 2026-04-15 — release quality structural gap post-mortem

### aarch64 CI smoke job
Add `.github/workflows/pi-smoke.yml` running `ubuntu-24.04-arm` runner.
Run `tests/test_normalizer_unit.py` and `tests/test_normalizer_extended.py` only.
Confirms filter chain correctness against real ffmpeg 8.x on aarch64.
Catches the entire "works on CI x86, dies on Pi" class of bug.
**Files:** `.github/workflows/pi-smoke.yml`

**Priority:** P1
**Source:** /research on 2026-04-15

### Silence health gate + /health 503
Upgrade the 30s `Queue empty for 30s` WARNING to an active recovery:
1. Attempt to queue first `norm_*.mp3` from cache_dir immediately
2. If still empty at 60s with active listeners → log ERROR + force-resume
3. `/health` endpoint returns 503 when queue empty >30s + listeners active
Lets HA Supervisor auto-restart the addon on silence instead of just logging.
**Files:** `mammamiradio/streamer.py`, `mammamiradio/producer.py`

## Infrastructure

**Priority:** P2
**Source:** /research on 2026-04-15

### Docker container smoke test in CI
After `addon-build.yml` builds the image, run a 30s smoke test:
- `docker run` → wait 10s → `curl -f /health`
- Check logs contain no `Queue empty` warning in first 20s
Catches "server starts but can't produce audio" — the exact production failure class.
**Files:** `.github/workflows/addon-build.yml`

**Priority:** P2
**Source:** /research on 2026-04-15

### scripts/pre-release-check.sh
A fast pre-version-bump check script:
1. ffmpeg filter chain equalizer count = 3 (highpass + de-mud 200Hz + presence 3kHz + HF shelf 12kHz — assert in normalizer.py source)
2. ha-addon CHANGELOG version matches config.yaml version
3. At least one test with `_pick_canned_clip` returning None in producer tests
4. At least one test covering post-restart session_stopped scenario
5. pyproject.toml version matches config.yaml version
Run from Makefile as `make pre-release`.
**Files:** `scripts/pre-release-check.sh`, `Makefile`

## Completed

### 3-scenario invariant test rule in CLAUDE.md
**Completed:** v2.10.2 (2026-04-15)
Added "Audio delivery test coverage rule" section to CLAUDE.md under Review Discipline. Three scenarios required for all audio-delivery PRs: Normal, Empty fallback (no canned clips/norm cache), Post-restart (flag files, session_stopped state).
