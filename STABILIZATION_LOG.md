# Stabilization Log

Weekly record of self-reported post-ship fix hours and emergency patch counts. This is the KPI capture surface for the stabilization run started 2026-04-17.

## Definitions

- **fix_hours:** self-reported hours spent on post-ship fixes in the week (Monday 00:00 UTC through Sunday 23:59 UTC). Float, rounded to 0.5h.
- **emergency_patch:** a release published within 48h of the prior release that fixes a P0 or P1 regression introduced by the prior release. Auto-counted from `main` commits with `fix:` prefix in the 7-day window; adjust the count if an auto-detected commit was not actually a regression fix.
- **Target:** `fix_hours < 2` for four consecutive weeks = stabilization complete.

## Day 1 Cooldown-Alone Experiment

Active window: **2026-04-17 through 2026-04-24** (7 days).

Only change shipped: `release-cooldown.yml` (24h minimum gap between releases; `hotfix` label bypasses).

Day 8 Go/No-Go rubric:

- `fix_hours < 2` **and** `emergency_patches == 0` in the window → cooldown alone may be sufficient. Weeks 2–4 reclassify from scheduled to opportunistic (still ship CONTRACTS.yaml + validator for durability; skip canary + Claude-review until a future regression motivates them).
- `fix_hours >= 2` **or** `emergency_patches > 0` → proceed to full Weeks 1–4 as originally scoped.
- Mixed signal (one metric improves, one does not) → proceed to full plan with stronger evidence about which components matter.

The Day 8 decision itself is a deliverable. Write it to this file as a `## Week 1 Decision` section.

## Log

| Week (ISO) | fix_hours | emergency_patches | notes |
|------------|-----------|-------------------|-------|
| 2026-W13 to 2026-W15 (baseline) | ~13 / wk | 4 | v2.10.0–v2.10.5 window: 6 patch releases in 3 days. Four qualified as emergency patches by the 48h/P0-P1 definition (v2.10.1 fixed v2.10.0 install gate; v2.10.2 fixed Pi silence; v2.10.3 reversed v2.10.2's auto-resume; v2.10.5 re-fixed the token cost counter). fix_hours back-estimated from the 40h / 3wk self-report. |
| 2026-W16 | _tbd Sun 2026-04-19_ | _auto_ | Day 1 cooldown-alone experiment active. |
