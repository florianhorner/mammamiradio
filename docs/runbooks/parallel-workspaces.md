# Worktrees, merge checks and queue reports

This runbook describes worktree safety, merge checks and queue reports.

Related:

- Human and feature landing gate: `scripts/land-pr.sh`
- Queue dashboard: `scripts/pr-queue-status.sh` (advisory, not a gate)
- Workspace lifecycle: [`docs/conductor.md`](../conductor.md)
- Agent rules: [`docs/agents.md`](../agents.md)

Dependabot patch and minor updates use their own guarded auto-merge workflow.
See [Dependabot](#dependabot).

## Inspect checkouts and landing state

From a clean checkout, inspect the current queue and local worktrees:

```bash
git fetch origin
bash scripts/pr-queue-status.sh
git worktree list
```

Use one writer per worktree and avoid concurrent changes to overlapping files.
Resolve branch conflicts and validate the result in the checkout that owns the
branch.

`scripts/land-pr.sh` arms auto-merge and can return before GitHub merges the PR.
Confirm the PR state before retiring the source checkout:

```bash
gh pr view <PR#> --json state --jq .state
```

The committed `.conductor/settings.toml` routes archive events to
`scripts/conductor-archive.sh`. Repository-local `settings.local.toml` has higher
precedence; inspect it if the effective hooks differ from the committed settings.

## Integration branches

Use a feature branch and a PR to main for independent changes. Coupled changes
can use a temporary integration branch followed by one PR to main. Direct
pushes and merges into `train/**` do not trigger `quality.yml` or `pi-smoke.yml`;
those workflows run for PRs targeting main or `train/**`. Run affected local
checks after each integration.

## Shared-file ownership

Release metadata changes have one owner during the release:

- `pyproject.toml`
- `CHANGELOG.md`
- `ha-addon/mammamiradio/CHANGELOG.md`
- `ha-addon/mammamiradio/config.yaml` when changing the version
- `custom_components/mammamiradio/manifest.json` when changing the version

Feature changes do not bump versions. See `docs/release-process.md`.

The following files require one exclusive owner because they affect every
workspace:

- `.github/workflows/**`
- `scripts/conductor-*.sh`
- `scripts/land-pr.sh`, `scripts/land-gates.sh`, `scripts/edge-select.sh`, `scripts/land-queue-plan.sh`, `scripts/pr-queue-status.sh`, `scripts/check-merge-gate.sh`
- `.conductor/settings.toml`

Avoid concurrent changes to these shared files. Assign one owner and report
shared dependencies before editing them.

## PR body and changelog lint mechanics

The editorial boundary itself (what never goes into the public changelogs, PR
bodies, or maintainer-authored issues) is the rule in the root agent guide,
"Changelog editorial boundary". This section holds how it is enforced.

**Enforcement — two pattern sets, unioned.**

The *baseline* lives in `florianhorner/gh-workflows` and is shared by every repo. It matches the grammatical **shape** of a violation rather than a literal phrase — a review noun near a discovery verb, a review actor mid-action, a telemetry possessive, a numeric process tally, a review-artifact heading — so a paraphrase of a banned class does not walk through. A literal-phrase list cannot do this: the previous one held a pattern for `findings raised` and still passed "Findings from review". An em-dash rule ships alongside it and is now **blocking**, not advisory (since gh-workflows v1.5; `.github/workflows/pr-body-lint.yml` pins the exact revision in use). Two carve-outs keep it from contradicting the proof gate that runs on the same body in the same invocation: a line matching `n/a —` and any checkbox line (`- [ ]` / `- [x]`) are exempt, so a `## Proof` block never trips it. Only prose em-dashes fail. Since gh-workflows#18 the baseline also ignores the two-line footer a cloud agent session appends to the very end of a PR or issue body; the same words anywhere else in a body, and in every changelog, still fail.

The *project* list is `scripts/lint-patterns.sh` here (`LINT_PATTERNS`), passed to the reusable workflow as `extra_patterns`. It holds only what is specific to mammamiradio: sprint labels, cathedral vocabulary, this repo's contributor archaeology. **Overlap with the baseline is deliberate and must not be tidied away.** The three local lints below source this file *alone*, so deleting a pattern because the baseline also covers it silently drops changelog and issue coverage for it.

A repo may only ADD patterns. Suppressing a baseline pattern is a pull request to `gh-workflows`, not a local override.

**What this cannot catch.** Regex reaches lexical tells, not meaning. Metaphor carrying no lexical signal is out of reach, and renaming a review section to a neutral label defeats the heading rule, which matches the label and never the content. A passing lint is a tripwire on habitual violations, never evidence that a body is clean.

Three local lints consume `LINT_PATTERNS`:

- `scripts/check-changelog-lint.sh` — runs in `quality.yml` against `CHANGELOG.md` and `ha-addon/mammamiradio/CHANGELOG.md`.
- `scripts/check-pr-body-lint.sh` checks PR text in `pr-body-lint.yml`.
- `scripts/check-issue-body-lint.sh` checks maintainer-authored issue bodies in
  `issue-body-lint.yml`; it does not check outside contributors' issues.

To extend the rules, add a regex to `LINT_PATTERNS` in `scripts/lint-patterns.sh` — all three lints pick it up automatically.

## Check the write-set

Record the immutable starting commit and compare the completed change against it:

```bash
git merge-base --is-ancestor <base-sha> HEAD
git diff --name-only <base-sha>...HEAD
```

Keep changes within the agreed files and report any shared-file dependencies.
Describe the resulting behavior and its validation in the PR. Keep workspace
inventories, agent assignments and private handoff records outside public Git
history.

## Shadow land queue (report-only)

`.github/workflows/land-queue.yml` runs `scripts/land-queue-plan.sh` every 30
minutes and writes to the job summary the one thing an auto-land controller
would do next: which PR it would integrate or arm, and why every other open PR
is not that PR. **It changes nothing.** No push, no comment, no arm, no merge,
no edge move. The workflow requests `contents: read`, `pull-requests: read` and
`actions: read` — the last one because the edge lane asks `gh run list` which
add-on builds went green. It holds no write permission at all.

The queue reports recommendations for maintainer review. It cannot merge PRs.

Read it yourself any time:

```bash
bash scripts/land-queue-plan.sh                # human summary
bash scripts/land-queue-plan.sh --json         # machine-readable states
bash scripts/land-queue-plan.sh --json-out F   # both, from one query (what CI runs)
bash scripts/pr-queue-status.sh --json         # the same states, plus local worktrees
```

The queue and `land-pr.sh` both use `verify_head` from `scripts/land-gates.sh`.
A regression test verifies that a refusal from `verify_head` blocks the queue
recommendation. The merge script also checks release-cut restrictions before
enabling auto-merge; the queue does not establish those release conditions.

Immediately before arming, the merge script rechecks that the PR is open,
still targets main, and retains the checked head and base commits. A changed
or unreadable snapshot requires another landing attempt. After requesting
auto-merge, it checks the target again and attempts to disable auto-merge if
the target changed or cannot be verified. These reads are not an atomic target
lock; cancellation cannot undo an immediate merge or prevent later retargeting.

Non-main targets are skipped in all lanes, and an unknown base stops planning.
Edge selection refreshes `origin/main` once per run, independently of PR
classification, and uses one resolved commit for eligibility and the advertised
pin. A failed refresh blocks the Edge verdict.

Lanes are keyed on what the automation itself emits — the bot flag, and the
`edge-release/*` branch `cut-edge-release.sh` pushes, and the Dependabot author
login — never on the PR title. Non-feature lanes use their own merge paths and
are exempt from the shared feature-head gates.

PRs are ordered by creation time. The JSON field `fifo_key_source` identifies
this ordering.

A blocked head *stalls* the queue rather than being reordered around — that is
what keeps the ordering fair to whoever has been waiting longest. When a head is blocked on something
only its owner can fix (a conflict, usually), the `skip-queue` label drops it
out of head contention so one stuck PR cannot hold every other fix hostage.
`hold` or `manual-land` is the other direction: it takes a PR out of the queue's
hands entirely and stalls the head, for when you mean to merge it through the maintainer's explicit command.

**What each state means:**

| State | Meaning |
|---|---|
| `READY` | Gates pass and no required check is red. Also covers `UNSTABLE` (only non-required checks red) and `HAS_HOOKS` |
| `READY_BEHIND` | Gates pass but the base moved; needs integrate + push |
| `CI_PENDING` | A merge state GitHub has not settled: required checks, thread resolution, or mergeability not yet computed |
| `BLOCKED_BOT` | Unresolved Major/Critical/P0/P1 bot thread |
| `BLOCKED_CONFLICT` | Merge conflict — the owning workspace resolves it |
| `BLOCKED_YOU` | `hold` or `manual-land` label |
| `BLOCKED_HEAD` | Head object could not be fetched; gates cannot be evaluated |
| `SKIPPED` | `skip-queue` label or non-main base — out of head contention |
| `EXEMPT` | Bot, Dependabot or edge lane; keeps its own merge path |
| `OPEN` | Draft |

**Turning it off.** Delete `.github/land-queue.enabled` (a one-line PR, with an
audit trail), or set the repository variable `LAND_QUEUE=0`. Either one stops
it. Two switches because the file works through a PR and
the variable works without one. Both are enforced inside
`scripts/land-queue-plan.sh`, not only in the workflow, so "off" means off from
a local run too — the switch belongs to the controller, not to one caller. The
workflow passes the repository variable into the script as well, so both halves
reach a local run and a scheduled one alike. Only the literal `0` switches off:
`LAND_QUEUE=false` reads as on. `scripts/pr-queue-status.sh --json` derives from
the planner, so it reports the switch rather than working while the queue is
off; the human dashboard is unaffected.

Self-test: `bash tests/workflows/test_land_queue.sh`.


## Conflict ownership

Resolve conflicts in the source checkout. Rerun affected checks and review the
resulting diff.

## Dependabot

Dependabot is the automated exception to the human and feature landing path.
Patch and minor Python PRs may use
`.github/workflows/dependabot-automerge.yml`. Stale PR handling follows
`docs/agents.md`: rejected or behind PRs park for an authenticated maintainer.
The workflow adds `cut-window-hold` to PRs it disarms during a release cut.
The sweep only disarms. After publication, a fresh PR event with verified
Dependabot metadata may re-arm patch/minor updates. Major GitHub Actions
updates still use `scripts/land-pr.sh`. Before a cut, the release operator runs
`GH_REPO=florianhorner/mammamiradio scripts/dependabot-window-hold.sh freeze`: disable new runs, drain existing runs, then
disarm PRs. Stable-version changes require that verified freeze at landing.
Keep human landings paused through both architecture promotions and resume
explicitly with `GH_REPO=florianhorner/mammamiradio scripts/dependabot-window-hold.sh thaw <release-run-id>`. The report-only queue covers merge state,
labels and unresolved bot threads; its READY result does not establish cut admission. See
`docs/runbooks/ha-addon.md`, "The cut window".
