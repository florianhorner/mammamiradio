# Stabilization Log

Weekly record of self-reported post-ship fix hours and emergency patch counts. This is the KPI capture surface for the stabilization run started 2026-04-17.

## Definitions

- **fix_hours:** self-reported hours spent on post-ship fixes in the week (Monday 00:00 UTC through Sunday 23:59 UTC). Float, rounded to 0.5h.
- **emergency_patch:** a release published within 48h of the prior release that fixes a P0 or P1 regression introduced by the prior release. Auto-counted from `main` commits with `fix:` prefix in the 7-day window; adjust the count if an auto-detected commit was not actually a regression fix.
- **Target:** `fix_hours < 2` for four consecutive weeks = stabilization complete.

## Day 1 Cooldown-Alone Experiment

Active window: **2026-04-17 through 2026-04-24** (7 days).

Only change shipped: `release-cooldown.yml` (24h minimum gap between releases; `hotfix` label bypasses).

**Bypass trust model (explicit):** the `hotfix` label is not access-controlled beyond the repo's default label permissions. Anyone with triage rights can apply it. The design doc's aspirational criteria (≤50 LOC, P0/P1 severity) are *not* enforced by the workflow in Day 1 scope — only the label is checked. Acceptable for the current single-maintainer team; if PR volume grows or the label gets abused, tighten via a GitHub label-protection rule or a CODEOWNERS-gated check. Log any hotfix use below with a 1-line rationale so Day 8 has data.

Day 8 Go/No-Go rubric:

- `fix_hours < 2` **and** `emergency_patches == 0` in the window → cooldown alone may be sufficient. Weeks 2–4 reclassify from scheduled to opportunistic (still ship CONTRACTS.yaml + validator for durability; skip canary + Claude-review until a future regression motivates them).
- `fix_hours >= 2` **or** `emergency_patches > 0` → proceed to full Weeks 1–4 as originally scoped.
- Mixed signal (one metric improves, one does not) → proceed to full plan with stronger evidence about which components matter.

The Day 8 decision itself is a deliverable. Write it to this file as a `## Week 1 Decision` section.

## Log

| Week (ISO) | fix_hours | emergency_patches | notes |
|------------|-----------|-------------------|-------|
| 2026-W13 to 2026-W15 (baseline) | ~13 / wk | 4 | v2.10.0–v2.10.5 window: 6 patch releases in 3 days. Four qualified as emergency patches by the 48h/P0-P1 definition (v2.10.1 fixed v2.10.0 install gate; v2.10.2 fixed Pi silence; v2.10.3 reversed v2.10.2's auto-resume; v2.10.5 re-fixed the token cost counter). fix_hours back-estimated from the 40h / 3wk self-report. |
| 2026-W16 | 2–4 | 0 | Cooldown gate held the line: 11 `fix:` commits merged to main during the window, batched into v2.10.10 (released 2026-04-28). Zero releases published in window — last before: v2.9.0 (2026-04-13); next after: v2.10.10 (2026-04-28). #213 repaired #207's v2.10.8 CSP regression pre-release; under W13–W15 baseline cadence this would have shipped as an emergency patch. |

## Week 1 Decision

**Window:** 2026-04-17 → 2026-04-24. **Decision recorded:** 2026-05-09 (15 days late — the lateness itself is logged so the next experiment doesn't repeat it).

**Measurements:**
- `fix_hours = 2–4` (self-report, moderate band)
- `emergency_patches = 0` (auto-counted; zero releases published in-window)

**Rubric outcome — mixed signal.** `emergency_patches` cleared the bar (0 vs. baseline 4). `fix_hours` improved sharply vs. baseline (~13/wk → 2–4/wk) but is still above the `<2` target. Per the original Day 8 rubric: *"Mixed signal (one metric improves, one does not) → proceed to full plan with stronger evidence about which components matter."*

**Action:** proceed to Week 2 with the next durability layer only — `CONTRACTS.yaml` + validator. Defer canary + Claude-review until Week 2 measurement either confirms the durability layer is sufficient or shows fix_hours is sticking above target.

**What we are NOT doing, and why:**
- **Not** declaring cooldown-alone sufficient — `fix_hours` is still above target.
- **Not** running the full Weeks 1–4 plan in one batch — mixed signal warrants per-week evidence, not a flood of governance.
- **Not** extending the rubric to credit cooldown-prevented patches like #213 — observed and noted, but kept out of the decision criteria to preserve rubric stability.

**Next checkpoint:** Day 15 (W17 close, 2026-05-01 — also overdue; will be backfilled in the same cadence as this row).
