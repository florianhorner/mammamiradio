# Refactor-cut runbook (god-module split)

Behavior-preserving decomposition of `web/streamer.py` and `hosts/scriptwriter.py`,
one small PR at a time. Plan of record: `.context/plans/god-module-split-best-of.md`
(gitignored working note). The high-level rules live in `CLAUDE.md` → "Refactor
discipline"; this runbook is the durable per-cut checklist + the cut-by-cut lessons, so a
fresh session or workspace inherits them.

## Per-cut pre-flight checklist

Run at SCOPE time (before naming what moves), then again before shipping:

- [ ] **Whole-repo grep** of every moved symbol — including `scripts/`,
      `.github/workflows/`, and `docs/`, not just `mammamiradio/` + `tests/`.
- [ ] **Dependency-closure verified** — no move-target drags a primitive shared with
      code staying behind or a later cut (e.g. CSRF/auth).
- [ ] **Test bodies read** (not just grep counts); pre-existing duplication collapsed.
- [ ] **Byte-faithfulness** — AST `get_source_segment` compare of moved bodies vs
      `origin/main`.
- [ ] **Facade-identity guard test** (`facade.X is newmodule.X`) for every re-exported
      symbol.
- [ ] **Edge soak per cut** — `bump-edge` auto-bumps the edge calver on every
      addon-changing merge again (the #384 protected-branch deadlock is fixed), so the
      cut reaches the soak Pi on its own. Don't batch (cuts went 34 commits without
      soaking).

## Lessons (cuts 1–6: W1 / H1 / W3a / W3b + edge release)

1. **CI guards hardcode symbol names.** `scripts/validate-addon.sh` check 10 AST-scans
   `mammamiradio/web/*.py` for a top-level `def _inject_ingress_prefix`; moving the function
   between modules no longer red-fails the addon build. The handover's gotcha list missed
   the original hardcoded-path version because it scoped to `mammamiradio/` + `tests/`.
   Structural fix landed in #467: check 10 now discovers the helper across
   `mammamiradio/web/*.py` instead of hardcoding `pages.py`.
2. **Handovers can name a layering-inverting target.** W3b's handover said move
   `_render_admin_response`, which shares `_get_csrf_token` with the auth cut (W2). The
   dependency-closure check caught it; the planning artifact should have.
3. **Grep counts hide duplication.** Two identical sanitize-test sets lived in two files;
   the plan said "4 tests," reality was 8. Reading the files surfaced it.
4. **Merged ≠ soaked.** `bump-edge` was parked (#384) for a stretch, so 5 cuts merged
   without reaching the soak Pi until a manual edge bump. It auto-bumps again now; soak
   per cut, not in a batch.

## What worked (repeat)

- Two parallel Plan agents designing both boundary options — caught the CSRF entanglement.
- `/plan-eng-review` surfacing a handover contradiction as a User Challenge, not auto-deciding.
- Byte-faithfulness AST compare — cheap, high-confidence proof a "verbatim move" was verbatim.
- The pre-ship squad's docs/config dimension — caught a stale comment all green tests + codex missed.
- Verifying codex findings empirically (grep) instead of reflexively complying.

## End-of-train (earmarked, not yet built)

When the train completes, fold this checklist + the standing loop
(refactor → `/codex review` → `/simplify` → `/ship`) into the
`florianhorner/engineering-standards` repo + a reusable `/refactor-cut` skill. Deferred
on purpose — the hard cuts (auth keystone, playback loop, routes) will teach more before
the procedure is worth freezing.
