# Repo-Local Agent Rules

This file supplements the global instructions for the `mammamiradio` repository.

## Repo Profile

- Stack: Python, FastAPI, Docker, Bash lifecycle scripts, `pyproject.toml` versioning
- Product: `mammamiradio`, an AI-powered Italian radio station with a Home Assistant add-on

## Working Rules

- Conventional commits only: use `type(scope): subject` with the canonical
  types `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`,
  `build`, `perf`, and `revert`.
- Dependency commits use `chore(deps): ...`; `deps:` is not a commit type.
- Never modify `.context/` runtime state
- First Listen and other Home Assistant-facing development or QA use the
  disposable local HA lab by default. Treat live Home Assistant as production:
  never connect branch code to it, reuse its token or backup, reconfigure it,
  or attach its household/cloud/MQTT devices without explicit authorization in
  the current message. Keep lab state and credentials under gitignored
  `tmp/first-listen-ha-lab/`, never `.context/` or tracked files.
- Before opening any PR, follow [`## Shipping (agents)`](#shipping-agents) below.
  The preship evidence sequence there is mandatory for every runtime (Claude,
  Codex, Cursor), not optional because `/ship` ran a review squad.
- If Conductor lifecycle hooks change, update the `scripts/conductor-*.sh` files (and your Conductor `.conductor/settings.toml`) in the same change
- On version bumps, keep `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md` in sync
- In engineering reviews, present real alternatives and their trade-offs, then
  recommend one and explain why it is superior for this repository.
- Keep human and feature PRs under 1,000 changed lines, counting additions and
  deletions in the full diff. If a change approaches that limit, split it before
  implementation. Do not meet the limit by removing regression tests, required
  documentation, or review evidence.

## Shipping (agents)

**Canonical ship contract for this repository.** When asked to ship, deploy, or
open a PR, invoke gstack `/ship` for its test and review pipeline, then apply
the overrides below. **This section wins over generic gstack `/ship` steps,
Cursor/GitHub PR-helper rules, and any tool-default `gh pr create` flow** when
they conflict.

### Two playbooks — one outcome

| Layer | Role |
|-------|------|
| **gstack `/ship`** | Merge `main`, run tests/review/adversarial, push, open or update the PR |
| **This section** | mammamiradio version model, preship proof commits, PR body/title shape, no merge |

Codex and other runtimes without Claude hooks have **no local enforcement** —
committed proof files and a compliant PR body are how you prove the squad ran.
Do not treat a green `~/.gstack/.../reviews.jsonl` entry as a substitute for
the proof commits below.

### Feature PRs vs release cuts

Under [`docs/release-process.md`](release-process.md) (cut-don't-open), **feature
PRs do not bump published version fields**. Skip gstack `/ship` Step 12
(version bump) and Step 13 (CHANGELOG version header). When gstack supports
`--no-version-bump`, use it; otherwise skip those steps manually.

- **Do not change** on feature PRs: `pyproject.toml` `version`,
  `ha-addon/mammamiradio/config.yaml` `version`,
  `custom_components/mammamiradio/manifest.json` `version`, or either changelog
  `## [X.Y.Z]` header.
- **Do** add user-visible notes under `## [Unreleased]` in both changelogs when
  the change warrants it.
- **Release cuts** (`chore(release): cut X.Y.Z`) are a separate, integrator-owned
  PR — not a normal feature `/ship`.

PR titles may still use a four-component **`vMAJOR.MINOR.PATCH.MICRO` prefix** as
a landing-queue claim (see `.config/commit-rules.json`). That prefix is **not**
a version bump — it does not authorize editing `pyproject.toml` or the add-on
version fields.

### Ship checklist (ordered)

1. **Branch** — feature branch only; never ship from `main`.
2. **Integrate** — `git fetch origin main && git merge origin/main --no-edit`
   (or let `/ship` Step 3 do this).
3. **Test** — `make check` (authoritative; see `CLAUDE.md` Commands). Do not
   substitute bare `pytest` when the full gate is required.
4. **Pre-ship review squad** — run via `/ship` Step 9–11 or equivalent: checklist
   review, specialists on large diffs, adversarial pass. Include an explicit
   **docs/config-consistency** pass: when code behavior or config keys change,
   grep for stale references in `CLAUDE.md`, `docs/`, add-on docs, and operator-
   facing copy; fix or flag before opening the PR.
5. **Preship proof commits** (mandatory; see also `CONTRIBUTING.md` proof
   conventions):
   1. Commit the implementation.
   2. Run the review squad on that commit.
   3. `scripts/emit-review-evidence.sh` → commit `proof/preship-review.json`.
   4. Review again on the tree that includes the v1 file (v1 is in the v2
      digest — do not edit v1 after v2).
   5. `scripts/emit-review-evidence.sh --v2` → commit the receipt under
      `proof/preship-reviews/v2/`.
6. **Push** — `git push -u origin <branch>`.
7. **Open or update PR** — only after steps 3–5 pass. May use `gh pr create` /
   `gh pr edit` as the **final** step of this checklist, not as a shortcut that
   skips review or proof. Claude Code should still enter through `/ship` so the
   pre-ship hook and review log stay aligned.

`preship-evidence.yml` is report-only today; emit and commit proof anyway.

### PR title

- Form: `vMAJOR.MINOR.PATCH.MICRO type(scope): subject` when using a queue claim,
  else `type(scope): subject`.
- Follow `.config/commit-rules.json` and `.conductor/settings.toml` `create_pr`
  (imperative, ≤72 chars, no banned short prefixes like `v2.18`).

### PR body (repo shape — not the default gstack template)

Required sections (see also `.github/pull_request_template.md` and
`.conductor/settings.toml`):

```md
## Summary
<what changed and why — user/operator language>

## Test plan
<checks run and results>

## QA Impact
Classification: Player / Admin / Both / None / Deferred to release candidate
Reason:
- Touched surfaces:
- Why this QA scope is sufficient:
QA performed:
- Player QA: run / reused / not applicable / deferred
- Admin QA: run / reused / not applicable / deferred

## Proof
- [x] tests: <artifact or command output>
- [ ] runtime: n/a — <reason>
```

Also include when applicable:

- **`## Admin Panel Standards`** — when `admin.html` or `listener.html` changed
  (checklist from the PR template).
- **Pre-ship / review summary** — brief; no agent tool provenance.

**Never include** in the PR body or title: `🤖 Generated with`, `codex review`,
`Claude Code`, `Conductor session`, workspace archaeology, sprint labels, or
other editorial bans from `.conductor/settings.toml` `create_pr`.

You may reuse gstack review *findings* in prose; do not paste gstack's default
PR footer or raw tool-attribution blocks into public text.

### QA and landing

- State honest **QA Impact**; run `/qa` on affected surfaces per `CLAUDE.md`
  Quality gates when the diff touches listener or admin UI.
- **Feature agents never merge.** Soak until the maintainer signals; landing
  uses `scripts/land-pr.sh <PR#>` from the landing conductor only.

### Quick reference — common mistakes

| Mistake | Correct action |
|---------|----------------|
| Bump `pyproject.toml` on a feature PR | `[Unreleased]` only; version changes in cut PRs |
| Run `/ship` review but skip proof commits | Run `emit-review-evidence.sh` v1 then v2; commit both |
| Use gstack PR body as-is | Add QA Impact + Proof; strip agent footers |
| `gh pr create` with no prior review | Complete checklist first; PR create is step 7 only |
| Codex assumes hooks enforced the squad | Proof commits are mandatory — hooks do not run in Codex |

## Parallel workspaces

Admission, Path A vs Path B, write-sets, and the "do this now" triage live in
[`docs/runbooks/parallel-workspaces.md`](runbooks/parallel-workspaces.md).
Hard rules agents must not invent around:

- Max 3 active Conductor workspaces for this repo, including an active train.
  Do not create a fourth to "help" merge, fix conflicts, or orchestrate the
  others.
- Do not merge. The maintainer lands with `scripts/land-pr.sh`.
- Path B workers do not edit shared files listed in that runbook; put a
  manifest note in the handoff. A dedicated Path A workspace may own those
  files only when its objective and exclusive write-set name them.
- Do not spawn workspaces, trains, or extra PRs unless the current message
  assigned that role.

## PR Landing Queue

- For multi-PR or coordinator landing sessions, run `scripts/pr-queue-status.sh`
  at the start and again after each confirmed merge so the remaining queue is
  based on current GitHub/worktree state rather than chat memory.
- The single landing conductor owns the full output and merge order. Individual
  PR agents may include the relevant script output as their readiness receipt
  before handing off.
- For ordinary single-PR work, `scripts/pr-queue-status.sh` is optional and
  advisory only. It must not be treated as a merge gate; `scripts/land-pr.sh`
  remains the only required landing path.

## Dependabot Batches

- Start with `gh pr list` state, current head SHAs/checks, and a clean tracked
  worktree. After every Dependabot merge, expect the rest of the batch to become
  stale. Do not fan out automated rebase comments across the batch; handle the
  next PR only when it is actually ready to land.
- Let pure patch/minor Python Dependabot PRs with auto-merge armed land through
  Dependabot when they remain current and fresh required checks pass. A stale
  PR parks until an authenticated maintainer updates it; this is deliberate. If
  quality fails on an unrelated one-test timeout, verify the focused test
  locally before treating it as a rerunnable flake; stop on any deterministic
  dependency break.
- Treat semver-major GitHub Actions PRs as manual landings: inspect the fresh
  rebased diff, confirm required checks are green, include HA integration checks
  when workflow changes touch the Home Assistant surface, write review-log
  coverage for the exact head, then run `scripts/land-pr.sh <pr>`.
- If Dependabot says it cannot rebase a PR because the branch was edited, or a
  dependency PR becomes conflict-dirty after another dependency merge, use
  `@dependabot recreate` from an authenticated maintainer account and re-review
  the recreated head. GitHub Actions must not post Dependabot rebase or recreate
  commands: its bot actor is rejected, and batch-wide nudges create repeated
  comment and CI churn under strict up-to-date checks.

## Integration Trains

- Default ship path, WIP cap, and write-sets:
  [`docs/runbooks/parallel-workspaces.md`](runbooks/parallel-workspaces.md).
- When active, `Train/Listener QS` uses `train/listener-qs` from a recorded
  `origin/main` SHA. Path B stays dormant until the maintainer creates that
  branch and its dedicated Conductor workspace. Feature worktrees targeting
  the active train hand off through `docs/listener-qs-train.md`.
