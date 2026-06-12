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
- [ ] **Edge soak per cut** — cut a `make edge-release` once the cut's `Build HA Addon`
      is green, so it reaches the soak Pi. Don't batch (cuts went 34 commits without
      soaking).

## Lessons (cut 7: W2 auth keystone → `web/auth.py`)

5. **Patch strings are usages too.** Four tests patched
   `mammamiradio.web.streamer._is_loopback_client` while exercising callees
   (`_is_private_network`, `_is_hassio_or_loopback`) that resolve the name from their
   OWN module globals. After the move those patches would silently no-op — the whole-repo
   grep must include `patch("...")` literals and `monkeypatch.setattr` targets, and the
   patched lookup-site must move (and the string be rewritten) in the same cut.
6. **The local import auto-fixer fights facade re-exports twice.** It strips the
   `# noqa: F401` comment while the old definitions still shadow the import, then strips
   the now-"unused" re-exported names once the definitions are deleted. Re-assert the
   combined `# noqa: F401` import after the deletions land, and verify with the pinned
   venv ruff before committing.
7. **Docs carry forward references that unblock on a cut.** `web/pages.py`'s docstring
   said `_render_admin_response` "stays in streamer for now ... once that primitive's
   home is settled" — settling `_get_csrf_token` in auth.py made that paragraph stale.
   Doc-sync includes comments in sibling modules waiting on the cut, not just `docs/`.

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
4. **Merged ≠ soaked.** The edge channel only advances when you cut a release, so 5 cuts
   merged without reaching the soak Pi. Cut a `make edge-release` per cut, not in a batch.

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
