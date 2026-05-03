# TODOS

Deferred work captured from strategic sessions, scope-parked findings, and
follow-ups. Append-only — one line per entry — to avoid merge conflicts across
parallel Conductor worktrees.

Format: `- [P1|P2|P3] [tag] description (source: <where this came from>)`

## From /office-hours + /plan-eng-review 2026-05-03 — CICD freeze reflection

- [P1] [stabilization-log] Resume measurement protocol — `docs/stabilization-log.md` row for 2026-W16 still says `_tbd Sun 2026-04-19_`. Day 8 decision was 2026-04-24 — write the decision retroactively from current evidence (no v-tag panic-cycle visible in commits since 2026-04-17 cooldown deploy). (source: research surfaced during /office-hours session)
- [P2] [scope-discipline] Watch for scope-creep pattern recurrence over the next 10 PRs. If audit at PR #292 shows >4/10 with creep, reactivate the scope-guard mechanism path (design preserved at `~/.gstack/projects/florianhorner-mammamiradio/florianhorner-cicd-freeze-reflection-design-20260503.md`). (source: scope-discipline rule landed 2026-05-03; audit revisit gate)
- [P3] [meta] Generalize the "designated observer role" pattern in memory — release-manager is one instance; document the meta-rule for when to designate a new role. Already partially captured in `feedback_designated_observer_pattern.md`. (source: /office-hours session insight)
- [P3] [pattern] Audit-before-build is now a validated cheap step — the 3-agent PR audit took ~45s and flipped a 2-3 day build into a 5-line CLAUDE.md rule. Worth codifying as a pre-build gate when a proposed mechanism's frequency justification is unmeasured. (source: /plan-eng-review reversed the /office-hours recommendation based on agent-swarm data)
