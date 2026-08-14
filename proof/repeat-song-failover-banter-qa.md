# QA Report — PR #901 (rescue ladder / repeat-song fix)

Date: 2026-07-26
Branch: florianhorner/fix/repeat-song-and-failover-banter
Target: http://localhost:8137 (real station, 82 live chart tracks, cold cache)
Mode: diff-aware, both surfaces
Duration: ~20 min

## Scope

Two surfaces, per the project's two-surface QA gate:
- Player (`/`) — the rescue/continuity ladder is listener-facing
- Admin (`/admin` -> Motore) — the Queue rescue health row changed

## Health

| Surface | Console errors | Broken links | Result |
|---|---|---|---|
| Player desktop 1440x900 | 0 | 0 | pass |
| Player mobile 390x844 | 0 | 0 | pass |
| Admin desktop | 0 | 0 | pass |
| Admin Motore tab | 0 | 0 | pass |

Score: 96/100. No critical, high, or medium issues. One low (cosmetic copy) below.

## What the changed code actually did at runtime

1. Reworded bridge log fires correctly. Cold cache, idle bridge:
   `Idle bridge: inserting packaged recovery clip`
   `Idle bridge: no cache music queued behind the canned clip`
   The new wording is cause-neutral as intended, and the ladder took the clip
   rung because the cache was genuinely empty.

2. Audio flowed to a real listener: 974,848 bytes over the first gap. No dead
   air, no silence markers, no tracebacks in the whole session.

3. Song identity stamps correctly. `/public-status` now_streaming carried
   `title_only: "AL MIO PAESE"` alongside `artist`, so `segment_track_key`
   produces a real ban-comparable key.

4. No back-to-back repeats, which is the PR's headline promise. Aired order:
   AL MIO PAESE -> banter -> Canzone Estiva -> BAILE INoLVIDABLE -> banter ->
   Brivido. Zero consecutive duplicate music entries.

5. Rescue alarm stayed honest under operator load. Three skips in quick
   succession (threshold is 2 per 30 min) left the row HEALTHY, window_count 1.

## Not proven by this run

Continuity audio never aired during the window, so `_record_continuity_air` and
the alarm exclusion were not exercised end to end. The real queue refilled first,
which is the documented common case. Both are covered by mutation-verified unit
tests. Staging the air path reliably needs a forced starvation window.

## Issues

### QA-001 (low, cosmetic) — machine words on the Queue rescue row
The Engine Room row renders `last: idle/canned`. Both tokens are internal
vocabulary on an operator-facing screen. Pre-existing, not introduced here, and
already logged as a follow-up from the pre-merge review. Not fixed in this PR to
keep it scoped.

## Protected UI elements — all present
- Token cost counter and cost split (Host scripts / Transitions / Voice synthesis)
- Gold "Mi" accent in both headers
- Italian tricolor stripe/band on both surfaces
- Admin espresso surface, not washed taupe
- Play button state colour is blue, not gold

## Environment note (not a bug)
Script provider showed BACKUP ACTIVE: the Anthropic key hit its usage limit and
the station failed over to OpenAI, surfacing "check your plan at anthropic.com"
with a retry countdown. Correct failover behaviour and human-readable copy.
